#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 03_databox_to_blob.sh - post-Data-Box-ingest verification.
#
# Run AFTER Microsoft confirms the Data Box has landed and its contents are
# in the destination blob container. This script:
#   1. Downloads checksums.sha256 + manifest.json from the blob.
#   2. Re-verifies sha256sums against blob-side files (sampled or full).
#   3. Reports any drift or missing files.
#
# Full re-checksum on a 20 TB landing is hours of read; in production we
# verify the manifest is intact + spot-check a percentage of files. The
# wrapper accepts SPOT_CHECK_PCT=100 for full coverage.
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${AZ_STORAGE_ACCOUNT:?AZ_STORAGE_ACCOUNT must be set}"
: "${AZ_CONTAINER:=mig-stage}"
: "${BLOB_PREFIX:=hrpro/}"
: "${SPOT_CHECK_PCT:=10}"        # percentage of files to re-checksum
: "${LOCAL_VERIFY_DIR:=/tmp/databox_verify}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
command -v az         >/dev/null || { log "fatal: az CLI not in PATH"; exit 127; }
command -v sha256sum  >/dev/null || { log "fatal: sha256sum missing";  exit 127; }
command -v awk        >/dev/null || { log "fatal: awk missing";        exit 127; }
command -v jq         >/dev/null || { log "fatal: jq missing";         exit 127; }

mkdir -p "${LOCAL_VERIFY_DIR}"
cd "${LOCAL_VERIFY_DIR}"

log "Listing files under ${AZ_CONTAINER}/${BLOB_PREFIX}"
az storage blob list \
    --auth-mode login \
    --account-name "${AZ_STORAGE_ACCOUNT}" \
    --container-name "${AZ_CONTAINER}" \
    --prefix "${BLOB_PREFIX}" \
    --query '[].name' --output tsv > blob_listing.txt

log "Downloading manifest + checksums"
for f in $(grep -E '(manifest\.json|checksums\.sha256)$' blob_listing.txt); do
    az storage blob download \
        --auth-mode login \
        --account-name "${AZ_STORAGE_ACCOUNT}" \
        --container-name "${AZ_CONTAINER}" \
        --name "$f" --file "$(basename "$f")" --output none
done

if [ ! -f checksums.sha256 ] || [ ! -f manifest.json ]; then
    log "fatal: manifest or checksums missing from blob"
    exit 2
fi

# Recover the staging-date directory from manifest.json. Data Box transit
# typically spans 1-2 weeks, so today's date is NOT the staging date and
# computing the blob path with `date -u +%Y%m%d` breaks every download.
STAGING_DATE=$(jq -r '.run_date | split("T")[0] | gsub("-"; "")' manifest.json)
if [ -z "${STAGING_DATE}" ] || [ "${STAGING_DATE}" = "null" ]; then
    log "fatal: could not derive staging date from manifest.json .run_date"
    exit 2
fi
log "Staging-date directory derived from manifest: ${STAGING_DATE}"

dump_count=$(grep -cE '\.dmp$' blob_listing.txt)
log "Manifest says: $(jq -r '.file_count' manifest.json) dump files"
log "Blob listing:  ${dump_count} dump files"

# Spot-check
total=$(wc -l < checksums.sha256)
sample_n=$(( total * SPOT_CHECK_PCT / 100 ))
[ "${sample_n}" -lt 1 ] && sample_n=1
log "Spot-checking ${sample_n} of ${total} files (${SPOT_CHECK_PCT}%)"

shuf -n "${sample_n}" checksums.sha256 > sample.sha256 || head -n "${sample_n}" checksums.sha256 > sample.sha256

failures=0
while read -r expected_hash _ filename; do
    blob_path="${BLOB_PREFIX}${STAGING_DATE}/${filename}"
    az storage blob download \
        --auth-mode login \
        --account-name "${AZ_STORAGE_ACCOUNT}" \
        --container-name "${AZ_CONTAINER}" \
        --name "${blob_path}" --file "${filename}" --output none 2>/dev/null
    actual_hash=$(sha256sum "${filename}" | awk '{print $1}')
    if [ "${expected_hash}" = "${actual_hash}" ]; then
        log "  OK  ${filename}"
    else
        log "  ERR ${filename}: expected=${expected_hash} actual=${actual_hash}"
        failures=$(( failures + 1 ))
    fi
    rm -f "${filename}"
done < sample.sha256

if [ "${failures}" -gt 0 ]; then
    log "fatal: ${failures} checksum failure(s); do NOT proceed to load"
    exit 3
fi
log "Spot-check passed: ${sample_n} files verified. Proceed to scripts/04_load_*.sh"
