--------------------------------------------------------------------------------
-- T-SQL refactor of Oracle's HRPRO.PKG_PAYROLL_RUN.
--
-- Target : Azure SQL Database / Azure SQL Managed Instance
-- Patterns refactored (see Ch.5 prose for each):
--   1. PRAGMA AUTONOMOUS_TRANSACTION  -> Service Broker / external sink note
--   2. UTL_FILE                       -> files_to_emit queue
--   3. EXECUTE IMMEDIATE              -> sp_executesql with parameter binding
--   4. Cursor + BULK COLLECT          -> set-based aggregate
--   5. Oracle exception block          -> TRY/CATCH with THROW
--
-- Sequences are referenced via NEXT VALUE FOR (cross-engine standard syntax).
--------------------------------------------------------------------------------

------------------------------------------------------------------------
-- Logger procedure.
--
-- Oracle's PRAGMA AUTONOMOUS_TRANSACTION ensures log rows survive the
-- caller's ROLLBACK. T-SQL has no direct equivalent inside the same
-- session. Three options for true autonomous semantics:
--
--   A. Service Broker (Azure SQL MI supports it): SEND ON CONVERSATION
--      to a queue that an activated procedure drains and writes to the
--      log table. The send is auto-committed independently.
--
--   B. External logging sink (Application Insights, Event Hub): the
--      app layer reads from a queue table and ships logs out-of-band.
--
--   C. Loopback OPENROWSET / sp_executesql at remote endpoint -- works
--      on MI but is an anti-pattern (introduces a second authentication
--      surface and operational complexity).
--
-- This procedure ships as a plain INSERT and relies on the architectural
-- recommendation in Ch.5 prose: structure the caller so it does NOT
-- hold an open transaction when usp_payroll_log is invoked. See
-- 01_pkg_payroll_run.sql's COMMIT TRANSACTION sequencing.
------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE dbo.usp_payroll_log
    @run_id    BIGINT,
    @level     VARCHAR(10),
    @message   NVARCHAR(4000)
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO dbo.payroll_run_log (run_id, log_level, message)
    VALUES (@run_id, @level, @message);
END
GO

------------------------------------------------------------------------
-- Main payroll batch.
------------------------------------------------------------------------
CREATE OR ALTER PROCEDURE dbo.usp_payroll_run
    @run_date DATE
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE @run_id      BIGINT         = NEXT VALUE FOR dbo.seq_payroll_run;
    DECLARE @emp_count   INT            = 0;
    DECLARE @total_gross DECIMAL(14, 2) = 0;
    DECLARE @run_date_iso NVARCHAR(10) = CONVERT(NVARCHAR(10), @run_date, 23);

    -- Step 1: commit the run header BEFORE the main work so subsequent
    -- usp_payroll_log calls write outside any user transaction.
    INSERT INTO dbo.payroll_run (run_id, run_date, status, started_at)
    VALUES (@run_id, @run_date, 'RUNNING', SYSUTCDATETIME());

    EXEC dbo.usp_payroll_log @run_id, 'INFO',
         N'Payroll run started for ' + @run_date_iso;

    BEGIN TRY
        BEGIN TRANSACTION;
            -- Step 2: set-based aggregation replaces the Oracle cursor +
            -- BULK COLLECT loop. One statement, one round-trip, set semantics.
            SELECT
                @emp_count   = COUNT(*),
                @total_gross = COALESCE(SUM(eh.salary), 0)
            FROM   dbo.employee AS e
            LEFT JOIN dbo.employee_history AS eh
                   ON eh.emp_id   = e.emp_id
                  AND eh.end_date IS NULL
            WHERE  e.hire_date <= @run_date;

            -- Step 3: file emission externalized to the queue table.
            -- An Azure Function (EventGrid/Service Bus trigger) drains
            -- files_to_emit and writes the actual blob to Azure Storage.
            INSERT INTO dbo.files_to_emit (run_id, file_name, payload)
            VALUES (
                @run_id,
                CONCAT(N'payroll_', @run_id, N'.txt'),
                CONCAT(
                    N'run_id,',      @run_id,      CHAR(10),
                    N'emp_count,',   @emp_count,   CHAR(10),
                    N'total_gross,', @total_gross, CHAR(10)
                )
            );

            -- Step 4: dynamic-SQL example, parameterized via sp_executesql.
            -- Production would dispatch to pkg_dept_proc.run_for_dept; the
            -- placeholder mirrors the source's EXECUTE IMMEDIATE pattern
            -- so the parameter-binding shape is visible in Ch.5 prose.
            DECLARE @sql NVARCHAR(MAX) = N'SELECT @out_run = @in_run;';
            DECLARE @out_run BIGINT;
            EXEC sp_executesql
                @sql,
                N'@in_run BIGINT, @out_run BIGINT OUTPUT',
                @in_run  = @run_id,
                @out_run = @out_run OUTPUT;

            UPDATE dbo.payroll_run
            SET   status       = 'COMPLETED',
                  completed_at = SYSUTCDATETIME(),
                  emp_count    = @emp_count,
                  total_gross  = @total_gross
            WHERE run_id = @run_id;
        COMMIT TRANSACTION;

        EXEC dbo.usp_payroll_log @run_id, 'INFO',
             CONCAT(N'Payroll completed: ', @emp_count,
                    N' employees, gross=', @total_gross);
    END TRY
    BEGIN CATCH
        DECLARE @err NVARCHAR(4000) = ERROR_MESSAGE();
        IF XACT_STATE() <> 0 ROLLBACK TRANSACTION;

        EXEC dbo.usp_payroll_log @run_id, 'ERROR',
             N'Payroll failed: ' + @err;

        UPDATE dbo.payroll_run
        SET    status       = 'FAILED',
               completed_at = SYSUTCDATETIME()
        WHERE  run_id = @run_id;

        THROW;
    END CATCH;
END
GO
