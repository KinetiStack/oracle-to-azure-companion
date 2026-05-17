#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 05_disable_public_access.sh - the LAST step in the Ch.8 ordering discipline.
#
# Pre-conditions (the chapter's § 8.3 ordering is non-negotiable):
#   1. scripts/01_apply_network.sh has completed -- PEs exist + Private DNS
#      resolves the SQL/PG FQDNs to private IPs from inside the spoke VNet.
#   2. The operator has verified private-path connectivity from a host
#      INSIDE the spoke (sqlcmd / psql succeed via PE).
#   3. Chapters 4, 5, 6 have finished landing schema, code, and data via
#      the public endpoint OR via the PE.
#
# After this script: the SQL Server and PG Flex resources are reachable
# ONLY via their Private Endpoints. A workstation without a route into
# the spoke VNet cannot connect.
#
# Confirmation: requires typing the resource-group name to avoid an
# accidental run during a workshop / demo.
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${LAB_RG:=rg-oracle-lab}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
command -v az >/dev/null || { log "fatal: az CLI not in PATH"; exit 127; }

printf 'Type the resource group name to confirm disabling public access (%s): ' "${LAB_RG}"
read -r CONFIRM
if [ "${CONFIRM}" != "${LAB_RG}" ]; then
    log "Confirmation did not match. Aborted."
    exit 1
fi

# Discover both PaaS targets in the RG; flip publicNetworkAccess to Disabled.
SQL_NAME=$(az sql server list -g "${LAB_RG}" --query '[0].name' -o tsv 2>/dev/null || true)
PG_NAME=$(az postgres flexible-server list -g "${LAB_RG}" --query '[0].name' -o tsv 2>/dev/null || true)

if [ -n "${SQL_NAME}" ]; then
    log "Disabling public access on Azure SQL server ${SQL_NAME}"
    az sql server update \
        --resource-group "${LAB_RG}" \
        --name "${SQL_NAME}" \
        --enable-public-network false \
        --output none
fi

if [ -n "${PG_NAME}" ]; then
    log "Disabling public access on PG Flex server ${PG_NAME}"
    # PG Flex 'public-access' flips through the network sub-property; the
    # supported CLI flag is --public-access Disabled. See az postgres
    # flexible-server update --help for parameter currency.
    az postgres flexible-server update \
        --resource-group "${LAB_RG}" \
        --name "${PG_NAME}" \
        --public-access Disabled \
        --output none
fi

log "Public access disabled. Re-verify via:"
log "  az sql server show -g ${LAB_RG} -n ${SQL_NAME} --query publicNetworkAccess"
log "  az postgres flexible-server show -g ${LAB_RG} -n ${PG_NAME} --query network.publicNetworkAccess"
log ""
log "WARNING: workstations without a route into the spoke VNet can no longer"
log "         connect. To re-enable temporarily, swap Disabled -> Enabled."
