#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 99_teardown.sh - destroy the lab to stop all charges.
#
# Deletes the Azure resource group (async) and stops + removes the Oracle
# container. Confirms by requiring the resource-group name to be typed.
#-------------------------------------------------------------------------------
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
LAB_ROOT="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"

: "${LAB_RG:=rg-oracle-lab}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

printf 'Type the resource group name to confirm teardown (%s): ' "${LAB_RG}"
read -r CONFIRM
if [ "${CONFIRM}" != "${LAB_RG}" ]; then
    log "Confirmation did not match. Aborted."
    exit 1
fi

log "Stopping Oracle container (data volumes removed)"
( cd "${LAB_ROOT}/oracle" && docker compose down -v ) || true

log "Deleting Azure resource group ${LAB_RG} (async)"
az group delete -n "${LAB_RG}" --yes --no-wait

log "Teardown initiated. Verify on the Azure Portal that ${LAB_RG} has been removed."
