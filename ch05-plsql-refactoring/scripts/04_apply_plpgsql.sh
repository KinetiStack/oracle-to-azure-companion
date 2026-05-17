#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 04_apply_plpgsql.sh - apply the Ch.5 PL/pgSQL refactors to the Ch.3.5 lab's
# Azure Database for PostgreSQL Flexible Server (labdb).
#-------------------------------------------------------------------------------
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
DDL_DIR="$(cd -- "${SCRIPT_DIR}/../refactor/plpgsql" &>/dev/null && pwd)"

: "${PG_FQDN:?PG_FQDN must be set (Bicep output pgFlexFqdn)}"
: "${PG_DB:=labdb}"
: "${PG_USER:=labadmin}"
: "${PG_PASSWORD:?PG_PASSWORD must be set (Bicep adminPassword)}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
command -v psql >/dev/null 2>&1 || { log "fatal: psql not in PATH"; exit 127; }

export PGPASSWORD="${PG_PASSWORD}"

apply() {
    local file="$1"
    log "Applying $(basename "${file}")"
    psql -h "${PG_FQDN}" -U "${PG_USER}" -d "${PG_DB}" \
         --set=sslmode=require -v ON_ERROR_STOP=1 \
         -f "${file}"
}

apply "${DDL_DIR}/00_support_tables.sql"
apply "${DDL_DIR}/01_pkg_payroll_run.sql"
apply "${DDL_DIR}/02_trg_employee_audit.sql"

log "PL/pgSQL refactor applied to ${PG_DB}@${PG_FQDN}"
