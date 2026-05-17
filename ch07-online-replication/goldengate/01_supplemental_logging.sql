--------------------------------------------------------------------------------
-- 01_supplemental_logging.sql
--
-- Enables Oracle supplemental logging so GoldenGate's LogMiner-based Extract
-- can reconstruct row-level changes from redo.
--
-- ***** RUN THIS BEFORE THE Ch.6 BULK EXPORT *****
-- If supplemental logging is enabled AFTER expdp, the gap from the bulk-seed
-- SCN to "supp-log-on" SCN cannot be replayed by GoldenGate and you have to
-- re-export the full schema. There is no graceful recovery.
--
-- This script enables:
--   1. Minimal supplemental logging (database-level) -- required for GG.
--   2. Force logging -- prevents NOLOGGING operations from creating redo gaps.
--   3. Per-table supplemental logging for primary keys + unique indexes on
--      the application schemas (HRPRO in the lab).
--
-- Redo overhead: 5-30% depending on workload. Ch.3 sizing must already
-- account for this; if it didn't, revisit redo_mbps in the BoM.
--------------------------------------------------------------------------------

-- Run as SYS or with ALTER SYSTEM + ALTER DATABASE privileges.
ALTER DATABASE ADD SUPPLEMENTAL LOG DATA;
ALTER DATABASE FORCE LOGGING;

-- Verify
SELECT supplemental_log_data_min, supplemental_log_data_pk, force_logging
  FROM v$database;
-- Expected: YES, YES (or IMPLICIT), YES

-- Per-schema enable. GG's ADD TRANDATA command performs the equivalent on
-- the application's behalf; this DDL is the explicit form.
BEGIN
    FOR rec IN (SELECT owner, table_name
                  FROM dba_tables
                 WHERE owner = 'HRPRO'
                   AND temporary = 'N'
                   AND iot_type IS NULL) LOOP
        EXECUTE IMMEDIATE
            'ALTER TABLE "' || rec.owner || '"."' || rec.table_name ||
            '" ADD SUPPLEMENTAL LOG DATA (PRIMARY KEY, UNIQUE INDEX) COLUMNS';
    END LOOP;
END;
/

-- For RAC sources: archive log mode + forced logging across all instances is required.
-- Verify with: SELECT log_mode FROM v$database;  -- expect ARCHIVELOG.
