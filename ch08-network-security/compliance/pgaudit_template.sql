--------------------------------------------------------------------------------
-- pgaudit_template.sql  --  pgAudit configuration for HR-Pro on Azure DB for
-- PostgreSQL Flexible Server.
--
-- *** SEMANTIC GAP NOTICE ***
-- pgAudit operates at the STATEMENT level. It cannot audit:
--   - Per-column reads (Oracle FGA's defining capability)
--   - Row-level predicates
--
-- If the source FGA policy was put in place to satisfy a PCI / HIPAA / SOX
-- control that names a specific column, pgAudit alone DOES NOT satisfy it.
-- Either:
--   (a) Add a trigger-based audit overlay on the relevant table (this file
--       includes a sample), or
--   (b) Move the column-level audit responsibility to the application layer
--       (Ch.8.5 wires the application changes).
--
-- The AMR emitted by 03_audit_translate.py flags the policies that fall
-- into this category as 'DEGRADED' or 'UNSUPPORTED'.
--------------------------------------------------------------------------------

-- (1) Enable the pgAudit extension. Azure DB for PostgreSQL Flex must have
--     pgaudit in the 'azure.extensions' server parameter list first; the
--     Bicep / Azure CLI step is documented in Ch.8 prose.
CREATE EXTENSION IF NOT EXISTS pgaudit;

-- (2) Server-wide pgAudit configuration. Tune via Azure CLI:
--     az postgres flexible-server parameter set --name pgaudit.log --value 'write,ddl' ...
-- Statement classes:
--   read   - SELECT, COPY (when source is a table)
--   write  - INSERT, UPDATE, DELETE, TRUNCATE
--   function - function and DO blocks
--   role   - GRANT, REVOKE, ROLE / USER
--   ddl    - CREATE, ALTER, DROP, COMMENT
--   misc   - SHOW, RESET, etc.

-- Example: pgAudit equivalent for source FGA_PII_SSN policy.
-- Fidelity: DEGRADED -- pgAudit logs every SELECT/UPDATE on the table; cannot
-- isolate to the SSN column specifically.
ALTER SYSTEM SET pgaudit.log = 'read, write';
ALTER SYSTEM SET pgaudit.log_catalog = 'off';     -- skip catalog queries (noise)
ALTER SYSTEM SET pgaudit.log_parameter = 'on';    -- include parameter values
ALTER SYSTEM SET pgaudit.log_relation = 'on';     -- include affected relations

-- (3) Trigger-based per-column audit overlay (for policies that the AMR
--     flags as DEGRADED and compliance has NOT accepted the reduction).
--     One overlay per audited column. The trigger writes to a dedicated
--     audit table that pgAudit's log stream does not duplicate.
CREATE TABLE IF NOT EXISTS hrpro.pii_audit (
    audit_id      BIGSERIAL PRIMARY KEY,
    audit_at      TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    db_user       TEXT        NOT NULL DEFAULT current_user,
    operation     TEXT        NOT NULL,                -- 'SELECT' | 'UPDATE'
    object_owner  TEXT        NOT NULL,
    object_name   TEXT        NOT NULL,
    column_name   TEXT        NOT NULL,
    row_pk        TEXT,
    client_addr   TEXT        DEFAULT inet_client_addr()::text
);

-- (Note: SELECT auditing via triggers requires a wrapper view + INSTEAD OF
-- trigger on PG; the lab shows the UPDATE-side trigger which is the simpler
-- case. The SELECT-side overlay is covered in Ch.8 prose.)
CREATE OR REPLACE FUNCTION hrpro.trg_pii_audit_update()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.ssn IS DISTINCT FROM OLD.ssn THEN
        INSERT INTO hrpro.pii_audit
            (operation, object_owner, object_name, column_name, row_pk)
        VALUES
            ('UPDATE', 'hrpro', TG_TABLE_NAME, 'ssn', NEW.emp_id::text);
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_pii_audit_employee_update
AFTER UPDATE OF ssn ON hrpro.employee
FOR EACH ROW
EXECUTE FUNCTION hrpro.trg_pii_audit_update();
