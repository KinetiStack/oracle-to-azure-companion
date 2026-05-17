#!/usr/bin/env python3
"""dr_drill.py - orchestrate a Disaster Recovery drill and emit an HDR
(HA/DR Assessment Report) JSON document.

The drill:
  1. Connect to the PRIMARY target; write a marker row to a probe table.
  2. Record the timestamp (T0).
  3. Initiate a planned switchover (graceful) OR a forced failover.
  4. Poll target endpoint until the secondary is reachable and writable.
  5. Record the timestamp (T1) -> RTO = T1 - T0.
  6. Query the marker row on the (now-)primary -> RPO is the marker's
     write-to-visible delta if the marker is present; the largest pre-failover
     marker missing if it's not.
  7. Emit hdr.json with measured RTO/RPO vs. target thresholds and PASS/FAIL.

This script is engine-agnostic at the orchestration level. The actual
'initiate switchover' step is engine-specific and lives in an external
command supplied via --switchover-command (e.g., a dgmgrl invocation, an
az CLI failover-group failover, a postgres flexible-server replica promote).

Usage:
    dr_drill.py --engine mssql --primary-host ... --secondary-host ... \\
                --user ... --password ... \\
                --switchover-command 'az sql failover-group failover ...' \\
                --target-rto-seconds 120 --target-rpo-seconds 30 \\
                --out hdr.json
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

HDR_VERSION = "1.0.0"
log = logging.getLogger("dr_drill")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextlib.contextmanager
def _connect(engine: str, host: str, user: str, password: str, database: str):
    """Engine-specific connect as a context manager that ALWAYS closes the
    underlying connection. pyodbc and psycopg2's native __exit__ commit but
    do NOT close; we force the close in a finally to prevent connection leaks
    across the drill's many short-lived sessions.

    For Oracle, `database` is the SERVICE NAME (e.g. ORCLPDB1), not the
    database name -- the CLI's --database flag means service name when
    --engine=oracle. See --help for clarification.
    """
    conn = None
    try:
        if engine == "mssql":
            import pyodbc
            cs = (f"DRIVER={{ODBC Driver 18 for SQL Server}};"
                  f"SERVER=tcp:{host},1433;DATABASE={database};UID={user};PWD={password};"
                  f"Encrypt=yes;TrustServerCertificate=no;Connection Timeout=15")
            conn = pyodbc.connect(cs)
        elif engine == "pg":
            try:
                import psycopg as pg
            except ImportError:
                import psycopg2 as pg  # type: ignore[no-redef]
            conn = pg.connect(host=host, user=user, password=password,
                              dbname=database, sslmode="require")
        elif engine == "oracle":
            import oracledb
            conn = oracledb.connect(user=user, password=password,
                                    dsn=f"{host}:1521/{database}")
        else:
            raise ValueError(f"unknown engine: {engine}")
        yield conn
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _ensure_probe_table(engine: str, conn) -> None:
    sql_mssql  = """
        IF OBJECT_ID('dbo.dr_probe', 'U') IS NULL
            CREATE TABLE dbo.dr_probe (
                drill_id NVARCHAR(64) NOT NULL,
                written_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
                CONSTRAINT pk_dr_probe PRIMARY KEY (drill_id));
    """
    sql_pg     = """
        CREATE TABLE IF NOT EXISTS dr_probe (
            drill_id   text NOT NULL PRIMARY KEY,
            written_at timestamptz NOT NULL DEFAULT clock_timestamp());
    """
    sql_oracle = """
        BEGIN
            EXECUTE IMMEDIATE 'CREATE TABLE dr_probe (
                drill_id   VARCHAR2(64) PRIMARY KEY,
                written_at TIMESTAMP DEFAULT SYSTIMESTAMP)';
        EXCEPTION WHEN OTHERS THEN
            IF SQLCODE != -955 THEN RAISE; END IF; -- ORA-00955 table exists
        END;
    """
    cur = conn.cursor()
    cur.execute({"mssql": sql_mssql, "pg": sql_pg, "oracle": sql_oracle}[engine])
    conn.commit()


def _write_marker(engine: str, conn, drill_id: str) -> None:
    cur = conn.cursor()
    if engine == "mssql":
        cur.execute("INSERT INTO dbo.dr_probe (drill_id) VALUES (?)", drill_id)
    elif engine == "pg":
        cur.execute("INSERT INTO dr_probe (drill_id) VALUES (%s)", (drill_id,))
    else:  # oracle
        cur.execute("INSERT INTO dr_probe (drill_id) VALUES (:1)", [drill_id])
    conn.commit()


def _marker_present(engine: str, conn, drill_id: str) -> bool:
    cur = conn.cursor()
    if engine == "mssql":
        cur.execute("SELECT 1 FROM dbo.dr_probe WHERE drill_id = ?", drill_id)
    elif engine == "pg":
        cur.execute("SELECT 1 FROM dr_probe WHERE drill_id = %s", (drill_id,))
    else:
        cur.execute("SELECT 1 FROM dr_probe WHERE drill_id = :1", [drill_id])
    return cur.fetchone() is not None


def _delete_probe(engine: str, conn, drill_id: str) -> None:
    cur = conn.cursor()
    if engine == "mssql":
        cur.execute("DELETE FROM dbo.dr_probe WHERE drill_id = ?", drill_id)
    elif engine == "pg":
        cur.execute("DELETE FROM dr_probe WHERE drill_id = %s", (drill_id,))
    else:
        cur.execute("DELETE FROM dr_probe WHERE drill_id = :1", [drill_id])
    conn.commit()


def _poll_until_writable(engine, host, user, password, database,
                         deadline_at: datetime,
                         drill_id: str) -> tuple[bool, datetime]:
    """Try a true WRITE probe every 5s until success or deadline.

    A read probe (SELECT 1) is insufficient: an unpromoted PG replica, a
    SQL MI read-only listener, and an Oracle standby in READ ONLY WITH APPLY
    all accept SELECT 1 while still in secondary role. The drill measures
    role-swap completion, so the probe must require write privilege.

    We INSERT a single-row probe marker (`_writable_check_<drill_id>`) and
    DELETE it on success. The real drill marker is written separately in
    run_drill() before the switchover is initiated.
    """
    probe_marker = f"_writable_check_{drill_id}"
    while _now() < deadline_at:
        try:
            with _connect(engine, host, user, password, database) as conn:
                _ensure_probe_table(engine, conn)
                _write_marker(engine, conn, probe_marker)
                _delete_probe(engine, conn, probe_marker)
                return True, _now()
        except Exception as exc:
            log.debug("not yet writable: %s", exc)
            time.sleep(5)
    return False, _now()


@dataclasses.dataclass
class DrillResult:
    drill_id:                 str
    started_at_utc:           str
    completed_at_utc:         str
    rto_seconds_measured:     float
    rpo_seconds_measured:     float | None    # 0.0 when marker preserved; None when unknown/lost
    rto_seconds_target:       float
    rpo_seconds_target:       float
    rto_status:               str  # PASS | FAIL
    rpo_status:               str  # PASS | FAIL | UNKNOWN
    marker_present_on_promoted: bool
    notes:                    list[str]


def run_drill(args) -> DrillResult:
    drill_id = f"drill-{int(time.time())}"
    notes: list[str] = []
    log.info("Starting DR drill %s", drill_id)

    # Step 1: write marker on primary. Gracefully tolerate a primary that's
    # already down (forced-failover drill per Ch.10 § 10.6) -- the marker
    # step is best-effort; the drill still measures RTO from the switchover
    # command onward.
    marker_t0 = None
    try:
        log.info("Writing marker row to primary %s", args.primary_host)
        with _connect(args.engine, args.primary_host, args.user, args.password,
                      args.database) as conn:
            _ensure_probe_table(args.engine, conn)
            marker_t0 = _now()
            _write_marker(args.engine, conn, drill_id)
        notes.append(f"marker written at {marker_t0.isoformat()}")
    except Exception as exc:
        notes.append(
            f"primary marker-write skipped (forced-failover or primary down): {exc}"
        )
        log.warning("primary marker-write failed; continuing with measured RTO only")

    # Step 2: initiate switchover via supplied external command
    switchover_t0 = _now()
    log.info("Invoking switchover command: %s", args.switchover_command)
    proc = subprocess.run(args.switchover_command, shell=True,
                          capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        notes.append(f"switchover command exit={proc.returncode}: "
                     f"stderr={proc.stderr.strip()[:200]}")

    # Step 3: poll secondary until WRITABLE (not just reachable). See
    # _poll_until_writable() docstring for why SELECT 1 isn't sufficient.
    deadline = _now() + timedelta(seconds=max(args.target_rto_seconds * 4, 300))
    ok, switchover_t1 = _poll_until_writable(
        args.engine, args.secondary_host, args.user, args.password,
        args.database, deadline_at=deadline, drill_id=drill_id)

    rto_seconds = (switchover_t1 - switchover_t0).total_seconds()
    if not ok:
        notes.append("secondary did not become writable before deadline")
        log.error("RTO deadline missed; reporting FAIL")

    # Step 4: marker visibility check on promoted side. The RPO semantics:
    #   - marker_t0 not captured (primary was down): RPO unknown -- emit
    #     marker_present=False with an explanatory note.
    #   - marker present on promoted side: zero data loss FOR THIS MARKER.
    #     Report rpo_seconds_measured=0.0; the drill validates that committed
    #     transactions at marker_t0 survived the role swap.
    #   - marker absent: data loss occurred. Report rpo_seconds_measured=null
    #     (unbounded for this drill).
    marker_present = False
    rpo_seconds: float | None = None
    if marker_t0 is None:
        notes.append("RPO measurement skipped (no primary marker); "
                     "drill measures RTO only")
    else:
        try:
            with _connect(args.engine, args.secondary_host, args.user,
                          args.password, args.database) as conn:
                marker_present = _marker_present(args.engine, conn, drill_id)
            if marker_present:
                rpo_seconds = 0.0
                notes.append("marker preserved on promoted side -- "
                             "zero data loss for this drill's committed transaction")
            else:
                notes.append("marker NOT present on promoted side -- "
                             "data loss occurred at the switchover")
        except Exception as exc:
            notes.append(f"marker query on promoted side failed: {exc}")

    rpo_status = "PASS" if marker_present else (
        "UNKNOWN" if marker_t0 is None else "FAIL"
    )

    return DrillResult(
        drill_id=drill_id,
        started_at_utc=switchover_t0.isoformat(),
        completed_at_utc=switchover_t1.isoformat(),
        rto_seconds_measured=round(rto_seconds, 1),
        rpo_seconds_measured=rpo_seconds,
        rto_seconds_target=args.target_rto_seconds,
        rpo_seconds_target=args.target_rpo_seconds,
        rto_status="PASS" if rto_seconds <= args.target_rto_seconds and ok else "FAIL",
        rpo_status=rpo_status,
        marker_present_on_promoted=marker_present,
        notes=notes,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--engine",   required=True, choices=("oracle", "mssql", "pg"))
    p.add_argument("--primary-host",   required=True)
    p.add_argument("--secondary-host", required=True)
    p.add_argument("--database", required=True)
    p.add_argument("--user",     required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--switchover-command", required=True,
                   help="External command that triggers the role swap "
                        "(dgmgrl, az failover, postgres replica promote, ...).")
    p.add_argument("--target-rto-seconds", type=float, required=True)
    p.add_argument("--target-rpo-seconds", type=float, required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    result = run_drill(args)
    hdr = {
        "_artifact":     "hdr",
        "hdr_version":   HDR_VERSION,
        "generated_at_utc": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine":        args.engine,
        "primary_host":  args.primary_host,
        "secondary_host": args.secondary_host,
        "drill":         dataclasses.asdict(result),
        "open_questions": [],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(hdr, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    log.info("HDR written: %s", args.out)
    log.info("RTO: measured=%.1fs target=%.1fs -> %s",
             result.rto_seconds_measured, result.rto_seconds_target, result.rto_status)
    log.info("RPO: measured=%s target=%.1fs -> %s",
             result.rpo_seconds_measured, result.rpo_seconds_target, result.rpo_status)
    return 0 if result.rto_status == "PASS" and result.rpo_status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
