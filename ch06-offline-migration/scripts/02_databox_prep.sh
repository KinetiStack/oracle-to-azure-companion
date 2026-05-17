#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 02_databox_prep.sh - stage Data Pump dump files onto a mounted Azure
# Data Box, generate the per-shipment manifest, and emit SHA-256 checksums.
#
# Pre-requirements:
#   1. Order a Data Box (or Data Box Heavy) from the Azure portal pointing at
#      the destination storage account (the same one used as the Ch.1 MAB
#      archive, or a dedicated migration container).
#   2. Receive the device, follow Microsoft's unlock + mount instructions.
#   3. Mount the SMB share, e.g. /mnt/databox
#
# The device performs hardware AES-256 at rest. The SHA-256 manifest is for
# *content integrity* across the ship + ingest path -- the Microsoft side
# verifies it at landing.
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${DATABOX_MOUNT:=/mnt/databox}"
: "${DUMP_DIR:=/var/dumps}"
: "${CONTAINER_NAME:=mig-stage}"
: "${SOURCE_DB_NAME:=ORA19CPROD}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
command -v jq         >/dev/null || { log "fatal: jq not in PATH";         exit 127; }
command -v sha256sum  >/dev/null || { log "fatal: sha256sum not in PATH";  exit 127; }

run_date_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
stage_dir="${DATABOX_MOUNT}/${CONTAINER_NAME}/hrpro/$(date -u +%Y%m%d)"

[ -d "${DATABOX_MOUNT}" ] || { log "fatal: Data Box not mounted at ${DATABOX_MOUNT}"; exit 1; }

log "Staging dump files into ${stage_dir}"
mkdir -p "${stage_dir}"
cp "${DUMP_DIR}"/hrpro_*.dmp        "${stage_dir}/"
cp "${DUMP_DIR}"/hrpro_export.log   "${stage_dir}/"

log "Computing checksums"
(
    cd "${stage_dir}"
    sha256sum -- *.dmp *.log > checksums.sha256
)

log "Writing manifest.json"
file_count=$(ls -1 "${stage_dir}"/*.dmp | wc -l)
bytes=$(du -sb "${stage_dir}" | awk '{print $1}')
jq -n \
    --arg  run_date     "${run_date_iso}" \
    --arg  source_db    "${SOURCE_DB_NAME}" \
    --arg  schemas      "HRPRO" \
    --argjson file_count "${file_count}" \
    --argjson size_bytes "${bytes}" \
    '{
       run_date:         $run_date,
       source_db:        $source_db,
       schemas:          $schemas,
       file_count:       $file_count,
       size_bytes:       $size_bytes,
       dumpfile_pattern: "hrpro_%U.dmp",
       characterset:     "AL32UTF8",
       tde_protected:    true,
       encryption_mode:  "PASSWORD"
     }' > "${stage_dir}/manifest.json"

log "Data Box staging complete:"
log "  path  = ${stage_dir}"
log "  files = ${file_count} dump segment(s) + 1 export log + checksums + manifest"
log "  bytes = ${bytes}"
log ""
log "Next: unmount the Data Box and ship per Microsoft's RMA instructions."
log "On ingest: scripts/03_databox_to_blob.sh verifies SHA-256 against the manifest."
