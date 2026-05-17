#!/usr/bin/env python3
"""tasm_evaluator.py — Target Architecture Selection Matrix evaluator.

Ingests a Migration Assessment Bundle (MAB) produced by Chapter 1 and emits a
deterministic TASM recommendation as JSON conforming to tasm_schema.json.

Design constraints:
  - Standard library only (Python 3.11+). No pip dependencies.
  - Deterministic output: same MAB in => same recommendation out, byte-identical.
  - Documented thresholds at module top — change them via PR, not at runtime.
  - Conservative: when in doubt the script lowers confidence and emits
    open_questions for the architect to resolve. It does not invent precision.

Usage:
    tasm_evaluator.py --mab-dir /var/mab/<run-id> --out tasm_recommendation.json
                      [--p-quantile 0.999]
                      [--prefer-open-source]
                      [--cannot-refactor]
"""
from __future__ import annotations

import argparse
import csv
import dataclasses
import enum
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

EVALUATOR_VERSION = "1.0.0"

# -----------------------------------------------------------------------------
# Azure performance ceilings — published values as of 2026-Q1. Change via PR.
# Source: Microsoft Learn disk SKU documentation. Validate before each major
# engagement; Azure raises ceilings between training cutoff and engagement date.
# -----------------------------------------------------------------------------
PREMIUM_SSD_V2_MAX_IOPS_PER_DISK   = 80_000
PREMIUM_SSD_V2_MAX_MBPS_PER_DISK   = 1_200
ULTRA_DISK_MAX_IOPS_PER_DISK       = 400_000
ULTRA_DISK_MAX_MBPS_PER_DISK       = 4_000
ANF_ULTRA_MAX_MBPS_PER_VOLUME      = 4_500   # validated for Oracle workloads

# Refactor effort bands keyed on total PL/SQL lines-of-code in application schemas.
EFFORT_BANDS: list[tuple[int, str, str]] = [
    (5_000,        "LOW",      "2-4 weeks"),
    (25_000,       "MEDIUM",   "1-3 months"),
    (100_000,      "HIGH",     "3-9 months"),
    (10**9,        "EXTREME",  "9+ months - consider lift-not-refactor instead"),
]

# Features whose presence forces a regulatory-grade compensating-control review.
REGULATORY_FEATURES = frozenset({
    "Database Vault",
    "Label Security",
    "Workspace Manager",  # temporal/version compliance use case
})

log = logging.getLogger("tasm")


class Target(str, enum.Enum):
    ORACLE_DB_AZURE     = "Oracle Database@Azure"
    ORACLE_IAAS_SINGLE  = "Oracle on Azure IaaS (single-instance)"
    ORACLE_IAAS_RAC_FG  = "Oracle on Azure IaaS RAC (FlashGrid)"
    AZURE_SQL_MI        = "Azure SQL Managed Instance"
    POSTGRESQL_FLEX     = "Azure Database for PostgreSQL Flexible Server"


class StorageTier(str, enum.Enum):
    DB_AZURE_NATIVE = "Exadata Smart Storage (Oracle Database@Azure native)"
    PREMIUM_SSD_V2  = "Azure Premium SSD v2"
    ULTRA_DISK      = "Azure Ultra Disk"
    ANF_ULTRA       = "Azure NetApp Files (Ultra service level)"


# -----------------------------------------------------------------------------
# Input shapes
# -----------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class WorkloadFingerprint:
    peak_read_iops:  int
    peak_write_iops: int
    peak_read_mbps:  float
    peak_write_mbps: float
    peak_redo_mbps:  float
    avg_cpu_pct:     float
    peak_pga_mb:     float
    quantile:        float  # which P-quantile was used to compute peaks


@dataclasses.dataclass(frozen=True)
class FeatureSignal:
    rac_required:        bool
    spatial_present:     bool
    java_in_db_present:  bool
    high_blocker_count:  int
    red_feature_count:   int
    amber_feature_count: int
    regulatory_blockers: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class CodeSurface:
    total_plsql_loc:    int
    package_body_count: int
    trigger_count:      int
    hotspot_count:      int
    fga_policy_count:   int


@dataclasses.dataclass(frozen=True)
class Options:
    quantile:           float
    prefer_open_source: bool
    cannot_refactor:    bool


# -----------------------------------------------------------------------------
# MAB readers
# -----------------------------------------------------------------------------
def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FileNotFoundError(f"required MAB file missing: {path}")
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _to_int(value: str | None, default: int = 0) -> int:
    if value is None or value.strip() == "":
        return default
    return int(float(value))


def _to_float(value: str | None, default: float = 0.0) -> float:
    if value is None or value.strip() == "":
        return default
    return float(value)


def _quantile(values: Iterable[float], q: float) -> float:
    """Linear-interpolation quantile. Returns 0.0 on empty input.

    Implemented locally to avoid the numpy dependency and to keep the
    semantics under our control (no surprise upgrades changing rounding).
    """
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return 0.0
    if q <= 0:
        return xs[0]
    if q >= 1:
        return xs[-1]
    pos = q * (len(xs) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def load_workload(mab: Path, quantile: float) -> WorkloadFingerprint:
    iops_rows = _read_csv(mab / "workload_baseline" / "awr_iops.csv")
    tput_rows = _read_csv(mab / "workload_baseline" / "awr_throughput.csv")
    cpu_rows  = _read_csv(mab / "workload_baseline" / "awr_cpu_pga.csv")

    # Peaks are quantiles across the full hourly time-series, summed across
    # RAC instances per hour first (the storage subsystem sees the cluster
    # total, not the per-node value).
    def _sum_per_bucket(rows: list[dict[str, str]], time_col: str,
                       value_cols: list[str]) -> list[float]:
        bucket: dict[str, float] = {}
        for r in rows:
            t = r.get(time_col) or r.get(time_col.upper()) or ""
            total = sum(_to_float(r.get(c) or r.get(c.upper())) for c in value_cols)
            bucket[t] = bucket.get(t, 0.0) + total
        return list(bucket.values())

    read_iops_series  = _sum_per_bucket(iops_rows, "HOUR_UTC", ["READ_IOPS_AVG"])
    write_iops_series = _sum_per_bucket(iops_rows, "HOUR_UTC", ["WRITE_IOPS_AVG"])
    read_mbps_series  = _sum_per_bucket(tput_rows, "HOUR_UTC", ["READ_MB_PER_SEC"])
    write_mbps_series = _sum_per_bucket(tput_rows, "HOUR_UTC", ["WRITE_MB_PER_SEC"])
    redo_mbps_series  = _sum_per_bucket(tput_rows, "HOUR_UTC", ["REDO_MB_PER_SEC"])
    cpu_series        = [_to_float(r.get("CPU_PCT")) for r in cpu_rows]
    pga_series        = [_to_float(r.get("PGA_MB"))  for r in cpu_rows]

    return WorkloadFingerprint(
        peak_read_iops  = int(_quantile(read_iops_series,  quantile)),
        peak_write_iops = int(_quantile(write_iops_series, quantile)),
        peak_read_mbps  = round(_quantile(read_mbps_series,  quantile), 2),
        peak_write_mbps = round(_quantile(write_mbps_series, quantile), 2),
        peak_redo_mbps  = round(_quantile(redo_mbps_series,  quantile), 2),
        avg_cpu_pct     = round(sum(cpu_series) / len(cpu_series), 1) if cpu_series else 0.0,
        peak_pga_mb     = round(_quantile(pga_series, quantile), 1),
        quantile        = quantile,
    )


def load_features(mab: Path) -> FeatureSignal:
    feat_rows = _read_csv(mab / "feature_usage" / "feature_score.csv")
    blk_rows  = _read_csv(mab / "blockers" / "blockers_inventory.csv")

    def _is_used(row: dict[str, str]) -> bool:
        return (row.get("CURRENTLY_USED") == "TRUE"
                or _to_int(row.get("DETECTED_USAGES")) > 0)

    rac_required = any(
        "Real Application Clusters" in (r.get("FEATURE_NAME") or "")
        and r.get("TARGET_SCORE") == "RED" and _is_used(r)
        for r in feat_rows
    )
    spatial_present = any(
        "Spatial" in (r.get("FEATURE_NAME") or "") and _is_used(r)
        for r in feat_rows
    )
    java_in_db_present = any(
        (r.get("BLOCKER_NAME") or "").startswith("Java in Database")
        for r in blk_rows
    )

    red_count   = sum(1 for r in feat_rows if r.get("TARGET_SCORE") == "RED"   and _is_used(r))
    amber_count = sum(1 for r in feat_rows if r.get("TARGET_SCORE") == "AMBER" and _is_used(r))
    high_blockers = sum(1 for r in blk_rows if r.get("BAND") == "HIGH")

    regulatory: list[str] = []
    for r in feat_rows:
        name = r.get("FEATURE_NAME") or ""
        if _is_used(r) and any(rf in name for rf in REGULATORY_FEATURES):
            regulatory.append(name)

    return FeatureSignal(
        rac_required        = rac_required,
        spatial_present     = spatial_present,
        java_in_db_present  = java_in_db_present,
        high_blocker_count  = high_blockers,
        red_feature_count   = red_count,
        amber_feature_count = amber_count,
        regulatory_blockers = tuple(sorted(set(regulatory))),
    )


def load_code_surface(mab: Path) -> CodeSurface:
    inv_rows = _read_csv(mab / "schema_complexity" / "plsql_inventory.csv")
    hot_rows = _read_csv(mab / "schema_complexity" / "plsql_hotspots.csv")
    fga_rows = _read_csv(mab / "schema_complexity" / "fga_policies.csv")

    total_loc = sum(_to_int(r.get("TOTAL_LOC")) for r in inv_rows)
    pkg_body  = sum(_to_int(r.get("OBJECT_COUNT")) for r in inv_rows
                    if r.get("OBJECT_TYPE") == "PACKAGE BODY")
    triggers  = sum(_to_int(r.get("OBJECT_COUNT")) for r in inv_rows
                    if r.get("OBJECT_TYPE") == "TRIGGER")

    return CodeSurface(
        total_plsql_loc    = total_loc,
        package_body_count = pkg_body,
        trigger_count      = triggers,
        hotspot_count      = len(hot_rows),
        fga_policy_count   = len(fga_rows),
    )


# -----------------------------------------------------------------------------
# Decision logic
# -----------------------------------------------------------------------------
def effort_band(plsql_loc: int) -> tuple[str, str]:
    for ceiling, band, weeks in EFFORT_BANDS:
        if plsql_loc < ceiling:
            return band, weeks
    return EFFORT_BANDS[-1][1], EFFORT_BANDS[-1][2]


def pick_storage_tier(wl: WorkloadFingerprint, target: Target) -> dict[str, Any]:
    if target == Target.ORACLE_DB_AZURE:
        return {
            "tier":   StorageTier.DB_AZURE_NATIVE.value,
            "rationale": "Exadata Smart Storage is bundled with the service; Azure-disk sizing does not apply.",
        }

    total_iops = wl.peak_read_iops + wl.peak_write_iops
    total_mbps = wl.peak_read_mbps + wl.peak_write_mbps + wl.peak_redo_mbps

    # RAC on IaaS: ANF Ultra is the validated shared-storage fabric (often paired
    # with FlashGrid). Single-disk attached storage is not appropriate for RAC.
    if target == Target.ORACLE_IAAS_RAC_FG:
        return {
            "tier":   StorageTier.ANF_ULTRA.value,
            "rationale": (f"RAC requires shared storage; ANF Ultra is the Microsoft-validated "
                          f"path for Oracle on IaaS. Peak throughput {total_mbps:.0f} MB/s fits "
                          f"within the ~{ANF_ULTRA_MAX_MBPS_PER_VOLUME} MB/s per-volume ceiling."),
            "peak_iops_required":  total_iops,
            "peak_mbps_required":  round(total_mbps, 1),
        }

    # PaaS targets (MI, PG Flex) — storage is managed; surface the demand so the
    # tier within the target's storage menu is selected correctly.
    needs_ultra = (
        total_iops > PREMIUM_SSD_V2_MAX_IOPS_PER_DISK
        or total_mbps > PREMIUM_SSD_V2_MAX_MBPS_PER_DISK
    )
    if needs_ultra:
        return {
            "tier":   StorageTier.ULTRA_DISK.value,
            "rationale": (f"Peak demand ({total_iops:,} IOPS / {total_mbps:.0f} MB/s) exceeds the "
                          f"Premium SSD v2 single-disk ceiling "
                          f"({PREMIUM_SSD_V2_MAX_IOPS_PER_DISK:,} IOPS / "
                          f"{PREMIUM_SSD_V2_MAX_MBPS_PER_DISK} MB/s). Use Ultra Disk OR stripe "
                          f"multiple Premium SSD v2 disks behind the engine."),
            "peak_iops_required":  total_iops,
            "peak_mbps_required":  round(total_mbps, 1),
        }
    return {
        "tier":   StorageTier.PREMIUM_SSD_V2.value,
        "rationale": (f"Peak demand ({total_iops:,} IOPS / {total_mbps:.0f} MB/s) fits within a "
                      f"single Premium SSD v2 disk. Configure provisioned IOPS / throughput "
                      f"with 25-50% headroom over peak."),
        "peak_iops_required":  total_iops,
        "peak_mbps_required":  round(total_mbps, 1),
    }


def recommend(
    wl: WorkloadFingerprint,
    fs: FeatureSignal,
    cs: CodeSurface,
    opt: Options,
) -> dict[str, Any]:
    band, weeks = effort_band(cs.total_plsql_loc)
    rationale_primary: list[str] = []
    open_questions: list[str] = []
    confidence: str = "HIGH"

    # Step 1 — Refactoring posture.
    can_refactor = not opt.cannot_refactor

    # Step 2 — Hard architectural constraints.
    if fs.rac_required and not can_refactor:
        primary = Target.ORACLE_DB_AZURE
        secondary = Target.ORACLE_IAAS_RAC_FG
        rationale_primary += [
            "Real Application Clusters is currently used and refactor is off the table.",
            "Oracle Database@Azure provides Exadata/RAC natively without re-engineering.",
        ]
        open_questions.append(
            "Confirm Oracle Database@Azure regional availability for the target Azure region; "
            "fall back to IaaS RAC + FlashGrid if not yet available."
        )
    elif fs.rac_required and can_refactor:
        # RAC is used but team is willing to refactor: ask if RAC was for HA or scale.
        primary = Target.ORACLE_DB_AZURE
        secondary = (Target.POSTGRESQL_FLEX if opt.prefer_open_source else Target.AZURE_SQL_MI)
        rationale_primary += [
            "RAC is used today. Lift to Oracle Database@Azure preserves the architecture.",
            "Alternative: refactor away from RAC (most RAC deployments are for HA, which PaaS "
            "targets provide natively via zone-redundant high-availability).",
        ]
        open_questions.append(
            "Validate that RAC is operationally required (true active-active workload) "
            "vs. used for HA only. If HA-only, MI/PG zone-redundant HA suffices."
        )
        confidence = "MEDIUM"
    elif not can_refactor:
        primary = Target.ORACLE_IAAS_SINGLE
        secondary = Target.ORACLE_DB_AZURE
        rationale_primary += [
            "No-refactor posture rules out Azure SQL MI and PostgreSQL Flex.",
            "Single-instance Oracle on Azure IaaS provides full Oracle compatibility with "
            "Azure-native operations.",
        ]
    elif opt.prefer_open_source:
        primary = Target.POSTGRESQL_FLEX
        secondary = Target.AZURE_SQL_MI
        rationale_primary += [
            "Open-source target preferred and refactor is acceptable.",
            "Azure DB for PostgreSQL Flexible Server is first-party PaaS. NOTE: there is no "
            "native Oracle Compatibility Mode; use orafce + Ora2Pg + manual PL/pgSQL refactor.",
        ]
    else:
        # Refactor acceptable, no RAC, no open-source preference → SQL MI.
        primary = Target.AZURE_SQL_MI
        secondary = Target.POSTGRESQL_FLEX
        rationale_primary += [
            "Refactor is acceptable; no strict open-source requirement.",
            "Azure SQL MI is first-party PaaS with the most mature Oracle→T-SQL automated "
            "conversion toolchain (SSMA for Oracle).",
        ]

    # Step 3 — Modify confidence based on refactor scope and feature surface.
    if band in ("HIGH", "EXTREME") and primary in (Target.AZURE_SQL_MI, Target.POSTGRESQL_FLEX):
        confidence = "LOW" if band == "EXTREME" else "MEDIUM"
        rationale_primary.append(
            f"PL/SQL surface area is {cs.total_plsql_loc:,} LOC ({band} band). Validate refactor "
            f"appetite explicitly with the application owners before committing."
        )

    if fs.regulatory_blockers:
        open_questions.append(
            "Regulatory features in use: " + ", ".join(fs.regulatory_blockers) +
            ". Plan compensating controls on the target before cutover (see Ch.8)."
        )
        if primary != Target.ORACLE_DB_AZURE:
            confidence = "LOW" if confidence == "HIGH" else confidence

    if fs.spatial_present and primary not in (Target.ORACLE_DB_AZURE,
                                              Target.ORACLE_IAAS_SINGLE,
                                              Target.ORACLE_IAAS_RAC_FG):
        rationale_primary.append(
            "Spatial / SDO_GEOMETRY columns detected. On PG Flex, migrate to PostGIS. "
            "On MI, evaluate spatial parity carefully — geography type only, no networks."
        )

    if fs.java_in_db_present:
        open_questions.append(
            "Java in DB is present. None of the refactor targets support it. Plan "
            "externalization to Azure Functions / App Service before cutover."
        )

    storage = pick_storage_tier(wl, primary)

    return {
        "primary_recommendation": {
            "target":     primary.value,
            "confidence": confidence,
            "rationale":  rationale_primary,
        },
        "secondary_recommendation": {
            "target":    secondary.value,
            "rationale": "Provided as a fallback if the primary target is blocked by region, "
                         "licensing, or stakeholder decision.",
        },
        "storage_tier": storage,
        "refactor_estimate": {
            "effort_band":     band,
            "calendar_weeks":  weeks,
            "plsql_loc":       cs.total_plsql_loc,
            "hotspot_objects": cs.hotspot_count,
        },
        "open_questions": open_questions,
    }


# -----------------------------------------------------------------------------
# Output assembly
# -----------------------------------------------------------------------------
def build_recommendation(mab_dir: Path, opt: Options) -> dict[str, Any]:
    summary_path = mab_dir / "summary" / "mab_summary.json"
    if not summary_path.is_file():
        raise FileNotFoundError(f"MAB summary missing: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    wl = load_workload(mab_dir, opt.quantile)
    fs = load_features(mab_dir)
    cs = load_code_surface(mab_dir)
    rec = recommend(wl, fs, cs, opt)

    return {
        "_artifact":           "tasm",
        "mab_run_id":          summary.get("run_id"),
        "source_db":           summary.get("source"),
        "evaluated_at_utc":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "evaluator_version":   EVALUATOR_VERSION,
        "options": {
            "quantile":           opt.quantile,
            "prefer_open_source": opt.prefer_open_source,
            "cannot_refactor":    opt.cannot_refactor,
        },
        "workload_fingerprint": dataclasses.asdict(wl),
        "feature_signal":       dataclasses.asdict(fs),
        "code_surface":         dataclasses.asdict(cs),
        **rec,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mab-dir", required=True, type=Path,
                        help="Path to extracted MAB directory (the run_id folder).")
    parser.add_argument("--out", required=True, type=Path,
                        help="Output path for tasm_recommendation.json")
    parser.add_argument("--p-quantile", type=float, default=0.999, dest="quantile",
                        help="Quantile used to derive workload peaks (default 0.999 = P99.9).")
    parser.add_argument("--prefer-open-source", action="store_true",
                        help="Bias refactor target toward PostgreSQL Flex.")
    parser.add_argument("--cannot-refactor", action="store_true",
                        help="Constrain to lift-only targets (Oracle Database@Azure or IaaS).")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not (0.0 < args.quantile <= 1.0):
        parser.error("--p-quantile must be in (0, 1].")

    opt = Options(
        quantile           = args.quantile,
        prefer_open_source = args.prefer_open_source,
        cannot_refactor    = args.cannot_refactor,
    )

    rec = build_recommendation(args.mab_dir, opt)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(rec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    log.info("TASM recommendation written: %s", args.out)
    log.info("Primary target: %s (confidence=%s)",
             rec["primary_recommendation"]["target"],
             rec["primary_recommendation"]["confidence"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
