--------------------------------------------------------------------------------
-- mv-refresh/02b_pg_cron_schedule.sql  --  pg_cron schedule for the MV refresh.
--
-- Run against the `postgres` MAINTENANCE database (NOT labdb), e.g.:
--     psql -h <pg-flex-host> -U labadmin -d postgres --set=sslmode=require \
--          -v ON_ERROR_STOP=1 -f 02b_pg_cron_schedule.sql
--
-- This file is split from 02a_pg_mv.sql because pg_cron's catalog
-- (cron.job, cron.job_run_details) lives in the `postgres` database on
-- Azure PG Flex. The earlier monolithic file used `\c postgres` (a psql
-- meta-command) to switch -- but that breaks under any non-psql driver
-- (psycopg, JDBC, az CLI). The two-file split is portable across drivers.
--
-- Pre-requisite (one-time, via Azure portal or CLI):
--     az postgres flexible-server parameter set \
--         --resource-group <rg> --server-name <name> \
--         --name azure.extensions --value pg_cron,pgaudit
--
-- Then in psql to confirm:  SHOW azure.extensions;
--------------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Every 15 minutes, refresh the MV. The function targets a different DB
-- (labdb) than where pg_cron's catalog lives (postgres) -- pg_cron's
-- cron.schedule_in_database() makes the target explicit.
SELECT cron.schedule_in_database(
    job_name => 'refresh_headcount_rollup',
    schedule => '*/15 * * * *',
    command  => 'REFRESH MATERIALIZED VIEW CONCURRENTLY hrpro.mv_headcount_rollup',
    database => 'labdb'
);

-- Verify with:
--   SELECT * FROM cron.job;
--   SELECT * FROM cron.job_run_details ORDER BY start_time DESC LIMIT 10;
