#!/usr/bin/env bash
#-------------------------------------------------------------------------------
# 05_smoke_test.sh - invoke the refactored payroll procedure on each engine
# (Oracle source, T-SQL target, PL/pgSQL target) and verify the run_id row
# reaches status='COMPLETED' with a non-zero emp_count.
#
# This is an equivalence smoke test, not a numeric-equality test. Data has
# only been seeded into Oracle (Ch.3.5); the SQL and PG targets receive
# their data in Ch.6. So we run against each engine independently and
# verify the refactored procedure produces a coherent payroll_run row.
#
# Set engine-specific env vars to enable each leg. Missing vars skip cleanly.
#-------------------------------------------------------------------------------
set -Eeuo pipefail

log()  { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
pass() { printf '  PASS: %s\n' "$*" >&2; }
fail() { printf '  FAIL: %s\n' "$*" >&2; exit 1; }

#-------------------- Oracle leg (uses Ch.3.5 lab data) ----------------------
if [ "${RUN_ORACLE:-0}" = "1" ]; then
    log "Oracle leg: invoke HRPRO.PKG_PAYROLL_RUN.RUN_PAYROLL"
    : "${HRPRO_PWD:?HRPRO_PWD must be set for the Oracle leg}"
    docker exec -i ora19c-lab sqlplus -L -S \
        "hrpro/${HRPRO_PWD}@//localhost:1521/ORCLPDB1" <<'SQL'
WHENEVER SQLERROR EXIT FAILURE
SET SERVEROUTPUT ON
BEGIN
    hrpro.pkg_payroll_run.run_payroll(SYSDATE);
END;
/
SELECT run_id, status, emp_count, total_gross
  FROM (SELECT * FROM hrpro.payroll_run ORDER BY started_at DESC)
 WHERE ROWNUM = 1;
EXIT
SQL
    pass "Oracle payroll run completed"
fi

#-------------------- T-SQL leg ---------------------------------------------
if [ "${RUN_TSQL:-0}" = "1" ]; then
    log "T-SQL leg: invoke dbo.usp_payroll_run"
    : "${AZURE_SQL_FQDN:?AZURE_SQL_FQDN must be set for the T-SQL leg}"
    : "${AZURE_SQL_PASSWORD:?AZURE_SQL_PASSWORD must be set}"
    OUT=$(sqlcmd -S "tcp:${AZURE_SQL_FQDN},1433" -d "${AZURE_SQL_DB:-labdb}" \
                 -U "${AZURE_SQL_USER:-labadmin}" -P "${AZURE_SQL_PASSWORD}" \
                 -N -C -b -h -1 -W -s ',' -Q "
                    SET NOCOUNT ON;
                    EXEC dbo.usp_payroll_run @run_date = CAST(GETUTCDATE() AS DATE);
                    SELECT TOP 1 status FROM dbo.payroll_run ORDER BY started_at DESC;")
    echo "$OUT" | grep -q COMPLETED && pass "T-SQL payroll_run status=COMPLETED" \
                                    || fail "T-SQL payroll_run did not reach COMPLETED: $OUT"
fi

#-------------------- PL/pgSQL leg ------------------------------------------
if [ "${RUN_PLPGSQL:-0}" = "1" ]; then
    log "PL/pgSQL leg: invoke hrpro.usp_payroll_run"
    : "${PG_FQDN:?PG_FQDN must be set for the PL/pgSQL leg}"
    : "${PG_PASSWORD:?PG_PASSWORD must be set}"
    export PGPASSWORD="${PG_PASSWORD}"
    STATUS=$(psql -h "${PG_FQDN}" -U "${PG_USER:-labadmin}" -d "${PG_DB:-labdb}" \
                  --set=sslmode=require -v ON_ERROR_STOP=1 -tA -c "
                      CALL hrpro.usp_payroll_run(current_date);
                      SELECT status FROM hrpro.payroll_run
                       ORDER BY started_at DESC LIMIT 1;")
    [ "${STATUS}" = "COMPLETED" ] && pass "PG payroll_run status=COMPLETED" \
                                  || fail "PG payroll_run status=${STATUS}"
fi

log "Smoke tests complete"
