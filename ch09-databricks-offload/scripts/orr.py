#!/usr/bin/env python3
"""orr.py - Offload Readiness Report generator.

Reads the Ch.1 MAB output directory (schema_complexity + workload_baseline
+ fga_policies) and emits orr.json: a per-table recommendation about
whether to offload to Databricks and at which Medallion layers.

The scoring is opinionated and conservative -- mark tables as candidates
only when there is positive evidence of analytical workload pattern.
Production engagements refine weights after observing real Databricks
query patterns over the first weeks.

Usage:
    orr.py --mab-dir /var/mab/<run-id> --out orr.json
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ORR_VERSION = "1.0.0"
log = logging.getLogger("orr")


# Scoring rules. Each candidate table receives a recommendation across
# {SKIP, SILVER_ONLY, BRONZE_SILVER, FULL_MEDALLION}. The reasons attached
# tell the architect WHY -- so judgment calls can be reviewed.
@dataclasses.dataclass
class TableSignal:
    owner:               str
    table_name:          str
    object_type:         str
    is_partitioned:      bool
    row_estimate:        int | None
    has_fga:             bool
    fga_columns:         tuple[str, ...]
    has_lob:             bool
    is_materialized_view: bool


def classify(signal: TableSignal) -> dict[str, Any]:
    """Return the recommendation dict for one table.

    Rule order matters -- the LOG/AUDIT skip rule MUST come before the
    small-reference rule, otherwise log tables get classified SILVER_ONLY.
    """
    reasons: list[str] = []
    layer = "SKIP"
    name_upper = signal.table_name.upper()

    # Logs / audit tables -> Skip first (Ch.5's file_to_emit queue handles
    # operational logs; no analytical value justifying Medallion offload).
    # This rule must precede the small-reference rule so it isn't shadowed.
    if "LOG" in name_upper or "AUDIT" in name_upper:
        layer = "SKIP"
        reasons.append(
            "Audit / log table -- handled by Ch.5 file_to_emit consumer; "
            "no analytical value justifying Medallion offload."
        )
    # Big partitioned analytical table -> Full Medallion
    elif signal.is_partitioned and (signal.row_estimate or 0) > 1_000_000:
        layer = "FULL_MEDALLION"
        reasons.append(
            f"Range/list partitioned with >1M rows ({signal.row_estimate}); "
            f"natural fit for Delta time-travel and Z-ORDER on partition column."
        )
    # Materialized view -> Gold replacement
    elif signal.is_materialized_view:
        layer = "GOLD_REPLACEMENT"
        reasons.append(
            "Source materialized view -- compute it as a Gold Delta table "
            "rebuilt on schedule rather than refreshing in the OLTP engine."
        )
    # Large unpartitioned transactional table -> Bronze + Silver
    # (history retained in Bronze for forensics; Silver normalized for joins;
    #  no Gold rollup yet -- promote to Gold when an analytical pattern emerges)
    elif (signal.row_estimate or 0) > 1_000_000 and not signal.is_partitioned:
        layer = "BRONZE_SILVER"
        reasons.append(
            f"Large unpartitioned table (>1M rows: {signal.row_estimate}). "
            f"Bronze captures append-only history for forensic / audit needs; "
            f"Silver normalized for joins. No Gold rollup yet -- promote when "
            f"the analytics team identifies a recurring aggregate pattern."
        )
    # PII-bearing table -> Silver-only (with masks)
    elif signal.has_fga and not signal.is_partitioned:
        layer = "SILVER_ONLY"
        reasons.append(
            f"FGA-protected columns ({', '.join(signal.fga_columns) or '<unknown>'}); "
            f"Silver mirror with Unity Catalog column masking enables analytics "
            f"without exposing PII."
        )
    # Reference dim (small, no PII) -> Silver mirror
    elif (signal.row_estimate or 0) < 10_000 and not signal.has_fga:
        layer = "SILVER_ONLY"
        reasons.append(
            "Small reference dimension; Silver mirror sufficient for joins."
        )
    else:
        layer = "SKIP"
        reasons.append(
            "No positive offload signal (not partitioned, not large, no MV, "
            "no PII analytics need). Re-evaluate after workload observation."
        )

    # LOB columns warning regardless
    if signal.has_lob and layer != "SKIP":
        reasons.append(
            "LOB / CLOB columns present -- Bronze blobs will be substantial; "
            "consider routing LOBs to ADLS Gen2 directly with pointer columns "
            "in Bronze."
        )

    return {
        "owner":               signal.owner,
        "table_name":          signal.table_name,
        "object_type":         signal.object_type,
        "is_partitioned":      signal.is_partitioned,
        "row_estimate":        signal.row_estimate,
        "has_fga":             signal.has_fga,
        "is_materialized_view": signal.is_materialized_view,
        "recommended_layer":   layer,
        "reasons":             reasons,
    }


# ---------------------------------------------------------------------------
def load_signals_from_mab(mab_dir: Path) -> list[TableSignal]:
    """Read Ch.1 outputs and build a TableSignal per candidate object."""
    schema_complexity = mab_dir / "schema_complexity"
    fga_csv           = schema_complexity / "fga_policies.csv"
    partitions_csv    = schema_complexity / "partition_topology.csv"
    plsql_inv_csv     = schema_complexity / "plsql_inventory.csv"
    blockers_csv      = mab_dir / "blockers" / "blockers_inventory.csv"

    # FGA tables
    fga_by_table: dict[str, list[str]] = {}
    if fga_csv.is_file():
        with fga_csv.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                table = (row.get("OBJECT_NAME") or "").upper()
                col = row.get("POLICY_COLUMN") or ""
                if table:
                    fga_by_table.setdefault(table, []).append(col)

    # Partitioned tables, with real row counts when DBA_TAB_STATISTICS is
    # available. Ch.1 04_schema_complexity.sql emits NUM_ROWS via LEFT JOIN
    # dba_tables. When NUM_ROWS is missing (stats not gathered), we fall
    # back to a partition-count-based estimate AND log a warning so the
    # operator knows the classification is approximate.
    partitioned: set[str] = set()
    partition_row_counts: dict[str, int] = {}
    if partitions_csv.is_file():
        with partitions_csv.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                name = (row.get("TABLE_NAME") or "").upper()
                if not name:
                    continue
                partitioned.add(name)
                num_rows_raw = (row.get("NUM_ROWS") or "").strip()
                if num_rows_raw and num_rows_raw.lower() != "null":
                    partition_row_counts[name] = int(float(num_rows_raw))
                else:
                    pcount = int(row.get("PARTITION_COUNT") or "0")
                    partition_row_counts[name] = pcount * 1_000_000
                    log.warning("NUM_ROWS missing for %s; falling back to "
                                "%d partitions * 1M = %d (run DBMS_STATS.GATHER_TABLE_STATS "
                                "on the source for an accurate classification)",
                                name, pcount, partition_row_counts[name])

    # Materialized views found in blockers (Ch.1 routes MVs through blockers)
    mview_tables: set[str] = set()
    if blockers_csv.is_file():
        with blockers_csv.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if "Materialized view" in (row.get("BLOCKER_NAME") or ""):
                    # MV names come comma-separated in sample_objects; punt
                    # to manual classification by including any flagged MV.
                    for sample in (row.get("SAMPLE_OBJECTS") or "").split("|"):
                        name = sample.strip().split(".")[-1].upper()
                        if name:
                            mview_tables.add(name)

    # Build signals from a static HR-Pro inventory (production runs derive
    # from DBA_TABLES instead).
    static_inventory = [
        ("HRPRO", "DEPARTMENT",       "TABLE", 4),
        ("HRPRO", "JOB_GRADE",        "TABLE", 5),
        ("HRPRO", "EMPLOYEE",         "TABLE", 5_000),
        ("HRPRO", "EMPLOYEE_HISTORY", "TABLE", 30_000),
        ("HRPRO", "PAYROLL_RUN",      "TABLE", 100),
        ("HRPRO", "PAYROLL_RUN_LOG",  "TABLE", 1_000),
        ("HRPRO", "MV_HEADCOUNT_ROLLUP", "MATERIALIZED VIEW", 4),
    ]

    signals: list[TableSignal] = []
    for owner, name, otype, default_rows in static_inventory:
        is_part = name in partitioned
        rows = partition_row_counts.get(name, default_rows)
        signals.append(TableSignal(
            owner=owner,
            table_name=name,
            object_type=otype,
            is_partitioned=is_part,
            row_estimate=rows,
            has_fga=name in fga_by_table,
            fga_columns=tuple(fga_by_table.get(name, [])),
            has_lob=False,
            is_materialized_view=(otype == "MATERIALIZED VIEW") or (name in mview_tables),
        ))
    return signals


# ---------------------------------------------------------------------------
def build_orr(mab_dir: Path) -> dict[str, Any]:
    signals = load_signals_from_mab(mab_dir)
    tables = [classify(s) for s in signals]

    layer_counts: dict[str, int] = {}
    for t in tables:
        layer_counts[t["recommended_layer"]] = layer_counts.get(t["recommended_layer"], 0) + 1

    return {
        "_artifact":         "orr",
        "mab_dir":           str(mab_dir),
        "generated_at_utc":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "orr_version":       ORR_VERSION,
        "summary": {
            "total_objects":     len(tables),
            "by_layer":          dict(sorted(layer_counts.items())),
        },
        "tables":            tables,
        "open_questions": [
            f"{t['owner']}.{t['table_name']}: recommended layer={t['recommended_layer']}. "
            f"Confirm with the BI / analytics team before scoping the pipeline."
            for t in tables if t["recommended_layer"] != "SKIP"
        ],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--mab-dir", required=True, type=Path,
                   help="Path to extracted Ch.1 MAB directory.")
    p.add_argument("--out",     required=True, type=Path)
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.mab_dir.is_dir():
        p.error(f"--mab-dir is not a directory: {args.mab_dir}")

    orr = build_orr(args.mab_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(orr, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")

    log.info("ORR written: %s", args.out)
    log.info("Layer distribution: %s", orr["summary"]["by_layer"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
