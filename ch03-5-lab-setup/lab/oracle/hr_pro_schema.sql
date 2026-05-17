--------------------------------------------------------------------------------
-- HR-Pro sample schema — anchor reference architecture (lab edition)
--
-- Target  : Oracle Database 19c Enterprise Edition (PDB: ORCLPDB1)
-- Schema  : HRPRO
-- Purpose : Reproduce the *shape* of the anchor production environment used
--           throughout the book so every code chapter has the same source
--           to act against. Volumes are scaled down (rows, partitions, LOC)
--           but every feature surface from Chapters 1-3 is represented:
--             - Object type (UDT)               -> Ch.3 blocker
--             - XMLType column                  -> Ch.3 blocker
--             - Range partitioning              -> Ch.1 schema_complexity
--             - Materialized view               -> Ch.3 blocker
--             - FGA policy on PII column        -> Ch.1 fga_policies
--             - Trigger (audit columns)         -> Ch.1 plsql_inventory
--             - Sequences                       -> Ch.1 plsql_inventory
--
-- Run as  : HRPRO (created by 01_load_oracle_source.sh)
--------------------------------------------------------------------------------

ALTER SESSION SET CURRENT_SCHEMA = HRPRO;

--------------------------------------------------------------------------------
-- UDT — appears in Ch.3 blocker inventory as 'User-defined object type'
--------------------------------------------------------------------------------
CREATE OR REPLACE TYPE address_t AS OBJECT (
    street       VARCHAR2(120),
    city         VARCHAR2(80),
    state_code   CHAR(2),
    postal_code  VARCHAR2(20),
    country      CHAR(3)
);
/

--------------------------------------------------------------------------------
-- Reference tables
--------------------------------------------------------------------------------
CREATE TABLE department (
    dept_id      NUMBER       NOT NULL PRIMARY KEY,
    dept_code    VARCHAR2(10) NOT NULL UNIQUE,
    dept_name    VARCHAR2(120),
    cost_center  VARCHAR2(20),
    created_at   TIMESTAMP    DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE TABLE job_grade (
    grade_id     NUMBER       NOT NULL PRIMARY KEY,
    grade_code   VARCHAR2(8)  NOT NULL UNIQUE,
    min_salary   NUMBER(12,2) NOT NULL,
    max_salary   NUMBER(12,2) NOT NULL
);

--------------------------------------------------------------------------------
-- EMPLOYEE — audit-tracked, with PII (SSN) and XMLType metadata
--------------------------------------------------------------------------------
CREATE TABLE employee (
    emp_id         NUMBER       NOT NULL PRIMARY KEY,
    emp_number     VARCHAR2(20) NOT NULL UNIQUE,
    first_name     VARCHAR2(80) NOT NULL,
    last_name      VARCHAR2(80) NOT NULL,
    ssn            VARCHAR2(11) NOT NULL,
    email          VARCHAR2(160),
    hire_date      DATE         NOT NULL,
    dept_id        NUMBER       REFERENCES department(dept_id),
    grade_id       NUMBER       REFERENCES job_grade(grade_id),
    home_address   address_t,
    employee_meta  XMLTYPE,
    created_at     TIMESTAMP    DEFAULT SYSTIMESTAMP NOT NULL,
    created_by     VARCHAR2(30) DEFAULT USER NOT NULL,
    updated_at     TIMESTAMP,
    updated_by     VARCHAR2(30)
);

CREATE INDEX ix_employee_dept ON employee(dept_id);
CREATE INDEX ix_employee_hire ON employee(hire_date);

--------------------------------------------------------------------------------
-- EMPLOYEE_HISTORY — range-partitioned by effective_date
-- Production: >1B rows. Lab: ~30k rows across 2022-2026 partitions.
--------------------------------------------------------------------------------
CREATE TABLE employee_history (
    history_id      NUMBER       NOT NULL,
    emp_id          NUMBER       NOT NULL,
    effective_date  DATE         NOT NULL,
    end_date        DATE,
    dept_id         NUMBER,
    grade_id        NUMBER,
    salary          NUMBER(12,2),
    change_reason   VARCHAR2(80)
)
PARTITION BY RANGE (effective_date) (
    PARTITION p_2022   VALUES LESS THAN (TO_DATE('2023-01-01','YYYY-MM-DD')),
    PARTITION p_2023   VALUES LESS THAN (TO_DATE('2024-01-01','YYYY-MM-DD')),
    PARTITION p_2024   VALUES LESS THAN (TO_DATE('2025-01-01','YYYY-MM-DD')),
    PARTITION p_2025   VALUES LESS THAN (TO_DATE('2026-01-01','YYYY-MM-DD')),
    PARTITION p_2026   VALUES LESS THAN (TO_DATE('2027-01-01','YYYY-MM-DD')),
    PARTITION p_future VALUES LESS THAN (MAXVALUE)
);

CREATE INDEX ix_eh_emp ON employee_history(emp_id) LOCAL;

--------------------------------------------------------------------------------
-- PAYROLL tables — driven by pkg_payroll_run
--------------------------------------------------------------------------------
CREATE TABLE payroll_run (
    run_id        NUMBER       NOT NULL PRIMARY KEY,
    run_date      DATE         NOT NULL,
    status        VARCHAR2(20) NOT NULL,
    started_at    TIMESTAMP,
    completed_at  TIMESTAMP,
    emp_count     NUMBER,
    total_gross   NUMBER(14,2)
);

CREATE TABLE payroll_run_log (
    log_id     NUMBER       NOT NULL PRIMARY KEY,
    run_id     NUMBER       NOT NULL,
    log_at     TIMESTAMP    DEFAULT SYSTIMESTAMP NOT NULL,
    log_level  VARCHAR2(10) NOT NULL,
    message    VARCHAR2(4000)
);

CREATE INDEX ix_prl_run ON payroll_run_log(run_id);

--------------------------------------------------------------------------------
-- Sequences
--------------------------------------------------------------------------------
CREATE SEQUENCE seq_employee     START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE seq_emp_history  START WITH 1 INCREMENT BY 1 CACHE 1000;
CREATE SEQUENCE seq_payroll_run  START WITH 1 INCREMENT BY 1 CACHE 100;
CREATE SEQUENCE seq_payroll_log  START WITH 1 INCREMENT BY 1 CACHE 1000;

--------------------------------------------------------------------------------
-- Audit trigger — fires on every UPDATE; touches updated_at / updated_by
--------------------------------------------------------------------------------
CREATE OR REPLACE TRIGGER trg_employee_audit
BEFORE UPDATE ON employee
FOR EACH ROW
BEGIN
    :NEW.updated_at := SYSTIMESTAMP;
    :NEW.updated_by := USER;
END;
/

--------------------------------------------------------------------------------
-- Materialized view — Ch.3 blocker, regular complete-refresh
--------------------------------------------------------------------------------
CREATE MATERIALIZED VIEW mv_headcount_rollup
BUILD IMMEDIATE
REFRESH COMPLETE ON DEMAND
AS
SELECT  d.dept_code,
        d.dept_name,
        COUNT(*)        AS emp_count,
        AVG(eh.salary)  AS avg_salary
FROM    employee e
JOIN    department d ON d.dept_id = e.dept_id
LEFT JOIN employee_history eh
       ON eh.emp_id   = e.emp_id
      AND eh.end_date IS NULL
GROUP BY d.dept_code, d.dept_name;

--------------------------------------------------------------------------------
-- Fine-Grained Audit policy on the PII column.
-- Appears in Ch.1 fga_policies.csv and Ch.8 compliance-migration scope.
--------------------------------------------------------------------------------
BEGIN
    DBMS_FGA.ADD_POLICY(
        object_schema   => 'HRPRO',
        object_name     => 'EMPLOYEE',
        policy_name     => 'FGA_PII_SSN',
        audit_column    => 'SSN',
        statement_types => 'SELECT,UPDATE',
        enable          => TRUE
    );
END;
/
