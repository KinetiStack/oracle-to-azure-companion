#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# dns_ttl_check.sh - cutover-eve DNS pre-flight (P24 mitigation).
#
# Verifies that the FQDNs the application will resolve at cutover have low
# enough TTL for the swing to propagate within the hypercare window. The
# common rake: default Azure DNS TTL is 5 minutes -- but applications,
# JDBC pools, and OS resolvers cache aggressively beyond TTL. We check:
#
#   1. The authoritative TTL on the record (must be <= MAX_TTL_SECONDS).
#   2. The current resolved IP from the application's resolver (should
#      match the expected pre-cutover IP today; will flip post-cutover).
#   3. The JVM's networkaddress.cache.ttl property if a sample app is
#      provided -- this is the SQL Server JDBC driver's silent-cache bug.
#
# Run 24-48h before cutover. If TTL is higher than MAX_TTL_SECONDS, lower
# it via the DNS provider and wait one old-TTL period before the swing.
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${FQDNS:?FQDNS must be set (space-separated list of application-facing FQDNs)}"
: "${MAX_TTL_SECONDS:=300}"   # 5-minute ceiling
: "${SAMPLE_JAVA_CONFIG:=}"    # optional path to a java.security or similar

log()  { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
fail() { printf '[%s] FAIL: %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; exit 1; }

command -v dig >/dev/null || { log "fatal: dig not in PATH"; exit 127; }

failures=0
for fqdn in ${FQDNS}; do
    log "Checking ${fqdn}"
    # Get authoritative TTL. Accept A, AAAA, and CNAME so dual-stack
    # deployments (PE returning IPv6) are caught.
    ttl=$(dig +noall +answer "${fqdn}" |
          awk '$4 == "CNAME" || $4 == "A" || $4 == "AAAA" { print $2; exit }')
    if [ -z "${ttl}" ]; then
        log "  warn: ${fqdn} did not resolve via dig"
        failures=$((failures + 1))
        continue
    fi
    log "  TTL: ${ttl}s (ceiling ${MAX_TTL_SECONDS}s)"
    if [ "${ttl}" -gt "${MAX_TTL_SECONDS}" ]; then
        log "  FAIL: TTL ${ttl}s exceeds ceiling ${MAX_TTL_SECONDS}s"
        log "        Lower in DNS provider 24-48h pre-cutover to <= ${MAX_TTL_SECONDS}s"
        failures=$((failures + 1))
    fi

    # Show current resolved IP. Operators visually confirm it matches the
    # pre-cutover endpoint. (Automating the "expected IP" lookup would
    # depend on the workload's topology; we keep this human-readable.)
    resolved=$(dig +short "${fqdn}" | head -1)
    log "  Currently resolves to: ${resolved}"
done

# Optional JDBC-DNS-cache check.
# Tolerate whitespace around the '=' (legal in .security files) and around the
# value; reject non-integer values cleanly instead of erroring out the script.
if [ -n "${SAMPLE_JAVA_CONFIG}" ] && [ -f "${SAMPLE_JAVA_CONFIG}" ]; then
    log "Checking JVM DNS cache TTL in ${SAMPLE_JAVA_CONFIG}"
    if grep -qE '^[[:space:]]*networkaddress\.cache\.ttl[[:space:]]*=' "${SAMPLE_JAVA_CONFIG}"; then
        # Capture the value after the '=' with whitespace trimmed both sides.
        val=$(grep -E '^[[:space:]]*networkaddress\.cache\.ttl[[:space:]]*=' "${SAMPLE_JAVA_CONFIG}" \
              | head -1 \
              | sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]+$//')
        log "  networkaddress.cache.ttl = '${val}' (recommended: 60 or lower for cutover)"
        # Validate the value is a non-empty integer (handles negative)
        if ! printf '%s' "${val}" | grep -qE '^-?[0-9]+$'; then
            log "  FAIL: networkaddress.cache.ttl value is not an integer: '${val}'"
            failures=$((failures + 1))
        elif [ "${val}" = "-1" ] || [ "${val}" -gt 60 ]; then
            log "  FAIL: JVM caches DNS too aggressively for cutover (val=${val})"
            failures=$((failures + 1))
        fi
    else
        log "  WARN: networkaddress.cache.ttl not set; JVM default may cache forever"
        log "        Set 'networkaddress.cache.ttl=60' in \$JAVA_HOME/conf/security/java.security"
    fi
fi

if [ "${failures}" -gt 0 ]; then
    fail "${failures} DNS pre-flight checks failed; do NOT cut over until resolved"
fi
log "DNS pre-flight passed for ${FQDNS}"
