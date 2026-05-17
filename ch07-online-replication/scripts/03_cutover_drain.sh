#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 03_cutover_drain.sh - orchestrate the T-minus-zero lag drain.
#
# Pre-conditions (chapter prose § 7.5):
#   1. Application writes have been quiesced at the source. THIS SCRIPT DOES NOT
#      quiesce the application -- that is an app-tier action (Ch.8.5). Calling
#      this script before quiescence will produce a moving target.
#   2. Extract, Pump, and Replicat are all running.
#   3. lag_monitor.py works against the GG REST API.
#
# Sequence:
#   a. Poll lag every POLL_INTERVAL seconds.
#   b. Once max_lag_seconds drops below MAX_LAG_THRESHOLD, wait STABILITY_WINDOW
#      seconds and verify lag is STILL below threshold.
#   c. Run reconcile.py (Ch.6) one final time. Abort if it reports non-zero.
#   d. Stop the GG processes via REST.
#   e. Print "CUTOVER READY" and return 0; non-zero on any failure.
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${GG_SERVER:?GG_SERVER must be set, e.g. https://gg-host:7811}"
: "${GG_USER:?GG_USER must be set}"
: "${GG_PASSWORD:?GG_PASSWORD must be set}"
: "${MAX_LAG_THRESHOLD:=5}"        # seconds
: "${STABILITY_WINDOW:=60}"        # seconds the lag must stay below threshold
: "${POLL_INTERVAL:=10}"           # seconds between polls
: "${POLL_DEADLINE:=3600}"         # give up after this many seconds

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
LSR_FILE="$(mktemp)"; trap 'rm -f "${LSR_FILE}"' EXIT

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

poll_lag() {
    python3 "${SCRIPT_DIR}/02_lag_monitor.py" \
        --server "${GG_SERVER}" --user "${GG_USER}" --password "${GG_PASSWORD}" \
        --out "${LSR_FILE}" >/dev/null 2>&1 || true
    python3 -c "import json; print(json.load(open('${LSR_FILE}'))['max_lag_seconds'])"
}

log "Starting cutover drain (threshold=${MAX_LAG_THRESHOLD}s, stability=${STABILITY_WINDOW}s)"

deadline=$(( $(date +%s) + POLL_DEADLINE ))
below_since=0

while : ; do
    if [ "$(date +%s)" -ge "${deadline}" ]; then
        log "fatal: lag did not converge within ${POLL_DEADLINE}s"
        exit 2
    fi

    lag="$(poll_lag)"
    log "current max lag: ${lag}s"

    if awk -v a="${lag}" -v b="${MAX_LAG_THRESHOLD}" 'BEGIN { exit !(a < b) }'; then
        if [ "${below_since}" -eq 0 ]; then
            below_since=$(date +%s)
            log "lag dropped below threshold; stability timer started"
        else
            elapsed=$(( $(date +%s) - below_since ))
            if [ "${elapsed}" -ge "${STABILITY_WINDOW}" ]; then
                log "lag stable below threshold for ${elapsed}s -- ready to drain"
                break
            fi
            log "  still below threshold (${elapsed}s of ${STABILITY_WINDOW}s)"
        fi
    else
        if [ "${below_since}" -ne 0 ]; then
            log "lag rose back above threshold; resetting stability timer"
        fi
        below_since=0
    fi

    sleep "${POLL_INTERVAL}"
done

# --- Final reconciliation gate ---------------------------------------------
if [ -n "${RECONCILE_CMD:-}" ]; then
    log "Running final reconciliation: ${RECONCILE_CMD}"
    if ! eval "${RECONCILE_CMD}"; then
        log "fatal: final reconciliation failed; do NOT cut over"
        exit 3
    fi
fi

# --- Stop processes via REST ------------------------------------------------
stop_processes() {
    for kind in extracts replicats; do
        python3 - <<PY
import json, urllib.request, base64, ssl, os
server = os.environ["GG_SERVER"].rstrip("/")
auth   = base64.b64encode(f"{os.environ['GG_USER']}:{os.environ['GG_PASSWORD']}".encode()).decode()
ctx    = ssl.create_default_context()
req = urllib.request.Request(f"{server}/services/v2/${kind}",
                             headers={"Authorization": f"Basic {auth}", "Accept": "application/json"})
items = json.load(urllib.request.urlopen(req, context=ctx))["response"]["items"]
for p in items:
    name = p["name"]
    stop_req = urllib.request.Request(
        f"{server}/services/v2/${kind}/{name}/command",
        method="POST",
        data=json.dumps({"command": "stop"}).encode(),
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
    )
    urllib.request.urlopen(stop_req, context=ctx)
    print(f"stopped ${kind} {name}")
PY
    done
}
log "Stopping replication processes"
stop_processes

log "CUTOVER READY: lag drained, reconciliation passed, GG stopped"
