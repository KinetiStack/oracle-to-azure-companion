#!/usr/bin/env python3
"""conversion_diff.py - generate a Conversion Assessment Report (CAR).

Compares the HR-Pro source object inventory (queried from Oracle 19c) against
the converted SQL files produced by SSMA (Pipeline A) and Ora2Pg (Pipeline B).
Emits a CAR JSON document conforming to car_schema.json.

Inputs:
  - Oracle connection (thin-mode python-oracledb)
  - SSMA output directory (--ssma-dir; pass empty to skip Pipeline A)
  - Ora2Pg output directory (--ora2pg-dir; pass empty to skip Pipeline B)

The detection of "did the converter handle this object" is heuristic. We grep
for CREATE statements naming the source object in the produced .sql files.
False positives (e.g. a procedure body referencing a table) are filtered by
matching only top-of-statement CREATE forms.

Usage:
    python3 03_conversion_diff.py --schema HRPRO \\
        --oracle-dsn 'localhost:1521/ORCLPDB1' \\
        --oracle-user hrpro --oracle-password 'pw' \\
        --ssma-dir   ../conversion/converted/ssma \\
        --ora2pg-dir ../conversion/converted/ora2pg \\
        --out        ../car.json
"""
from __future__ import annotations
import argparse
import dataclasses
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import oracledb

CAR_VERSION = "1.0.0"

# Source object types we consider in scope for schema conversion.
# Excludes runtime objects (INDEX PARTITION, TABLE PARTITION, LOB) and
# things Ch.5 owns (PACKAGE BODY semantics — we still record PACKAGE
# but the procedural conversion is Ch.5's deliverable).
SOURCE_OBJECT_TYPES = (
    "TABLE", "VIEW", "MATERIALIZED VIEW", "TYPE", "SEQUENCE",
    "TRIGGER", "PACKAGE", "PROCEDURE", "FUNCTION",
)

log = logging.getLogger("car")


@dataclasses.dataclass(frozen=True)
class SourceObject:
    owner: str
    name: str
    object_type: str


def load_source_inventory(dsn: str, user: str, password: str,
                          schema: str) -> list[SourceObject]:
    log.info("Querying source inventory for schema %s", schema)
    with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
        cur = conn.cursor()
        ph = ",".join(f":t{i}" for i in range(len(SOURCE_OBJECT_TYPES)))
        cur.execute(
            f"SELECT owner, object_name, object_type "
            f"  FROM dba_objects "
            f" WHERE owner = :owner "
            f"   AND object_type IN ({ph}) "
            f" ORDER BY object_type, object_name",
            {"owner": schema, **{f"t{i}": v for i, v in enumerate(SOURCE_OBJECT_TYPES)}},
        )
        return [SourceObject(o, n, t) for o, n, t in cur.fetchall()]


# Regex anchored to start-of-statement CREATE forms; tolerates "OR REPLACE",
# "IF NOT EXISTS", schema qualifiers, and quoted/bracketed identifiers.
_CREATE_RE = re.compile(
    r"(?im)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?P<kind>TABLE|VIEW|MATERIALIZED\s+VIEW|TYPE|SEQUENCE|TRIGGER|"
    r"PACKAGE(?:\s+BODY)?|PROCEDURE|FUNCTION)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:[\w\"\[\]]+\s*\.\s*)?"            # optional schema qualifier
    r"[\"\[]?(?P<name>\w+)[\"\]]?",
)


def scan_output_dir(out_dir: Path, target: str) -> dict[str, dict[str, Any]]:
    """Return a map from object-name (uppercase) to detection record."""
    found: dict[str, dict[str, Any]] = {}
    if not out_dir or not out_dir.exists():
        return found

    if target == "SSMA":
        warning_re = re.compile(r"/\*\s*SSMA\s+(error|warning|info)", re.IGNORECASE)
    else:  # ORA2PG
        warning_re = re.compile(r"(?m)^\s*--\s*(WARNING|TODO|FIX)", re.IGNORECASE)

    for sql_file in sorted(out_dir.rglob("*.sql")):
        try:
            text = sql_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("could not read %s: %s", sql_file, exc)
            continue

        file_warnings = len(warning_re.findall(text))

        for m in _CREATE_RE.finditer(text):
            name = m.group("name").upper()
            kind = m.group("kind").upper().replace("  ", " ")
            existing = found.get(name)
            if existing is None or existing["warnings"] < file_warnings:
                found[name] = {
                    "object_type": kind,
                    "file":        str(sql_file.relative_to(out_dir.parent.parent)),
                    "warnings":    file_warnings,
                }
    return found


def build_car(schema: str, source: list[SourceObject],
              ssma: dict[str, dict[str, Any]],
              ora2pg: dict[str, dict[str, Any]]) -> dict[str, Any]:
    objects: list[dict[str, Any]] = []
    for o in source:
        s = ssma.get(o.name.upper())
        p = ora2pg.get(o.name.upper())
        objects.append({
            "owner":       o.owner,
            "object_name": o.name,
            "object_type": o.object_type,
            "ssma": {
                "converted": bool(s),
                "warnings":  s["warnings"] if s else 0,
                "file":      s["file"]     if s else None,
            },
            "ora2pg": {
                "converted": bool(p),
                "warnings":  p["warnings"] if p else 0,
                "file":      p["file"]     if p else None,
            },
        })

    total = len(objects) or 1
    ssma_conv   = sum(1 for r in objects if r["ssma"]["converted"])
    ora2pg_conv = sum(1 for r in objects if r["ora2pg"]["converted"])
    both        = sum(1 for r in objects if r["ssma"]["converted"] and r["ora2pg"]["converted"])
    neither     = sum(1 for r in objects if not r["ssma"]["converted"] and not r["ora2pg"]["converted"])

    open_questions: list[str] = []
    for r in objects:
        if not r["ssma"]["converted"] and not r["ora2pg"]["converted"]:
            open_questions.append(
                f"Neither converter produced output for {r['object_type']} "
                f"{r['owner']}.{r['object_name']} - manual review required.")
        elif r["ssma"]["warnings"] >= 3 or r["ora2pg"]["warnings"] >= 3:
            open_questions.append(
                f"High warning count on {r['object_type']} "
                f"{r['owner']}.{r['object_name']} - inspect converter output.")

    return {
        "_artifact":         "car",
        "schema":            schema,
        "generated_at_utc":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "car_version":       CAR_VERSION,
        "total_objects":     len(objects),
        "summary": {
            "ssma_converted":   ssma_conv,
            "ssma_pct":         round(100 * ssma_conv   / total, 1),
            "ora2pg_converted": ora2pg_conv,
            "ora2pg_pct":       round(100 * ora2pg_conv / total, 1),
            "both_converted":   both,
            "neither_converted": neither,
        },
        "objects":         objects,
        "open_questions":  open_questions,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--schema",          required=True)
    p.add_argument("--oracle-dsn",      required=True)
    p.add_argument("--oracle-user",     required=True)
    p.add_argument("--oracle-password", required=True)
    p.add_argument("--ssma-dir",        type=Path)
    p.add_argument("--ora2pg-dir",      type=Path)
    p.add_argument("--out",             required=True, type=Path)
    p.add_argument("--verbose", "-v",   action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    src = load_source_inventory(args.oracle_dsn, args.oracle_user,
                                args.oracle_password, args.schema)
    log.info("Source inventory: %d objects across %d types",
             len(src), len({o.object_type for o in src}))

    ssma   = scan_output_dir(args.ssma_dir,   "SSMA")   if args.ssma_dir   else {}
    ora2pg = scan_output_dir(args.ora2pg_dir, "ORA2PG") if args.ora2pg_dir else {}
    log.info("SSMA detected %d CREATE statements; Ora2Pg detected %d",
             len(ssma), len(ora2pg))

    car = build_car(args.schema, src, ssma, ora2pg)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(car, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    log.info("CAR written: %s", args.out)
    log.info("SSMA   converted: %d / %d (%.1f%%)",
             car["summary"]["ssma_converted"],
             car["total_objects"], car["summary"]["ssma_pct"])
    log.info("Ora2Pg converted: %d / %d (%.1f%%)",
             car["summary"]["ora2pg_converted"],
             car["total_objects"], car["summary"]["ora2pg_pct"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
