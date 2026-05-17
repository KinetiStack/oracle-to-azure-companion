#!/usr/bin/env python3
"""license_inventory.py -- build the Oracle license return inventory.

Consumes a per-host YAML (license_inventory.yaml) and emits a signed
license_return_inventory.json that:

  1. Computes the processor-license count per host using the correct
     core-factor rule (on-prem hyperthreaded x86 = ceil(vcpus * 0.5);
     Authorized Cloud Environment = ceil(vcpus / 2)).
  2. Aggregates totals by edition (EE / SE2) and by option (partitioning,
     advanced_security, RAC, GoldenGate).
  3. Computes the **support-cancellation deadline** -- (renewal_date -
     notice_days) -- and flags whether today is inside the notice window.

The output drives the §13.3 §13.8 DRR gate.

Usage:
    python3 license_inventory.py \\
        --inventory license_inventory.yaml \\
        --today 2026-08-01 \\
        --out license_return_inventory.json

Exit codes:
    0   inventory built; today inside or before notice window (action possible)
    2   today is past the cancellation deadline (action no longer possible
        for this renewal cycle)
    3   inventory file invalid (schema or math error)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required. Install: pip install pyyaml", file=sys.stderr)
    sys.exit(3)

CORE_FACTOR_ON_PREM_X86 = 0.5   # Oracle core factor for Intel/AMD x86


def _proc_licenses(vcpus: int, environment: str) -> int:
    if vcpus <= 0:
        raise ValueError(f"vcpus must be > 0, got {vcpus}")
    if environment == "on_prem":
        return math.ceil(vcpus * CORE_FACTOR_ON_PREM_X86)
    if environment == "authorized_cloud":
        return math.ceil(vcpus / 2)
    raise ValueError(f"environment must be 'on_prem' or 'authorized_cloud', got {environment!r}")


def _validate_host(h: dict[str, Any]) -> None:
    required = {"hostname", "vcpus", "environment", "edition"}
    missing = required - h.keys()
    if missing:
        raise ValueError(f"host missing required fields: {sorted(missing)} -- {h}")
    if h["edition"] not in {"EE", "SE2"}:
        raise ValueError(f"edition must be 'EE' or 'SE2', got {h['edition']!r}")
    if not isinstance(h.get("options", []), list):
        raise ValueError(f"options must be a list on host {h['hostname']}")
    if not isinstance(h.get("goldengate", False), bool):
        raise ValueError(f"goldengate must be a bool on host {h['hostname']}")


def build(inventory_path: Path, today: date) -> tuple[dict[str, Any], int]:
    raw = yaml.safe_load(inventory_path.read_text())
    if not isinstance(raw, dict) or "hosts" not in raw:
        raise ValueError("inventory file must be a YAML mapping with a 'hosts' list")

    contract     = raw.get("contract", "")
    renewal_raw  = raw.get("renewal_date", "")
    notice_days  = int(raw.get("notice_days", 30))
    if not contract:
        raise ValueError("'contract' field is required")
    try:
        renewal = datetime.strptime(renewal_raw, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"renewal_date must be YYYY-MM-DD, got {renewal_raw!r}") from exc

    notice_deadline = renewal - timedelta(days=notice_days)
    # The notice window is the actionable interval between "early enough
    # that paperwork can move" and "before the contractual cancellation
    # deadline". We start it at max(90, notice_days) so contracts with
    # long notice periods (some enterprise master agreements run 180d)
    # don't show as "window not open" while their actual deadline has
    # already arrived.
    window_lead_days   = max(90, notice_days)
    notice_window_open = today >= renewal - timedelta(days=window_lead_days)
    past_deadline      = today > notice_deadline

    hosts_out: list[dict[str, Any]] = []
    by_edition: dict[str, int] = {}
    by_option: dict[str, int] = {}
    gg_licenses = 0
    total_proc_licenses = 0

    for h in raw["hosts"]:
        _validate_host(h)
        proc = _proc_licenses(int(h["vcpus"]), h["environment"])
        total_proc_licenses += proc
        by_edition[h["edition"]] = by_edition.get(h["edition"], 0) + proc
        for opt in h.get("options", []):
            by_option[opt] = by_option.get(opt, 0) + proc
        if h.get("goldengate", False):
            gg_licenses += proc
        hosts_out.append({
            "hostname":    h["hostname"],
            "vcpus":       h["vcpus"],
            "environment": h["environment"],
            "edition":     h["edition"],
            "options":     h.get("options", []),
            "goldengate":  h.get("goldengate", False),
            "processor_licenses": proc,
        })

    doc: dict[str, Any] = {
        "_artifact":      "license_return_inventory",
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "today":          today.isoformat(),
        "contract":       contract,
        "renewal_date":   renewal.isoformat(),
        "notice_days":    notice_days,
        "cancellation_deadline":  notice_deadline.isoformat(),
        "notice_window_open":     notice_window_open,
        "past_cancellation_deadline": past_deadline,
        "totals": {
            "processor_licenses":   total_proc_licenses,
            "by_edition":           by_edition,
            "by_option":            by_option,
            "goldengate_licenses":  gg_licenses,
        },
        "hosts": hosts_out,
    }
    # Stable signature: hash everything except the signature itself.
    body = json.dumps(doc, sort_keys=True).encode()
    doc["_sha256"] = hashlib.sha256(body).hexdigest()

    exit_code = 2 if past_deadline else 0
    return doc, exit_code


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inventory", type=Path, required=True)
    ap.add_argument("--today", type=str, default=None,
                    help="Override today's date (YYYY-MM-DD). Defaults to system date.")
    ap.add_argument("--out", type=Path, default=Path("license_return_inventory.json"))
    args = ap.parse_args()

    today = (
        datetime.strptime(args.today, "%Y-%m-%d").date()
        if args.today else date.today()
    )

    try:
        doc, exit_code = build(args.inventory, today)
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    args.out.write_text(json.dumps(doc, indent=2))

    totals = doc["totals"]
    print(f"[Inventory] {len(doc['hosts'])} host entries; "
          f"total processor licenses: {totals['processor_licenses']}")
    print(f"[Inventory] GoldenGate licenses: {totals['goldengate_licenses']} (return T+30d)")
    print(f"[Inventory] Deadline check: {doc['contract']} support renewal "
          f"{doc['renewal_date']} -- cancellation deadline {doc['cancellation_deadline']}")
    if exit_code == 2:
        print(f"[Inventory] PAST DEADLINE: today {today.isoformat()} is past "
              f"{doc['cancellation_deadline']}; support will auto-renew for next cycle.")
    elif doc["notice_window_open"]:
        print(f"[Inventory] NOTICE WINDOW OPEN: submit cancellation by "
              f"{doc['cancellation_deadline']}")
    else:
        print(f"[Inventory] Notice window opens 90d before renewal.")
    print(f"[Inventory] Wrote {args.out}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
