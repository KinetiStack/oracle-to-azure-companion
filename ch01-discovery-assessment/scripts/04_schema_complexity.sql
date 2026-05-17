--------------------------------------------------------------------------------
-- File         : 04_schema_complexity.sql
-- Purpose      : Quantify PL/SQL surface area, partitioning topology, and FGA
--                policy inventory so Chapters 5, 7, and 8 can scope effort.
-- Target       : Oracle Database 19c
-- Privileges   : SELECT_CATALOG_ROLE. Read-only.
-- License pack : None required.
-- Output       : 4 CSVs under &OUT_DIR/schema_complexity/
-- Idempotent   : Yes.
--------------------------------------------------------------------------------

SET ECHO OFF FEEDBACK OFF VERIFY OFF HEADING ON PAGESIZE 50000 LINESIZE 32767 TRIMSPOOL ON TERMOUT OFF NEWPAGE NONE
SET MARKUP CSV ON DELIMITER ',' QUOTE ON

DEFINE OUT_DIR = '&1'

-- The same exclusion list as 03_blocker_detection.sql, defined as a view-style CTE
-- inline because each SPOOL block needs its own statement.

--==============================================================================
-- (a) PL/SQL inventory: counts and total LOC by owner+type
--==============================================================================
SPOOL &OUT_DIR/schema_complexity/plsql_inventory.csv

WITH app_owners AS (
    SELECT DISTINCT username AS owner
    FROM   dba_users
    WHERE  oracle_maintained = 'N'
      AND  username NOT IN ('AUDSYS','GSMADMIN_INTERNAL','REMOTE_SCHEDULER_AGENT')
),
src AS (
    SELECT s.owner, s.type, s.name, COUNT(*) AS loc
    FROM   dba_source s
    WHERE  s.owner IN (SELECT owner FROM app_owners)
      AND  s.type IN ('PACKAGE','PACKAGE BODY','PROCEDURE','FUNCTION','TRIGGER','TYPE','TYPE BODY')
    GROUP  BY s.owner, s.type, s.name
)
SELECT
    owner,
    type            AS object_type,
    COUNT(*)        AS object_count,
    SUM(loc)        AS total_loc,
    ROUND(AVG(loc)) AS avg_loc,
    MAX(loc)        AS max_loc
FROM   src
GROUP  BY owner, type
ORDER  BY owner, type;

SPOOL OFF

--==============================================================================
-- (b) PL/SQL hotspots: top objects by LOC + dependency fan-in
--==============================================================================
SPOOL &OUT_DIR/schema_complexity/plsql_hotspots.csv

WITH app_owners AS (
    SELECT DISTINCT username AS owner
    FROM   dba_users
    WHERE  oracle_maintained = 'N'
),
loc AS (
    SELECT owner, type, name, COUNT(*) AS lines_of_code
    FROM   dba_source
    WHERE  owner IN (SELECT owner FROM app_owners)
      AND  type IN ('PACKAGE BODY','PROCEDURE','FUNCTION','TRIGGER','TYPE BODY')
    GROUP  BY owner, type, name
),
fanin AS (
    SELECT referenced_owner AS owner, referenced_type AS type, referenced_name AS name,
           COUNT(DISTINCT owner || '.' || name) AS dependents
    FROM   dba_dependencies
    GROUP  BY referenced_owner, referenced_type, referenced_name
),
joined AS (
    SELECT l.owner, l.type, l.name, l.lines_of_code,
           NVL(f.dependents, 0) AS dependents,
           l.lines_of_code + NVL(f.dependents, 0) * 50 AS hotspot_score
    FROM   loc l
    LEFT   JOIN fanin f
           ON f.owner = l.owner AND f.name = l.name
              AND f.type = DECODE(l.type, 'PACKAGE BODY','PACKAGE','TYPE BODY','TYPE', l.type)
)
SELECT *
FROM (
    SELECT owner, type, name, lines_of_code, dependents, hotspot_score
    FROM   joined
    ORDER  BY hotspot_score DESC NULLS LAST
)
WHERE ROWNUM <= 50;

SPOOL OFF

--==============================================================================
-- (c) Partition topology: strategy and partition count per table
--==============================================================================
SPOOL &OUT_DIR/schema_complexity/partition_topology.csv

WITH app_owners AS (
    SELECT DISTINCT username AS owner
    FROM   dba_users
    WHERE  oracle_maintained = 'N'
)
SELECT
    p.owner,
    p.table_name,
    p.partitioning_type,
    p.subpartitioning_type,
    p.partition_count,
    NVL(s.subpartition_total, 0)     AS subpartition_total,
    p.def_subpartition_count,
    p.interval                       AS interval_clause,
    p.ref_ptn_constraint_name        AS reference_partitioned_by,
    -- num_rows from DBA_TAB_STATISTICS. NULL when stats haven't been
    -- gathered; Ch.9's orr.py treats NULL as "unknown" and emits a warning
    -- rather than fabricating a row count.
    t.num_rows                       AS num_rows,
    t.last_analyzed                  AS stats_last_analyzed
FROM   dba_part_tables p
LEFT   JOIN (
    SELECT table_owner AS owner, table_name, SUM(1) AS subpartition_total
    FROM   dba_tab_subpartitions
    GROUP  BY table_owner, table_name
) s ON s.owner = p.owner AND s.table_name = p.table_name
LEFT   JOIN dba_tables t
       ON t.owner = p.owner AND t.table_name = p.table_name
WHERE  p.owner IN (SELECT owner FROM app_owners)
ORDER  BY p.owner, p.partition_count DESC;

SPOOL OFF

--==============================================================================
-- (d) FGA policy inventory (Fine-Grained Audit + Unified Audit)
--==============================================================================
SPOOL &OUT_DIR/schema_complexity/fga_policies.csv

WITH app_owners AS (
    SELECT DISTINCT username AS owner
    FROM   dba_users
    WHERE  oracle_maintained = 'N'
)
SELECT
    'FGA' AS source_type,
    p.object_schema AS object_owner,
    p.object_name,
    p.policy_name,
    p.policy_column,
    p.enable,
    p.statement_types,
    p.audit_trail
FROM   dba_audit_policies p
WHERE  p.object_schema IN (SELECT owner FROM app_owners)

UNION ALL

SELECT
    'UNIFIED' AS source_type,
    NVL(uap.object_schema, '<global>') AS object_owner,
    NVL(uap.object_name,   '<global>') AS object_name,
    uap.policy_name,
    NULL                               AS policy_column,
    CASE WHEN ue.enabled_option IS NOT NULL THEN 'YES' ELSE 'NO' END AS enable,
    uap.audit_option                   AS statement_types,
    NULL                               AS audit_trail
FROM   audit_unified_policies uap
LEFT   JOIN audit_unified_enabled_policies ue
       ON ue.policy_name = uap.policy_name
WHERE  uap.policy_name NOT LIKE 'ORA_%'           -- exclude built-ins
ORDER  BY 1, 2, 3;

SPOOL OFF
SET MARKUP CSV OFF
SET TERMOUT ON
EXIT SUCCESS
