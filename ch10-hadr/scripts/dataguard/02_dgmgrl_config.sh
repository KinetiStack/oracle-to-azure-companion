#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 02_dgmgrl_config.sh - register primary + standby with Data Guard Broker.
#
# DG Broker (dgmgrl) gives us declarative state for the Data Guard
# configuration: protection mode, fast-start failover policy, observer
# membership, role transitions via `switchover to <name>` instead of
# multi-step manual SQL.
#
# Prerequisites:
#   - dg_broker_start=TRUE on both primary and standby (set by 01_create_standby.sh)
#   - Static listener entries that include a service for DGMGRL on both sides
#     (the broker uses these to issue commands during role transitions)
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${PRIMARY_DBNAME:?}"
: "${STANDBY_DBNAME:?}"
: "${SYS_PASSWORD:?}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

log "Registering primary + standby with DG Broker"
dgmgrl -silent sys/${SYS_PASSWORD}@${PRIMARY_DBNAME} <<DG
CREATE CONFIGURATION 'oramig_dg' AS PRIMARY DATABASE IS '${PRIMARY_DBNAME}' CONNECT IDENTIFIER IS ${PRIMARY_DBNAME};
ADD DATABASE '${STANDBY_DBNAME}' AS CONNECT IDENTIFIER IS ${STANDBY_DBNAME} MAINTAINED AS PHYSICAL;
ENABLE CONFIGURATION;
SHOW CONFIGURATION;
DG

log "Setting Maximum Availability protection mode (sync where possible; falls back to async)"
dgmgrl -silent sys/${SYS_PASSWORD}@${PRIMARY_DBNAME} <<DG
EDIT DATABASE '${STANDBY_DBNAME}' SET PROPERTY 'LogXptMode'='SYNC';
EDIT CONFIGURATION SET PROTECTION MODE AS MAXAVAILABILITY;
DG

log "Tuning FSFO target lag (allow up to 30s data divergence before auto-failover)"
log "and ARMING FSFO with ENABLE FAST_START FAILOVER -- without this command,"
log "FSFO is configured but inactive and auto-failover will not trigger."
dgmgrl -silent sys/${SYS_PASSWORD}@${PRIMARY_DBNAME} <<DG
EDIT DATABASE '${STANDBY_DBNAME}' SET PROPERTY FastStartFailoverTarget='${PRIMARY_DBNAME}';
EDIT DATABASE '${PRIMARY_DBNAME}' SET PROPERTY FastStartFailoverTarget='${STANDBY_DBNAME}';
EDIT CONFIGURATION SET PROPERTY FastStartFailoverThreshold=30;
ENABLE FAST_START FAILOVER;
SHOW FAST_START FAILOVER;
DG

log "DG Broker configuration complete; FSFO is now ENABLED."
log "Run scripts/dataguard/03_fsfo_observer.sh on a THIRD host to start the Observer."
log "Verify with: dgmgrl> SHOW CONFIGURATION;  (expect: SUCCESS, Maximum Availability,"
log "             Fast-Start Failover: ENABLED)"
