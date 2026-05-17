#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 01_apply_network.sh - deploy the Hub-Spoke + Private Endpoints Bicep.
#
# After this script succeeds, the Ch.3.5 SQL Server and PG Flex are reachable
# only via the PE IP -- but their public network access is still ENABLED.
# Disabling public access is a SEPARATE, FINAL step (see § 8.3 of the chapter
# and the 02_disable_public_access.sh runner referenced below).
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${LAB_RG:=rg-oracle-lab}"
: "${LAB_PREFIX:=oramig}"
: "${ONPREM_CIDR:=10.10.0.0/16}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
BICEP="$(cd -- "${SCRIPT_DIR}/../bicep" &>/dev/null && pwd)/main-network.bicep"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
command -v az >/dev/null || { log "fatal: az CLI not in PATH"; exit 127; }

# Discover the Ch.3.5 target names. We accept overrides via env vars when
# the deploy lives in a different RG.
SQL_NAME="${SQL_NAME:-$(az sql server list -g "${LAB_RG}" --query '[0].name' -o tsv)}"
PG_NAME="${PG_NAME:-$(az postgres flexible-server list -g "${LAB_RG}" --query '[0].name' -o tsv)}"

if [ -z "${SQL_NAME}" ] || [ -z "${PG_NAME}" ]; then
    log "fatal: could not discover SQL_NAME or PG_NAME in ${LAB_RG}"
    log "       export them explicitly or run the Ch.3.5 lab first"
    exit 1
fi

log "Deploying network Bicep against ${LAB_RG}"
log "  SQL server : ${SQL_NAME}"
log "  PG  server : ${PG_NAME}"
log "  onprem CIDR: ${ONPREM_CIDR}"

az deployment group create \
    --resource-group "${LAB_RG}" \
    --template-file  "${BICEP}" \
    --parameters \
        prefix="${LAB_PREFIX}" \
        onpremCidr="${ONPREM_CIDR}" \
        sqlServerName="${SQL_NAME}" \
        pgFlexServerName="${PG_NAME}" \
    --output none

log "Network deployed. Verify PE IPs:"
az deployment group show -g "${LAB_RG}" -n main-network \
    --query 'properties.outputs' -o json 2>/dev/null || \
    az network private-endpoint list -g "${LAB_RG}" \
        --query '[].{name:name, ip:customDnsConfigs[0].ipAddresses[0]}' -o table

log ""
log "NEXT STEPS (do NOT skip the order):"
log "  1. Apply Ch.8 audit DDL (scripts/03_audit_translate.py + manual sqlcmd/psql apply)."
log "  2. Test PE connectivity from a workstation inside the spoke VNet."
log "  3. ONLY THEN run: scripts/05_disable_public_access.sh"
