--------------------------------------------------------------------------------
-- PG trigger refactor.
--
-- PG supports BEFORE UPDATE FOR EACH ROW directly -- the source pattern
-- translates almost line-for-line. The only differences from Oracle:
--   - Triggers in PG are functions returning trigger, separately attached.
--   - SYSTIMESTAMP -> clock_timestamp().
--   - USER -> current_user.
--   - :NEW becomes NEW (no colon prefix).
--   - The function must RETURN NEW (or the modified row) for BEFORE triggers
--     to take effect; returning NULL silently aborts the row's update.
--------------------------------------------------------------------------------

SET search_path = hrpro, public;

CREATE OR REPLACE FUNCTION hrpro.fn_employee_audit()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $func$
BEGIN
    NEW.updated_at := clock_timestamp();
    NEW.updated_by := current_user;
    RETURN NEW;
END;
$func$;

DROP TRIGGER IF EXISTS trg_employee_audit ON hrpro.employee;

CREATE TRIGGER trg_employee_audit
BEFORE UPDATE ON hrpro.employee
FOR EACH ROW
EXECUTE FUNCTION hrpro.fn_employee_audit();
