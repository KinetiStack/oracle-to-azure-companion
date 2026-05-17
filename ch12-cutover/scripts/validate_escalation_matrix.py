#!/usr/bin/env python3
"""validate_escalation_matrix.py -- gate the cutover escalation matrix.

Runs runbook/escalation_matrix.json against runbook/escalation_matrix_schema.json.
Emits every defect (not just the first) and a final GO / NO_GO line. CI exits
non-zero on NO_GO so the matrix cannot ship half-filled.

Usage:
    python3 validate_escalation_matrix.py \\
        --matrix ../runbook/escalation_matrix.json \\
        --schema ../runbook/escalation_matrix_schema.json \\
        [--max-staleness-days 30]

Exit codes:
    0   GO      -- schema passed, last_updated within staleness window
    2   NO_GO   -- any schema violation, or last_updated too old, or file missing
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from jsonschema import Draft202012Validator
except ImportError:
    print("ERROR: jsonschema is required. Install with: pip install jsonschema", file=sys.stderr)
    sys.exit(2)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as exc:
        print(f"ERROR: {path} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(2)


def _format_path(path_parts: tuple[Any, ...]) -> str:
    if not path_parts:
        return "<root>"
    out = []
    for p in path_parts:
        if isinstance(p, int):
            out.append(f"[{p}]")
        else:
            out.append(f".{p}" if out else str(p))
    return "".join(out)


def main() -> int:
    here = Path(__file__).resolve().parent
    default_matrix = here.parent / "runbook" / "escalation_matrix.json"
    default_schema = here.parent / "runbook" / "escalation_matrix_schema.json"

    ap = argparse.ArgumentParser()
    ap.add_argument("--matrix", type=Path, default=default_matrix)
    ap.add_argument("--schema", type=Path, default=default_schema)
    ap.add_argument(
        "--max-staleness-days",
        type=int,
        default=30,
        help="Reject if last_updated is older than this many days (default 30).",
    )
    ap.add_argument(
        "--today",
        type=str,
        default=None,
        help="Override 'today' for deterministic testing (YYYY-MM-DD).",
    )
    args = ap.parse_args()

    schema = _load_json(args.schema)
    matrix = _load_json(args.matrix)
    validator = Draft202012Validator(schema)

    errors = sorted(validator.iter_errors(matrix), key=lambda e: list(e.absolute_path))
    failures: list[str] = []
    for err in errors:
        failures.append(f"  - {_format_path(tuple(err.absolute_path))}: {err.message}")

    today = (
        date.fromisoformat(args.today)
        if args.today
        else date.today()
    )

    last_updated_raw = matrix.get("last_updated", "")
    try:
        last_updated = datetime.strptime(last_updated_raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        last_updated = None

    if last_updated is not None:
        age = today - last_updated
        if age > timedelta(days=args.max_staleness_days):
            failures.append(
                f"  - last_updated: {last_updated_raw} is {age.days} days old "
                f"(ceiling {args.max_staleness_days}); refresh before cutover sign-off."
            )
        if last_updated > today:
            failures.append(
                f"  - last_updated: {last_updated_raw} is in the future relative to {today.isoformat()}; "
                "likely an error."
            )

    print(f"Matrix:  {args.matrix}")
    print(f"Schema:  {args.schema}")
    print(f"Today:   {today.isoformat()}  (staleness ceiling: {args.max_staleness_days}d)")
    print(f"Defects: {len(failures)}")
    for f in failures:
        print(f)

    if failures:
        print("verdict: NO_GO -- escalation matrix is not cutover-ready")
        return 2
    print("verdict: GO -- escalation matrix passes schema + staleness check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
