#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 01_create_standby.sh - create an Oracle Data Guard physical standby in a
# secondary Azure region via RMAN duplicate FROM ACTIVE DATABASE.
#
# Pre-conditions:
#   - Primary DB running, in ARCHIVELOG + FORCE LOGGING + supplemental log
#     (Ch.7 § 7.2 enabled these).
#   - Standby host(s) provisioned in the DR region; OS user oracle exists;
#     same Oracle Home version; tnsnames.ora entries for PRIMARY + STANDBY.
#   - Static listener entries on the standby for the SID (required for
#     duplicate-from-active to start the auxiliary instance).
#   - Same TDE wallet present on the standby (or use ENCRYPTION_PWD with a
#     newly-created wallet on the standby; copy the keystore + open it).
#
# This script captures the rote configuration; expect to adapt for your
# directory paths, ASM disk groups, and listener layout.
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${PRIMARY_TNS:?PRIMARY_TNS must be set (TNS alias for primary)}"
: "${STANDBY_TNS:?STANDBY_TNS must be set (TNS alias for standby aux instance)}"
: "${PRIMARY_DBNAME:?PRIMARY_DBNAME e.g. ORA19CPROD}"
: "${STANDBY_DBNAME:?STANDBY_DBNAME e.g. ORA19CSTBY}"
: "${SYS_PASSWORD:?SYS_PASSWORD must be set (matches primary)}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

log "Phase 1: configure primary for Data Guard"
sqlplus -L -S /nolog <<SQL
WHENEVER SQLERROR EXIT FAILURE
CONNECT sys/${SYS_PASSWORD}@${PRIMARY_TNS} AS SYSDBA
ALTER SYSTEM SET log_archive_config='DG_CONFIG=(${PRIMARY_DBNAME},${STANDBY_DBNAME})' SCOPE=BOTH;
ALTER SYSTEM SET log_archive_dest_2=
  'SERVICE=${STANDBY_DBNAME} ASYNC NOAFFIRM REOPEN=300 VALID_FOR=(ONLINE_LOGFILES,PRIMARY_ROLE) DB_UNIQUE_NAME=${STANDBY_DBNAME}'
  SCOPE=BOTH;
ALTER SYSTEM SET log_archive_dest_state_2=ENABLE SCOPE=BOTH;
ALTER SYSTEM SET fal_server=${STANDBY_DBNAME}                    SCOPE=BOTH;
ALTER SYSTEM SET standby_file_management=AUTO                    SCOPE=BOTH;
ALTER SYSTEM SET dg_broker_start=TRUE                            SCOPE=BOTH;
ALTER DATABASE FORCE LOGGING;
EXIT
SQL

log "Phase 2: prepare standby aux instance (write pfile + start NOMOUNT)"
# Provide a tiny pfile for the aux instance: DB_NAME must match the primary's
# DB_NAME; DB_UNIQUE_NAME must be the standby's. The duplicate-from-active
# pulls every other parameter from the primary, so we keep this minimal.
#
# The PFILE_PATH below assumes the script runs on the standby host under
# the same OS user that owns ORACLE_HOME. Override PFILE_PATH if your
# layout puts pfiles elsewhere.
: "${ORACLE_HOME:?ORACLE_HOME must be set on the standby host}"
PFILE_PATH="${PFILE_PATH:-${ORACLE_HOME}/dbs/init${STANDBY_DBNAME}_aux.ora}"
log "Writing aux pfile to ${PFILE_PATH}"
cat > "${PFILE_PATH}" <<PFILE
db_name=${PRIMARY_DBNAME}
db_unique_name=${STANDBY_DBNAME}
db_block_size=8192
sga_target=2g
PFILE

sqlplus -L -S /nolog <<SQL
WHENEVER SQLERROR EXIT FAILURE
CONNECT sys/${SYS_PASSWORD}@${STANDBY_TNS} AS SYSDBA
STARTUP NOMOUNT PFILE='${PFILE_PATH}';
EXIT
SQL

log "Phase 3: RMAN duplicate FROM ACTIVE DATABASE"
rman target sys/${SYS_PASSWORD}@${PRIMARY_TNS} \
     auxiliary sys/${SYS_PASSWORD}@${STANDBY_TNS} <<RMAN
RUN {
  DUPLICATE TARGET DATABASE FOR STANDBY FROM ACTIVE DATABASE
    SPFILE
      SET db_unique_name='${STANDBY_DBNAME}'
      SET fal_server='${PRIMARY_DBNAME}'
      SET log_archive_dest_2=
        'SERVICE=${PRIMARY_DBNAME} ASYNC NOAFFIRM REOPEN=300 VALID_FOR=(ONLINE_LOGFILES,PRIMARY_ROLE) DB_UNIQUE_NAME=${PRIMARY_DBNAME}'
    NOFILENAMECHECK;
}
RMAN

log "Phase 4: start managed recovery on standby"
sqlplus -L -S /nolog <<SQL
CONNECT sys/${SYS_PASSWORD}@${STANDBY_TNS} AS SYSDBA
ALTER DATABASE RECOVER MANAGED STANDBY DATABASE DISCONNECT FROM SESSION;
EXIT
SQL

log "Standby ${STANDBY_DBNAME} configured. Verify with:"
log "  sqlplus / 'as sysdba' @standby> SELECT database_role, open_mode FROM v\$database;"
log "  Expected: PHYSICAL STANDBY / MOUNTED (or READ ONLY WITH APPLY if you opened)"
log "Next: scripts/dataguard/02_dgmgrl_config.sh for DG Broker setup."
