#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 01_setup_supp_log.sh - enable supplemental logging on the source.
# MUST be run BEFORE Ch.6's bulk export. See chapter prose § 7.2.
#-------------------------------------------------------------------------------
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
SQL_FILE="$(cd -- "${SCRIPT_DIR}/../goldengate" &>/dev/null && pwd)/01_supplemental_logging.sql"

: "${ORACLE_CONNECT:?ORACLE_CONNECT must be set with SYSDBA privileges, e.g. sys/****@//host:1521/SVC as sysdba}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

log "Verifying supplemental-logging state before change"
sqlplus -L -S /nolog <<SQL
WHENEVER SQLERROR EXIT FAILURE
CONNECT ${ORACLE_CONNECT}
SET PAGESIZE 0 FEEDBACK OFF HEADING ON LINESIZE 200
SELECT supplemental_log_data_min  AS supp_min,
       supplemental_log_data_pk   AS supp_pk,
       force_logging              AS forced
  FROM v\$database;
EXIT
SQL

log "Applying ${SQL_FILE}"
sqlplus -L -S /nolog <<SQL
WHENEVER SQLERROR EXIT FAILURE
CONNECT ${ORACLE_CONNECT}
@${SQL_FILE}
EXIT
SQL

log "Supplemental logging enabled. Proceed with Ch.6 bulk export."
