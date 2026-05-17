#!/usr/bin/env python3
"""audit_translate.py - translate Oracle FGA / Unified Audit policies into
target-engine audit DDL and emit a draft Audit Migration Report (AMR).

Input  : Ch.1 fga_policies.csv (and any unified-audit rows it contains)
Output : per-target DDL file(s) + amr.json (draft; 04_audit_validate.py
         re-runs against the live target to produce the final AMR)

Usage:
    audit_translate.py --in fga_policies.csv \\
                       --target-engine mssql|pg \\
                       --target-schema dbo|hrpro \\
                       --out-ddl audit.sql \\
                       --out-amr amr.json
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AMR_VERSION = "1.0.0"
log = logging.getLogger("audit_translate")


# ---------------------------------------------------------------------------
# Per-table PK column lookup.
#
# Trigger-based audit overlays (PG path's DEGRADED case) need to reference
# the PK column of the audited table. The map below is the lab's HR-Pro
# baseline; production engagements pass --pk-map pointing at a JSON file
# derived from DBA_CONSTRAINTS.
# ---------------------------------------------------------------------------
HRPRO_PK_MAP: dict[str, str] = {
    "DEPARTMENT":       "DEPT_ID",
    "JOB_GRADE":        "GRADE_ID",
    "EMPLOYEE":         "EMP_ID",
    "EMPLOYEE_HISTORY": "HISTORY_ID",
    "PAYROLL_RUN":      "RUN_ID",
    "PAYROLL_RUN_LOG":  "LOG_ID",
}


def resolve_pk(object_name: str, pk_map: dict[str, str]) -> str | None:
    """Look up PK column case-insensitively; return None if absent."""
    if not object_name:
        return None
    key = object_name.upper()
    return pk_map.get(key)


# ---------------------------------------------------------------------------
# Translation rules.
#
# For each (source, target) pair we compute:
#   - target_statement: the DDL to apply
#   - fidelity:         EQUIVALENT | DEGRADED | UNSUPPORTED
#   - degradation_notes: human-readable reason if fidelity != EQUIVALENT
# ---------------------------------------------------------------------------
def translate_to_mssql(policy: dict[str, str], target_schema: str) -> dict[str, Any]:
    source_obj  = policy.get("OBJECT_NAME") or "<unknown>"
    source_col  = policy.get("POLICY_COLUMN") or ""
    actions     = [a.strip() for a in (policy.get("STATEMENT_TYPES") or "").split(",") if a.strip()]
    policy_name = policy.get("POLICY_NAME") or "policy"

    spec_name = f"HRPro_Audit_{policy_name}"
    add_lines = ",\n".join(
        f"ADD ({act} ON OBJECT::{target_schema}.{source_obj} BY public)"
        for act in actions
    ) or f"ADD (SELECT ON OBJECT::{target_schema}.{source_obj} BY public)"

    ddl = (
        f"-- Translation of FGA policy {policy_name} on {policy.get('OBJECT_OWNER')}.{source_obj}"
        f"{(' column ' + source_col) if source_col else ''}\n"
        f"CREATE DATABASE AUDIT SPECIFICATION [{spec_name}]\n"
        f"FOR SERVER AUDIT [HRPro_Audit]\n"
        f"{add_lines}\n"
        f"WITH (STATE = ON);\n"
        f"GO\n"
    )

    fidelity = "EQUIVALENT"
    notes = ""
    if source_col:
        fidelity = "DEGRADED"
        notes = (f"Source FGA audited column '{source_col}' specifically. "
                 f"Azure SQL Audit reduces this to object-level on "
                 f"{target_schema}.{source_obj}.")
    return {"target_statement": ddl, "fidelity": fidelity, "degradation_notes": notes}


def translate_to_pg(policy: dict[str, str], target_schema: str,
                    pk_map: dict[str, str]) -> dict[str, Any]:
    source_obj_raw = policy.get("OBJECT_NAME") or ""
    source_obj  = source_obj_raw.lower()
    source_col  = (policy.get("POLICY_COLUMN") or "").lower()
    actions     = [a.strip().upper() for a in (policy.get("STATEMENT_TYPES") or "").split(",") if a.strip()]
    policy_name = policy.get("POLICY_NAME") or "policy"

    # pgAudit log classes mapping
    classes: set[str] = set()
    if "SELECT" in actions:  classes.add("read")
    if any(a in actions for a in ("INSERT", "UPDATE", "DELETE", "TRUNCATE")): classes.add("write")
    classes_str = ", ".join(sorted(classes)) or "read, write"

    # Trigger-based overlay shape for the column-level case.
    # The row identifier comes from the per-table PK map (Ch.6's HRPRO_TABLES
    # baseline by default; production overrides via --pk-map).
    pk_col_upper = resolve_pk(source_obj_raw, pk_map)
    trigger_overlay = ""
    fidelity = "EQUIVALENT" if not source_col else "DEGRADED"
    notes = ""

    if source_col:
        if pk_col_upper is None:
            fidelity = "UNSUPPORTED"
            notes = (f"pgAudit cannot audit reads per-column; trigger-overlay "
                     f"fallback requires a PK column for '{source_obj_raw}' but "
                     f"none was supplied. Provide --pk-map JSON mapping "
                     f"OBJECT_NAME -> PK column to enable the overlay.")
            trigger_overlay = (
                f"-- UNSUPPORTED: no PK known for {source_obj_raw}. Supply "
                f"--pk-map or add a manual overlay before applying.\n"
            )
        else:
            pk_col = pk_col_upper.lower()
            notes = (f"pgAudit cannot audit reads per-column. Source FGA on "
                     f"column '{source_col}' is preserved for UPDATE via the "
                     f"emitted trigger keyed on PK={pk_col}; SELECT-side "
                     f"per-column audit requires either an application-layer "
                     f"overlay (Ch.8.5) or a view + INSTEAD OF trigger.")
            trigger_overlay = (
                f"-- Trigger-based per-column audit overlay for {source_col}\n"
                f"CREATE OR REPLACE FUNCTION {target_schema}.trg_audit_{policy_name.lower()}()\n"
                f"RETURNS TRIGGER LANGUAGE plpgsql AS $$\n"
                f"BEGIN\n"
                f"    IF NEW.{source_col} IS DISTINCT FROM OLD.{source_col} THEN\n"
                f"        INSERT INTO {target_schema}.pii_audit\n"
                f"          (operation, object_owner, object_name, column_name, row_pk)\n"
                f"        VALUES ('UPDATE', '{target_schema}', TG_TABLE_NAME,\n"
                f"                '{source_col}', NEW.{pk_col}::text);\n"
                f"    END IF;\n"
                f"    RETURN NEW;\n"
                f"END;\n"
                f"$$;\n\n"
                f"DROP TRIGGER IF EXISTS trg_audit_{policy_name.lower()} ON {target_schema}.{source_obj};\n"
                f"CREATE TRIGGER trg_audit_{policy_name.lower()}\n"
                f"AFTER UPDATE OF {source_col} ON {target_schema}.{source_obj}\n"
                f"FOR EACH ROW EXECUTE FUNCTION {target_schema}.trg_audit_{policy_name.lower()}();\n"
            )

    ddl = (
        f"-- Translation of FGA policy {policy_name} on "
        f"{policy.get('OBJECT_OWNER')}.{source_obj}"
        f"{(' column ' + source_col) if source_col else ''}\n"
        f"-- Server-level pgAudit classes (set via Azure portal/CLI, not SQL):\n"
        f"--   pgaudit.log = '{classes_str}'\n"
        f"\n"
        f"{trigger_overlay}"
    )

    return {"target_statement": ddl, "fidelity": fidelity, "degradation_notes": notes}


# ---------------------------------------------------------------------------
def build_amr(rows: list[dict[str, str]], engine: str,
              target_schema: str, pk_map: dict[str, str]) -> tuple[str, dict[str, Any]]:
    target_emitted: list[dict[str, Any]] = []
    ddl_blocks: list[str] = []

    for row in rows:
        if engine == "mssql":
            result = translate_to_mssql(row, target_schema)
        else:
            result = translate_to_pg(row, target_schema, pk_map)
        ddl_blocks.append(result["target_statement"])
        target_emitted.append({
            "source_policy": row.get("POLICY_NAME"),
            "source_object": f"{row.get('OBJECT_OWNER')}.{row.get('OBJECT_NAME')}",
            "source_column": row.get("POLICY_COLUMN") or None,
            "source_statement_types": row.get("STATEMENT_TYPES"),
            "target_statement":     result["target_statement"],
            "fidelity":             result["fidelity"],
            "degradation_notes":    result["degradation_notes"],
        })

    summary = {
        "total_policies":     len(target_emitted),
        "equivalent_count":   sum(1 for e in target_emitted if e["fidelity"] == "EQUIVALENT"),
        "degraded_count":     sum(1 for e in target_emitted if e["fidelity"] == "DEGRADED"),
        "unsupported_count":  sum(1 for e in target_emitted if e["fidelity"] == "UNSUPPORTED"),
    }

    open_questions = [
        f"Policy {e['source_policy']} on {e['source_object']}: fidelity={e['fidelity']}. "
        f"Compliance review required."
        for e in target_emitted if e["fidelity"] != "EQUIVALENT"
    ]

    amr = {
        "_artifact":         "amr",
        "source_db":         "ORA19CPROD",
        "target_engine":     engine,
        "generated_at_utc":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "amr_version":       AMR_VERSION,
        "fga_policies_in":   rows,
        "target_audit_emitted": target_emitted,
        "target_verified":   [],   # filled in by 04_audit_validate.py
        "summary":           summary,
        "open_questions":    open_questions,
    }

    ddl = "\n".join(ddl_blocks)
    return ddl, amr


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--in",            dest="in_csv",     required=True, type=Path)
    p.add_argument("--target-engine",                    required=True, choices=("mssql", "pg"))
    p.add_argument("--target-schema",                    required=True)
    p.add_argument("--pk-map",        type=Path,
                   help="Optional JSON file mapping OBJECT_NAME -> PK column. "
                        "Defaults to the HR-Pro baseline (HRPRO_PK_MAP).")
    p.add_argument("--out-ddl",                          required=True, type=Path)
    p.add_argument("--out-amr",                          required=True, type=Path)
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with args.in_csv.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    log.info("Read %d FGA / Unified Audit rows", len(rows))

    # Resolve PK map: --pk-map JSON overrides the HR-Pro baseline.
    pk_map = dict(HRPRO_PK_MAP)
    if args.pk_map:
        external = json.loads(args.pk_map.read_text(encoding="utf-8"))
        if not isinstance(external, dict):
            raise ValueError("--pk-map must be a JSON object {object_name: pk_column}")
        pk_map.update({k.upper(): v for k, v in external.items()})
    log.info("PK map has %d entries", len(pk_map))

    ddl, amr = build_amr(rows, args.target_engine, args.target_schema, pk_map)

    args.out_ddl.parent.mkdir(parents=True, exist_ok=True)
    args.out_ddl.write_text(ddl, encoding="utf-8")
    args.out_amr.parent.mkdir(parents=True, exist_ok=True)
    args.out_amr.write_text(json.dumps(amr, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")

    log.info("DDL emitted:    %s", args.out_ddl)
    log.info("Draft AMR:      %s", args.out_amr)
    s = amr["summary"]
    log.info("Summary: %d policies  equivalent=%d  degraded=%d  unsupported=%d",
             s["total_policies"], s["equivalent_count"],
             s["degraded_count"], s["unsupported_count"])
    if s["degraded_count"] or s["unsupported_count"]:
        log.warning("Compliance sign-off required for non-EQUIVALENT translations")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
