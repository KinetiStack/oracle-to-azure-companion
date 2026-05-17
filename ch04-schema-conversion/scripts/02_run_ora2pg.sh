#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 02_run_ora2pg.sh - run Ora2Pg against the HR-Pro lab and emit converted SQL
# files into converted/ora2pg/, one per export type.
#
# Requires: Perl, DBI, DBD::Oracle, ora2pg 24.x. See conversion/ora2pg/README.md.
#-------------------------------------------------------------------------------
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
CONV_ROOT="$(cd -- "${SCRIPT_DIR}/../conversion" &>/dev/null && pwd)"
ORA2PG_DIR="${CONV_ROOT}/ora2pg"
OUT_DIR="${CONV_ROOT}/converted/ora2pg"

: "${HRPRO_PWD:?HRPRO_PWD must be set (the password set by 01_load_oracle_source.sh)}"
: "${PG_FQDN:?PG_FQDN must be set (Bicep output pgFlexFqdn)}"
# PG_PASSWORD is the canonical name across Ch.5/Ch.6/Ch.8; PG_PWD is accepted
# as an alias for back-compat. Set either.
: "${PG_PASSWORD:=${PG_PWD:-}}"
: "${PG_PASSWORD:?PG_PASSWORD (or legacy PG_PWD) must be set (the Bicep adminPassword)}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

command -v ora2pg >/dev/null 2>&1 || { log "fatal: ora2pg not in PATH"; exit 127; }

mkdir -p "${OUT_DIR}"

# Render a working copy of ora2pg.conf with secrets substituted in.
RUN_CONF="$(mktemp)"
trap 'rm -f "${RUN_CONF}"' EXIT
sed -e "s|hrpro_lab_password|${HRPRO_PWD}|" \
    -e "s|__SET_BY_WRAPPER__\\.postgres\\.database\\.azure\\.com|${PG_FQDN}|" \
    -e "s|__SET_BY_WRAPPER__|${PG_PASSWORD}|" \
    "${ORA2PG_DIR}/ora2pg.conf" > "${RUN_CONF}"

# One pass per export type so a parse failure in one does not mask others.
for t in TYPE TABLE INDEXES SEQUENCE TRIGGER MVIEW PACKAGE; do
    out_file="${OUT_DIR}/hr_pro_$(echo "$t" | tr '[:upper:]' '[:lower:]').sql"
    log "ora2pg -t ${t} -> ${out_file}"
    if ! ora2pg -c "${RUN_CONF}" -t "${t}" -o "${out_file}"; then
        log "WARN: ora2pg failed for TYPE=${t} (continuing; see ${out_file})"
    fi
done

log "Ora2Pg pass complete. Output in ${OUT_DIR}"
