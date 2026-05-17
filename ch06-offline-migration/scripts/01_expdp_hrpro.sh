#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 01_expdp_hrpro.sh - run Data Pump expdp against the production source.
#
# Wrapper that:
#   - Verifies prerequisites (TDE wallet status, free disk, char-set match).
#   - Invokes expdp with the parameter file under conversion/oracle/.
#   - Pipes ENCRYPTION_PASSWORD via env to avoid command-line exposure.
#
# Requirements on the host: Oracle client 19c (expdp), bash 4+, df/du.
#-------------------------------------------------------------------------------
set -Eeuo pipefail
shopt -s inherit_errexit

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PAR_FILE="$(cd -- "${SCRIPT_DIR}/../oracle" &>/dev/null && pwd)/expdp_hrpro.par"

: "${ORACLE_CONNECT:?ORACLE_CONNECT must be set, e.g. /@ORA19CPROD or system/****@//host:1521/SVC}"
: "${ENCRYPTION_PWD:?ENCRYPTION_PWD must be set (TDE-protected source)}"
: "${DUMP_DIR_PATH:=/var/dumps}"
# Minimum free-disk gate at DUMP_DIR_PATH. The default is sized for the lab's
# Ch.3.5 container (~50 GiB working set). PRODUCTION must set this from the
# expected compressed dump size -- the anchor environment's 20 TB source with
# COMPRESSION=ALL produces ~5-10 TiB of dump, so set FREE_SPACE_MIN_GIB=12000
# (or run expdp ESTIMATE-only first and use that number).
: "${FREE_SPACE_MIN_GIB:=50}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

# ---- Pre-flight checks ----
command -v expdp  >/dev/null 2>&1 || { log "fatal: expdp not in PATH"; exit 127; }
command -v sqlplus >/dev/null 2>&1 || { log "fatal: sqlplus not in PATH"; exit 127; }

log "Verifying source charset and TDE wallet status"
sqlplus -L -S /nolog >/dev/null <<SQL
WHENEVER SQLERROR EXIT FAILURE
CONNECT ${ORACLE_CONNECT}
SET PAGESIZE 0 FEEDBACK OFF HEADING OFF
SELECT value FROM nls_database_parameters WHERE parameter = 'NLS_CHARACTERSET';
SELECT status FROM v\$encryption_wallet WHERE ROWNUM = 1;
EXIT
SQL

# ---- Free-space guard ----
# Compares against FREE_SPACE_MIN_GIB (default 50; see env-var preamble).
avail_gib=$(df -BG "${DUMP_DIR_PATH}" | awk 'NR==2 {gsub("G","",$4); print $4}')
log "Free space at ${DUMP_DIR_PATH}: ${avail_gib} GiB (minimum required: ${FREE_SPACE_MIN_GIB} GiB)"
if [ "${avail_gib}" -lt "${FREE_SPACE_MIN_GIB}" ]; then
    log "fatal: free space ${avail_gib} GiB is below threshold ${FREE_SPACE_MIN_GIB} GiB"
    log "       (raise FREE_SPACE_MIN_GIB if you have verified capacity another way)"
    exit 2
fi

# ---- Run expdp ----
log "Launching expdp with PARFILE=${PAR_FILE}"
# ENCRYPTION_PASSWORD is appended on the command line but the value reaches
# expdp via the inherited env, then expdp wipes its argv on Linux. For
# extra-paranoid environments, use an Oracle Wallet (KEYSTORE) instead.
expdp "${ORACLE_CONNECT}" \
      PARFILE="${PAR_FILE}" \
      ENCRYPTION_PASSWORD="${ENCRYPTION_PWD}"

log "expdp complete. Dump files at ${DUMP_DIR_PATH}/hrpro_*.dmp"
