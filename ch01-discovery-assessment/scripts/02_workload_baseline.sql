--------------------------------------------------------------------------------
-- File         : 02_workload_baseline.sql
-- Purpose      : Derive a 90-day workload baseline from AWR: IOPS, throughput,
--                redo generation, CPU pressure, and top wait events. RAC-aware
--                via DBA_HIST_* views which already aggregate per-instance.
-- Target       : Oracle Database 19c (RAC or single instance)
-- Privileges   : SELECT_CATALOG_ROLE.
-- License pack : *** DIAGNOSTICS AND TUNING PACK REQUIRED ***
--                Fallback path: re-implement against PERFSTAT.STATS$* (Statspack).
-- Output       : 4 CSV files under &OUT_DIR/workload_baseline/
-- Idempotent   : Yes. Window is rolling 90 days ending at SYSDATE.
--------------------------------------------------------------------------------

SET ECHO OFF FEEDBACK OFF VERIFY OFF HEADING ON PAGESIZE 50000 LINESIZE 32767 TRIMSPOOL ON TERMOUT OFF NEWPAGE NONE
SET MARKUP CSV ON DELIMITER ',' QUOTE ON

DEFINE OUT_DIR = '&1'

-- The retention window is bounded by AWR retention; we cap to 90 days
-- via the INTERVAL '90' DAY literal in each CTE below.

--==============================================================================
-- (a) IOPS by hour, per instance — read + write IO requests per second
--==============================================================================
SPOOL &OUT_DIR/workload_baseline/awr_iops.csv

WITH snaps AS (
    SELECT snap_id, instance_number, dbid,
           begin_interval_time, end_interval_time,
           (CAST(end_interval_time AS DATE) - CAST(begin_interval_time AS DATE)) * 86400 AS interval_secs
    FROM   dba_hist_snapshot
    WHERE  begin_interval_time >= SYSTIMESTAMP - INTERVAL '90' DAY
),
stat AS (
    SELECT s.snap_id, s.instance_number, s.dbid,
           SUM(CASE WHEN ss.stat_name = 'physical read total IO requests'  THEN ss.value END) AS read_reqs,
           SUM(CASE WHEN ss.stat_name = 'physical write total IO requests' THEN ss.value END) AS write_reqs
    FROM   dba_hist_sysstat ss
    JOIN   snaps s
           ON s.snap_id = ss.snap_id AND s.instance_number = ss.instance_number AND s.dbid = ss.dbid
    WHERE  ss.stat_name IN ('physical read total IO requests','physical write total IO requests')
    GROUP  BY s.snap_id, s.instance_number, s.dbid
),
delta AS (
    SELECT
        s.instance_number,
        TRUNC(CAST(s.end_interval_time AS DATE), 'HH24') AS hour_bucket,
        SUM(GREATEST(stat.read_reqs  - LAG(stat.read_reqs)
                     OVER (PARTITION BY s.instance_number ORDER BY s.snap_id), 0)) AS read_reqs_delta,
        SUM(GREATEST(stat.write_reqs - LAG(stat.write_reqs)
                     OVER (PARTITION BY s.instance_number ORDER BY s.snap_id), 0)) AS write_reqs_delta,
        SUM(s.interval_secs) AS secs
    FROM   snaps s
    JOIN   stat ON stat.snap_id = s.snap_id AND stat.instance_number = s.instance_number AND stat.dbid = s.dbid
    GROUP  BY s.instance_number, TRUNC(CAST(s.end_interval_time AS DATE), 'HH24')
)
SELECT
    instance_number,
    TO_CHAR(hour_bucket, 'YYYY-MM-DD"T"HH24:00:00') AS hour_utc,
    ROUND(read_reqs_delta  / NULLIF(secs, 0), 1)    AS read_iops_avg,
    ROUND(write_reqs_delta / NULLIF(secs, 0), 1)    AS write_iops_avg
FROM   delta
ORDER  BY hour_bucket, instance_number;

SPOOL OFF

--==============================================================================
-- (b) Throughput MB/s by hour, per instance
--==============================================================================
SPOOL &OUT_DIR/workload_baseline/awr_throughput.csv

WITH snaps AS (
    SELECT snap_id, instance_number, dbid, end_interval_time,
           (CAST(end_interval_time AS DATE) - CAST(begin_interval_time AS DATE)) * 86400 AS interval_secs
    FROM   dba_hist_snapshot
    WHERE  begin_interval_time >= SYSTIMESTAMP - INTERVAL '90' DAY
),
stat AS (
    SELECT s.snap_id, s.instance_number, s.dbid,
           SUM(CASE WHEN ss.stat_name = 'physical read total bytes'  THEN ss.value END) AS read_bytes,
           SUM(CASE WHEN ss.stat_name = 'physical write total bytes' THEN ss.value END) AS write_bytes,
           SUM(CASE WHEN ss.stat_name = 'redo size'                  THEN ss.value END) AS redo_bytes
    FROM   dba_hist_sysstat ss
    JOIN   snaps s
           ON s.snap_id = ss.snap_id AND s.instance_number = ss.instance_number AND s.dbid = ss.dbid
    WHERE  ss.stat_name IN ('physical read total bytes','physical write total bytes','redo size')
    GROUP  BY s.snap_id, s.instance_number, s.dbid
),
delta AS (
    SELECT
        s.instance_number,
        TRUNC(CAST(s.end_interval_time AS DATE), 'HH24') AS hour_bucket,
        SUM(GREATEST(stat.read_bytes  - LAG(stat.read_bytes)
                     OVER (PARTITION BY s.instance_number ORDER BY s.snap_id), 0)) AS read_bytes_delta,
        SUM(GREATEST(stat.write_bytes - LAG(stat.write_bytes)
                     OVER (PARTITION BY s.instance_number ORDER BY s.snap_id), 0)) AS write_bytes_delta,
        SUM(GREATEST(stat.redo_bytes  - LAG(stat.redo_bytes)
                     OVER (PARTITION BY s.instance_number ORDER BY s.snap_id), 0)) AS redo_bytes_delta,
        SUM(s.interval_secs) AS secs
    FROM   snaps s
    JOIN   stat ON stat.snap_id = s.snap_id AND stat.instance_number = s.instance_number AND stat.dbid = s.dbid
    GROUP  BY s.instance_number, TRUNC(CAST(s.end_interval_time AS DATE), 'HH24')
)
SELECT
    instance_number,
    TO_CHAR(hour_bucket, 'YYYY-MM-DD"T"HH24:00:00')                 AS hour_utc,
    ROUND(read_bytes_delta  / NULLIF(secs, 0) / 1048576, 2)         AS read_mb_per_sec,
    ROUND(write_bytes_delta / NULLIF(secs, 0) / 1048576, 2)         AS write_mb_per_sec,
    ROUND(redo_bytes_delta  / NULLIF(secs, 0) / 1048576, 2)         AS redo_mb_per_sec
FROM   delta
ORDER  BY hour_bucket, instance_number;

SPOOL OFF

--==============================================================================
-- (c) Top wait events by DB time (rolled up over the 90-day window)
--==============================================================================
SPOOL &OUT_DIR/workload_baseline/awr_top_waits.csv

WITH snaps AS (
    SELECT snap_id, instance_number, dbid
    FROM   dba_hist_snapshot
    WHERE  begin_interval_time >= SYSTIMESTAMP - INTERVAL '90' DAY
),
waits AS (
    SELECT
        e.event_name,
        e.wait_class,
        e.instance_number,
        GREATEST(e.time_waited_micro_fg
                 - LAG(e.time_waited_micro_fg)
                   OVER (PARTITION BY e.instance_number, e.event_name ORDER BY e.snap_id), 0)
        AS waited_micro_delta,
        GREATEST(e.total_waits_fg
                 - LAG(e.total_waits_fg)
                   OVER (PARTITION BY e.instance_number, e.event_name ORDER BY e.snap_id), 0)
        AS waits_delta
    FROM   dba_hist_system_event e
    JOIN   snaps s
           ON s.snap_id = e.snap_id AND s.instance_number = e.instance_number AND s.dbid = e.dbid
    WHERE  e.wait_class <> 'Idle'
)
SELECT
    event_name,
    wait_class,
    ROUND(SUM(waited_micro_delta) / 1e6, 1) AS total_wait_secs,
    SUM(waits_delta)                        AS total_waits,
    ROUND(SUM(waited_micro_delta)
          / NULLIF(SUM(waits_delta), 0) / 1000, 3) AS avg_ms_per_wait
FROM   waits
GROUP  BY event_name, wait_class
ORDER  BY total_wait_secs DESC NULLS LAST
FETCH FIRST 25 ROWS ONLY;

SPOOL OFF

--==============================================================================
-- (d) CPU utilization and PGA pressure by hour
--==============================================================================
SPOOL &OUT_DIR/workload_baseline/awr_cpu_pga.csv

WITH snaps AS (
    SELECT snap_id, instance_number, dbid, end_interval_time
    FROM   dba_hist_snapshot
    WHERE  begin_interval_time >= SYSTIMESTAMP - INTERVAL '90' DAY
),
os AS (
    SELECT s.snap_id, s.instance_number,
           SUM(CASE WHEN o.stat_name = 'BUSY_TIME' THEN o.value END) AS busy_cs,
           SUM(CASE WHEN o.stat_name = 'IDLE_TIME' THEN o.value END) AS idle_cs
    FROM   dba_hist_osstat o
    JOIN   snaps s
           ON s.snap_id = o.snap_id AND s.instance_number = o.instance_number AND s.dbid = o.dbid
    WHERE  o.stat_name IN ('BUSY_TIME','IDLE_TIME')
    GROUP  BY s.snap_id, s.instance_number
),
pga AS (
    -- Use DBA_HIST_PGASTAT for a meaningful instance-wide PGA metric.
    -- 'total PGA allocated' is the right aggregate; sampled at snapshot time.
    SELECT s.snap_id, s.instance_number,
           MAX(CASE WHEN p.name = 'total PGA allocated' THEN p.value END) AS pga_bytes
    FROM   dba_hist_pgastat p
    JOIN   snaps s
           ON s.snap_id = p.snap_id AND s.instance_number = p.instance_number AND s.dbid = p.dbid
    WHERE  p.name = 'total PGA allocated'
    GROUP  BY s.snap_id, s.instance_number
),
deltas AS (
    SELECT
        s.instance_number,
        s.snap_id,
        s.end_interval_time,
        GREATEST(os.busy_cs - LAG(os.busy_cs) OVER (PARTITION BY s.instance_number ORDER BY s.snap_id), 0) AS busy_delta,
        GREATEST(os.idle_cs - LAG(os.idle_cs) OVER (PARTITION BY s.instance_number ORDER BY s.snap_id), 0) AS idle_delta,
        pga.pga_bytes
    FROM   snaps s
    LEFT   JOIN os  ON os.snap_id  = s.snap_id AND os.instance_number  = s.instance_number
    LEFT   JOIN pga ON pga.snap_id = s.snap_id AND pga.instance_number = s.instance_number
)
SELECT
    instance_number,
    TO_CHAR(TRUNC(CAST(end_interval_time AS DATE), 'HH24'), 'YYYY-MM-DD"T"HH24:00:00') AS hour_utc,
    ROUND(100 * busy_delta / NULLIF(busy_delta + idle_delta, 0), 1) AS cpu_pct,
    ROUND(pga_bytes / 1048576, 1)                                   AS pga_mb
FROM   deltas
ORDER  BY end_interval_time, instance_number;

SPOOL OFF
SET MARKUP CSV OFF
SET TERMOUT ON
EXIT SUCCESS
