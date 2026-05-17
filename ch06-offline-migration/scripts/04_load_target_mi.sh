#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 04_load_target_mi.sh - load CSV exports into Azure SQL MI / SQL Database
# via bcp.
#
# Source format: one CSV per table, UTF-8 (BOM-stripped), comma-delimited,
# LF line endings. The book's expected emit method:
#   sqlcl> set sqlformat csv
#   sqlcl> spool /var/dumps/csv/employee.csv
#   sqlcl> SELECT * FROM hrpro.employee;
#
# Production engagements typically replace CSV with SSMA's Data Migration mode
# OR Azure Database Migration Service. Both are bcp-equivalent under the hood;
# the lab uses bcp directly for transparency.
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${AZURE_SQL_FQDN:?AZURE_SQL_FQDN must be set}"
: "${AZURE_SQL_DB:=labdb}"
: "${AZURE_SQL_USER:=labadmin}"
: "${AZURE_SQL_PASSWORD:?AZURE_SQL_PASSWORD must be set}"
: "${CSV_DIR:=/var/dumps/csv}"

# BCP_DELIMITER defaults to TAB. Tab is safer than comma: bcp -c does NOT
# understand RFC 4180 quoted CSV, so any column containing a comma or
# newline corrupts field parsing when -t ',' is used. Tab is rare in HR-Pro
# string values, so it is the safer default. Set BCP_DELIMITER=',' to force
# comma at your own risk; production engagements with potentially-comma
# values should keep tab (or switch to a format file).
: "${BCP_DELIMITER:=$'\t'}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
command -v bcp >/dev/null || { log "fatal: bcp not in PATH (mssql-tools)"; exit 127; }

bcp_table() {
    local table="$1" file="$2"
    if [ ! -f "${CSV_DIR}/${file}" ]; then
        log "skip ${table}: ${file} not present"; return 0
    fi
    log "bcp ${table} <- ${file} (delimiter=$(printf '%q' "${BCP_DELIMITER}"))"
    bcp "dbo.${table}" in "${CSV_DIR}/${file}" \
        -S "tcp:${AZURE_SQL_FQDN},1433" \
        -d "${AZURE_SQL_DB}" -U "${AZURE_SQL_USER}" -P "${AZURE_SQL_PASSWORD}" \
        -c -t "${BCP_DELIMITER}" -r '\n' -F 2 -e "/tmp/${table}_errors.log" -m 100 -k
    # -c   character mode
    # -t   field delimiter (configurable; defaults to tab)
    # -F 2 skip the header row
    # -k   keep nulls
    # -m   max errors before abort
}

# Reference tables first; FK dependents last.
bcp_table DEPARTMENT       department.csv
bcp_table JOB_GRADE        job_grade.csv
bcp_table EMPLOYEE         employee.csv
bcp_table EMPLOYEE_HISTORY employee_history.csv
bcp_table PAYROLL_RUN      payroll_run.csv
bcp_table PAYROLL_RUN_LOG  payroll_run_log.csv

log "bcp load complete. Run scripts/06_reconcile.py --target-engine mssql"
