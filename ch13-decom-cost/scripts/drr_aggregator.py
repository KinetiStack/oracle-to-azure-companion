#!/usr/bin/env python3
"""drr_aggregator.py -- Decommissioning Readiness Report.

Aggregates the Day-N signals from Ch.13 into a single signed JSON
document. Closes the data chain that began with MAB (Ch.1) and ran
through CRR (Ch.12); DRR is the thirteenth and final deliverable.

Inputs (all paths optional; missing = the gate is MISSING):
    --crr               ../ch12-cutover/crr.json
    --quiesce-result    source_quiesce_result.json
    --license-inventory license_return_inventory.json
    --rightsize         rightsize_proposal.json
    --timeline          scripts/decom_timeline.yaml
    --advisor-backlog   <int>   (count of actionable Advisor recommendations)
    --today             YYYY-MM-DD (override for deterministic tests)

Gates:
    crr       : MUST be GO from Ch.12. Required.
    quiesce   : source_quiesce_result.json verdict must be PASS. Required.
    license   : inventory built; not past cancellation deadline. Required
                at the license_return stage (T+180d); informational before.
    rightsize : proposal exists; status no_change / downsize / upsize.
                Informational (HOLD if pending review).
    timeline  : decom_timeline.yaml stage matches today's offset. Required.
    advisor   : Advisor backlog count <= 15 actionable. Informational
                (HOLD over 15; FAIL never).

Verdict:
    GO     -- every required gate PASS; no informational FAIL.
    HOLD   -- any DEGRADED on required or informational; required all PASS.
    NO_GO  -- any required gate FAIL or MISSING.

Exit codes:
    0 = GO; 1 = HOLD; 2 = NO_GO; 3 = environment error.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # only required if --timeline is supplied

DRR_VERSION = "1.0.0"

REQUIRED_GATES      = ("crr", "quiesce", "timeline")
INFORMATIONAL_GATES = ("license", "rightsize", "advisor")
ALL_GATES           = REQUIRED_GATES + INFORMATIONAL_GATES


@dataclasses.dataclass
class GateResult:
    name:     str
    present:  bool
    status:   str           # PASS | DEGRADED | FAIL | MISSING
    summary:  dict[str, Any]


# ---------------------------------------------------------------------------
# Per-gate classifiers
# ---------------------------------------------------------------------------

def _classify_crr(doc: dict[str, Any] | None) -> GateResult:
    if doc is None:
        return GateResult("crr", False, "MISSING", {})
    verdict = doc.get("verdict", "")
    summary = {
        "verdict":         verdict,
        "blocking_gates":  doc.get("blocking_gates", []),
        "degraded_gates":  doc.get("degraded_gates", []),
    }
    if verdict == "GO":
        return GateResult("crr", True, "PASS", summary)
    if verdict == "HOLD":
        return GateResult("crr", True, "DEGRADED", summary)
    return GateResult("crr", True, "FAIL", summary)


def _classify_quiesce(doc: dict[str, Any] | None) -> GateResult:
    if doc is None:
        return GateResult("quiesce", False, "MISSING", {})
    verdict  = doc.get("verdict", "")
    summary  = {
        "verdict":       verdict,
        "open_mode":     doc.get("checks", {}).get("open_mode"),
        "failure_count": len(doc.get("failures", [])),
    }
    return GateResult("quiesce", True, "PASS" if verdict == "PASS" else "FAIL", summary)


def _classify_license(doc: dict[str, Any] | None, today: date) -> GateResult:
    if doc is None:
        return GateResult("license", False, "MISSING", {})
    totals = doc.get("totals", {})
    summary = {
        "contract":              doc.get("contract"),
        "processor_licenses":    totals.get("processor_licenses"),
        "goldengate_licenses":   totals.get("goldengate_licenses"),
        "cancellation_deadline": doc.get("cancellation_deadline"),
        "past_deadline":         doc.get("past_cancellation_deadline"),
    }
    # FAIL only if past the deadline (action no longer possible this cycle).
    if doc.get("past_cancellation_deadline"):
        return GateResult("license", True, "FAIL", summary)
    return GateResult("license", True, "PASS", summary)


def _classify_rightsize(doc: dict[str, Any] | None) -> GateResult:
    if doc is None:
        return GateResult("rightsize", False, "MISSING", {})
    p = doc.get("proposal", {})
    summary = {
        "engine":         doc.get("engine"),
        "recommendation": p.get("recommendation"),
        "from":           p.get("from"),
        "to":             p.get("to"),
        "annual_delta_usd": p.get("annual_delta_usd"),
    }
    rec = p.get("recommendation")
    if rec in ("no_change",):
        return GateResult("rightsize", True, "PASS", summary)
    if rec in ("downsize", "upsize"):
        # pending review = DEGRADED (HOLD); applied state would be PASS
        return GateResult("rightsize", True, "DEGRADED", summary)
    if rec == "manual_review":
        return GateResult("rightsize", True, "DEGRADED", summary)
    return GateResult("rightsize", True, "FAIL", summary)


def _classify_advisor(count: int | None) -> GateResult:
    if count is None:
        return GateResult("advisor", False, "MISSING", {})
    summary = {"actionable_count": count}
    # >15 actionable = backlog discipline broke down (DEGRADED, HOLD).
    if count > 15:
        return GateResult("advisor", True, "DEGRADED", summary)
    return GateResult("advisor", True, "PASS", summary)


def _classify_timeline(timeline_path: Path | None, today: date) -> GateResult:
    if timeline_path is None or not timeline_path.is_file():
        return GateResult("timeline", False, "MISSING", {})
    if yaml is None:
        return GateResult("timeline", False, "MISSING",
                          {"error": "pyyaml not installed"})
    try:
        raw = yaml.safe_load(timeline_path.read_text())
    except yaml.YAMLError as exc:
        return GateResult("timeline", True, "FAIL", {"error": f"yaml parse: {exc}"})

    # pyyaml auto-promotes unquoted YYYY-MM-DD literals to datetime.date;
    # accept either a string or a date so both stylistic forms work.
    cutover_raw = raw.get("cutover_date", "")
    if isinstance(cutover_raw, date):
        cutover = cutover_raw
    else:
        try:
            cutover = datetime.strptime(str(cutover_raw), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return GateResult("timeline", True, "FAIL",
                              {"error": f"bad cutover_date: {cutover_raw!r}"})
    days_since = (today - cutover).days
    if days_since < 0:
        return GateResult("timeline", True, "FAIL",
                          {"error": "today is before cutover_date"})

    # Determine which stage we should currently be in (the latest stage
    # whose offset_days <= days_since).
    stages = raw.get("stages", [])
    current_stage = None
    for s in stages:
        if int(s.get("offset_days", 0)) <= days_since:
            current_stage = s
        else:
            break
    if current_stage is None:
        return GateResult("timeline", True, "FAIL",
                          {"error": "no stage matches today's offset"})
    summary = {
        "cutover_date":  cutover_raw,
        "days_since":    days_since,
        "current_stage": current_stage.get("id"),
        "current_label": current_stage.get("label"),
        "next_stage":    current_stage.get("transitions_to"),
    }
    return GateResult("timeline", True, "PASS", summary)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _load(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def aggregate(*,
              crr_path: Path | None,
              quiesce_path: Path | None,
              license_path: Path | None,
              rightsize_path: Path | None,
              timeline_path: Path | None,
              advisor_backlog: int | None,
              today: date) -> dict[str, Any]:
    gates: dict[str, GateResult] = {}

    crr_doc = _load(crr_path)
    gates["crr"]       = _classify_crr(crr_doc)
    gates["quiesce"]   = _classify_quiesce(_load(quiesce_path))
    gates["license"]   = _classify_license(_load(license_path), today)
    gates["rightsize"] = _classify_rightsize(_load(rightsize_path))
    gates["advisor"]   = _classify_advisor(advisor_backlog)
    gates["timeline"]  = _classify_timeline(timeline_path, today)

    required_blockers = [g for g in REQUIRED_GATES
                         if gates[g].status in ("FAIL", "MISSING")]
    info_blockers     = [g for g in INFORMATIONAL_GATES
                         if gates[g].status == "FAIL"]
    blocking = required_blockers + info_blockers

    degraded = [g for g, r in gates.items()
                if r.status == "DEGRADED"]

    if blocking:
        verdict = "NO_GO"
    elif degraded:
        verdict = "HOLD"
    else:
        verdict = "GO"

    # Propagate open_questions from upstream CRR (which already accumulated
    # them from every prior signed deliverable) and append DRR-local ones.
    # Dropping these would lose the architecture board's running issues list
    # at the exact moment the terminal go/no-go is being judged.
    open_questions: list[str] = []
    if isinstance(crr_doc, dict):
        for q in crr_doc.get("open_questions", []):
            open_questions.append(f"[from crr] {q}")
    rs_summary = gates["rightsize"].summary
    if gates["rightsize"].status == "DEGRADED" and rs_summary.get("recommendation") in (
        "downsize", "upsize", "manual_review"
    ):
        open_questions.append(
            f"[drr] rightsize proposal '{rs_summary.get('recommendation')}' pending "
            f"review (annual delta ${rs_summary.get('annual_delta_usd', 0):+,})"
        )
    if gates["advisor"].status == "DEGRADED":
        open_questions.append(
            f"[drr] advisor backlog has {gates['advisor'].summary.get('actionable_count')} "
            "actionable items (>15 ceiling); triage in next FinOps weekly"
        )
    if gates["license"].status == "FAIL":
        open_questions.append(
            f"[drr] license cancellation deadline "
            f"{gates['license'].summary.get('cancellation_deadline')} has passed; "
            "auto-renewal will occur for next cycle"
        )

    doc: dict[str, Any] = {
        "_artifact":          "drr",
        "drr_version":        DRR_VERSION,
        "generated_at_utc":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "today":              today.isoformat(),
        "verdict":            verdict,
        "blocking_gates":     blocking,
        "degraded_gates":     degraded,
        "open_questions":     open_questions,
        "gates":              {name: dataclasses.asdict(g) for name, g in gates.items()},
    }
    body = json.dumps(doc, sort_keys=True).encode()
    doc["_sha256"] = hashlib.sha256(body).hexdigest()
    return doc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--crr",                type=Path, default=None)
    ap.add_argument("--quiesce-result",     type=Path, default=None)
    ap.add_argument("--license-inventory",  type=Path, default=None)
    ap.add_argument("--rightsize",          type=Path, default=None)
    ap.add_argument("--timeline",           type=Path, default=None)
    ap.add_argument("--advisor-backlog",    type=int,  default=None)
    ap.add_argument("--today",              type=str,  default=None)
    ap.add_argument("--out",                type=Path, default=Path("drr.json"))
    args = ap.parse_args()

    today = (
        datetime.strptime(args.today, "%Y-%m-%d").date()
        if args.today else date.today()
    )

    doc = aggregate(
        crr_path        = args.crr,
        quiesce_path    = args.quiesce_result,
        license_path    = args.license_inventory,
        rightsize_path  = args.rightsize,
        timeline_path   = args.timeline,
        advisor_backlog = args.advisor_backlog,
        today           = today,
    )
    args.out.write_text(json.dumps(doc, indent=2))

    print(f"[DRR] verdict: {doc['verdict']}")
    required_pass = sum(1 for g in REQUIRED_GATES if doc["gates"][g]["status"] == "PASS")
    informational_pass = sum(1 for g in INFORMATIONAL_GATES if doc["gates"][g]["status"] == "PASS")
    print(f"[DRR] required gates PASS: {required_pass}/{len(REQUIRED_GATES)}")
    print(f"[DRR] informational gates PASS: {informational_pass}/{len(INFORMATIONAL_GATES)}")
    print(f"[DRR] blocking gates: {doc['blocking_gates'] or 'none'}")
    print(f"[DRR] degraded gates: {doc['degraded_gates'] or 'none'}")
    print(f"[DRR] open questions: {len(doc['open_questions'])}")
    print(f"[DRR] Wrote {args.out} (signed, sha256: {doc['_sha256'][:8]}...)")

    return {"GO": 0, "HOLD": 1, "NO_GO": 2}[doc["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
