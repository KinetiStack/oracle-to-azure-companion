#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 04_load_target_pg.sh - load HR-Pro data into Azure DB for PostgreSQL Flex.
#
# Uses Ora2Pg in COPY mode. Two operating modes:
#   - DIRECT: Ora2Pg connects to BOTH Oracle source and PG target; streams data
#             between them. Best when both sides are reachable from the same
#             host (lab / online migrations).
#   - FILE:   Ora2Pg connects to Oracle ONLY and emits .sql files containing
#             COPY statements that the wrapper later pipes through psql. This
#             is the Data Box-friendly mode: the .sql files transit on the
#             device, then psql plays them back at the target.
#
# Set OPMODE=direct or OPMODE=file.
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${OPMODE:=file}"
: "${HRPRO_PWD:?HRPRO_PWD must be set (Oracle source)}"
: "${PG_FQDN:?PG_FQDN must be set}"
: "${PG_PASSWORD:?PG_PASSWORD must be set}"
: "${ORA_DSN:=dbi:Oracle:host=localhost;service_name=ORCLPDB1;port=1521}"
: "${OUT_DIR:=/var/dumps/pg}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
command -v ora2pg >/dev/null || { log "fatal: ora2pg not in PATH"; exit 127; }
command -v psql   >/dev/null || { log "fatal: psql not in PATH";   exit 127; }

mkdir -p "${OUT_DIR}"

CONF=$(mktemp); trap 'rm -f "${CONF}"' EXIT
cat > "${CONF}" <<EOF
ORACLE_DSN     ${ORA_DSN}
ORACLE_USER    hrpro
ORACLE_PWD     ${HRPRO_PWD}
SCHEMA         HRPRO
EXPORT_SCHEMA  0
PARALLEL_TABLES 4
USE_RESERVED_WORDS 1
STOP_ON_ERROR  0
PG_DSN         dbi:Pg:dbname=labdb;host=${PG_FQDN};port=5432;sslmode=require
PG_USER        labadmin
PG_PWD         ${PG_PASSWORD}
OUTPUT_DIR     ${OUT_DIR}
TYPE           COPY
EOF

if [ "${OPMODE}" = "direct" ]; then
    log "DIRECT mode: streaming Ora2Pg -> PG Flex"
    ora2pg -c "${CONF}" -t COPY -j 4
else
    log "FILE mode: emitting COPY .sql files for Data Box transit"
    # Telling Ora2Pg to write files instead of stream: omit PG_DSN/PG_USER at
    # invocation time. We do this by overriding via env on the command line.
    ORA2PG_PG_DSN= ORA2PG_PG_USER= ORA2PG_PG_PWD= \
        ora2pg -c "${CONF}" -t COPY -o hr_pro_copy.sql -j 4

    log "Output: ${OUT_DIR}/hr_pro_copy.sql"
    log "After transit, replay with: PGPASSWORD=... psql -h ${PG_FQDN} -U labadmin -d labdb -f hr_pro_copy.sql"
fi

log "PG load step complete. Run scripts/06_reconcile.py --target-engine pg"
