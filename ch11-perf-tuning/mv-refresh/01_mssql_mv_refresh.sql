--------------------------------------------------------------------------------
-- mv-refresh/01_mssql_mv_refresh.sql
--
-- The MV_HEADCOUNT_ROLLUP replacement on Azure SQL MI. SQL Server has no
-- direct equivalent of Oracle's fast-refresh MV; indexed views exist but
-- have severe restrictions on what aggregations + joins they support.
-- The book's pattern: a scheduled stored procedure rebuilds a regular
-- table on a cadence the BI tool can tolerate.
--
-- Schedule via Azure SQL Database Elastic Jobs OR Azure Automation; this
-- file ships the SQL, not the scheduler. Production engagements wire the
-- Elastic Job pointing at this procedure on a 15-minute or hourly cadence
-- depending on freshness needs.
--------------------------------------------------------------------------------

-- The persistent cache table. NOT an indexed view -- regular heap +
-- clustered index. Recreating in-place keeps the BI tool's queries simple.
IF OBJECT_ID('dbo.mv_headcount_rollup', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.mv_headcount_rollup (
        dept_code   VARCHAR(10) NOT NULL,
        dept_name   NVARCHAR(120),
        emp_count   INT          NOT NULL,
        avg_salary  DECIMAL(14, 2),
        refreshed_at DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT pk_mv_headcount_rollup PRIMARY KEY CLUSTERED (dept_code)
    );
END
GO

-- Refresh procedure. A transaction wraps DELETE + INSERT so the BI tool
-- never sees a partial state. The procedure replays the source MV's
-- definition (see Ch.3.5 hr_pro_schema.sql).
CREATE OR ALTER PROCEDURE dbo.usp_refresh_headcount_rollup
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    BEGIN TRANSACTION;

    -- TRUNCATE is faster than DELETE for full-rebuild MVs and resets identity,
    -- but it cannot be combined with the same row's UPDATE in one tx on MI's
    -- default isolation; DELETE + INSERT keeps the lock surface predictable.
    DELETE FROM dbo.mv_headcount_rollup;

    INSERT INTO dbo.mv_headcount_rollup (dept_code, dept_name, emp_count, avg_salary)
    SELECT  d.dept_code,
            d.dept_name,
            COUNT(*)                          AS emp_count,
            CAST(AVG(eh.salary) AS DECIMAL(14, 2)) AS avg_salary
    FROM    dbo.employee   AS e
    JOIN    dbo.department AS d ON d.dept_id = e.dept_id
    LEFT JOIN dbo.employee_history AS eh
              ON eh.emp_id = e.emp_id AND eh.end_date IS NULL
    GROUP BY d.dept_code, d.dept_name;

    COMMIT TRANSACTION;
END
GO

-- Verify with: EXEC dbo.usp_refresh_headcount_rollup;
--              SELECT * FROM dbo.mv_headcount_rollup;
-- Schedule via Elastic Jobs (recommended) or Azure Automation runbook.
