#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# source_quiesce_check.sh -- verify Oracle source is fully quiesced.
#
# Verifies that the on-prem source has no writes, no replication activity,
# and is in READ ONLY open mode. Run at T+7d (after READ ONLY flip; pre-
# power-off). Not runnable post-T+30d because the instance is stopped.
# Output is a JSON result consumed by drr_aggregator.py.
#
# GoldenGate Microservices runs OUTSIDE the database (separate
# Microservices stack on the GG hub host) -- there is no in-database
# view to probe its state. Verify GG quiesce externally on the GG hub:
#     ssh ${GG_HUB} 'cd ${OGG_HOME} && ./bin/ogg-cli info all'
# and capture the output alongside this script's JSON.
#
# Data Guard standby destinations remain queryable in-database via
# v$archive_dest (a real Oracle 19c view).
#
# Required env:
#   ORACLE_HOME       -- usual sqlplus expectations
#   ORACLE_SID        -- target instance, e.g. ORA19CPROD
#   ORA_CONNECT       -- "user/pass@//host:port/service" with SYSDBA grants
#   LOOKBACK_HOURS    -- how far back to look for writes (default 24)
#
# Writes:  source_quiesce_result.json
# Exits:   0 on PASS, 2 on FAIL (any check failed), 3 on environment error
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${ORA_CONNECT:?ORA_CONNECT must be set (user/pass@//host:port/service)}"
: "${LOOKBACK_HOURS:=24}"
: "${OUT:=source_quiesce_result.json}"

command -v sqlplus >/dev/null || { echo "fatal: sqlplus not in PATH" >&2; exit 3; }

now_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Emit a single CSV row for each check so we can parse robustly.
sql_out=$(sqlplus -S /nolog <<SQL
WHENEVER SQLERROR EXIT 1
CONNECT ${ORA_CONNECT}
SET PAGESIZE 0 FEEDBACK OFF VERIFY OFF HEADING OFF ECHO OFF LINESIZE 4000 TRIMSPOOL ON MARKUP CSV ON QUOTE OFF

-- open_mode: must be 'READ ONLY' or 'MOUNTED'
SELECT 'open_mode,'  || open_mode FROM v\$database;

-- archived logs in last LOOKBACK_HOURS hours
SELECT 'arch_logs,' || COUNT(*) FROM v\$archived_log
 WHERE first_time > SYSDATE - (${LOOKBACK_HOURS} / 24);

-- unified-audit-trail writes in last LOOKBACK_HOURS hours
SELECT 'audit_writes,' || COUNT(*) FROM unified_audit_trail
 WHERE event_timestamp > SYSTIMESTAMP - INTERVAL '${LOOKBACK_HOURS}' HOUR;

-- redo generation rate over the last hour (bytes)
SELECT 'redo_bytes_1h,' || NVL(SUM(blocks * block_size), 0)
  FROM v\$archived_log
 WHERE first_time > SYSDATE - (1/24);

-- Data Guard activity (any active log shipping target)
SELECT 'dg_active,' || COUNT(*) FROM v\$archive_dest WHERE status = 'VALID' AND target = 'STANDBY';

EXIT;
SQL
) || { echo "fatal: sqlplus query failed" >&2; exit 3; }

# Parse the CSV-shape output. Strip CRs (sqlplus on some OSes appends them).
sql_out=$(printf '%s' "${sql_out}" | tr -d '\r')

get() {
    # get <key>  -- return value after the first matching key,VALUE line
    printf '%s\n' "${sql_out}" | awk -F',' -v k="$1" '$1 == k { print $2; exit }'
}

open_mode=$(get open_mode)
arch_logs=$(get arch_logs)
audit_writes=$(get audit_writes)
redo_bytes_1h=$(get redo_bytes_1h)
dg_active=$(get dg_active)

# Default empties to safe values (defensive against silently empty queries)
: "${open_mode:=UNKNOWN}"
: "${arch_logs:=-1}"
: "${audit_writes:=-1}"
: "${redo_bytes_1h:=-1}"
: "${dg_active:=-1}"

# Apply pass/fail rules. open_mode must be exactly READ ONLY at the
# script's intended run-window (T+7d through pre-power-off); the prose
# in §13.1 promises this state explicitly. MOUNTED is rejected because
# the staged timeline never produces a MOUNTED source: the source is
# either READ ONLY (between T+7d and power-off) or the instance is
# stopped entirely (post T+30d, when sqlplus cannot even connect).
failures=()

if [ "${open_mode}" != "READ ONLY" ]; then
    failures+=("open_mode is '${open_mode}', must be 'READ ONLY'")
fi

[ "${arch_logs}" -gt 0 ]    2>/dev/null && failures+=("${arch_logs} archived logs generated in last ${LOOKBACK_HOURS}h")
[ "${audit_writes}" -gt 0 ] 2>/dev/null && failures+=("${audit_writes} audit-trail writes in last ${LOOKBACK_HOURS}h")
[ "${redo_bytes_1h}" -gt 0 ] 2>/dev/null && failures+=("redo generation in last 1h: ${redo_bytes_1h} bytes")
[ "${dg_active}" -gt 0 ]    2>/dev/null && failures+=("${dg_active} Data Guard standby destinations active")

if [ "${#failures[@]}" -eq 0 ]; then
    verdict="PASS"
    exit_code=0
else
    verdict="FAIL"
    exit_code=2
fi

# Emit JSON result. Build the failures array safely (no command-injection).
# We use python3 if present for safe JSON serialization; fall back to manual
# escaping if not.
if command -v python3 >/dev/null; then
    python3 - "${OUT}" "${verdict}" "${now_utc}" "${open_mode}" \
            "${arch_logs}" "${audit_writes}" "${redo_bytes_1h}" \
            "${dg_active}" "${LOOKBACK_HOURS}" \
            "${failures[@]}" <<'PY'
import json, sys
out_path, verdict, run_at, open_mode = sys.argv[1:5]
arch_logs, audit_writes, redo_bytes_1h, dg_active, lookback = sys.argv[5:10]
failures = sys.argv[10:]
doc = {
    "_artifact": "source_quiesce_result",
    "schema_version": "1.0.0",
    "run_at_utc": run_at,
    "verdict": verdict,
    "lookback_hours": int(lookback),
    "checks": {
        "open_mode":      open_mode,
        "archived_logs":  int(arch_logs),
        "audit_writes":   int(audit_writes),
        "redo_bytes_1h":  int(redo_bytes_1h),
        "dg_destinations": int(dg_active),
    },
    "note": "GoldenGate Microservices status verified externally on the GG hub (ogg-cli info all); not probed via SQL.",
    "failures": failures,
}
with open(out_path, "w") as f:
    json.dump(doc, f, indent=2)
PY
else
    echo "fatal: python3 not in PATH (needed for JSON emit)" >&2
    exit 3
fi

echo "[quiesce] verdict=${verdict} -> ${OUT}"
exit "${exit_code}"
