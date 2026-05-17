--------------------------------------------------------------------------------
-- File         : 01_feature_usage_audit.sql
-- Purpose      : Audit DBA_FEATURE_USAGE_STATISTICS and score each detected
--                Oracle feature against an Azure-portability rubric.
-- Target       : Oracle Database 19c (RAC or single-instance, CDB or non-CDB)
-- Privileges   : SELECT_CATALOG_ROLE on the source database. Read-only.
-- License pack : None required. DBA_FEATURE_USAGE_STATISTICS is base RDBMS.
-- Output       : CSV at &OUT_DIR/feature_usage/feature_score.csv
-- Idempotent   : Yes. Safe to re-run.
--------------------------------------------------------------------------------

SET ECHO OFF
SET FEEDBACK OFF
SET VERIFY OFF
SET HEADING ON
SET PAGESIZE 50000
SET LINESIZE 32767
SET TRIMSPOOL ON
SET TERMOUT OFF
SET NEWPAGE NONE
SET MARKUP CSV ON DELIMITER ',' QUOTE ON

DEFINE OUT_DIR = '&1'

SPOOL &OUT_DIR/feature_usage/feature_score.csv

WITH rubric (feature_pattern, target_score, target_notes) AS (
    SELECT 'Partitioning',                   'GREEN', 'Native on Azure SQL MI; emulated on PG via pg_partman'                    FROM DUAL UNION ALL
    SELECT 'Advanced Compression',           'AMBER', 'PAGE compression on MI; TOAST on PG (different semantics)'                FROM DUAL UNION ALL
    SELECT 'Transparent Data Encryption',    'GREEN', 'TDE native on MI; PG uses storage-layer encryption via Azure'             FROM DUAL UNION ALL
    SELECT 'Real Application Clusters (RAC)','RED',   'No equivalent on MI/PG. Options: Oracle Database@Azure or Always-On AG' FROM DUAL UNION ALL
    SELECT 'Data Guard',                     'AMBER', 'Replaced by AG (MI) or PG streaming replication + failover groups'        FROM DUAL UNION ALL
    SELECT 'Spatial and Graph',              'RED',   'Spatial: PostGIS on PG. Graph: out-of-DB (Azure Cosmos DB Gremlin)'       FROM DUAL UNION ALL
    SELECT 'OLAP',                           'RED',   'Deprecated 12.2+. Migrate to Power BI / Synapse Analytics'                FROM DUAL UNION ALL
    SELECT 'Java',                           'RED',   'Java-in-DB not supported. Externalize to Azure Functions / App Service'   FROM DUAL UNION ALL
    SELECT 'Advanced Queuing',               'AMBER', 'Migrate to Azure Service Bus or PG LISTEN/NOTIFY (limited semantics)'     FROM DUAL UNION ALL
    SELECT 'GoldenGate',                     'GREEN', 'Supported as the primary cross-cloud replication tool (Ch.7)'             FROM DUAL UNION ALL
    SELECT 'Materialized View',              'AMBER', 'MI: indexed views. PG: matviews (no FAST REFRESH equivalent)'             FROM DUAL UNION ALL
    SELECT 'Virtual Private Database',       'AMBER', 'MI: Row-Level Security. PG: RLS policies. Re-implementation required'    FROM DUAL UNION ALL
    SELECT 'Fine-Grained Auditing',          'AMBER', 'MI: SQL Audit. PG: pgAudit. Policy translation required (Ch.8)'           FROM DUAL UNION ALL
    SELECT 'Label Security',                 'RED',   'No drop-in equivalent. Re-architect via RLS + application logic'          FROM DUAL UNION ALL
    SELECT 'Database Vault',                 'RED',   'No equivalent. Compensating controls via RBAC + auditing'                 FROM DUAL UNION ALL
    SELECT 'Multitenant',                    'AMBER', 'CDB/PDB has no direct analogue. Plan per-PDB target topology'             FROM DUAL UNION ALL
    SELECT 'Sharding',                       'RED',   'No equivalent on MI. PG: Citus extension. Re-architect required'          FROM DUAL UNION ALL
    SELECT 'Heat Map',                       'GREEN', 'Optional. ILM equivalents exist on Azure storage tiers'                   FROM DUAL UNION ALL
    SELECT 'Automatic Data Optimization',    'AMBER', 'Re-implement via Azure Blob lifecycle policies'                           FROM DUAL UNION ALL
    SELECT 'In-Memory',                      'AMBER', 'MI: columnstore indexes. PG: no equivalent. Workload review needed'       FROM DUAL UNION ALL
    SELECT 'XML DB',                         'AMBER', 'Native XML on MI; PG xml type. XQuery surface differs significantly'      FROM DUAL UNION ALL
    SELECT 'Text',                           'AMBER', 'MI: Full-Text Search. PG: tsvector / pg_trgm. Index rebuild required'     FROM DUAL UNION ALL
    SELECT 'Streams',                        'RED',   'Desupported. Migrate to GoldenGate before any cutover'                    FROM DUAL UNION ALL
    SELECT 'Change Data Capture',            'RED',   'Desupported. Replace with GoldenGate / LogMiner-based CDC'                FROM DUAL UNION ALL
    SELECT 'Editions',                       'RED',   'Edition-Based Redefinition has no analogue. Re-architect deploy strategy' FROM DUAL UNION ALL
    SELECT 'Workspace Manager',              'RED',   'No equivalent. Re-implement temporal logic via system-versioned tables'   FROM DUAL
),
feature_used AS (
    SELECT
        f.dbid,
        f.name                AS feature_name,
        f.currently_used,
        f.detected_usages,
        f.total_samples,
        f.first_usage_date,
        f.last_usage_date,
        f.aux_count,
        ROW_NUMBER() OVER (PARTITION BY f.dbid, f.name ORDER BY f.last_sample_date DESC NULLS LAST) AS rn
    FROM dba_feature_usage_statistics f
    WHERE f.detected_usages > 0
       OR f.currently_used = 'TRUE'
)
SELECT
    fu.feature_name,
    fu.currently_used,
    fu.detected_usages,
    fu.first_usage_date,
    fu.last_usage_date,
    fu.aux_count,
    NVL(r.target_score, 'AMBER')                                   AS target_score,
    NVL(r.target_notes, 'No rubric entry - manual review required') AS target_notes
FROM   feature_used fu
LEFT   JOIN rubric r
       ON  UPPER(fu.feature_name) LIKE '%' || UPPER(r.feature_pattern) || '%'
WHERE  fu.rn = 1
ORDER  BY
    CASE NVL(r.target_score, 'AMBER')
        WHEN 'RED'   THEN 1
        WHEN 'AMBER' THEN 2
        WHEN 'GREEN' THEN 3
    END,
    fu.feature_name;

SPOOL OFF
SET MARKUP CSV OFF
SET TERMOUT ON
EXIT SUCCESS
