#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 02_apply_keyvault.sh - deploy Key Vault Bicep + populate initial secrets.
#
# Pre-requirements:
#   - 01_apply_network.sh has completed
#   - The deploying principal has rights to assign 'Key Vault Secrets Officer'
#     at the Key Vault scope (or the Bicep's roleAssignment step fails).
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${LAB_RG:=rg-oracle-lab}"
: "${LAB_PREFIX:=oramig}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
BICEP="$(cd -- "${SCRIPT_DIR}/../bicep" &>/dev/null && pwd)/main-keyvault.bicep"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
command -v az >/dev/null || { log "fatal: az CLI not in PATH"; exit 127; }

# Resolve the deploying principal's object ID.
ADMIN_OID="${ADMIN_OID:-$(az ad signed-in-user show --query id -o tsv)}"
[ -n "${ADMIN_OID}" ] || { log "fatal: could not resolve signed-in user OID"; exit 1; }

# Discover the network outputs we need.
SPOKE_VNET_ID=$(az network vnet show -g "${LAB_RG}" -n "${LAB_PREFIX}-spoke-db-vnet" --query id -o tsv)
PE_SUBNET_ID="${SPOKE_VNET_ID}/subnets/pe-subnet"
KV_PDZ_ID=$(az network private-dns zone show -g "${LAB_RG}" -n 'privatelink.vaultcore.azure.net' --query id -o tsv)

log "Deploying Key Vault Bicep"
DEPLOY_OUT=$(az deployment group create \
    --resource-group "${LAB_RG}" \
    --template-file  "${BICEP}" \
    --parameters \
        prefix="${LAB_PREFIX}" \
        adminPrincipalObjectId="${ADMIN_OID}" \
        peSubnetId="${PE_SUBNET_ID}" \
        kvPrivateDnsZoneId="${KV_PDZ_ID}" \
    --query 'properties.outputs' -o json)

KV_NAME=$(echo "${DEPLOY_OUT}" | python3 -c "import sys,json; print(json.load(sys.stdin)['keyVaultName']['value'])")
log "Key Vault: ${KV_NAME}"

# Populate initial secrets. The Bicep granted Secrets Officer to the deployer
# specifically so this step works on first run; tighten the role after.
log "Populating initial secrets (skip if values already set externally)"
populate() {
    local name="$1" value="${!1:-}"
    if [ -z "${value}" ]; then
        log "  skip ${name} (env var unset)"; return 0
    fi
    az keyvault secret set --vault-name "${KV_NAME}" --name "${name}" \
        --value "${value}" --output none
    log "  set ${name}"
}

populate ORACLE_SOURCE_PWD
populate AZURE_SQL_PASSWORD
populate PG_PASSWORD
populate GG_ADMIN_PWD
populate ENCRYPTION_PWD

log "Key Vault populated. Workload hosts grant their Managed Identity the"
log "'Key Vault Secrets User' role at the vault scope; never share secrets via env files."
