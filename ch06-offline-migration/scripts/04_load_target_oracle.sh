#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 04_load_target_oracle.sh - impdp the staged dump into the target Oracle DB
# (Oracle Database@Azure or Oracle IaaS).
#
# Assumes:
#   - Dump segments are accessible from the target via DATA_PUMP_DIR (typically
#     pre-staged from blob storage to ASM/FS via AzCopy + an Oracle directory
#     object pointing at the same path).
#   - Target user / tablespaces have been pre-created OR will be created by
#     the impdp run (REMAP_TABLESPACE handles relocation).
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${TARGET_ORACLE_CONNECT:?TARGET_ORACLE_CONNECT must be set, e.g. /@ORATGT}"
: "${ENCRYPTION_PWD:?ENCRYPTION_PWD must be set (same as expdp run)}"
: "${SOURCE_TABLESPACE:=USERS}"
: "${TARGET_TABLESPACE:=USERS}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
command -v impdp >/dev/null || { log "fatal: impdp not in PATH"; exit 127; }

log "Launching impdp into target (REPLACE existing tables)"
impdp "${TARGET_ORACLE_CONNECT}" \
      DIRECTORY=DATA_PUMP_DIR \
      DUMPFILE=hrpro_%U.dmp \
      LOGFILE=hrpro_import.log \
      SCHEMAS=HRPRO \
      PARALLEL=8 \
      TABLE_EXISTS_ACTION=REPLACE \
      REMAP_TABLESPACE="${SOURCE_TABLESPACE}":"${TARGET_TABLESPACE}" \
      ENCRYPTION_PASSWORD="${ENCRYPTION_PWD}" \
      METRICS=YES

log "impdp complete. Run scripts/06_reconcile.py to prove row equivalence."
