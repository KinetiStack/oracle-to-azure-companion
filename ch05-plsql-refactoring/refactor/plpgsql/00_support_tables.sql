--------------------------------------------------------------------------------
-- Ch.5 supporting DDL for the PL/pgSQL refactor of pkg_payroll_run.
--
-- Target : Azure Database for PostgreSQL Flexible Server, schema hrpro
-- Assumes: Ora2Pg output from Ch.4 has been applied (base HR-Pro tables exist).
-- Adds the same three structures as the T-SQL companion file:
--   1. hrpro.seq_payroll_run / hrpro.seq_payroll_log
--   2. hrpro.files_to_emit       -- file-externalization queue
--   3. hrpro.payroll_run_log     -- log sink
--------------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS hrpro;
SET search_path = hrpro, public;

CREATE SEQUENCE IF NOT EXISTS hrpro.seq_payroll_run AS BIGINT START WITH 1 INCREMENT BY 1 CACHE 100;
CREATE SEQUENCE IF NOT EXISTS hrpro.seq_payroll_log AS BIGINT START WITH 1 INCREMENT BY 1 CACHE 1000;

CREATE TABLE IF NOT EXISTS hrpro.files_to_emit (
    emit_id      BIGSERIAL PRIMARY KEY,
    run_id       BIGINT       NOT NULL,
    file_name    VARCHAR(255) NOT NULL,
    payload      TEXT         NOT NULL,
    enqueued_at  TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
    status       VARCHAR(20)  NOT NULL DEFAULT 'PENDING',
    consumed_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS ix_files_to_emit_status
    ON hrpro.files_to_emit(status, enqueued_at);

CREATE TABLE IF NOT EXISTS hrpro.payroll_run_log (
    log_id     BIGINT       NOT NULL PRIMARY KEY,
    run_id     BIGINT       NOT NULL,
    log_at     TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
    log_level  VARCHAR(10)  NOT NULL,
    message    VARCHAR(4000)
);

CREATE INDEX IF NOT EXISTS ix_prl_run ON hrpro.payroll_run_log(run_id);
