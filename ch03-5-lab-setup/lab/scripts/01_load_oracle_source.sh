#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 01_load_oracle_source.sh - create the HRPRO schema and seed synthetic data.
#
# Requires the Oracle 19c container started by 00_provision_lab.sh and the
# host to have python3 + python-oracledb installed (pip install oracledb).
#-------------------------------------------------------------------------------
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
SQL_DIR="$(cd -- "${SCRIPT_DIR}/../oracle" &>/dev/null && pwd)"
readonly CONTAINER=ora19c-lab
readonly PDB=ORCLPDB1

: "${ORACLE_PWD:?ORACLE_PWD must be set (container SYS password)}"
: "${HRPRO_PWD:=hrpro_lab_password}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

# Copy DDL into the container so sqlplus can execute them by path
log "Copying SQL files into ${CONTAINER}"
docker cp "${SQL_DIR}/hr_pro_schema.sql"   "${CONTAINER}:/tmp/hr_pro_schema.sql"
docker cp "${SQL_DIR}/pkg_payroll_run.sql" "${CONTAINER}:/tmp/pkg_payroll_run.sql"

# Create the HRPRO user + privileges + payroll directory (as SYS)
log "Creating HRPRO user and DBMS_FGA grant"
docker exec -i "${CONTAINER}" sqlplus -L -S /nolog <<SQL
WHENEVER SQLERROR EXIT FAILURE
CONNECT sys/${ORACLE_PWD}@//localhost:1521/${PDB} AS SYSDBA
CREATE USER hrpro IDENTIFIED BY "${HRPRO_PWD}" QUOTA UNLIMITED ON USERS;
GRANT CREATE SESSION, CREATE TABLE, CREATE TYPE, CREATE TRIGGER,
      CREATE PROCEDURE, CREATE SEQUENCE, CREATE VIEW,
      CREATE MATERIALIZED VIEW TO hrpro;
GRANT EXECUTE ON DBMS_FGA   TO hrpro;
GRANT EXECUTE ON DBMS_MVIEW TO hrpro;
CREATE OR REPLACE DIRECTORY payroll_dir AS '/tmp';
GRANT READ, WRITE ON DIRECTORY payroll_dir TO hrpro;
EXIT
SQL

# Run DDL as HRPRO
log "Creating HRPRO schema objects"
docker exec -i "${CONTAINER}" sqlplus -L -S /nolog <<SQL
WHENEVER SQLERROR EXIT FAILURE
CONNECT hrpro/${HRPRO_PWD}@//localhost:1521/${PDB}
@/tmp/hr_pro_schema.sql
@/tmp/pkg_payroll_run.sql
EXIT
SQL

# Seed synthetic data from the host (thin-mode python-oracledb -> no client needed)
log "Seeding HR-Pro synthetic data (~35k rows)"
python3 "${SQL_DIR}/hr_pro_seed.py" \
    --user hrpro \
    --password "${HRPRO_PWD}" \
    --dsn 'localhost:1521/ORCLPDB1'

log "HRPRO schema and data ready. Run Ch.1 discovery scripts next."
