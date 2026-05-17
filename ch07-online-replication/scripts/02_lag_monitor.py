#!/usr/bin/env python3
"""lag_monitor.py - poll GoldenGate Microservices REST API and emit a
Lag Status Report (LSR) JSON document conforming to lag_status_schema.json.

Consumed by:
  - The cutover-drain orchestrator (03_cutover_drain.sh) for the gate-condition
    "max_lag_seconds < threshold".
  - Ch.12 cutover playbook as the T-minus-zero gate.

Usage:
    lag_monitor.py --server https://gg-host:7811 \\
                   --user admin --password '...' \\
                   --out lsr.json \\
                   [--insecure]   # disable cert verify (dev only)
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LSR_VERSION = "1.0.0"
log = logging.getLogger("lag_monitor")


def _http_get(url: str, user: str, password: str, ctx: ssl.SSLContext) -> dict[str, Any]:
    req = urllib.request.Request(url)
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GG REST {url} returned HTTP {e.code}: {e.read().decode()[:200]}") from e


def _lag_seconds(item: dict[str, Any]) -> float:
    """Extract lag in seconds from the GG REST response item.

    Newer MSA versions expose 'lag.lagInSeconds'; older ones 'lagSeconds'
    or 'lag.value' (a duration string). We try them in order and fall
    back to +inf with a warning -- because the cutover-drain script uses
    max_lag_seconds as a gate, and unknown lag must NOT silently pass as 0.
    """
    lag = item.get("lag")
    if isinstance(lag, dict):
        if "lagInSeconds" in lag: return float(lag["lagInSeconds"])
        if "seconds"      in lag: return float(lag["seconds"])
    if "lagSeconds" in item:      return float(item["lagSeconds"])
    log.warning("could not extract lag from process %s -- treating as +inf "
                "(prevents silent cutover-drain pass on a stuck process)",
                item.get("name", "<unknown>"))
    return float("inf")


def collect(server: str, user: str, password: str, insecure: bool) -> dict[str, Any]:
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    base = server.rstrip("/") + "/services/v2"
    extracts  = _http_get(f"{base}/extracts",  user, password, ctx).get("response", {}).get("items", [])
    replicats = _http_get(f"{base}/replicats", user, password, ctx).get("response", {}).get("items", [])

    def _shape(p: dict[str, Any], kind: str) -> dict[str, Any]:
        return {
            "kind":         kind,
            "name":         p.get("name"),
            "status":       p.get("status"),
            "lag_seconds":  _lag_seconds(p),
            "checkpoint":   p.get("position") or p.get("checkpointPosition"),
            "messages":     p.get("messages", [])[:5],   # cap noise
        }

    ext_rows = [_shape(e, "extract")  for e in extracts]
    rep_rows = [_shape(r, "replicat") for r in replicats]

    all_lags = [r["lag_seconds"] for r in ext_rows + rep_rows]
    max_lag = max(all_lags) if all_lags else 0.0

    return {
        "_artifact":       "lsr",
        "captured_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lsr_version":     LSR_VERSION,
        "gg_server":       server,
        "extract_count":   len(ext_rows),
        "replicat_count":  len(rep_rows),
        "max_lag_seconds": max_lag,
        "extracts":        ext_rows,
        "replicats":       rep_rows,
        "open_questions":  [],
        "all_running": all(p["status"] == "RUNNING"
                           for p in ext_rows + rep_rows),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--server",   required=True, help="GG MSA URL, e.g. https://host:7811")
    p.add_argument("--user",     required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--out",      required=True, type=Path)
    p.add_argument("--insecure", action="store_true",
                   help="Skip TLS verification (dev only).")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    lsr = collect(args.server, args.user, args.password, args.insecure)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(lsr, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    log.info("LSR written: %s", args.out)
    log.info("max_lag_seconds=%.1f  all_running=%s  extracts=%d replicats=%d",
             lsr["max_lag_seconds"], lsr["all_running"],
             lsr["extract_count"], lsr["replicat_count"])
    return 0 if lsr["all_running"] else 2


if __name__ == "__main__":
    sys.exit(main())
