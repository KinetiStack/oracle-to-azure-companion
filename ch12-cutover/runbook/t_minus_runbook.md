# T-Minus Cutover Runbook

> Print this. Tape it to the wall. The runbook is the only acceptable communication artifact during cutover — verbal updates get lost; chat messages get scrolled past; the runbook is checked off line-by-line by the cutover lead and witnessed by the architecture board.

**Cutover lead:** _________________     **Architecture board observer:** _________________

**Cutover window:** __________ UTC start  →  __________ UTC end

**Source environment:** ORA19CPROD   **Target environment:** _________________

---

## T-7 days — Pre-flight

- [ ] CRR exit code is `0` (GO). Run: `python3 crr_aggregator.py --mab ... --pvr ... --out crr.json`
- [ ] All `*open_questions[]` from prior deliverables reviewed and signed off by named owners
- [ ] DNS TTL on application-facing FQDNs lowered to ≤ 300s (see `dns_ttl_check.sh`)
- [ ] On-call rotation confirmed; escalation matrix distributed
- [ ] `python3 ch12-cutover/scripts/validate_escalation_matrix.py` exits `0` (verdict GO). The template ships failing on purpose: every TBD/placeholder/stale `last_updated` is surfaced. NO_GO at this checkpoint blocks the window.
- [ ] Dress rehearsal completed in non-prod; rehearsal CRR exit code 0; rehearsal runbook archived

## T-48h — Application freeze prep

- [ ] Application teams notified: writes freeze starts T-4h, ends T+15min
- [ ] All non-essential batch jobs suspended (cron, scheduler, etc.)
- [ ] Backup window confirmed: last full backup at T-24h or later
- [ ] PVR re-run against current data (the workload may have shifted)
- [ ] MRR re-run; deltas must all be zero on tier-1 tables

## T-24h — Final reconciliation

- [ ] Run `ch07-online-replication/scripts/02_lag_monitor.py`; record max_lag_seconds: _____ s
- [ ] Run `ch06-offline-migration/scripts/06_reconcile.py`; all tables PASS: _____ / _____
- [ ] Run `ch12-cutover/scripts/crr_aggregator.py` against ALL latest deliverables; verdict: _____
- [ ] Stakeholder calendar confirmation: all needed parties online for window
- [ ] War-room bridge created: _________________ (URL or dial-in)

## T-4h — Application freeze, replication catchup

- [ ] Application writes blocked at the load-balancer / API gateway layer
- [ ] Confirm freeze: no INSERTs/UPDATEs to source for 5 minutes (check `v$sysstat` or equivalent)
- [ ] Replication lag monitored — when `max_lag_seconds < 5` AND `all_running = true`, proceed
- [ ] Run `ch07-online-replication/scripts/03_cutover_drain.sh` with `MAX_LAG_THRESHOLD=5 STABILITY_WINDOW=60`
- [ ] Drain script exit code 0: confirmed _____

## T-1h — Final verification

- [ ] Final reconcile: `ch06-offline-migration/scripts/06_reconcile.py` exit 0 — confirmed _____
- [ ] Sample query timing on target matches PVR baseline (±15%): confirmed _____
- [ ] Backup target database state (in case rollback needed)
- [ ] Pre-warm target connection pool from app servers (without flipping DNS yet)
- [ ] Architecture board final GO confirmation: _________________ (name + timestamp)

## T-0 — The swing

- [ ] **Stop replication** — pick the engine-appropriate command:
  - Oracle target:     `dgmgrl> SWITCHOVER TO <standby_db_unique_name>;`
  - Azure SQL MI:      `az sql failover-group failover --name <fog-name> --resource-group <rg> --server <primary-mi>`
  - Azure DB for PG:   `az postgres flexible-server replica promote --resource-group <rg> --name <replica-server-name>`
- [ ] Swap DNS — paste-ready commands (replace `<...>` with your values):
  - Azure DNS CNAME:   `az network dns record-set cname set-record --resource-group <rg> --zone-name <zone> --record-set-name <name> --cname <new-target-fqdn>`
  - Azure Private DNS: `az network private-dns record-set cname set-record --resource-group <rg> --zone-name <zone> --record-set-name <name> --cname <new-target-fqdn>`
- [ ] Time stamp DNS change: _____
- [ ] Verify DNS propagation from at least 3 application-resolver locations:
  - [ ] App pod 1: `dig <fqdn>` returns target IP: _____
  - [ ] App pod 2: _____
  - [ ] App pod 3: _____
- [ ] Lift application freeze: writes re-enabled at gateway
- [ ] First synthetic transaction succeeds: _____

## T+15min — Application validation

- [ ] PVR golden query suite executes against new primary; status: _____ (must be GO)
- [ ] Key application paths exercised (login, primary read query, write transaction): _____
- [ ] Application error rate (Application Insights / Prometheus): _____ (baseline ±20%)
- [ ] Database connection pool fully populated against new primary: _____

## T+1h — Stabilization

- [ ] No P1 incidents reported by app teams or end users
- [ ] Hypercare dashboard (Log Analytics): no anomalies on the saved KQLs
- [ ] On-call handoff to BAU SRE team — confirmed handoff at: _____
- [ ] Source environment marked READ-ONLY (do NOT decommission yet — rollback window stays open 72h)

## T+24h, T+72h — Hypercare checkpoints

- [ ] T+24h CRR re-run (uses fresh PVR against live target): verdict _____
- [ ] T+72h CRR re-run: verdict _____
- [ ] **Restore DNS TTL** to pre-cutover value via `az network dns record-set ... update --set ttl=<original>`. Until this step, rollback latency stays low; after this, restored TTL takes effect on the next record refresh.
- [ ] Architecture board sign-off on cutover complete: _________________
- [ ] Source environment formally decommissioned (or contractually retained per policy)

---

**Rollback criteria — when to abort:**

Any of the following triggers an immediate rollback (revert DNS, restore source as primary):
- Target reconcile fails after the swing (data missing or diverged)
- Application error rate > 2× baseline for > 15 minutes with no clear cause
- Database engine on target reports any FATAL or correctness errors
- Any compliance / audit team objection that cannot be resolved within the window

**Rollback procedure:** DNS swing back to source IP (TTL ≤ 5 min); application freeze lifted against source; target marked READ-ONLY; war-room debrief within 24h.
