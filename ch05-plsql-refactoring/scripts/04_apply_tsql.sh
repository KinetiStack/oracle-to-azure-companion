#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 04_apply_tsql.sh - apply the Ch.5 T-SQL refactors to the Ch.3.5 lab's
# Azure SQL Database (labdb).
#
# Requires sqlcmd (mssql-tools) on PATH. Install via:
#   curl https://packages.microsoft.com/keys/microsoft.asc | sudo apt-key add -
#   sudo apt-get install -y mssql-tools18 unixodbc-dev
#-------------------------------------------------------------------------------
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
DDL_DIR="$(cd -- "${SCRIPT_DIR}/../refactor/tsql" &>/dev/null && pwd)"

: "${AZURE_SQL_FQDN:?AZURE_SQL_FQDN must be set (Bicep output sqlServerFqdn)}"
: "${AZURE_SQL_DB:=labdb}"
: "${AZURE_SQL_USER:=labadmin}"
: "${AZURE_SQL_PASSWORD:?AZURE_SQL_PASSWORD must be set (Bicep adminPassword)}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
command -v sqlcmd >/dev/null 2>&1 || { log "fatal: sqlcmd not in PATH"; exit 127; }

apply() {
    local file="$1"
    log "Applying $(basename "${file}")"
    sqlcmd -S "tcp:${AZURE_SQL_FQDN},1433" -d "${AZURE_SQL_DB}" \
           -U "${AZURE_SQL_USER}" -P "${AZURE_SQL_PASSWORD}" \
           -N -C -b -I -i "${file}"
}

apply "${DDL_DIR}/00_support_tables.sql"
apply "${DDL_DIR}/01_pkg_payroll_run.sql"
apply "${DDL_DIR}/02_trg_employee_audit.sql"

log "T-SQL refactor applied to ${AZURE_SQL_DB}@${AZURE_SQL_FQDN}"
