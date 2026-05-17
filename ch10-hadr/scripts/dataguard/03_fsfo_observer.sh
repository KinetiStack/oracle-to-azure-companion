#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 03_fsfo_observer.sh - start the Fast-Start Failover Observer.
#
# *** THE OBSERVER MUST RUN ON A THIRD LOCATION ***
# Not on the primary host. Not on the standby host. A third VM, ideally in
# a third region or AZ. The Observer is the tiebreaker for network-partition
# scenarios: when primary and standby cannot see each other, the Observer
# decides who survives. If the Observer is co-located with one of the two,
# a network partition that isolates that side fails into ambiguity and FSFO
# DOES NOT TRIGGER -- the symptom every Data Guard engagement sees first
# at drill time.
#
# Sizing: the Observer is a single-process binary (~50 MB RAM, <1% CPU).
# A B1s VM ($3-5/month) is more than enough.
#
# This script registers the Observer as a systemd service so it survives
# reboots and gets logged via journald.
#-------------------------------------------------------------------------------
set -Eeuo pipefail

: "${PRIMARY_TNS:?}"
: "${STANDBY_TNS:?}"
: "${SYS_PASSWORD:?}"
: "${OBSERVER_USER:=oracle}"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }

# Locate ORACLE_HOME for the dgmgrl binary
: "${ORACLE_HOME:?ORACLE_HOME must be set on the observer host}"
DGMGRL="${ORACLE_HOME}/bin/dgmgrl"
[ -x "${DGMGRL}" ] || { log "fatal: dgmgrl not at ${DGMGRL}"; exit 127; }

# Sanity: are we on the primary or standby host? If so, abort.
HOSTNAME_LC=$(hostname | tr '[:upper:]' '[:lower:]')
if echo "${HOSTNAME_LC}" | grep -qE "(primary|prod-rac|standby|dr-rac)"; then
    log "fatal: this hostname (${HOSTNAME_LC}) appears to be primary or standby."
    log "       Observer MUST run on a THIRD host. Refusing to start."
    exit 2
fi

# Generate the systemd unit FIRST -- the START OBSERVER call below is a
# foreground blocking process, so anything after it would be unreachable
# during a normal run. Producing the template up front means the operator
# always has it on disk before the observer starts.
log "Writing systemd unit template to /tmp/fsfo-observer.service.template"
cat > /tmp/fsfo-observer.service.template <<UNIT
# /etc/systemd/system/fsfo-observer.service
#
# Customize the ExecStart credentials before installing:
#   - Replace SYS_PASSWORD with the value from your Key Vault.
#   - Replace PRIMARY_TNS with the TNS alias to the primary.
#   - Adjust ORACLE_HOME path to match this host's install.
[Unit]
Description=Oracle Data Guard Fast-Start Failover Observer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${OBSERVER_USER}
Environment=ORACLE_HOME=${ORACLE_HOME}
Environment=PATH=${ORACLE_HOME}/bin:/usr/bin
ExecStart=${ORACLE_HOME}/bin/dgmgrl -silent \
  sys/SYS_PASSWORD@${PRIMARY_TNS} "START OBSERVER FILE='/var/log/oracle/fsfo.dat' LOGFILE='/var/log/oracle/fsfo.log'"
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

log "systemd unit template written. To install:"
log "  sudo cp /tmp/fsfo-observer.service.template /etc/systemd/system/fsfo-observer.service"
log "  sudo \$EDITOR /etc/systemd/system/fsfo-observer.service     # replace SYS_PASSWORD"
log "  sudo systemctl daemon-reload && sudo systemctl enable --now fsfo-observer"
log ""
log "Starting Observer NOW in the foreground for diagnostics. Press Ctrl-C to stop;"
log "production runs use the systemd unit you just wrote (which restarts automatically)."
${DGMGRL} -silent sys/${SYS_PASSWORD}@${PRIMARY_TNS} <<EOF
START OBSERVER FILE='/var/log/oracle/fsfo.dat' LOGFILE='/var/log/oracle/fsfo.log';
EOF
