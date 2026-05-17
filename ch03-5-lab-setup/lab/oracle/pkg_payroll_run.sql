--------------------------------------------------------------------------------
-- PKG_PAYROLL_RUN — realistic enterprise payroll batch
--
-- Demonstrates the PL/SQL constructs the book's anchor environment relies on:
--   - PRAGMA AUTONOMOUS_TRANSACTION for tamper-evident logging
--   - Explicit cursor with BULK COLLECT (batched fetch)
--   - Dynamic SQL via EXECUTE IMMEDIATE
--   - UTL_FILE dependency             -> Ch.1 blocker, Ch.5 refactor target
--   - DBMS_OUTPUT for trace
--   - Structured exception handling with rollback semantics
--
-- The package is intentionally compact (~110 LOC) yet exercises every Ch.5
-- refactor pattern; production packages will be 10-50x larger but follow
-- the same shape.
--------------------------------------------------------------------------------

ALTER SESSION SET CURRENT_SCHEMA = HRPRO;

CREATE OR REPLACE PACKAGE pkg_payroll_run AS
    PROCEDURE run_payroll(p_run_date IN DATE);
    PROCEDURE log_message(
        p_run_id   IN NUMBER,
        p_level    IN VARCHAR2,
        p_message  IN VARCHAR2
    );
END pkg_payroll_run;
/

CREATE OR REPLACE PACKAGE BODY pkg_payroll_run AS

    --------------------------------------------------------------------------
    -- Autonomous-transaction logger — log rows survive ROLLBACK of caller.
    --------------------------------------------------------------------------
    PROCEDURE log_message(
        p_run_id   IN NUMBER,
        p_level    IN VARCHAR2,
        p_message  IN VARCHAR2
    ) IS
        PRAGMA AUTONOMOUS_TRANSACTION;
    BEGIN
        INSERT INTO payroll_run_log(log_id, run_id, log_level, message)
        VALUES (seq_payroll_log.NEXTVAL, p_run_id, p_level, SUBSTR(p_message, 1, 4000));
        COMMIT;
    END log_message;

    --------------------------------------------------------------------------
    -- Main batch.
    --------------------------------------------------------------------------
    PROCEDURE run_payroll(p_run_date IN DATE) IS
        v_run_id         NUMBER;
        v_emp_count      NUMBER := 0;
        v_total_gross    NUMBER(14,2) := 0;
        v_sql            VARCHAR2(4000);
        v_file           UTL_FILE.FILE_TYPE;

        TYPE emp_rec IS RECORD (
            emp_id NUMBER,
            salary NUMBER(12,2)
        );
        TYPE emp_tab IS TABLE OF emp_rec INDEX BY PLS_INTEGER;
        l_emps emp_tab;

        CURSOR c_active_emps IS
            SELECT  e.emp_id,
                    NVL(eh.salary, 0) AS salary
            FROM    employee e
            LEFT JOIN employee_history eh
                   ON eh.emp_id   = e.emp_id
                  AND eh.end_date IS NULL
            WHERE   e.hire_date <= p_run_date;
    BEGIN
        v_run_id := seq_payroll_run.NEXTVAL;

        INSERT INTO payroll_run(run_id, run_date, status, started_at)
        VALUES (v_run_id, p_run_date, 'RUNNING', SYSTIMESTAMP);
        COMMIT;

        log_message(v_run_id, 'INFO',
                    'Payroll run started for ' || TO_CHAR(p_run_date, 'YYYY-MM-DD'));

        -- Batched fetch — production tunes LIMIT against PGA budget.
        OPEN c_active_emps;
        LOOP
            FETCH c_active_emps BULK COLLECT INTO l_emps LIMIT 500;
            EXIT WHEN l_emps.COUNT = 0;

            FOR i IN 1 .. l_emps.COUNT LOOP
                v_emp_count   := v_emp_count + 1;
                v_total_gross := v_total_gross + l_emps(i).salary;
            END LOOP;
        END LOOP;
        CLOSE c_active_emps;

        -- Dynamic SQL: production would dispatch to pkg_dept_proc.run_for_dept
        -- The placeholder is enough to make EXECUTE IMMEDIATE appear in Ch.1
        -- dependency analysis.
        v_sql := 'BEGIN NULL; END;';
        EXECUTE IMMEDIATE v_sql;

        -- UTL_FILE summary — Ch.1 blocker / Ch.5 refactor case
        BEGIN
            v_file := UTL_FILE.FOPEN('PAYROLL_DIR',
                                     'payroll_' || v_run_id || '.txt',
                                     'W');
            UTL_FILE.PUT_LINE(v_file, 'run_id,'      || v_run_id);
            UTL_FILE.PUT_LINE(v_file, 'emp_count,'   || v_emp_count);
            UTL_FILE.PUT_LINE(v_file, 'total_gross,' || v_total_gross);
            UTL_FILE.FCLOSE(v_file);
        EXCEPTION
            WHEN OTHERS THEN
                IF UTL_FILE.IS_OPEN(v_file) THEN
                    UTL_FILE.FCLOSE(v_file);
                END IF;
                log_message(v_run_id, 'WARN',
                            'UTL_FILE write failed: ' || SQLERRM);
        END;

        UPDATE payroll_run
           SET status       = 'COMPLETED',
               completed_at = SYSTIMESTAMP,
               emp_count    = v_emp_count,
               total_gross  = v_total_gross
         WHERE run_id = v_run_id;
        COMMIT;

        log_message(v_run_id, 'INFO',
                    'Payroll completed: '   || v_emp_count
                    || ' employees, gross=' || v_total_gross);
    EXCEPTION
        WHEN OTHERS THEN
            ROLLBACK;
            log_message(v_run_id, 'ERROR',
                        'Payroll failed: ' || SQLERRM);
            UPDATE payroll_run
               SET status       = 'FAILED',
                   completed_at = SYSTIMESTAMP
             WHERE run_id = v_run_id;
            COMMIT;
            RAISE;
    END run_payroll;

END pkg_payroll_run;
/
