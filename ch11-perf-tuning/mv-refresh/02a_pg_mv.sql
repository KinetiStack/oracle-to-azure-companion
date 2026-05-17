--------------------------------------------------------------------------------
-- mv-refresh/02a_pg_mv.sql  --  Materialized view for HR-Pro on PG Flex.
--
-- Run against the APPLICATION database (labdb), e.g.:
--     psql -h <pg-flex-host> -U labadmin -d labdb --set=sslmode=require \
--          -v ON_ERROR_STOP=1 -f 02a_pg_mv.sql
--
-- See 02b_pg_cron_schedule.sql for the schedule -- it lives in a separate
-- file because pg_cron's catalog resides in the `postgres` database on
-- Azure PG Flex, NOT in labdb. The split keeps both files runnable via any
-- programmatic driver (psycopg, JDBC, az CLI) without psql-only directives.
--------------------------------------------------------------------------------

SET search_path = hrpro, public;

-- CONCURRENTLY refresh requires a UNIQUE index on the MV, so we create
-- that first.
DROP MATERIALIZED VIEW IF EXISTS hrpro.mv_headcount_rollup;
CREATE MATERIALIZED VIEW hrpro.mv_headcount_rollup AS
SELECT  d.dept_code,
        d.dept_name,
        COUNT(*)::INTEGER                         AS emp_count,
        AVG(eh.salary)::NUMERIC(14, 2)            AS avg_salary,
        clock_timestamp()                         AS refreshed_at
FROM    hrpro.employee   e
JOIN    hrpro.department d ON d.dept_id = e.dept_id
LEFT JOIN hrpro.employee_history eh
          ON eh.emp_id = e.emp_id AND eh.end_date IS NULL
GROUP BY d.dept_code, d.dept_name;

CREATE UNIQUE INDEX ux_mv_headcount_rollup_dept
    ON hrpro.mv_headcount_rollup(dept_code);

-- Verify with:  SELECT * FROM hrpro.mv_headcount_rollup;
-- Manually refresh via: REFRESH MATERIALIZED VIEW CONCURRENTLY hrpro.mv_headcount_rollup;
