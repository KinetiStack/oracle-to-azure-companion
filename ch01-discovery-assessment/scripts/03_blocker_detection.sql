--------------------------------------------------------------------------------
-- File         : 03_blocker_detection.sql
-- Purpose      : Detect known migration blockers and emit a single inventory
--                CSV: blocker_name, object_count, sample_objects, band.
-- Target       : Oracle Database 19c
-- Privileges   : SELECT_CATALOG_ROLE.
-- License pack : None required.
-- Output       : &OUT_DIR/blockers/blockers_inventory.csv
-- Idempotent   : Yes.
--
-- Remediation bands:
--   LOW    - portable with config flag or driver swap
--   MEDIUM - code refactor required, no architectural change
--   HIGH   - architectural change required (out-of-DB, re-platform)
--------------------------------------------------------------------------------

SET ECHO OFF FEEDBACK OFF VERIFY OFF HEADING ON PAGESIZE 50000 LINESIZE 32767 TRIMSPOOL ON TERMOUT OFF NEWPAGE NONE
SET MARKUP CSV ON DELIMITER ',' QUOTE ON

DEFINE OUT_DIR = '&1'

SPOOL &OUT_DIR/blockers/blockers_inventory.csv

WITH excluded_owners AS (
    SELECT owner_name FROM (
        SELECT 'SYS' owner_name FROM DUAL UNION ALL
        SELECT 'SYSTEM'         FROM DUAL UNION ALL
        SELECT 'OUTLN'          FROM DUAL UNION ALL
        SELECT 'DBSNMP'         FROM DUAL UNION ALL
        SELECT 'APPQOSSYS'      FROM DUAL UNION ALL
        SELECT 'AUDSYS'         FROM DUAL UNION ALL
        SELECT 'GSMADMIN_INTERNAL' FROM DUAL UNION ALL
        SELECT 'GSMUSER'        FROM DUAL UNION ALL
        SELECT 'GSMCATUSER'     FROM DUAL UNION ALL
        SELECT 'DBSFWUSER'      FROM DUAL UNION ALL
        SELECT 'REMOTE_SCHEDULER_AGENT' FROM DUAL UNION ALL
        SELECT 'SYSBACKUP'      FROM DUAL UNION ALL
        SELECT 'SYSDG'          FROM DUAL UNION ALL
        SELECT 'SYSKM'          FROM DUAL UNION ALL
        SELECT 'SYSRAC'         FROM DUAL UNION ALL
        SELECT 'XS$NULL'        FROM DUAL UNION ALL
        SELECT 'OJVMSYS'        FROM DUAL UNION ALL
        SELECT 'XDB'            FROM DUAL UNION ALL
        SELECT 'WMSYS'          FROM DUAL UNION ALL
        SELECT 'CTXSYS'         FROM DUAL UNION ALL
        SELECT 'ORDSYS'         FROM DUAL UNION ALL
        SELECT 'ORDDATA'        FROM DUAL UNION ALL
        SELECT 'ORDPLUGINS'     FROM DUAL UNION ALL
        SELECT 'MDSYS'          FROM DUAL UNION ALL
        SELECT 'OLAPSYS'        FROM DUAL UNION ALL
        SELECT 'DVSYS'          FROM DUAL UNION ALL
        SELECT 'DVF'            FROM DUAL UNION ALL
        SELECT 'LBACSYS'        FROM DUAL
    )
),
java_in_db AS (
    SELECT 'Java in Database'                                AS blocker_name,
           COUNT(*)                                          AS object_count,
           LISTAGG(CASE WHEN rn <= 10 THEN owner || '.' || object_name END,
                   ' | ' ON OVERFLOW TRUNCATE WITHOUT COUNT)
                  WITHIN GROUP (ORDER BY rn)                 AS sample_objects,
           'HIGH'                                            AS band
    FROM   (
        SELECT owner, object_name,
               ROW_NUMBER() OVER (ORDER BY owner, object_name) AS rn
        FROM   dba_objects
        WHERE  object_type IN ('JAVA SOURCE','JAVA CLASS','JAVA RESOURCE')
          AND  owner NOT IN (SELECT owner_name FROM excluded_owners)
    )
),
spatial AS (
    SELECT 'Spatial / SDO_GEOMETRY columns'                  AS blocker_name,
           COUNT(*)                                          AS object_count,
           LISTAGG(CASE WHEN rn <= 10 THEN owner || '.' || table_name || '.' || column_name END,
                   ' | ' ON OVERFLOW TRUNCATE WITHOUT COUNT)
                  WITHIN GROUP (ORDER BY rn)                 AS sample_objects,
           'HIGH'                                            AS band
    FROM   (
        SELECT owner, table_name, column_name,
               ROW_NUMBER() OVER (ORDER BY owner, table_name, column_name) AS rn
        FROM   dba_tab_columns
        WHERE  data_type = 'SDO_GEOMETRY'
          AND  owner NOT IN (SELECT owner_name FROM excluded_owners)
    )
),
xmltype_cols AS (
    SELECT 'XMLType columns'                                 AS blocker_name,
           COUNT(*)                                          AS object_count,
           LISTAGG(CASE WHEN rn <= 10 THEN owner || '.' || table_name || '.' || column_name END,
                   ' | ' ON OVERFLOW TRUNCATE WITHOUT COUNT)
                  WITHIN GROUP (ORDER BY rn)                 AS sample_objects,
           'MEDIUM'                                          AS band
    FROM   (
        SELECT owner, table_name, column_name,
               ROW_NUMBER() OVER (ORDER BY owner, table_name, column_name) AS rn
        FROM   dba_tab_columns
        WHERE  data_type = 'XMLTYPE'
          AND  owner NOT IN (SELECT owner_name FROM excluded_owners)
    )
),
complex_udt AS (
    SELECT 'User-defined object/collection types in app schemas' AS blocker_name,
           COUNT(*)                                          AS object_count,
           LISTAGG(CASE WHEN rn <= 10 THEN owner || '.' || type_name END,
                   ' | ' ON OVERFLOW TRUNCATE WITHOUT COUNT)
                  WITHIN GROUP (ORDER BY rn)                 AS sample_objects,
           'MEDIUM'                                          AS band
    FROM   (
        SELECT owner, type_name,
               ROW_NUMBER() OVER (ORDER BY owner, type_name) AS rn
        FROM   dba_types
        WHERE  predefined = 'NO'
          AND  owner NOT IN (SELECT owner_name FROM excluded_owners)
    )
),
db_links AS (
    SELECT 'Database links (outgoing)'                       AS blocker_name,
           COUNT(*)                                          AS object_count,
           LISTAGG(CASE WHEN rn <= 10 THEN owner || '.' || db_link END,
                   ' | ' ON OVERFLOW TRUNCATE WITHOUT COUNT)
                  WITHIN GROUP (ORDER BY rn)                 AS sample_objects,
           'MEDIUM'                                          AS band
    FROM   (
        SELECT owner, db_link,
               ROW_NUMBER() OVER (ORDER BY owner, db_link) AS rn
        FROM   dba_db_links
        WHERE  owner NOT IN (SELECT owner_name FROM excluded_owners)
    )
),
mviews AS (
    SELECT 'Materialized views (refresh chains)'             AS blocker_name,
           COUNT(*)                                          AS object_count,
           LISTAGG(CASE WHEN rn <= 10 THEN owner || '.' || mview_name END,
                   ' | ' ON OVERFLOW TRUNCATE WITHOUT COUNT)
                  WITHIN GROUP (ORDER BY rn)                 AS sample_objects,
           'MEDIUM'                                          AS band
    FROM   (
        SELECT owner, mview_name,
               ROW_NUMBER() OVER (ORDER BY owner, mview_name) AS rn
        FROM   dba_mviews
        WHERE  owner NOT IN (SELECT owner_name FROM excluded_owners)
    )
),
ext_tables AS (
    SELECT 'External tables'                                 AS blocker_name,
           COUNT(*)                                          AS object_count,
           LISTAGG(CASE WHEN rn <= 10 THEN owner || '.' || table_name END,
                   ' | ' ON OVERFLOW TRUNCATE WITHOUT COUNT)
                  WITHIN GROUP (ORDER BY rn)                 AS sample_objects,
           'MEDIUM'                                          AS band
    FROM   (
        SELECT owner, table_name,
               ROW_NUMBER() OVER (ORDER BY owner, table_name) AS rn
        FROM   dba_external_tables
        WHERE  owner NOT IN (SELECT owner_name FROM excluded_owners)
    )
),
aq_queues AS (
    SELECT 'Advanced Queuing queues'                         AS blocker_name,
           COUNT(*)                                          AS object_count,
           LISTAGG(CASE WHEN rn <= 10 THEN owner || '.' || name END,
                   ' | ' ON OVERFLOW TRUNCATE WITHOUT COUNT)
                  WITHIN GROUP (ORDER BY rn)                 AS sample_objects,
           'MEDIUM'                                          AS band
    FROM   (
        SELECT owner, name,
               ROW_NUMBER() OVER (ORDER BY owner, name) AS rn
        FROM   dba_queues
        WHERE  owner NOT IN (SELECT owner_name FROM excluded_owners)
    )
),
utl_dependencies AS (
    SELECT 'Code referencing UTL_FILE / UTL_HTTP / UTL_SMTP / DBMS_PIPE'
                                                             AS blocker_name,
           COUNT(*)                                          AS object_count,
           LISTAGG(CASE WHEN rn <= 10 THEN owner || '.' || name || ':' || referenced_name END,
                   ' | ' ON OVERFLOW TRUNCATE WITHOUT COUNT)
                  WITHIN GROUP (ORDER BY rn)                 AS sample_objects,
           'MEDIUM'                                          AS band
    FROM   (
        SELECT owner, name, referenced_name,
               ROW_NUMBER() OVER (ORDER BY owner, name, referenced_name) AS rn
        FROM   dba_dependencies
        WHERE  referenced_name IN ('UTL_FILE','UTL_HTTP','UTL_SMTP','UTL_TCP','DBMS_PIPE')
          AND  referenced_owner = 'SYS'
          AND  owner NOT IN (SELECT owner_name FROM excluded_owners)
    )
)
SELECT * FROM java_in_db        WHERE object_count > 0
UNION ALL SELECT * FROM spatial         WHERE object_count > 0
UNION ALL SELECT * FROM xmltype_cols    WHERE object_count > 0
UNION ALL SELECT * FROM complex_udt     WHERE object_count > 0
UNION ALL SELECT * FROM db_links        WHERE object_count > 0
UNION ALL SELECT * FROM mviews          WHERE object_count > 0
UNION ALL SELECT * FROM ext_tables      WHERE object_count > 0
UNION ALL SELECT * FROM aq_queues       WHERE object_count > 0
UNION ALL SELECT * FROM utl_dependencies WHERE object_count > 0
ORDER BY CASE band WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, blocker_name;

SPOOL OFF
SET MARKUP CSV OFF
SET TERMOUT ON
EXIT SUCCESS
