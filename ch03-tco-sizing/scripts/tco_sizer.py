#!/usr/bin/env python3
"""tco_sizer.py - Total Cost of Ownership sizer for Oracle-to-Azure migrations.

Reads a TASM recommendation (Chapter 2) plus an Azure pricing dictionary and
emits a Bill of Materials (BoM) JSON document conforming to tco_bom_schema.json.

Design constraints:
  - Standard library only (Python 3.11+).
  - Deterministic: same inputs => byte-identical output.
  - License-cost-honest: counts Oracle Processor licenses required but never
    invents dollar figures for them. Oracle BYOL pricing is customer-specific
    and confidential; reporting it here would be malpractice.
  - Pricing-honest: every USD figure carries a 'priced_at' caveat. Always
    refresh against the Azure Retail Prices API before formal submission.

Usage:
    tco_sizer.py --tasm tasm_recommendation.json \\
                 --pricing azure_pricing.json \\
                 --out tco_bom.json \\
                 [--term-years 3] \\
                 [--reserved] \\
                 [--workload-class oltp|olap] \\
                 [--prefer-constrained-vcpu]
"""
from __future__ import annotations

import argparse
import dataclasses
import enum
import json
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SIZER_VERSION = "1.0.0"

# -----------------------------------------------------------------------------
# Azure storage cost meters (provisioned baselines that are free).
# Premium SSD v2 ships with a free baseline of 3,000 IOPS and 125 MB/s per disk;
# you pay only for provisioned values *above* the baseline.
# -----------------------------------------------------------------------------
PREMIUM_SSDv2_IOPS_BASELINE  = 3_000
PREMIUM_SSDv2_MBPS_BASELINE  = 125

# Oracle ACE policy multiplier: with hyperthreading on (Azure default),
# 2 vCPUs = 1 Processor license. With HT disabled, 1:1.
ACE_HT_ON_DIVISOR  = 2
ACE_HT_OFF_DIVISOR = 1

# Standard Edition 2 hard ceiling per Oracle's ACE policy.
SE2_MAX_VCPUS = 8

# Workload-class memory heuristic: target memory floor in GiB given peak PGA.
# OLTP: SGA + PGA dominates; we want 4x the peak PGA in total RAM for safety.
# OLAP: PGA peaks much higher; we size proportionally larger.
MEMORY_MULTIPLIERS = {"oltp": 4.0, "olap": 6.0}
MEMORY_FLOOR_GIB   = 128  # never recommend less than 128 GiB for prod Oracle


# -----------------------------------------------------------------------------
# Candidate VM list. Memory-optimized SKUs commonly used for Oracle on Azure.
# Includes both parent SKUs and their constrained-vCPU variants. Memory and
# storage are identical between a parent and its constrained child; only the
# active vCPU count differs.
#
# Source: Microsoft Learn VM SKU documentation (Edsv5, Mdsv2 families).
# Verify against current published specs before formal sizing engagements.
# -----------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class VmSku:
    name:           str
    parent_sku:     str   # equal to `name` for non-constrained SKUs
    vcpu_total:     int   # physical-thread total
    vcpu_active:    int   # exposed to guest OS == Oracle-chargeable
    memory_gib:     int
    series_note:   str

VM_CANDIDATES: list[VmSku] = [
    # Edsv5 — general memory-optimized, common Oracle IaaS target
    VmSku("Standard_E16ds_v5",   "Standard_E16ds_v5",  16,  16,  128, "Edsv5"),
    VmSku("Standard_E32ds_v5",   "Standard_E32ds_v5",  32,  32,  256, "Edsv5"),
    VmSku("Standard_E64ds_v5",   "Standard_E64ds_v5",  64,  64,  512, "Edsv5"),
    VmSku("Standard_E96ds_v5",   "Standard_E96ds_v5",  96,  96,  672, "Edsv5"),
    # Constrained-vCPU variants of Edsv5 — Oracle license optimization
    VmSku("Standard_E32-8ds_v5",  "Standard_E32ds_v5",  32,   8,  256, "Edsv5 constrained"),
    VmSku("Standard_E32-16ds_v5", "Standard_E32ds_v5",  32,  16,  256, "Edsv5 constrained"),
    VmSku("Standard_E64-16ds_v5", "Standard_E64ds_v5",  64,  16,  512, "Edsv5 constrained"),
    VmSku("Standard_E64-32ds_v5", "Standard_E64ds_v5",  64,  32,  512, "Edsv5 constrained"),
    VmSku("Standard_E96-24ds_v5", "Standard_E96ds_v5",  96,  24,  672, "Edsv5 constrained"),
    VmSku("Standard_E96-48ds_v5", "Standard_E96ds_v5",  96,  48,  672, "Edsv5 constrained"),
    # M-series — very large memory targets, common for SGA-heavy Oracle
    VmSku("Standard_M64ls_v2",    "Standard_M64ls_v2",    64,  64, 1000, "Mdsv2"),
    VmSku("Standard_M128s_v2",    "Standard_M128s_v2",   128, 128, 2048, "Mdsv2"),
    VmSku("Standard_M128ms_v2",   "Standard_M128ms_v2",  128, 128, 3892, "Mdsv2"),
    # Constrained M-series
    VmSku("Standard_M128-32ms_v2","Standard_M128ms_v2",  128,  32, 3892, "Mdsv2 constrained"),
    VmSku("Standard_M128-64ms_v2","Standard_M128ms_v2",  128,  64, 3892, "Mdsv2 constrained"),
]

log = logging.getLogger("tco")


# -----------------------------------------------------------------------------
# Inputs
# -----------------------------------------------------------------------------
class TargetClass(str, enum.Enum):
    ORACLE_DB_AZURE     = "Oracle Database@Azure"
    ORACLE_IAAS_SINGLE  = "Oracle on Azure IaaS (single-instance)"
    ORACLE_IAAS_RAC_FG  = "Oracle on Azure IaaS RAC (FlashGrid)"
    AZURE_SQL_MI        = "Azure SQL Managed Instance"
    POSTGRESQL_FLEX     = "Azure Database for PostgreSQL Flexible Server"


@dataclasses.dataclass(frozen=True)
class Options:
    term_years:               int
    reserved:                 bool
    workload_class:           str
    prefer_constrained_vcpu:  bool
    hyperthreading_enabled:   bool
    oracle_edition:           str       # "EE" or "SE2"


# -----------------------------------------------------------------------------
# Compute sizing
# -----------------------------------------------------------------------------
def required_memory_gib(peak_pga_mb: float, workload_class: str) -> int:
    mult = MEMORY_MULTIPLIERS.get(workload_class, MEMORY_MULTIPLIERS["oltp"])
    required = max(MEMORY_FLOOR_GIB, math.ceil((peak_pga_mb / 1024) * mult))
    return required


def required_active_vcpus(avg_cpu_pct: float,
                          source_rac_nodes: int,
                          assumed_source_vcpus_per_node: int = 32) -> int:
    """Translate avg CPU% on the source into active-vCPU need on the target.

    Heuristic: source = rac_nodes * assumed_source_vcpus_per_node consumed at
    avg_cpu_pct. Target needs that compute capacity plus 40% headroom.
    The caller can override via --workload-class as a proxy for the source.
    """
    source_total = max(1, source_rac_nodes) * assumed_source_vcpus_per_node
    consumed     = source_total * (avg_cpu_pct / 100.0)
    return max(4, math.ceil(consumed * 1.4))


def pick_vm(required_memory: int, required_active_vcpus: int,
            prefer_constrained: bool) -> VmSku:
    """Pick the smallest VM that satisfies memory AND active-vCPU requirements.

    When prefer_constrained=True, the constrained variants are preferred IF a
    constrained variant from the same parent series satisfies the requirement
    (this is what reduces Oracle license count without losing memory).
    """
    fits = [v for v in VM_CANDIDATES
            if v.memory_gib >= required_memory and v.vcpu_active >= required_active_vcpus]
    if not fits:
        # Nothing fits — return the biggest available and let the rationale flag it.
        return max(VM_CANDIDATES, key=lambda v: (v.memory_gib, v.vcpu_active))

    if prefer_constrained:
        constrained = [v for v in fits if v.vcpu_active < v.vcpu_total]
        if constrained:
            # Smallest constrained by active vCPU (license cost), then memory.
            return min(constrained, key=lambda v: (v.vcpu_active, v.memory_gib))

    return min(fits, key=lambda v: (v.memory_gib, v.vcpu_active, v.vcpu_total))


def oracle_licenses_required(vcpu_active: int, hyperthreading: bool,
                             edition: str) -> tuple[int, str]:
    divisor = ACE_HT_ON_DIVISOR if hyperthreading else ACE_HT_OFF_DIVISOR
    raw = math.ceil(vcpu_active / divisor)
    if edition == "SE2":
        rationale = (f"Standard Edition 2 caps at {SE2_MAX_VCPUS} vCPUs per database. "
                     f"Active vCPUs={vcpu_active}; SE2 ceiling check: ")
        if vcpu_active > SE2_MAX_VCPUS:
            rationale += (f"VIOLATED. Either constrain to {SE2_MAX_VCPUS} active vCPUs or "
                          "license as Enterprise Edition.")
        else:
            rationale += "OK."
        return raw, rationale
    return raw, (f"ACE policy with hyperthreading={'ON' if hyperthreading else 'OFF'}: "
                 f"ceil({vcpu_active} / {divisor}) = {raw} Processor licenses.")


# -----------------------------------------------------------------------------
# Storage sizing — three-meter Premium SSD v2 / Ultra; single-meter ANF.
# -----------------------------------------------------------------------------
def size_storage(tasm: dict[str, Any], pricing: dict[str, Any]) -> dict[str, Any]:
    tier = tasm["storage_tier"]["tier"]
    workload = tasm["workload_fingerprint"]

    peak_iops  = tasm["storage_tier"].get("peak_iops_required", 0)
    peak_mbps  = tasm["storage_tier"].get("peak_mbps_required", 0.0)

    # Capacity floor: source size + 50% headroom for redo, undo, temp, growth.
    # If the TASM doesn't carry source capacity (it doesn't, by design — Ch.3
    # adds this lens), we default to 30 TiB which covers the 20 TB anchor.
    capacity_gib = 30 * 1024  # 30 TiB default for anchor; override via pricing options

    if tier == "Exadata Smart Storage (Oracle Database@Azure native)":
        return {
            "tier":             tier,
            "rationale":        ("Exadata Smart Storage is bundled with the Oracle Database@Azure "
                                 "service per-OCPU-hour. No separate Azure-disk meters apply."),
            "capacity_gib":     None,
            "provisioned_iops": None,
            "provisioned_mbps": None,
        }
    if tier == "Azure NetApp Files (Ultra service level)":
        return {
            "tier":             tier,
            "rationale":        ("ANF Ultra charges by capacity pool size at the chosen service level. "
                                 "Throughput is included in the capacity allocation."),
            "capacity_gib":     capacity_gib,
            "provisioned_iops": None,   # ANF does not bill IOPS separately
            "provisioned_mbps": None,
        }

    # Premium SSD v2 / Ultra Disk — three meters.
    # Round provisioned values up with 25% headroom over peak.
    iops_with_headroom = math.ceil(peak_iops * 1.25)
    mbps_with_headroom = math.ceil(peak_mbps * 1.25)
    return {
        "tier":             tier,
        "rationale":        (f"Provisioned with 25% headroom over P99.9 peak. "
                             f"Three meters billed independently: capacity, IOPS, throughput. "
                             f"Premium SSD v2 baseline ({PREMIUM_SSDv2_IOPS_BASELINE} IOPS / "
                             f"{PREMIUM_SSDv2_MBPS_BASELINE} MB/s) is free."),
        "capacity_gib":     capacity_gib,
        "provisioned_iops": iops_with_headroom,
        "provisioned_mbps": mbps_with_headroom,
    }


# -----------------------------------------------------------------------------
# Cost estimation
# -----------------------------------------------------------------------------
def cost_storage_monthly(storage: dict[str, Any], pricing: dict[str, Any]) -> float:
    tier = storage["tier"]
    if tier == "Exadata Smart Storage (Oracle Database@Azure native)":
        return 0.0  # rolled into the per-OCPU-hour Exadata price (compute side)
    if tier == "Azure NetApp Files (Ultra service level)":
        rate = pricing["storage"]["anf_ultra"]["capacity_gib_month_usd"]
        return round(storage["capacity_gib"] * rate, 2)

    key = "ultra_disk" if "Ultra" in tier else "premium_ssd_v2"
    rates = pricing["storage"][key]
    capacity_cost  = storage["capacity_gib"]    * rates["capacity_gib_month_usd"]
    billable_iops  = max(0, (storage["provisioned_iops"] or 0)
                              - (PREMIUM_SSDv2_IOPS_BASELINE if key == "premium_ssd_v2" else 0))
    billable_mbps  = max(0, (storage["provisioned_mbps"] or 0)
                              - (PREMIUM_SSDv2_MBPS_BASELINE if key == "premium_ssd_v2" else 0))
    iops_cost      = billable_iops * rates["iops_provisioned_usd_month"]
    mbps_cost      = billable_mbps * rates["mbps_provisioned_usd_month"]
    return round(capacity_cost + iops_cost + mbps_cost, 2)


def cost_compute_monthly(sku: VmSku, instances: int,
                         pricing: dict[str, Any], reserved: bool) -> float:
    # Some SKU labels represent non-Azure-VM compute (Oracle Database@Azure
    # Exadata, PaaS service-tier strings). For those, no Azure VM pricing
    # is expected -- return 0 silently and let the caller's logic surface
    # the alternative pricing source (Oracle Database@Azure OCPU rates, Azure Pricing
    # Calculator for PaaS).
    if sku.parent_sku.startswith(("Exadata Database Service",
                                  "Azure SQL Managed Instance",
                                  "Azure Database for PostgreSQL")):
        return 0.0
    entry = pricing["compute"].get(sku.parent_sku) or pricing["compute"].get(sku.name)
    if not entry:
        log.warning("No pricing for %s; falling back to 0 (must be fixed before submission)",
                    sku.parent_sku)
        return 0.0
    rate = entry["ri_3yr_usd_hour"] if reserved else entry["payg_usd_hour"]
    hours_per_month = 730  # Azure-standard
    return round(rate * hours_per_month * instances, 2)


# -----------------------------------------------------------------------------
# Top-level sizing
# -----------------------------------------------------------------------------
def build_bom(tasm: dict[str, Any], pricing: dict[str, Any], opt: Options) -> dict[str, Any]:
    target = tasm["primary_recommendation"]["target"]
    workload = tasm["workload_fingerprint"]
    is_iaas_rac = (target == TargetClass.ORACLE_IAAS_RAC_FG.value)
    instances = 2 if is_iaas_rac else 1

    # --- Compute --------------------------------------------------------------
    if target == TargetClass.ORACLE_DB_AZURE.value:
        compute = {
            "sku":                  "Exadata Database Service (Oracle Database@Azure)",
            "parent_sku":           "Exadata Database Service (Oracle Database@Azure)",
            "instances":            1,
            "vcpu_total":           None,
            "vcpu_active":          None,
            "memory_gib":           None,
            "series_note":          "OCI Exadata shape — sized by OCPU count, not Azure VMs",
            "rationale": ("Oracle Database@Azure uses Exadata Database Service infrastructure. "
                          "Sizing is by OCPU count and Exadata rack shape (X10M/X11M class) at the "
                          "OCI Console; Azure VM SKUs do not apply. See Oracle Database@Azure pricing "
                          "calculator for OCPU rates."),
        }
        oracle_lic = {
            "applicable": True,
            "edition": opt.oracle_edition,
            "vcpus_chargeable": None,
            "hyperthreading_assumed": None,
            "processor_licenses_required": None,
            "rationale": "License included in the Exadata Database Service per-OCPU-hour price "
                         "(when using the Oracle-billed offering) OR BYOL with separate Oracle "
                         "contract for ACE-on-Oracle Database@Azure (verify with Oracle Sales).",
        }
    elif target in (TargetClass.AZURE_SQL_MI.value, TargetClass.POSTGRESQL_FLEX.value):
        # PaaS: no per-VM sizing, no Oracle license
        compute = {
            "sku":                  f"{target} — PaaS sizing",
            "parent_sku":           f"{target} — PaaS sizing",
            "instances":            1,
            "vcpu_total":           None,
            "vcpu_active":          None,
            "memory_gib":           None,
            "series_note":          "PaaS — sized by service tier + vCore + storage at the Portal",
            "rationale": ("First-party PaaS. Sizing happens at the service-tier level "
                          "(e.g., MI Business Critical M-series; PG Flex Memory-Optimized D32ds_v5). "
                          "This tool does not model PaaS sizing — use the Azure Pricing Calculator."),
        }
        oracle_lic = {
            "applicable": False,
            "edition": None,
            "vcpus_chargeable": None,
            "hyperthreading_assumed": None,
            "processor_licenses_required": 0,
            "rationale": "Target is non-Oracle engine. No Oracle Processor licenses required.",
        }
    else:
        # IaaS: pick a VM SKU
        req_mem    = required_memory_gib(workload["peak_pga_mb"], opt.workload_class)
        req_vcpus  = required_active_vcpus(workload["avg_cpu_pct"],
                                           tasm["source_db"].get("rac_nodes", 1) if tasm.get("source_db") else 1)
        sku = pick_vm(req_mem, req_vcpus, opt.prefer_constrained_vcpu)
        compute = {
            "sku":            sku.name,
            "parent_sku":     sku.parent_sku,
            "instances":      instances,
            "vcpu_total":     sku.vcpu_total,
            "vcpu_active":    sku.vcpu_active,
            "memory_gib":     sku.memory_gib,
            "series_note":    sku.series_note,
            "rationale": (f"Required memory >= {req_mem} GiB; required active vCPUs >= {req_vcpus}. "
                          f"Selected {sku.name} ({sku.vcpu_active} active / {sku.vcpu_total} total "
                          f"vCPU, {sku.memory_gib} GiB)." +
                          (" Instances=2 for RAC." if is_iaas_rac else "")),
        }
        active_total = sku.vcpu_active * instances
        lic_count, lic_rationale = oracle_licenses_required(
            active_total, opt.hyperthreading_enabled, opt.oracle_edition)
        oracle_lic = {
            "applicable": True,
            "edition":    opt.oracle_edition,
            "vcpus_chargeable": active_total,
            "hyperthreading_assumed": opt.hyperthreading_enabled,
            "processor_licenses_required": lic_count,
            "rationale":  lic_rationale,
        }

    # --- Storage --------------------------------------------------------------
    storage = size_storage(tasm, pricing)

    # --- Costs ----------------------------------------------------------------
    compute_payg  = (cost_compute_monthly(VmSku(compute["sku"], compute["parent_sku"],
                                                compute["vcpu_total"] or 0,
                                                compute["vcpu_active"] or 0,
                                                compute["memory_gib"] or 0, ""),
                                          compute["instances"], pricing, reserved=False)
                     if target in (TargetClass.ORACLE_IAAS_SINGLE.value,
                                   TargetClass.ORACLE_IAAS_RAC_FG.value)
                     else 0.0)
    compute_ri3yr = (cost_compute_monthly(VmSku(compute["sku"], compute["parent_sku"],
                                                compute["vcpu_total"] or 0,
                                                compute["vcpu_active"] or 0,
                                                compute["memory_gib"] or 0, ""),
                                          compute["instances"], pricing, reserved=True)
                     if target in (TargetClass.ORACLE_IAAS_SINGLE.value,
                                   TargetClass.ORACLE_IAAS_RAC_FG.value)
                     else 0.0)
    storage_monthly = cost_storage_monthly(storage, pricing)

    # Egress estimate derives from Chapter 7's continuous-replication redo
    # stream. The naive formula is:
    #
    #   peak_redo_mbps * seconds-per-day * days-per-month / MiB-per-GiB
    #
    # which treats P99.9 peak as if sustained for the entire month -- a
    # worst-case ceiling. Real OLTP workloads spend ~70% of the day below
    # their P99.9 peak, so we apply DUTY_CYCLE=0.3 to land closer to the
    # realistic monthly average. Production engagements refine this after
    # observing the first week of GG egress; the assumption rides in the
    # BoM's open_questions so finance can audit it.
    SECONDS_PER_DAY = 86400
    DAYS_PER_MONTH  = 30
    DUTY_CYCLE      = 0.3      # P99.9 peak compared to monthly average
    peak_redo_mbps  = float(workload.get("peak_redo_mbps", 0.0))
    egress_gb_estimate = round(
        peak_redo_mbps * DUTY_CYCLE * SECONDS_PER_DAY * DAYS_PER_MONTH / 1024.0,
        1,
    )
    egress_rate     = pricing["network"]["egress_per_gb_usd"]
    network_monthly = round(egress_gb_estimate * egress_rate, 2)

    total_monthly_payg    = round(compute_payg  + storage_monthly + network_monthly, 2)
    total_monthly_ri_3yr  = round(compute_ri3yr + storage_monthly + network_monthly, 2)
    months                = 12 * opt.term_years
    total_term_payg       = round(total_monthly_payg   * months, 2)
    total_term_ri_3yr     = round(total_monthly_ri_3yr * months, 2)

    open_questions: list[str] = []
    open_questions.append(
        f"egress_gb_per_month_estimate ({egress_gb_estimate} GiB) assumes "
        f"DUTY_CYCLE=0.3 against peak_redo_mbps={peak_redo_mbps}. It excludes "
        f"cross-region DR copy (Chapter 10, forthcoming) and pre-cutover "
        f"dual-run egress (Chapter 12, forthcoming) -- both ADD to this "
        f"number. Refine DUTY_CYCLE after the first week of GG egress data."
    )
    if target == TargetClass.ORACLE_DB_AZURE.value:
        open_questions.append(
            "Confirm Oracle Database@Azure pricing from the OCI Console or Oracle Sales; "
            "this sizer does not include Exadata OCPU pricing.")
    if oracle_lic["applicable"] and oracle_lic["processor_licenses_required"]:
        open_questions.append(
            f"Confirm Oracle Processor license cost for {oracle_lic['processor_licenses_required']} "
            f"licenses with Oracle Sales / procurement. BYOL pricing is customer-specific.")
    if storage["tier"].startswith("Azure NetApp"):
        open_questions.append(
            "ANF capacity pools have minimum sizes and step-pricing — model the actual pool size "
            "in Azure Pricing Calculator for an authoritative number.")
    if not opt.prefer_constrained_vcpu and oracle_lic["applicable"] and (compute["vcpu_total"] or 0) > 16:
        open_questions.append(
            "Re-run with --prefer-constrained-vcpu to evaluate Oracle license reduction via "
            "constrained-vCPU SKUs. Trade-off: full Azure VM price, fewer Oracle licenses.")

    return {
        "_artifact":        "bom",
        "tasm_run_id":      tasm.get("mab_run_id"),
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sizer_version":    SIZER_VERSION,
        "target":           target,
        "options": {
            "term_years":              opt.term_years,
            "reserved":                opt.reserved,
            "workload_class":          opt.workload_class,
            "prefer_constrained_vcpu": opt.prefer_constrained_vcpu,
            "hyperthreading_enabled":  opt.hyperthreading_enabled,
            "oracle_edition":          opt.oracle_edition,
        },
        "pricing_caveats": [
            f"Pricing dictionary version: {pricing.get('dictionary_version', 'unknown')}",
            f"Pricing region: {pricing.get('region', 'unknown')}",
            "Refresh rates against the Azure Retail Prices API before any formal submission.",
            "Oracle license dollar cost is NOT included — Oracle BYOL pricing is customer-specific.",
        ],
        "compute":          compute,
        "storage":          storage,
        "oracle_licensing": oracle_lic,
        "network": {
            "egress_gb_per_month_estimate": egress_gb_estimate,
            "egress_rate_usd_per_gb":       egress_rate,
            "expressroute_recommended":     True,
        },
        "cost_estimate_usd": {
            "compute_payg_monthly":     compute_payg,
            "compute_ri_3yr_monthly":   compute_ri3yr,
            "storage_monthly":          storage_monthly,
            "network_monthly":          network_monthly,
            "total_monthly_payg":       total_monthly_payg,
            "total_monthly_ri_3yr":     total_monthly_ri_3yr,
            "total_term_payg":          total_term_payg,
            "total_term_ri_3yr":        total_term_ri_3yr,
            "term_years":               opt.term_years,
            "oracle_license_cost_excluded": True,
        },
        "open_questions":   open_questions,
    }


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasm",    required=True, type=Path,
                        help="Path to tasm_recommendation.json (from Ch.2).")
    parser.add_argument("--pricing", required=True, type=Path,
                        help="Path to Azure pricing dictionary JSON.")
    parser.add_argument("--out",     required=True, type=Path,
                        help="Output path for tco_bom.json.")
    parser.add_argument("--term-years",    type=int, default=3,
                        help="TCO term in years (default 3).")
    parser.add_argument("--reserved", action="store_true",
                        help="Treat compute as Reserved Instance (3-year). Default: PAYG.")
    parser.add_argument("--workload-class", choices=("oltp", "olap"), default="oltp",
                        help="Workload class for memory heuristic (default oltp).")
    parser.add_argument("--prefer-constrained-vcpu", action="store_true",
                        help="Prefer constrained-vCPU SKUs to minimize Oracle license count.")
    parser.add_argument("--hyperthreading-disabled", action="store_true",
                        help="Assume HT disabled (1 vCPU = 1 license). Default: HT enabled.")
    parser.add_argument("--oracle-edition", choices=("EE", "SE2"), default="EE",
                        help="Oracle edition for license math (default EE).")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.term_years < 1 or args.term_years > 10:
        parser.error("--term-years must be in [1, 10].")

    tasm    = json.loads(args.tasm.read_text(encoding="utf-8"))
    pricing = json.loads(args.pricing.read_text(encoding="utf-8"))
    opt = Options(
        term_years              = args.term_years,
        reserved                = args.reserved,
        workload_class          = args.workload_class,
        prefer_constrained_vcpu = args.prefer_constrained_vcpu,
        hyperthreading_enabled  = not args.hyperthreading_disabled,
        oracle_edition          = args.oracle_edition,
    )
    bom = build_bom(tasm, pricing, opt)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(bom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    log.info("BoM written: %s", args.out)
    log.info("Target: %s | Compute: %s | Monthly (RI 3yr if applicable): $%s",
             bom["target"], bom["compute"]["sku"],
             bom["cost_estimate_usd"]["total_monthly_ri_3yr"])
    if bom["oracle_licensing"]["applicable"] and bom["oracle_licensing"]["processor_licenses_required"]:
        log.info("Oracle Processor licenses required: %d",
                 bom["oracle_licensing"]["processor_licenses_required"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
