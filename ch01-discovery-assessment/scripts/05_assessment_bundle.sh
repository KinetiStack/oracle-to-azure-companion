#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# File        : 05_assessment_bundle.sh
# Purpose     : Orchestrate the four discovery SQL scripts, capture an
#               environment fingerprint, build the Migration Assessment Bundle
#               (MAB), sign it, and upload the archive to Azure Blob Storage.
# Target      : Linux (RHEL 8 / Ubuntu 22.04) on a hardened assessment
#               workstation. Bash 4+, GNU coreutils, jq, tar, gpg, openssl,
#               sqlplus (Instant Client 19c), az CLI.
# Privileges  : Runs as the MIG_ASSESS Oracle account (read-only). No SYSDBA.
# Idempotent  : Yes. Re-running creates a new RUN_ID subdirectory.
# Failure mode: set -Eeuo pipefail + trap. Any failing step aborts the run
#               and emits a structured error to stderr.
#-------------------------------------------------------------------------------
set -Eeuo pipefail
shopt -s inherit_errexit

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
readonly SCRIPT_DIR
readonly SCRIPT_BUNDLE_VERSION="1.0.0"

#-------------------------------------------------------------------------------
# Configuration via environment variables (no secrets on the command line).
#
# CREDENTIAL HARDENING (recommended for production):
# Prefer Oracle Wallet authentication. Configure a wallet, then set
#   ORACLE_CONNECT='/@ORA19CPROD_RO'
# This keeps no password in env vars (which are readable from /proc/<pid>/environ
# by anyone with the same UID) or in shell history. The bash-level expansion of
# this variable into the sqlplus heredoc never echoes the secret.
#
# Required tools: sqlplus, jq, tar, gpg, sha256sum, az CLI, uuidgen, python3.
#-------------------------------------------------------------------------------
: "${ORACLE_CONNECT:?ORACLE_CONNECT must be set, e.g. /@ORA19CPROD_RO (wallet) or mig_assess/****@//prod-rac-scan:1521/ORA19CPROD}"
: "${MAB_OUT_BASE:=/var/mab}"
: "${AZ_STORAGE_ACCOUNT:?AZ_STORAGE_ACCOUNT must be set}"
: "${AZ_CONTAINER:=mab-archive}"
: "${GPG_SIGN_KEY:?GPG_SIGN_KEY must be set (key id or email)}"

# Tools sanity check.
for tool in sqlplus jq tar gpg sha256sum az uuidgen python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf >&2 'fatal: required tool not found in PATH: %s\n' "$tool"
        exit 127
    fi
done

readonly RUN_ID="$(uuidgen)"
readonly OUT_DIR="${MAB_OUT_BASE}/${RUN_ID}"
readonly CAPTURED_AT_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Section subdirectories used by the SQL scripts via the &1 substitution var.
mkdir -p \
    "${OUT_DIR}/feature_usage" \
    "${OUT_DIR}/workload_baseline" \
    "${OUT_DIR}/blockers" \
    "${OUT_DIR}/schema_complexity" \
    "${OUT_DIR}/summary"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

on_error() {
    local exit_code=$?
    local line=${1:-?}
    log "fatal: command failed at line ${line} (exit ${exit_code}). RUN_ID=${RUN_ID}"
    # Keep partial output for forensics — do NOT auto-delete.
    exit "$exit_code"
}
trap 'on_error ${LINENO}' ERR

#-------------------------------------------------------------------------------
# 1. Verify connection (RO probe). Fails fast if the DB or credentials are bad.
#-------------------------------------------------------------------------------
log "Verifying read-only connection to source database"
sqlplus -L -S /nolog >/dev/null <<SQL
WHENEVER SQLERROR EXIT FAILURE
WHENEVER OSERROR  EXIT FAILURE
CONNECT ${ORACLE_CONNECT}
SELECT 'ok' FROM dual;
EXIT SUCCESS
SQL

#-------------------------------------------------------------------------------
# 2. Capture environment fingerprint into manifest.json.
#-------------------------------------------------------------------------------
log "Capturing environment fingerprint"
FINGERPRINT_RAW="$(sqlplus -L -S /nolog <<SQL
WHENEVER SQLERROR EXIT FAILURE
CONNECT ${ORACLE_CONNECT}
SET HEADING OFF FEEDBACK OFF PAGESIZE 0 LINESIZE 32767 TRIMSPOOL ON
SELECT name        || '|' ||
       version_full|| '|' ||
       cdb         || '|' ||
       (SELECT COUNT(*) FROM gv\$instance) || '|' ||
       (SELECT value FROM nls_database_parameters WHERE parameter='NLS_CHARACTERSET') || '|' ||
       platform_name
FROM v\$database d, v\$instance i
WHERE ROWNUM = 1;
EXIT
SQL
)"

IFS='|' read -r FP_DBNAME FP_VERSION FP_CDB FP_RAC_NODES FP_CHARSET FP_PLATFORM <<<"${FINGERPRINT_RAW//$'\n'/}"

jq -n \
    --arg run_id           "$RUN_ID" \
    --arg captured_at_utc  "$CAPTURED_AT_UTC" \
    --arg db_name          "$FP_DBNAME" \
    --arg version          "$FP_VERSION" \
    --argjson rac_nodes    "${FP_RAC_NODES:-1}" \
    --arg cdb              "$FP_CDB" \
    --arg charset          "$FP_CHARSET" \
    --arg platform         "$FP_PLATFORM" \
    --arg bundle_version   "$SCRIPT_BUNDLE_VERSION" \
    --arg host             "$(hostname -f)" \
    '{
       run_id:          $run_id,
       captured_at_utc: $captured_at_utc,
       source: {
         db_name:       $db_name,
         version:       $version,
         cdb:           $cdb,
         rac_nodes:     $rac_nodes,
         characterset:  $charset,
         platform:      $platform
       },
       tooling: {
         script_bundle_version: $bundle_version
       },
       operator: {
         host: $host
       }
     }' > "${OUT_DIR}/manifest.json"

#-------------------------------------------------------------------------------
# 3-6. Run each discovery SQL, passing OUT_DIR as the &1 substitution var.
#-------------------------------------------------------------------------------
run_sql() {
    local script="$1"
    log "Running ${script}"
    sqlplus -L -S /nolog <<SQL
WHENEVER SQLERROR EXIT FAILURE
WHENEVER OSERROR  EXIT FAILURE
CONNECT ${ORACLE_CONNECT}
@${SCRIPT_DIR}/${script} ${OUT_DIR}
EXIT
SQL
}

run_sql 01_feature_usage_audit.sql
run_sql 02_workload_baseline.sql
run_sql 03_blocker_detection.sql
run_sql 04_schema_complexity.sql

#-------------------------------------------------------------------------------
# 7. Aggregate a high-level summary that Ch.2 / Ch.3 consume.
#-------------------------------------------------------------------------------
log "Building mab_summary.json"

# Use a CSV-aware parser. SQL*Plus MARKUP CSV emits RFC 4180-compliant rows,
# including quoted fields with embedded commas which awk -F',' would misparse.
count_csv_band() {
    local file="$1" col_name="$2" value="$3"
    python3 - "$file" "$col_name" "$value" <<'PY'
import csv, sys
path, col, val = sys.argv[1], sys.argv[2], sys.argv[3]
n = 0
with open(path, newline='') as f:
    reader = csv.DictReader(f)
    for row in reader:
        if row.get(col, '').strip() == val:
            n += 1
print(n)
PY
}

RED_COUNT=$(count_csv_band   "${OUT_DIR}/feature_usage/feature_score.csv"   TARGET_SCORE RED)
AMBER_COUNT=$(count_csv_band "${OUT_DIR}/feature_usage/feature_score.csv"   TARGET_SCORE AMBER)
GREEN_COUNT=$(count_csv_band "${OUT_DIR}/feature_usage/feature_score.csv"   TARGET_SCORE GREEN)
HIGH_BLOCKERS=$(count_csv_band "${OUT_DIR}/blockers/blockers_inventory.csv" BAND         HIGH)

jq -n \
    --argjson red_features    "$RED_COUNT" \
    --argjson amber_features  "$AMBER_COUNT" \
    --argjson green_features  "$GREEN_COUNT" \
    --argjson high_blockers   "$HIGH_BLOCKERS" \
    --slurpfile manifest "${OUT_DIR}/manifest.json" \
    '{
       _artifact:     "mab",
       run_id:        $manifest[0].run_id,
       source:        $manifest[0].source,
       scorecard: {
         red_features:    $red_features,
         amber_features:  $amber_features,
         green_features:  $green_features,
         high_blockers:   $high_blockers
       },
       open_questions: []
     }' > "${OUT_DIR}/summary/mab_summary.json"

#-------------------------------------------------------------------------------
# 8. Checksums + GPG signature.
#-------------------------------------------------------------------------------
log "Computing checksums and signing"
(
    cd "${OUT_DIR}"
    find . -type f \! -name 'checksums.sha256' \! -name 'mab.sig' -print0 \
        | sort -z \
        | xargs -0 sha256sum > checksums.sha256
    gpg --batch --yes --local-user "${GPG_SIGN_KEY}" --detach-sign --armor \
        --output mab.sig checksums.sha256
)

#-------------------------------------------------------------------------------
# 9. Archive + upload to immutable blob storage.
#-------------------------------------------------------------------------------
ARCHIVE="${MAB_OUT_BASE}/mab-${RUN_ID}.tar.gz"
log "Creating archive ${ARCHIVE}"
tar --owner=0 --group=0 --numeric-owner -czf "${ARCHIVE}" -C "${MAB_OUT_BASE}" "${RUN_ID}"

log "Uploading to Azure Storage account=${AZ_STORAGE_ACCOUNT} container=${AZ_CONTAINER}"
az storage blob upload \
    --auth-mode login \
    --account-name "${AZ_STORAGE_ACCOUNT}" \
    --container-name "${AZ_CONTAINER}" \
    --name "mab-${RUN_ID}.tar.gz" \
    --file "${ARCHIVE}" \
    --overwrite false \
    --output none

log "MAB ready: run_id=${RUN_ID} archive=${ARCHIVE}"
printf '%s\n' "${RUN_ID}"
