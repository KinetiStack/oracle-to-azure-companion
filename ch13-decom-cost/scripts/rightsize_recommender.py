#!/usr/bin/env python3
"""rightsize_recommender.py -- propose SKU changes from 30-day telemetry.

Pulls metrics from `az monitor metrics list` and emits rightsize_proposal.json
with a current SKU vs. recommended SKU comparison and an estimated annualized
$ delta. **The script does not apply any changes.** Apply or reject in the
quarterly FinOps review.

Supported targets:
  - Azure SQL Managed Instance (--engine mi)
  - Azure Database for PostgreSQL Flexible Server (--engine pg)

Usage:
    az login          # one-time; the script shells out to `az`
    python3 rightsize_recommender.py \\
        --engine mi \\
        --resource-id /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Sql/managedInstances/<mi> \\
        --days 30 \\
        --out rightsize_proposal.json

For deterministic CI testing, use --metrics-fixture <path-to-json>
to bypass `az` and feed precomputed p95 values. The fixture schema is:
    {"avg_cpu_percent": 32.4, "avg_memory_usage_percent": 64.1,
     "io_requests": 2400, "log_write_percent": 18.2}

Exit codes:
    0   proposal generated (may be 'no change' or 'downsize' or 'upsize')
    2   telemetry insufficient (< days requested available)
    3   az CLI / environment error
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------------------
# SKU ladders. These are illustrative ratios -- the `annual_list_usd`
# values are deliberately simplified ($9K per 4 MI vCores, $3K per 2 PG
# vCores) for unit-test reproducibility and ordering. Real Azure list
# pricing is materially HIGHER (commonly 3-5x the values below): East US
# 2026 list for an 8-vCore MI GP is approximately $50K-$65K/year, and an
# 8-vCore PG Flex D8ds_v5 is materially above $12K. Treat the
# `annual_delta_usd` field in the output as a RATIO indicator -- if the
# recommended SKU is at half the ladder cost, real savings will be
# proportionally half of real cost. Pull authoritative pricing at apply-
# time via the Azure Retail Prices API:
#     curl 'https://prices.azure.com/api/retail/prices?$filter=...'
# or `az sql managed-instance list-skus` / `az postgres flexible-server list-skus`.
# ----------------------------------------------------------------------------

MI_GP_LADDER = [
    {"sku": "GP_Gen5_4",  "vcores":  4,  "annual_list_usd":  9000},
    {"sku": "GP_Gen5_8",  "vcores":  8,  "annual_list_usd": 18000},
    {"sku": "GP_Gen5_16", "vcores": 16,  "annual_list_usd": 36000},
    {"sku": "GP_Gen5_24", "vcores": 24,  "annual_list_usd": 54000},
    {"sku": "GP_Gen5_32", "vcores": 32,  "annual_list_usd": 72000},
    {"sku": "GP_Gen5_40", "vcores": 40,  "annual_list_usd": 90000},
    {"sku": "GP_Gen5_64", "vcores": 64,  "annual_list_usd": 144000},
    {"sku": "GP_Gen5_80", "vcores": 80,  "annual_list_usd": 180000},
]

PG_FLEX_LADDER = [
    {"sku": "Standard_D2ds_v5",  "vcores":  2,  "annual_list_usd":  3000},
    {"sku": "Standard_D4ds_v5",  "vcores":  4,  "annual_list_usd":  6000},
    {"sku": "Standard_D8ds_v5",  "vcores":  8,  "annual_list_usd": 12000},
    {"sku": "Standard_D16ds_v5", "vcores": 16,  "annual_list_usd": 24000},
    {"sku": "Standard_D32ds_v5", "vcores": 32,  "annual_list_usd": 48000},
    {"sku": "Standard_D64ds_v5", "vcores": 64,  "annual_list_usd": 96000},
]

# Metrics required by the proposer logic. Kept minimal: requiring metrics
# that don't affect the decision creates fixture-validation friction
# during testing and surprises users. Additional p95 metrics worth
# reporting (e.g. read_iops, write_iops for PG) are noted in the prose
# but not required here -- collect them out-of-band via az monitor if
# you need a richer dashboard.
METRICS_BY_ENGINE = {
    "mi": ["avg_cpu_percent", "avg_memory_usage_percent", "log_write_percent"],
    "pg": ["cpu_percent", "memory_percent"],
}


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _fetch_metric(resource_id: str, metric_name: str, days: int) -> list[float]:
    """Pull per-hour metric points via `az monitor metrics list`."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    cmd = [
        "az", "monitor", "metrics", "list",
        "--resource", resource_id,
        "--metric", metric_name,
        "--interval", "PT1H",
        "--aggregation", "Average",
        "--start-time", start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--end-time",   end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--output", "json",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
    except FileNotFoundError as exc:
        raise RuntimeError("az CLI not on PATH. Run `az login` and retry.") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"az monitor metrics list failed: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("az monitor metrics list timed out (60s)") from exc

    payload = json.loads(out.stdout)
    values: list[float] = []
    for series in payload.get("value", []):
        for ts in series.get("timeseries", []):
            for point in ts.get("data", []):
                v = point.get("average")
                if v is not None:
                    values.append(float(v))
    return values


def _propose_mi(metrics: dict[str, float], current_sku: str) -> dict[str, Any]:
    cpu_p95   = metrics["avg_cpu_percent"]
    mem_p95   = metrics["avg_memory_usage_percent"]
    log_p95   = metrics.get("log_write_percent", 0.0)

    try:
        idx = next(i for i, e in enumerate(MI_GP_LADDER) if e["sku"] == current_sku)
    except StopIteration:
        return {"recommendation": "manual_review",
                "reason": f"current SKU '{current_sku}' not in known ladder; review by hand"}

    cur = MI_GP_LADDER[idx]
    # Upsize if memory-bound (>80%) OR log-bound (>80%) -- one tier up
    if mem_p95 > 80 or log_p95 > 80:
        if idx + 1 >= len(MI_GP_LADDER):
            return {"recommendation": "manual_review",
                    "reason": "at top of ladder; consider Business Critical tier"}
        new = MI_GP_LADDER[idx + 1]
        delta = new["annual_list_usd"] - cur["annual_list_usd"]
        return {"recommendation": "upsize", "from": cur["sku"], "to": new["sku"],
                "annual_delta_usd": delta,
                "reason": f"mem_p95={mem_p95:.1f}% / log_p95={log_p95:.1f}% indicates pressure"}

    # Downsize if CPU consistently low and memory not pressured -- one tier down
    if cpu_p95 < 50 and mem_p95 < 65 and idx > 0:
        new = MI_GP_LADDER[idx - 1]
        delta = new["annual_list_usd"] - cur["annual_list_usd"]
        return {"recommendation": "downsize", "from": cur["sku"], "to": new["sku"],
                "annual_delta_usd": delta,
                "reason": f"cpu_p95={cpu_p95:.1f}% / mem_p95={mem_p95:.1f}% under target"}

    return {"recommendation": "no_change", "from": cur["sku"], "to": cur["sku"],
            "annual_delta_usd": 0,
            "reason": f"cpu_p95={cpu_p95:.1f}% / mem_p95={mem_p95:.1f}% in target band"}


def _propose_pg(metrics: dict[str, float], current_sku: str) -> dict[str, Any]:
    cpu_p95 = metrics["cpu_percent"]
    mem_p95 = metrics["memory_percent"]

    try:
        idx = next(i for i, e in enumerate(PG_FLEX_LADDER) if e["sku"] == current_sku)
    except StopIteration:
        return {"recommendation": "manual_review",
                "reason": f"current SKU '{current_sku}' not in known ladder; review by hand"}

    cur = PG_FLEX_LADDER[idx]
    if mem_p95 > 80:
        if idx + 1 >= len(PG_FLEX_LADDER):
            return {"recommendation": "manual_review",
                    "reason": "at top of ladder; consider Memory Optimized tier"}
        new = PG_FLEX_LADDER[idx + 1]
        return {"recommendation": "upsize", "from": cur["sku"], "to": new["sku"],
                "annual_delta_usd": new["annual_list_usd"] - cur["annual_list_usd"],
                "reason": f"mem_p95={mem_p95:.1f}% over threshold"}
    if cpu_p95 < 50 and mem_p95 < 65 and idx > 0:
        new = PG_FLEX_LADDER[idx - 1]
        return {"recommendation": "downsize", "from": cur["sku"], "to": new["sku"],
                "annual_delta_usd": new["annual_list_usd"] - cur["annual_list_usd"],
                "reason": f"cpu_p95={cpu_p95:.1f}% / mem_p95={mem_p95:.1f}% under target"}
    return {"recommendation": "no_change", "from": cur["sku"], "to": cur["sku"],
            "annual_delta_usd": 0,
            "reason": f"cpu_p95={cpu_p95:.1f}% / mem_p95={mem_p95:.1f}% in target band"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["mi", "pg"], required=True)
    ap.add_argument("--resource-id", required=False, default="",
                    help="Azure resource ID for the target instance (required unless --metrics-fixture)")
    ap.add_argument("--current-sku", required=True,
                    help="Current SKU label (e.g. GP_Gen5_8, Standard_D8ds_v5)")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--metrics-fixture", type=Path, default=None,
                    help="JSON fixture for testing (bypasses az)")
    ap.add_argument("--out", type=Path, default=Path("rightsize_proposal.json"))
    args = ap.parse_args()

    metric_names = METRICS_BY_ENGINE[args.engine]
    metrics: dict[str, float] = {}

    if args.metrics_fixture:
        raw = json.loads(args.metrics_fixture.read_text())
        for name in metric_names:
            if name not in raw:
                print(f"ERROR: fixture missing metric '{name}'", file=sys.stderr)
                return 3
            metrics[name] = float(raw[name])
    else:
        if not args.resource_id:
            print("ERROR: --resource-id required when not using --metrics-fixture", file=sys.stderr)
            return 3
        for name in metric_names:
            try:
                values = _fetch_metric(args.resource_id, name, args.days)
            except RuntimeError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 3
            if not values:
                print(f"ERROR: no data for metric '{name}' over {args.days}d; "
                      "telemetry insufficient", file=sys.stderr)
                return 2
            metrics[name] = _percentile(values, 0.95)

    if args.engine == "mi":
        proposal = _propose_mi(metrics, args.current_sku)
    else:
        proposal = _propose_pg(metrics, args.current_sku)

    doc = {
        "_artifact": "rightsize_proposal",
        "schema_version": "1.0.0",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine":      args.engine,
        "resource_id": args.resource_id or None,
        "lookback_days": args.days,
        "metrics_p95": {k: round(v, 2) for k, v in metrics.items()},
        "proposal":    proposal,
        "note": "Script does NOT apply SKU changes. Review and apply via az CLI / IaC.",
    }
    args.out.write_text(json.dumps(doc, indent=2))

    p = proposal
    print(f"[rightsize] engine={args.engine}  current={args.current_sku}")
    for k, v in metrics.items():
        print(f"[rightsize]   p95 {k} = {v:.2f}")
    print(f"[rightsize] recommendation: {p['recommendation']}")
    if p.get("from") and p.get("to") and p["from"] != p["to"]:
        print(f"[rightsize]   {p['from']} -> {p['to']}  (annual delta ${p['annual_delta_usd']:+,})")
    print(f"[rightsize]   reason: {p['reason']}")
    print(f"[rightsize] wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
