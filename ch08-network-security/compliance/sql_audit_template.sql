--------------------------------------------------------------------------------
-- sql_audit_template.sql  --  Azure SQL Audit setup for HR-Pro on Azure SQL DB / MI.
--
-- This file is the reference shape. 03_audit_translate.py reads Ch.1's
-- fga_policies.csv and emits a customized version of this template (one
-- DATABASE AUDIT SPECIFICATION per source FGA policy).
--
-- For Azure SQL Database: the SERVER AUDIT lives at the logical server level
-- and writes to a Storage Account, Event Hub, or Log Analytics workspace.
-- For Azure SQL MI: same, with the option of writing to a file path on the
-- instance's storage.
--
-- The book uses Log Analytics for downstream querying via KQL (Ch.11).
--------------------------------------------------------------------------------

-- (1) SERVER AUDIT -- created once per logical server / MI instance.
--
-- Target-type choice depends on the environment:
--   * Azure SQL Managed Instance -> TO FILE (uses MI's managed storage path
--     mounted at the instance level).
--   * Azure SQL Database         -> CONFIGURED VIA PORTAL / az CLI / Bicep
--     auditingPolicies on the logical server. The T-SQL form below is NOT
--     used on Azure SQL DB; comment it out and use Azure-side configuration
--     instead (see chapter prose § 8.6.3).
--   * SQL Server on IaaS / on-prem -> TO FILE with a filesystem path you
--     control.
--
-- The lab uses the MI form (TO FILE). Replace the path / sizes for your
-- environment.
USE master;
GO

CREATE SERVER AUDIT [HRPro_Audit]
TO FILE (
    FILEPATH            = '/var/opt/mssql/audit/',
    MAXSIZE             = 100 MB,
    MAX_ROLLOVER_FILES  = 50,
    RESERVE_DISK_SPACE  = OFF
)
WITH (
    QUEUE_DELAY = 1000,
    ON_FAILURE  = FAIL_OPERATION       -- audit failure halts the operation;
                                       -- alternative is CONTINUE for availability over compliance.
);
GO

ALTER SERVER AUDIT [HRPro_Audit] WITH (STATE = ON);
GO

-- (2) DATABASE AUDIT SPECIFICATION -- per source FGA policy, scoped to the
--     converted target object. Translator emits one of these blocks per policy.
USE [labdb];
GO

-- Example: translation of source policy FGA_PII_SSN on HRPRO.EMPLOYEE.SSN
-- Fidelity: DEGRADED (column-level reduced to object-level; see AMR)
CREATE DATABASE AUDIT SPECIFICATION [HRPro_Audit_FGA_PII_SSN]
FOR SERVER AUDIT [HRPro_Audit]
ADD (SELECT ON OBJECT::dbo.EMPLOYEE BY public),
ADD (UPDATE ON OBJECT::dbo.EMPLOYEE BY public)
WITH (STATE = ON);
GO

-- Verification query for 04_audit_validate.py to call against the target.
-- SELECT name, audit_action_name, is_state_enabled
--   FROM sys.database_audit_specification_details
--  WHERE audit_specification_id = OBJECT_ID('HRPro_Audit_FGA_PII_SSN');
