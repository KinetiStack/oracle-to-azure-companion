#!/usr/bin/env python3
"""reconcile.py - prove source = target after offline data migration.

Computes per-table row counts and per-table sample-row hashes against the
Oracle source and one target engine (Oracle, MS SQL / MI, or PostgreSQL).
Emits a Migration Reconciliation Report (MRR) JSON document conforming to
reconciliation_schema.json.

Design constraints:
  - Stdlib + the engine-specific driver only.
  - Deterministic: same data, same sample-size, same seed -> same MRR.
  - Engine-agnostic hashing: rows are pulled raw and hashed *in Python*,
    so the comparison doesn't depend on the engines' native hash semantics.
  - Hard-codes the HR-Pro schema; production engagements derive the table
    list from INFORMATION_SCHEMA / DBA_TABLES.

Usage:
    reconcile.py --schema HRPRO \\
                 --source-dsn 'localhost:1521/ORCLPDB1' \\
                 --source-user hrpro --source-password '...' \\
                 --target-engine pg \\
                 --target-host pg-flex.postgres.database.azure.com \\
                 --target-db   labdb --target-user labadmin \\
                 --target-password '...' \\
                 --sample-size 1000 \\
                 --out mrr.json
"""
from __future__ import annotations

import argparse
import dataclasses
import decimal
import hashlib
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

RECONCILE_VERSION = "1.0.0"

# Hard-coded HR-Pro inventory. In a production run this would be loaded
# from DBA_TABLES + DBA_CONSTRAINTS. The lab keeps it inline for clarity.
HRPRO_TABLES: list[dict[str, Any]] = [
    {"name": "DEPARTMENT",       "pk": "DEPT_ID",    "is_partitioned": False},
    {"name": "JOB_GRADE",        "pk": "GRADE_ID",   "is_partitioned": False},
    {"name": "EMPLOYEE",         "pk": "EMP_ID",     "is_partitioned": False},
    {"name": "EMPLOYEE_HISTORY", "pk": "HISTORY_ID", "is_partitioned": True},
    {"name": "PAYROLL_RUN",      "pk": "RUN_ID",     "is_partitioned": False},
    {"name": "PAYROLL_RUN_LOG",  "pk": "LOG_ID",     "is_partitioned": False},
]

log = logging.getLogger("reconcile")


# -----------------------------------------------------------------------------
# Cross-engine value normalization for deterministic hashing.
# -----------------------------------------------------------------------------
def normalize(value: Any) -> str:
    if value is None:
        return "<NULL>"
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, bytearray):
        return bytes(value).hex()
    if isinstance(value, datetime):
        # Strip TZ to UTC ISO 8601; this matters for Oracle TIMESTAMP WITH TZ
        # vs PG timestamptz vs T-SQL datetime2 (no TZ).
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, decimal.Decimal):
        # Normalize: strip trailing zeros, no leading "+"
        return format(value.normalize(), "f").rstrip("0").rstrip(".") or "0"
    if isinstance(value, float):
        return repr(value)  # repr() is round-trip-stable for floats
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def row_hash(row: Iterable[Any]) -> str:
    payload = "|".join(normalize(v) for v in row)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# Engine adapters. Each provides: connect(), count(), sample_rows().
# -----------------------------------------------------------------------------
class OracleAdapter:
    def __init__(self, dsn: str, user: str, password: str) -> None:
        import oracledb
        self.conn = oracledb.connect(user=user, password=password, dsn=dsn)
        self.identifier_case = "upper"

    def count(self, owner: str, table: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {owner}.{table}")
            return cur.fetchone()[0]

    def sample_rows(self, owner: str, table: str, pk: str,
                    sample_size: int) -> tuple[list[Any], list[tuple]]:
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM {owner}.{table} "
                f"WHERE MOD({pk}, :step) = 0 "
                f"ORDER BY {pk} "
                f"FETCH FIRST :n ROWS ONLY",
                step=max(1, 10), n=sample_size,
            )
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            return cols, rows


class MssqlAdapter:
    def __init__(self, host: str, db: str, user: str, password: str) -> None:
        import pyodbc
        cs = (f"DRIVER={{ODBC Driver 18 for SQL Server}};"
              f"SERVER=tcp:{host},1433;DATABASE={db};UID={user};PWD={password};"
              f"Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30")
        self.conn = pyodbc.connect(cs)
        self.identifier_case = "preserved"

    def count(self, owner: str, table: str) -> int:
        cur = self.conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {owner}.{table}")
        return cur.fetchone()[0]

    def sample_rows(self, owner: str, table: str, pk: str,
                    sample_size: int) -> tuple[list[Any], list[tuple]]:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT TOP (?) * FROM {owner}.{table} "
            f"WHERE {pk} % 10 = 0 "
            f"ORDER BY {pk}", sample_size,
        )
        cols = [d[0] for d in cur.description]
        rows = [tuple(r) for r in cur.fetchall()]
        return cols, rows


class PgAdapter:
    def __init__(self, host: str, db: str, user: str, password: str) -> None:
        try:
            import psycopg as _pg
            self.driver = "psycopg3"
        except ImportError:
            import psycopg2 as _pg
            self.driver = "psycopg2"
        cs = f"host={host} dbname={db} user={user} password={password} sslmode=require"
        self.conn = _pg.connect(cs)
        self.identifier_case = "lower"

    def count(self, owner: str, table: str) -> int:
        cur = self.conn.cursor()
        cur.execute(f'SELECT COUNT(*) FROM "{owner.lower()}"."{table.lower()}"')
        return cur.fetchone()[0]

    def sample_rows(self, owner: str, table: str, pk: str,
                    sample_size: int) -> tuple[list[Any], list[tuple]]:
        cur = self.conn.cursor()
        cur.execute(
            f'SELECT * FROM "{owner.lower()}"."{table.lower()}" '
            f'WHERE "{pk.lower()}" %% 10 = 0 '
            f'ORDER BY "{pk.lower()}" '
            f'LIMIT %s', (sample_size,),
        )
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return cols, rows


# -----------------------------------------------------------------------------
# Reconciliation core
# -----------------------------------------------------------------------------
@dataclasses.dataclass
class TableResult:
    owner:             str
    table_name:        str
    source_row_count:  int | None = None
    target_row_count:  int | None = None
    sample_size:       int       = 0
    hash_matches:      int       = 0
    hash_mismatches:   int       = 0
    error_message:     str | None = None

    @property
    def row_delta(self) -> int | None:
        if self.source_row_count is None or self.target_row_count is None:
            return None
        return self.target_row_count - self.source_row_count

    @property
    def row_delta_pct(self) -> float | None:
        if self.source_row_count in (None, 0):
            return None if self.source_row_count is None else 100.0
        return round(100 * (self.row_delta or 0) / self.source_row_count, 3)

    @property
    def status(self) -> str:
        if self.error_message is not None:
            return "ERROR"
        if self.target_row_count == 0 and self.source_row_count and self.source_row_count > 0:
            return "MISSING"
        if self.row_delta == 0 and self.hash_mismatches == 0:
            return "PASS"
        return "DELTA"


def reconcile(source, target, source_schema: str, target_schema: str,
              sample_size: int) -> list[TableResult]:
    """Reconcile source vs target. source_schema and target_schema may differ
    when the conversion mapped HRPRO -> dbo (Ch.4 SSMA default) or
    HRPRO -> hrpro (Ora2Pg case-fold). Source identity is preserved in
    the result's `owner` field; target queries use target_schema."""
    results: list[TableResult] = []
    for entry in HRPRO_TABLES:
        name = entry["name"]; pk = entry["pk"]
        r = TableResult(owner=source_schema, table_name=name, sample_size=sample_size)
        try:
            r.source_row_count = source.count(source_schema, name)
            r.target_row_count = target.count(target_schema, name)

            scols, srows = source.sample_rows(source_schema, name, pk, sample_size)
            tcols, trows = target.sample_rows(target_schema, name, pk, sample_size)

            # Order columns case-insensitively into a shared shape and zip rows by PK.
            scolmap = {c.upper(): i for i, c in enumerate(scols)}
            tcolmap = {c.upper(): i for i, c in enumerate(tcols)}
            shared  = [c for c in scolmap if c in tcolmap]
            pk_u    = pk.upper()

            src_by_pk = {tuple([normalize(s[scolmap[pk_u]])]): s for s in srows}
            tgt_by_pk = {tuple([normalize(t[tcolmap[pk_u]])]): t for t in trows}

            for key, srow in src_by_pk.items():
                trow = tgt_by_pk.get(key)
                if trow is None:
                    r.hash_mismatches += 1
                    continue
                shared_src = [srow[scolmap[c]] for c in shared]
                shared_tgt = [trow[tcolmap[c]] for c in shared]
                if row_hash(shared_src) == row_hash(shared_tgt):
                    r.hash_matches += 1
                else:
                    r.hash_mismatches += 1
        except Exception as exc:
            r.error_message = f"{type(exc).__name__}: {exc}"
        results.append(r)
    return results


def build_mrr(args, source, target, results: list[TableResult]) -> dict[str, Any]:
    total_src = sum((r.source_row_count or 0) for r in results)
    total_tgt = sum((r.target_row_count or 0) for r in results)
    # target_endpoint is the connection coordinate for the target, not the
    # source. Oracle target uses --target-dsn; MSSQL/PG use --target-host.
    target_endpoint = (
        getattr(args, "target_dsn",  None)
        or getattr(args, "target_host", None)
        or "<unknown>"
    )
    return {
        "_artifact":         "mrr",
        "schema":            args.schema,
        "target_schema":     args.target_schema,
        "source_db":         args.source_dsn,
        "target_engine":     args.target_engine,
        "target_endpoint":   target_endpoint,
        "sample_size":       args.sample_size,
        "generated_at_utc":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reconcile_version": RECONCILE_VERSION,
        "summary": {
            "total_tables":      len(results),
            "tables_pass":       sum(1 for r in results if r.status == "PASS"),
            "tables_with_delta": sum(1 for r in results if r.status == "DELTA"),
            "tables_missing":    sum(1 for r in results if r.status == "MISSING"),
            "tables_error":      sum(1 for r in results if r.status == "ERROR"),
            "total_source_rows": total_src,
            "total_target_rows": total_tgt,
        },
        "tables": [
            {
                "owner":            r.owner,
                "table_name":       r.table_name,
                "status":           r.status,
                "source_row_count": r.source_row_count,
                "target_row_count": r.target_row_count,
                "row_delta":        r.row_delta,
                "row_delta_pct":    r.row_delta_pct,
                "sample_size":      r.sample_size,
                "hash_matches":     r.hash_matches,
                "hash_mismatches":  r.hash_mismatches,
                "error_message":    r.error_message,
            }
            for r in results
        ],
        "open_questions": [
            f"{r.owner}.{r.table_name}: row delta {r.row_delta} ({r.row_delta_pct}%)"
            for r in results if r.status == "DELTA"
        ] + [
            f"{r.owner}.{r.table_name}: ERROR -- {r.error_message}"
            for r in results if r.status == "ERROR"
        ],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--schema",          required=True,
                   help="Source-side schema name (e.g. HRPRO)")
    p.add_argument("--target-schema",
                   help="Target-side schema name. Defaults: HRPRO for "
                        "--target-engine=oracle; dbo for mssql; hrpro for pg.")
    p.add_argument("--source-dsn",      required=True)
    p.add_argument("--source-user",     required=True)
    p.add_argument("--source-password", required=True)
    p.add_argument("--target-engine",   required=True, choices=("oracle", "mssql", "pg"))
    p.add_argument("--target-dsn")          # for target=oracle
    p.add_argument("--target-host")         # for target=mssql or pg
    p.add_argument("--target-db",        default="labdb")
    p.add_argument("--target-user",     required=True)
    p.add_argument("--target-password", required=True)
    p.add_argument("--sample-size",     type=int, default=1000)
    p.add_argument("--out",             required=True, type=Path)
    p.add_argument("--verbose", "-v",   action="store_true")
    args = p.parse_args(argv)

    # Defaults that match Ch.4 conversion conventions.
    if not args.target_schema:
        args.target_schema = {
            "oracle": args.schema,
            "mssql":  "dbo",
            "pg":     args.schema.lower(),
        }[args.target_engine]

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    log.info("Connecting to Oracle source")
    source = OracleAdapter(args.source_dsn, args.source_user, args.source_password)

    if args.target_engine == "oracle":
        if not args.target_dsn:
            p.error("--target-dsn required for --target-engine=oracle")
        target = OracleAdapter(args.target_dsn, args.target_user, args.target_password)
    elif args.target_engine == "mssql":
        if not args.target_host:
            p.error("--target-host required for --target-engine=mssql")
        target = MssqlAdapter(args.target_host, args.target_db,
                              args.target_user, args.target_password)
    else:  # pg
        if not args.target_host:
            p.error("--target-host required for --target-engine=pg")
        target = PgAdapter(args.target_host, args.target_db,
                           args.target_user, args.target_password)

    log.info("Reconciling %d tables (source.schema=%s -> target.schema=%s)",
             len(HRPRO_TABLES), args.schema, args.target_schema)
    results = reconcile(source, target, args.schema, args.target_schema,
                        args.sample_size)

    mrr = build_mrr(args, source, target, results)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(mrr, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    log.info("MRR written: %s", args.out)
    s = mrr["summary"]
    log.info("PASS: %d   DELTA: %d   MISSING: %d   ERROR: %d   (source rows: %d   target rows: %d)",
             s["tables_pass"], s["tables_with_delta"],
             s["tables_missing"], s["tables_error"],
             s["total_source_rows"], s["total_target_rows"])
    return 0 if s["tables_with_delta"] == 0 and s["tables_missing"] == 0 and s["tables_error"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
