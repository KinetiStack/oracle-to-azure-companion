--------------------------------------------------------------------------------
-- PL/pgSQL refactor of Oracle's HRPRO.PKG_PAYROLL_RUN.
--
-- Target  : Azure Database for PostgreSQL Flexible Server (PG 16)
-- Schema  : hrpro
--
-- PG 11+ procedures (vs. functions) support COMMIT and ROLLBACK inside the
-- body. We use a procedure for the main batch and a function for the
-- logger; the logger is invoked from the procedure with PERFORM so it
-- behaves like a fire-and-forget call.
--
-- Cross-engine architectural note (see Ch.5 § 5.2):
-- True PRAGMA AUTONOMOUS_TRANSACTION semantics in PG require dblink to
-- spawn a separate connection that commits independently. We do not use
-- dblink here; the procedure structure (COMMIT after each logical step)
-- means usp_payroll_log writes are committed at the next COMMIT in the
-- caller -- close enough for most audit use cases. For tamper-evident
-- audit (where logs MUST survive ROLLBACK), externalize logging to the
-- application tier per § 5.2.
--------------------------------------------------------------------------------

SET search_path = hrpro, public;

------------------------------------------------------------------------
-- Logger as a function (no transaction control needed)
------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION hrpro.usp_payroll_log(
    p_run_id  BIGINT,
    p_level   VARCHAR,
    p_message VARCHAR
) RETURNS VOID
LANGUAGE plpgsql
AS $func$
BEGIN
    INSERT INTO hrpro.payroll_run_log (log_id, run_id, log_level, message)
    VALUES (nextval('hrpro.seq_payroll_log'), p_run_id, p_level, p_message);
END;
$func$;

------------------------------------------------------------------------
-- Main payroll batch as a procedure (PG 11+ supports transaction control)
------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE hrpro.usp_payroll_run(p_run_date DATE)
LANGUAGE plpgsql
AS $proc$
DECLARE
    v_run_id       BIGINT := nextval('hrpro.seq_payroll_run');
    v_emp_count    INTEGER := 0;
    v_total_gross  NUMERIC(14, 2) := 0;
    v_sql          TEXT;
    v_failed       BOOLEAN := FALSE;
    v_err_message  TEXT;
BEGIN
    -- Step 1: commit the run header so the logger writes survive any
    -- subsequent rollback.
    INSERT INTO hrpro.payroll_run (run_id, run_date, status, started_at)
    VALUES (v_run_id, p_run_date, 'RUNNING', clock_timestamp());
    COMMIT;

    PERFORM hrpro.usp_payroll_log(v_run_id, 'INFO',
        format('Payroll run started for %s', to_char(p_run_date, 'YYYY-MM-DD')));

    -- Step 2: main work in an inner BEGIN/EXCEPTION block.
    -- PostgreSQL gives this block subtransaction semantics: an exception
    -- inside it implicitly rolls back ONLY this block's writes. Critically,
    -- COMMIT and ROLLBACK are NOT permitted inside an EXCEPTION-bearing
    -- block (PG rejects with "cannot commit while a subtransaction is
    -- active"); transaction control is hoisted to the outer block via the
    -- v_failed flag below.
    BEGIN
        -- Set-based aggregation, no cursor.
        SELECT COUNT(*), COALESCE(SUM(eh.salary), 0)
          INTO v_emp_count, v_total_gross
          FROM hrpro.employee AS e
          LEFT JOIN hrpro.employee_history AS eh
                 ON eh.emp_id   = e.emp_id
                AND eh.end_date IS NULL
         WHERE e.hire_date <= p_run_date;

        -- File emission externalized to files_to_emit queue.
        INSERT INTO hrpro.files_to_emit (run_id, file_name, payload)
        VALUES (
            v_run_id,
            format('payroll_%s.txt', v_run_id),
            format(E'run_id,%s\nemp_count,%s\ntotal_gross,%s\n',
                   v_run_id, v_emp_count, v_total_gross)
        );

        -- Parameterized dynamic SQL via EXECUTE ... USING.
        -- format() handles safe identifier quoting via %I; %L for literals.
        v_sql := format('SELECT 1 WHERE %L::bigint = $1', v_run_id);
        EXECUTE v_sql USING v_run_id;

        UPDATE hrpro.payroll_run
           SET status       = 'COMPLETED',
               completed_at = clock_timestamp(),
               emp_count    = v_emp_count,
               total_gross  = v_total_gross
         WHERE run_id = v_run_id;
    EXCEPTION
        WHEN OTHERS THEN
            -- PG auto-rolled back this block's writes. Capture the message
            -- and route to the failure path in the outer block.
            v_failed      := TRUE;
            v_err_message := SQLERRM;
    END;

    -- Step 3: commit/log success OR commit/log failure, but outside the
    -- subtransaction so COMMIT is legal.
    IF v_failed THEN
        UPDATE hrpro.payroll_run
           SET status       = 'FAILED',
               completed_at = clock_timestamp()
         WHERE run_id = v_run_id;
        PERFORM hrpro.usp_payroll_log(v_run_id, 'ERROR',
            format('Payroll failed: %s', v_err_message));
        COMMIT;
        RAISE EXCEPTION 'Payroll % failed: %', v_run_id, v_err_message;
    ELSE
        COMMIT;
        PERFORM hrpro.usp_payroll_log(v_run_id, 'INFO',
            format('Payroll completed: %s employees, gross=%s',
                   v_emp_count, v_total_gross));
    END IF;
END;
$proc$;
