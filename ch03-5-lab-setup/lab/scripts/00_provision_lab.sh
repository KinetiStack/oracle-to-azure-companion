#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 00_provision_lab.sh - one-shot lab provisioner.
#
# 1. Creates the Azure resource group.
# 2. Deploys main.bicep (Azure SQL Database + PG Flex).
# 3. Starts the Oracle 19c container locally and waits for healthy state.
#
# Requirements on the host: az CLI (logged in), docker compose, curl, bash 4+.
# Run time: 5-15 minutes on a warm cache (first Oracle image pull adds ~10 min).
#-------------------------------------------------------------------------------
set -Eeuo pipefail
shopt -s inherit_errexit

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
readonly SCRIPT_DIR
LAB_ROOT="$(cd -- "${SCRIPT_DIR}/.." &>/dev/null && pwd)"
readonly LAB_ROOT

: "${LAB_RG:=rg-oracle-lab}"
: "${LAB_LOCATION:=eastus}"
: "${LAB_PREFIX:=oramig}"
: "${LAB_ADMIN_PASSWORD:?Set LAB_ADMIN_PASSWORD to a strong password (>=12 chars)}"
: "${ORACLE_PWD:?Set ORACLE_PWD for the Oracle container SYS/SYSTEM password}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

for tool in az docker curl; do
    command -v "$tool" >/dev/null 2>&1 || { log "fatal: $tool not in PATH"; exit 127; }
done

# 1) Resource group
log "Creating resource group ${LAB_RG} in ${LAB_LOCATION}"
az group create -n "${LAB_RG}" -l "${LAB_LOCATION}" --output none

# 2) Bicep deploy with the workstation's public IP added to firewall.
#    We pin --name so step 3 (and any operator query) can look the deployment
#    up by a stable name instead of guessing the auto-generated one.
readonly DEPLOY_NAME="lab-main"
CLIENT_IP="$(curl -fsS https://api.ipify.org)"
log "Deploying Bicep (client IP=${CLIENT_IP}, deployment name=${DEPLOY_NAME})"
az deployment group create \
    --resource-group "${LAB_RG}" \
    --name "${DEPLOY_NAME}" \
    --template-file "${LAB_ROOT}/bicep/main.bicep" \
    --parameters prefix="${LAB_PREFIX}" \
                 adminPassword="${LAB_ADMIN_PASSWORD}" \
                 allowedClientIp="${CLIENT_IP}" \
    --output none

# 3) Oracle container
log "Starting Oracle 19c container (first run pulls ~3 GB)"
( cd "${LAB_ROOT}/oracle" && ORACLE_PWD="${ORACLE_PWD}" docker compose up -d )

log "Waiting for Oracle container health (start_period 10m)"
deadline=$(( $(date +%s) + 900 ))   # 15-minute ceiling
while : ; do
    status="$(docker inspect -f '{{.State.Health.Status}}' ora19c-lab 2>/dev/null || echo none)"
    if [ "${status}" = "healthy" ]; then break; fi
    if [ "$(date +%s)" -ge "${deadline}" ]; then
        log "fatal: Oracle container did not become healthy within 15 minutes"
        docker logs --tail 80 ora19c-lab >&2 || true
        exit 1
    fi
    sleep 20
    log "  ... container status=${status}"
done
log "Oracle 19c ready on localhost:1521/ORCLPDB1"

log "Lab provisioned. Endpoints:"
az deployment group show -g "${LAB_RG}" -n "${DEPLOY_NAME}" \
    --query 'properties.outputs' --output json
