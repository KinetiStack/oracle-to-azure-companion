--------------------------------------------------------------------------------
-- Ch.5 supporting DDL for the T-SQL refactor of pkg_payroll_run.
--
-- Target : Azure SQL Database / Azure SQL Managed Instance, schema dbo
-- Assumes: the Ch.4 CAR has been applied (the base HR-Pro tables exist on the
--          target). This file adds the three structures the refactored
--          procedures depend on but the converter could not produce:
--
--   1. dbo.seq_payroll_run                        -- run-id sequence (used by
--                                                   usp_payroll_run; NEXT VALUE FOR)
--   2. dbo.files_to_emit                          -- file-externalization queue
--   3. dbo.payroll_run_log                        -- log sink with IDENTITY log_id
--                                                   (no companion sequence needed;
--                                                   the PG variant uses a sequence
--                                                   for symmetry with PL/SQL but
--                                                   T-SQL prefers IDENTITY)
--------------------------------------------------------------------------------

IF NOT EXISTS (SELECT 1 FROM sys.sequences WHERE name = 'seq_payroll_run')
    CREATE SEQUENCE dbo.seq_payroll_run AS BIGINT START WITH 1 INCREMENT BY 1 CACHE 100;
GO

IF OBJECT_ID('dbo.files_to_emit', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.files_to_emit (
        emit_id      BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        run_id       BIGINT       NOT NULL,
        file_name    NVARCHAR(255) NOT NULL,
        payload      NVARCHAR(MAX) NOT NULL,
        enqueued_at  DATETIME2(3)  NOT NULL DEFAULT SYSUTCDATETIME(),
        status       VARCHAR(20)   NOT NULL DEFAULT 'PENDING',
        consumed_at  DATETIME2(3)  NULL
    );
    CREATE INDEX ix_files_to_emit_status ON dbo.files_to_emit(status, enqueued_at);
END
GO

IF OBJECT_ID('dbo.payroll_run_log', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.payroll_run_log (
        log_id     BIGINT IDENTITY(1,1) NOT NULL PRIMARY KEY,
        run_id     BIGINT       NOT NULL,
        log_at     DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
        log_level  VARCHAR(10)  NOT NULL,
        message    NVARCHAR(4000)
    );
    CREATE INDEX ix_prl_run ON dbo.payroll_run_log(run_id);
END
GO
