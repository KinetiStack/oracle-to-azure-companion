--------------------------------------------------------------------------------
-- T-SQL refactor of Oracle's BEFORE UPDATE FOR EACH ROW trigger.
--
-- T-SQL has NO BEFORE-UPDATE row trigger. The Oracle pattern (mutate :NEW
-- before the row is written) does not exist on SQL Server / Azure SQL.
--
-- The portable T-SQL equivalent is AFTER UPDATE with a secondary UPDATE
-- against `inserted` (the virtual table of post-image rows). This costs a
-- second write per modified row, which on a high-write OLTP path is a real
-- consideration -- Ch.11 covers the perf trade-off.
--
-- Alternative (not used here): an INSTEAD OF UPDATE trigger on a view layered
-- over `employee`. Faster path but introduces a view dependency the
-- application must respect.
--------------------------------------------------------------------------------

CREATE OR ALTER TRIGGER dbo.trg_employee_audit
ON dbo.employee
AFTER UPDATE
AS
BEGIN
    SET NOCOUNT ON;

    -- Only stamp audit columns if some non-audit column actually changed.
    -- Without this guard, the trigger recurses on its own UPDATE.
    IF NOT (UPDATE(updated_at) OR UPDATE(updated_by))
    BEGIN
        UPDATE e
        SET   updated_at = SYSUTCDATETIME(),
              updated_by = SUSER_SNAME()
        FROM  dbo.employee AS e
        INNER JOIN inserted AS i ON i.emp_id = e.emp_id;
    END
END
GO
