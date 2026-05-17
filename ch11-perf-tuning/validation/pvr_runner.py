#!/usr/bin/env python3
"""pvr_runner.py - run the Golden Query Suite against source + target and
emit a Performance Validation Report (PVR).

The PVR is Ch.11's signed deliverable. For each query in the suite, it
records:
  - Latency on source
  - Latency on target
  - Result-row count comparison
  - Status: PASS (target within regression threshold)
                | DEGRADED (target slower than threshold; investigate)
                | REGRESSED (target >2x slower; blocker)
                | FAIL (different row counts or query error)

The framing per P22 (Ch.11 § 11.1): we compare OUTCOMES (rows, latency),
NOT plan trees. Plan trees use different operator vocabularies per engine.

Usage:
    pvr_runner.py --queries golden_queries.json \\
                  --source-engine oracle --source-dsn 'host:1521/SVC' \\
                  --source-user mig_assess --source-password '...' \\
                  --target-engine pg --target-host pg-flex.../ ... \\
                  --target-user labadmin --target-password '...' \\
                  --regression-threshold 1.5 \\
                  --out pvr.json
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PVR_VERSION = "1.0.0"
log = logging.getLogger("pvr")


@contextlib.contextmanager
def _connect(engine: str, host: str, dsn: str | None,
             user: str, password: str, database: str | None):
    conn = None
    try:
        if engine == "oracle":
            import oracledb
            conn = oracledb.connect(user=user, password=password, dsn=dsn or host)
        elif engine == "mssql":
            import pyodbc
            cs = (f"DRIVER={{ODBC Driver 18 for SQL Server}};"
                  f"SERVER=tcp:{host},1433;DATABASE={database or 'labdb'};"
                  f"UID={user};PWD={password};"
                  f"Encrypt=yes;TrustServerCertificate=no")
            conn = pyodbc.connect(cs)
        elif engine == "pg":
            try:
                import psycopg as pg
            except ImportError:
                import psycopg2 as pg  # type: ignore[no-redef]
            conn = pg.connect(host=host, dbname=database or "labdb",
                              user=user, password=password, sslmode="require")
        else:
            raise ValueError(f"unknown engine {engine}")
        yield conn
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _measure_query(engine: str, conn, sql: str, params: list[Any],
                   runs: int = 3, warmup_runs: int = 1) -> dict[str, Any]:
    """Run the query `warmup_runs + runs` times; discard the warmup; return
    per-run latency on the remaining runs plus the final row count.

    Cold-cache bias note: a freshly-migrated target has empty buffer cache;
    the source has years of warm pages. `warmup_runs` (default 1) lets the
    target prime its cache before we start measuring, so the per-engine
    comparison isn't systematically biased against the target. Set to 0 to
    measure cold-cache latency explicitly.

    Failure semantics: on the first error, we break out and report whatever
    successful latencies we already collected. A failing run does NOT get
    its latency recorded (latency on error is meaningless and would skew the
    median).
    """
    latencies: list[float] = []
    rows_returned = 0
    error: str | None = None

    total_runs = warmup_runs + runs
    for i in range(total_runs):
        cur = conn.cursor()
        start = time.perf_counter()
        try:
            if engine == "mssql":
                cur.execute(sql, *params)
            elif engine == "oracle":
                cur.execute(sql, params)
            else:
                cur.execute(sql, tuple(params) if params else None)
            if cur.description is not None:
                rows = cur.fetchall()
                rows_returned = len(rows)
            elapsed = time.perf_counter() - start
            # Skip warmup runs from the latency record.
            if i >= warmup_runs:
                latencies.append(elapsed)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            break
        finally:
            try:
                cur.close()
            except Exception:
                pass

    # Keep both an unrounded median (for the regression-factor math; small
    # queries can round to 0.0 at 4 decimal places and silently mask
    # regressions) and the rounded values (for the report's readability).
    if latencies:
        median_raw = statistics.median(latencies)
        ret = {
            "latency_seconds_min":    round(min(latencies), 4),
            "latency_seconds_median": round(median_raw, 4),
            "latency_seconds_max":    round(max(latencies), 4),
            "_median_raw_seconds":    median_raw,    # used by _classify, not part of the report payload
        }
    else:
        ret = {
            "latency_seconds_min":    None,
            "latency_seconds_median": None,
            "latency_seconds_max":    None,
            "_median_raw_seconds":    None,
        }
    ret.update({
        "rows_returned":          rows_returned,
        "error":                  error,
        "runs":                   len(latencies),
        "warmup_runs_skipped":    warmup_runs,
    })
    return ret


@dataclasses.dataclass
class QueryResult:
    query_id:      str
    description:   str
    source_metrics: dict[str, Any]
    target_metrics: dict[str, Any]
    regression_factor: float | None  # target_median / source_median
    status:        str
    notes:         list[str]


def _classify(src: dict[str, Any], tgt: dict[str, Any],
              regression_threshold: float) -> tuple[str, float | None, list[str]]:
    notes: list[str] = []
    if src["error"] or tgt["error"]:
        notes.append(f"source error: {src['error']!r}; target error: {tgt['error']!r}")
        return "FAIL", None, notes
    if src["rows_returned"] != tgt["rows_returned"]:
        notes.append(
            f"row-count mismatch source={src['rows_returned']} "
            f"target={tgt['rows_returned']}")
        return "FAIL", None, notes

    # Use the UNROUNDED median for the regression-factor math. A query that
    # runs in 50us rounds to 0.0 at 4 decimal places; if we divided rounded
    # 0.0/0.0 we'd silently produce PASS for a 10x regression.
    src_med = src.get("_median_raw_seconds")
    tgt_med = tgt.get("_median_raw_seconds")
    if src_med is None or tgt_med is None or src_med == 0:
        notes.append("source median latency is null or zero; cannot compute regression factor")
        return "PASS", None, notes

    factor = tgt_med / src_med
    if factor > 2.0:
        return "REGRESSED", round(factor, 2), notes
    if factor > regression_threshold:
        return "DEGRADED", round(factor, 2), notes
    return "PASS", round(factor, 2), notes


def run_pvr(args) -> dict[str, Any]:
    # The shipped golden_queries.json wraps the list in a top-level object
    # so it can carry an _comment field. Tolerate both shapes: a top-level
    # list (legacy form) and an object with a 'queries' key (current form).
    doc = json.loads(args.queries.read_text(encoding="utf-8"))
    if isinstance(doc, dict) and "queries" in doc:
        queries = doc["queries"]
    elif isinstance(doc, list):
        queries = doc
    else:
        raise ValueError(
            f"{args.queries} must be either a JSON array of query specs "
            f"or an object with a 'queries' array; got {type(doc).__name__}"
        )

    with _connect(args.source_engine, args.source_host, args.source_dsn,
                  args.source_user, args.source_password, args.source_database) as src_conn, \
         _connect(args.target_engine, args.target_host, None,
                  args.target_user, args.target_password, args.target_database) as tgt_conn:
        results: list[QueryResult] = []
        for q in queries:
            qid = q["query_id"]
            log.info("running %s", qid)
            sql_src = q["sql"][args.source_engine]
            sql_tgt = q["sql"][args.target_engine]
            params  = q.get("params", [])

            src_metrics = _measure_query(args.source_engine, src_conn, sql_src,
                                         params, warmup_runs=args.warmup_runs)
            tgt_metrics = _measure_query(args.target_engine, tgt_conn, sql_tgt,
                                         params, warmup_runs=args.warmup_runs)

            status, factor, notes = _classify(src_metrics, tgt_metrics,
                                              args.regression_threshold)

            # Strip the internal _median_raw_seconds key before the metrics
            # ship in the PVR -- it's an implementation detail of _classify.
            src_metrics.pop("_median_raw_seconds", None)
            tgt_metrics.pop("_median_raw_seconds", None)

            results.append(QueryResult(
                query_id=qid,
                description=q.get("description", ""),
                source_metrics=src_metrics,
                target_metrics=tgt_metrics,
                regression_factor=factor,
                status=status,
                notes=notes,
            ))

    summary_counts: dict[str, int] = {"PASS": 0, "DEGRADED": 0, "REGRESSED": 0, "FAIL": 0}
    for r in results:
        summary_counts[r.status] = summary_counts.get(r.status, 0) + 1

    return {
        "_artifact":       "pvr",
        "pvr_version":     PVR_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_engine":   args.source_engine,
        "target_engine":   args.target_engine,
        "regression_threshold": args.regression_threshold,
        "summary":         {"total_queries": len(results),
                            "by_status":     summary_counts},
        "queries":         [dataclasses.asdict(r) for r in results],
        "open_questions": [
            f"{r.query_id}: status={r.status}, "
            f"regression_factor={r.regression_factor} -- {r.notes}"
            for r in results if r.status != "PASS"
        ],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--queries",            required=True, type=Path)
    p.add_argument("--source-engine",      required=True, choices=("oracle", "mssql", "pg"))
    p.add_argument("--source-host",        default="")
    p.add_argument("--source-dsn",         default=None)
    p.add_argument("--source-user",        required=True)
    p.add_argument("--source-password",    required=True)
    p.add_argument("--source-database",    default=None)
    p.add_argument("--target-engine",      required=True, choices=("oracle", "mssql", "pg"))
    p.add_argument("--target-host",        required=True)
    p.add_argument("--target-user",        required=True)
    p.add_argument("--target-password",    required=True)
    p.add_argument("--target-database",    default="labdb")
    p.add_argument("--regression-threshold", type=float, default=1.5,
                   help="Target/source latency ratio above this -> DEGRADED.")
    p.add_argument("--warmup-runs", type=int, default=1,
                   help="Number of warmup runs to discard before measurement. "
                        "Default 1 -- mitigates the freshly-migrated-target "
                        "cold-cache bias against the source.")
    p.add_argument("--out",                required=True, type=Path)
    p.add_argument("--verbose", "-v",      action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    pvr = run_pvr(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(pvr, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    log.info("PVR written: %s", args.out)
    s = pvr["summary"]
    log.info("Summary: %d queries  PASS=%d DEGRADED=%d REGRESSED=%d FAIL=%d",
             s["total_queries"], s["by_status"]["PASS"],
             s["by_status"]["DEGRADED"], s["by_status"]["REGRESSED"],
             s["by_status"]["FAIL"])
    # Non-zero exit if any REGRESSED or FAIL -- CI gating.
    return 0 if s["by_status"]["REGRESSED"] == 0 and s["by_status"]["FAIL"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
