# Databricks notebook source
# =============================================================================
# 01_bronze_ingest.py - land HR-Pro analytical tables into the Bronze layer.
#
# Pattern: parallel JDBC read from Oracle source -> append-mode Delta write.
# Bronze keeps every row, schema-preserved, with ingest metadata appended.
# No transformations here -- that's Silver's job.
#
# Cluster requirements:
#   - Databricks Runtime 14.x+ (Spark 3.5)
#   - Photon recommended for downstream Silver/Gold; Bronze is JDBC-bound
#   - Oracle JDBC driver (ojdbc11.jar) installed as a cluster library
#   - Secret scope wired to Key Vault for HRPRO credentials
# =============================================================================

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

spark = SparkSession.builder.getOrCreate()

# -----------------------------------------------------------------------------
# Configuration (overridable via Databricks job parameters).
#
# Idiomatic Databricks pattern: declare every widget with a default at the top
# of the notebook, then read with dbutils.widgets.get(...). The widget API's
# getAll() returns a dict[str, str] (NOT an iterable of objects with .name);
# the documented + safest form is just declare + get.
# -----------------------------------------------------------------------------
dbutils.widgets.text("oracle_host",    "prod-rac-scan", "Oracle source host")
dbutils.widgets.text("oracle_port",    "1521",          "Oracle source port")
dbutils.widgets.text("oracle_service", "ORA19CPROD",    "Oracle service name")
dbutils.widgets.text("bronze_catalog", "hrpro_bronze",  "Bronze Unity Catalog")

ORACLE_HOST    = dbutils.widgets.get("oracle_host")
ORACLE_PORT    = dbutils.widgets.get("oracle_port")
ORACLE_SERVICE = dbutils.widgets.get("oracle_service")
BRONZE_CATALOG = dbutils.widgets.get("bronze_catalog")
BRONZE_SCHEMA  = "raw"

# Secrets via Key Vault-backed scope (set up per Ch.8). Never read from env.
ORACLE_USER = dbutils.secrets.get(scope="hrpro-kv", key="oracle-user")
ORACLE_PWD  = dbutils.secrets.get(scope="hrpro-kv", key="oracle-pwd")

JDBC_URL = (
    f"jdbc:oracle:thin:@//{ORACLE_HOST}:{ORACLE_PORT}/{ORACLE_SERVICE}"
)

# -----------------------------------------------------------------------------
# Per-table ingest recipe.
#
# *** P18 fix: partitionColumn + lowerBound/upperBound + numPartitions are
# *** MANDATORY for any table over ~1M rows. Without them, the JDBC read
# *** funnels through the driver node and bandwidth is single-node-bound;
# *** see Ch.9 § 9.4 prose.
# -----------------------------------------------------------------------------
# Tables small enough that a single JDBC read is faster than 8+ parallel ones.
# Set partition_column=None to skip partitioning.
INGEST_RECIPES = [
    # Reference dims -- small, single-threaded JDBC read is fine.
    {
        "source_table":     "HRPRO.DEPARTMENT",
        "bronze_table":     "department_raw",
        "partition_column": None,
    },
    {
        "source_table":     "HRPRO.JOB_GRADE",
        "bronze_table":     "job_grade_raw",
        "partition_column": None,
    },
    # Large analytical / transactional -- discover MIN/MAX bounds at runtime
    # (P18 fix; see § 9.4 prose). Setting lower_bound/upper_bound to None
    # triggers the discover_bounds() helper before the read.
    {
        "source_table":     "HRPRO.EMPLOYEE_HISTORY",
        "bronze_table":     "employee_history_raw",
        "partition_column": "HISTORY_ID",
        "lower_bound":      None,
        "upper_bound":      None,
        "num_partitions":   32,
    },
    {
        "source_table":     "HRPRO.PAYROLL_RUN",
        "bronze_table":     "payroll_run_raw",
        "partition_column": "RUN_ID",
        "lower_bound":      None,
        "upper_bound":      None,
        "num_partitions":   8,
    },
    {
        "source_table":     "HRPRO.EMPLOYEE",
        "bronze_table":     "employee_raw",
        "partition_column": "EMP_ID",
        "lower_bound":      None,
        "upper_bound":      None,
        "num_partitions":   16,
    },
]


def discover_bounds(spark, recipe):
    """Run MIN/MAX once via a tiny JDBC query so the parallel read uses real
    bounds. Without this, hardcoded upperBound placeholders either (a) cap
    the read short of the actual MAX or (b) issue 31 empty WHERE-bands when
    the placeholder is wildly larger than the real range."""
    col = recipe["partition_column"]
    if col is None:
        return None, None
    query = (
        f"(SELECT MIN({col}) AS lo, MAX({col}) AS hi FROM {recipe['source_table']}) bounds_q"
    )
    row = (
        spark.read.format("jdbc")
             .option("url",      JDBC_URL)
             .option("driver",   "oracle.jdbc.OracleDriver")
             .option("user",     ORACLE_USER)
             .option("password", ORACLE_PWD)
             .option("dbtable",  query)
             .load()
             .first()
    )
    if row is None or row["LO"] is None or row["HI"] is None:
        # Empty table; skip partitioning rather than divide-by-zero.
        return None, None
    return int(row["LO"]), int(row["HI"])


def ingest_table(spark, recipe):
    """JDBC read; write append-mode Delta to Bronze.

    Two paths:
      - partition_column=None  -> single JDBC connection (small reference dims).
      - partition_column=<col> -> discover MIN/MAX once, then parallel read
        with numPartitions sessions reading disjoint key ranges.
    """
    reader = (
        spark.read.format("jdbc")
             .option("url",       JDBC_URL)
             .option("driver",    "oracle.jdbc.OracleDriver")
             .option("user",      ORACLE_USER)
             .option("password",  ORACLE_PWD)
             .option("dbtable",   recipe["source_table"])
             .option("fetchsize", 10_000)
    )
    if recipe["partition_column"]:
        lo, hi = discover_bounds(spark, recipe)
        if lo is not None and hi is not None and hi > lo:
            reader = (reader
                      .option("partitionColumn", recipe["partition_column"])
                      .option("lowerBound",      lo)
                      .option("upperBound",      hi)
                      .option("numPartitions",   recipe["num_partitions"]))
            print(f"  partitioned: {recipe['partition_column']} in [{lo}, {hi}] "
                  f"across {recipe['num_partitions']} sessions")
        else:
            print(f"  empty or no usable bounds; reading single-threaded")

    df = reader.load()

    # Append ingest-time metadata so Silver can reason about freshness +
    # source lineage without a join back to Oracle.
    df = (
        df.withColumn("_ingested_at_utc", F.current_timestamp())
          .withColumn("_source_table",    F.lit(recipe["source_table"]))
          .withColumn("_ingest_batch_id", F.lit(spark.conf.get(
                "spark.databricks.clusterUsageTags.clusterId", "lab")))
    )

    target = f"{BRONZE_CATALOG}.{BRONZE_SCHEMA}.{recipe['bronze_table']}"
    # Cache so the count() probe doesn't double-read.
    df = df.cache()
    print(f"Writing {df.count():,} rows -> {target}")
    (
        df.write
          .format("delta")
          .mode("append")
          .option("mergeSchema", "true")
          .saveAsTable(target)
    )
    df.unpersist()


if __name__ == "__main__":
    # Bootstrap catalog + schema (idempotent)
    spark.sql(f"CREATE CATALOG IF NOT EXISTS {BRONZE_CATALOG}")
    spark.sql(f"CREATE SCHEMA  IF NOT EXISTS {BRONZE_CATALOG}.{BRONZE_SCHEMA}")

    for recipe in INGEST_RECIPES:
        print(f"--- ingesting {recipe['source_table']} ---")
        ingest_table(spark, recipe)
    print("Bronze ingest complete.")
