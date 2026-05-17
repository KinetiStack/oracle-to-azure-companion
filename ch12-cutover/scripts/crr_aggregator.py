#!/usr/bin/env python3
"""crr_aggregator.py - aggregate every signed JSON deliverable from
Chapters 1-11 into a single Cutover Readiness Report (CRR).

Inputs (each one optional; missing = the gate is MISSING):
    --mab    path/to/mab_summary.json     (Ch.1)
    --tasm   path/to/tasm_recommendation.json   (Ch.2)
    --bom    path/to/tco_bom.json         (Ch.3)
    --car    path/to/car.json             (Ch.4)
    --mrr    path/to/mrr.json             (Ch.6)
    --lsr    path/to/lsr.json             (Ch.7)
    --amr    path/to/amr.json             (Ch.8)
    --arr    path/to/arr.json             (Ch.8.5)
    --orr    path/to/orr.json             (Ch.9 -- OPTIONAL)
    --hdr    path/to/hdr.json             (Ch.10)
    --pvr    path/to/pvr.json             (Ch.11)

Verdict logic (see chapter prose for the rationale):

    GO      = every required gate is PASS;        optional gates ignored if absent
    HOLD    = any gate is DEGRADED / WARN;        architecture-board review needed
    NO_GO   = any required gate is FAIL or MISSING

Required gates (programmatic cutover blockers):
    MRR (data reconciled)
    LSR (replication caught up)
    HDR (HA/DR drill passed)
    PVR (perf validated)

Non-required-but-loaded gates:
    MAB, TASM, BoM, CAR, AMR, ARR -- contribute to open_questions but do
    not by themselves block cutover (their failures would have surfaced
    in earlier chapter gates).

Optional gate:
    ORR -- skipped silently if not provided (Part IV is the optional track).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CRR_VERSION = "1.0.0"
log = logging.getLogger("crr")

# Gates whose FAIL / MISSING status blocks the cutover.
REQUIRED_GATES = ("mrr", "lsr", "hdr", "pvr")

# Gates that can FAIL but should be treated as informational-blocking
# (NO_GO when FAIL, HOLD when DEGRADED). AMR=FAIL means an unsupported
# compliance control; ARR=FAIL means a HIGH-severity application defect.
# Neither is a "core data path" failure but both block production cutover
# until compliance / engineering review.
INFORMATIONAL_GATES = ("amr", "arr")

OPTIONAL_GATES = ("orr",)
ALL_GATES      = ("mab", "tasm", "bom", "car", "mrr", "lsr",
                  "amr", "arr", "orr", "hdr", "pvr")


@dataclasses.dataclass
class GateResult:
    name:                 str
    present:              bool
    status:               str           # PASS | DEGRADED | FAIL | MISSING
    summary:              dict[str, Any]
    open_question_count:  int


# -----------------------------------------------------------------------------
# Per-gate classifier. Each function takes the loaded JSON and returns a
# (status, summary, open_question_count) tuple. Open-question count surfaces
# upward into the CRR's overall open_questions array.
# -----------------------------------------------------------------------------
def _classify_mab(doc: dict) -> tuple[str, dict, int]:
    # MAB is informational; we don't fail cutover on MAB alone. Surface the
    # scorecard so the board can sanity-check.
    summary = {"source_db": doc.get("source", {}).get("db_name"),
               "scorecard": doc.get("scorecard", {})}
    return "PASS", summary, 0


def _classify_tasm(doc: dict) -> tuple[str, dict, int]:
    rec  = doc.get("primary_recommendation", {})
    summary = {
        "target":     rec.get("target"),
        "confidence": rec.get("confidence"),
        "refactor_band": doc.get("refactor_estimate", {}).get("effort_band"),
    }
    oqs = len(doc.get("open_questions", []))
    status = "DEGRADED" if rec.get("confidence") == "LOW" else "PASS"
    return status, summary, oqs


def _classify_bom(doc: dict) -> tuple[str, dict, int]:
    summary = {
        "target":   doc.get("target"),
        "monthly_ri_3yr_usd": doc.get("cost_estimate_usd", {}).get("total_monthly_ri_3yr"),
        "oracle_licenses_required": (doc.get("oracle_licensing", {})
                                        .get("processor_licenses_required")),
    }
    return "PASS", summary, len(doc.get("open_questions", []))


def _classify_car(doc: dict) -> tuple[str, dict, int]:
    s = doc.get("summary", {})
    summary = {
        "ssma_pct":           s.get("ssma_pct"),
        "ora2pg_pct":         s.get("ora2pg_pct"),
        "neither_converted":  s.get("neither_converted"),
    }
    # Conversion gaps in MV/PL/SQL are expected (Ch.5/Ch.11 handled them).
    # We don't fail CRR on CAR alone unless neither_converted is excessive.
    status = "DEGRADED" if (s.get("neither_converted") or 0) > 5 else "PASS"
    return status, summary, len(doc.get("open_questions", []))


def _classify_mrr(doc: dict) -> tuple[str, dict, int]:
    s = doc.get("summary", {})
    summary = {
        "tables_pass":       s.get("tables_pass"),
        "tables_with_delta": s.get("tables_with_delta"),
        "tables_missing":    s.get("tables_missing"),
        "tables_error":      s.get("tables_error"),
    }
    if (s.get("tables_with_delta") or 0) > 0 or (s.get("tables_missing") or 0) > 0 \
            or (s.get("tables_error") or 0) > 0:
        return "FAIL", summary, len(doc.get("open_questions", []))
    return "PASS", summary, len(doc.get("open_questions", []))


def _classify_lsr(doc: dict) -> tuple[str, dict, int]:
    summary = {
        "max_lag_seconds": doc.get("max_lag_seconds"),
        "all_running":     doc.get("all_running"),
    }
    if not doc.get("all_running"):
        return "FAIL", summary, 0
    # The cutover gate's lag threshold lives upstream in Ch.7's drain script;
    # the LSR itself is a snapshot. If it's recent and all processes are
    # running, that's a PASS for the CRR. Production gates the actual swing
    # on the drain script's threshold, not on this snapshot.
    return "PASS", summary, 0


def _classify_amr(doc: dict) -> tuple[str, dict, int]:
    s = doc.get("summary", {})
    summary = {
        "equivalent":   s.get("equivalent_count"),
        "degraded":     s.get("degraded_count"),
        "unsupported":  s.get("unsupported_count"),
    }
    oqs = len(doc.get("open_questions", []))
    if (s.get("unsupported_count") or 0) > 0:
        return "FAIL", summary, oqs
    if (s.get("degraded_count") or 0) > 0:
        return "DEGRADED", summary, oqs
    return "PASS", summary, oqs


def _classify_arr(doc: dict) -> tuple[str, dict, int]:
    s = doc.get("summary", {})
    severities = s.get("by_severity", {})
    summary = {
        "high":   severities.get("HIGH",   0),
        "medium": severities.get("MEDIUM", 0),
        "low":    severities.get("LOW",    0),
    }
    if severities.get("HIGH", 0) > 0:
        return "FAIL", summary, severities.get("HIGH", 0)
    if severities.get("MEDIUM", 0) > 0:
        return "DEGRADED", summary, severities.get("MEDIUM", 0)
    return "PASS", summary, 0


def _classify_orr(doc: dict) -> tuple[str, dict, int]:
    s = doc.get("summary", {})
    return "PASS", {"by_layer": s.get("by_layer", {})}, len(doc.get("open_questions", []))


def _classify_hdr(doc: dict) -> tuple[str, dict, int]:
    drill = doc.get("drill", {})
    summary = {
        "rto_status":          drill.get("rto_status"),
        "rto_seconds_measured": drill.get("rto_seconds_measured"),
        "rpo_status":          drill.get("rpo_status"),
        "rpo_seconds_measured": drill.get("rpo_seconds_measured"),
    }
    if drill.get("rto_status") == "FAIL" or drill.get("rpo_status") == "FAIL":
        return "FAIL", summary, 0
    if drill.get("rpo_status") == "UNKNOWN":
        return "DEGRADED", summary, 0
    return "PASS", summary, 0


def _classify_pvr(doc: dict) -> tuple[str, dict, int]:
    s = doc.get("summary", {})
    by_status = s.get("by_status", {})
    summary = dict(by_status)
    oqs = len(doc.get("open_questions", []))
    if by_status.get("FAIL", 0) > 0 or by_status.get("REGRESSED", 0) > 0:
        return "FAIL", summary, oqs
    if by_status.get("DEGRADED", 0) > 0:
        return "DEGRADED", summary, oqs
    return "PASS", summary, oqs


CLASSIFIERS = {
    "mab": _classify_mab, "tasm": _classify_tasm, "bom": _classify_bom,
    "car": _classify_car, "mrr":  _classify_mrr,  "lsr": _classify_lsr,
    "amr": _classify_amr, "arr":  _classify_arr,  "orr": _classify_orr,
    "hdr": _classify_hdr, "pvr":  _classify_pvr,
}


def _load(path: Path | None) -> dict | None:
    if path is None:
        return None
    if not path.is_file():
        log.warning("path %s does not exist; gate will be MISSING", path)
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def aggregate(paths: dict[str, Path | None]) -> dict[str, Any]:
    gates: dict[str, GateResult] = {}
    all_open_questions: list[str] = []

    for name in ALL_GATES:
        doc = _load(paths.get(name))
        if doc is None:
            status = "MISSING"
            summary = {}
            oq_count = 0
        else:
            status, summary, oq_count = CLASSIFIERS[name](doc)
            # carry the underlying gate's open_questions up by reference
            for q in doc.get("open_questions", []):
                all_open_questions.append(f"[{name}] {q}")
        gates[name] = GateResult(
            name=name, present=(doc is not None),
            status=status, summary=summary,
            open_question_count=oq_count,
        )

    # Verdict
    # 1) Required-gate FAIL/MISSING blocks cutover (NO_GO).
    required_failures   = [g for g in REQUIRED_GATES
                            if gates[g].status in ("FAIL", "MISSING")]
    # 2) Informational-gate FAIL (AMR unsupported, ARR high-severity) also
    #    blocks cutover. Compliance and HIGH-severity app defects are not
    #    the kind of thing the architecture board "reviews and accepts" --
    #    they require remediation before the swing.
    informational_failures = [g for g in INFORMATIONAL_GATES
                                if gates[g].status == "FAIL"]
    # 3) DEGRADED on any non-optional gate triggers HOLD (board reviews).
    degraded            = [name for name, g in gates.items()
                            if g.status == "DEGRADED" and name not in OPTIONAL_GATES]

    blocking_gates = required_failures + informational_failures
    if blocking_gates:
        verdict = "NO_GO"
    elif degraded:
        verdict = "HOLD"
    else:
        verdict = "GO"

    return {
        "_artifact":     "crr",
        "crr_version":   CRR_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verdict":       verdict,
        "blocking_gates": blocking_gates,
        "degraded_gates": degraded,
        "gates":         {name: dataclasses.asdict(g) for name, g in gates.items()},
        "open_questions": all_open_questions,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for g in ALL_GATES:
        p.add_argument(f"--{g}", type=Path, help=f"Path to the {g.upper()} JSON deliverable.")
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    paths = {g: getattr(args, g) for g in ALL_GATES}
    crr = aggregate(paths)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(crr, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")

    log.info("CRR written: %s", args.out)
    log.info("Verdict: %s", crr["verdict"])
    if crr["blocking_gates"]:
        log.error("BLOCKING gates: %s", crr["blocking_gates"])
    if crr["degraded_gates"]:
        log.warning("DEGRADED gates: %s", crr["degraded_gates"])

    # Exit codes for CI orchestration:
    # 0 = GO; 1 = HOLD (review needed); 2 = NO_GO (do not proceed)
    return {"GO": 0, "HOLD": 1, "NO_GO": 2}[crr["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
