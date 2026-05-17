# Databricks notebook source
# =============================================================================
# 02_silver_transform.py - Bronze -> Silver transformation for HR-Pro.
#
# Silver responsibilities:
#   - Type normalization (Oracle NUMBER -> Spark decimal/long with explicit precision)
#   - PII masking enforced via Unity Catalog column masks (see § 9.10)
#   - Slowly-changing dimension handling for employee_history
#   - Deduplication on natural keys
#   - Drop ingest-metadata columns that don't belong in the analytic surface
# =============================================================================

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql import types as T
from pyspark.sql.window import Window   # Window is NOT exposed via pyspark.sql.functions

spark = SparkSession.builder.getOrCreate()

BRONZE_CATALOG = "hrpro_bronze"
SILVER_CATALOG = "hrpro_silver"
BRONZE_SCHEMA  = "raw"
SILVER_SCHEMA  = "clean"


# -----------------------------------------------------------------------------
# employee_history -- the largest analytical table; SCD Type-2 in Silver.
# Bronze appends every redo-replayed snapshot; Silver collapses to the latest
# version per (emp_id, effective_date) using a window dedup.
# -----------------------------------------------------------------------------
def build_silver_employee_history():
    bronze = spark.read.table(f"{BRONZE_CATALOG}.{BRONZE_SCHEMA}.employee_history_raw")

    silver = (
        bronze
        # Type normalization
        .withColumn("history_id",      F.col("HISTORY_ID").cast(T.LongType()))
        .withColumn("emp_id",          F.col("EMP_ID").cast(T.LongType()))
        .withColumn("effective_date",  F.col("EFFECTIVE_DATE").cast(T.DateType()))
        .withColumn("end_date",        F.col("END_DATE").cast(T.DateType()))
        .withColumn("dept_id",         F.col("DEPT_ID").cast(T.IntegerType()))
        .withColumn("grade_id",        F.col("GRADE_ID").cast(T.IntegerType()))
        .withColumn("salary",          F.col("SALARY").cast(T.DecimalType(12, 2)))
        .withColumn("change_reason",   F.col("CHANGE_REASON").cast(T.StringType()))
        # Dedup: keep the latest ingest per natural key.
        # Secondary sort on HISTORY_ID gives a deterministic tiebreak when
        # two Bronze rows land in the same current_timestamp() bucket (which
        # is per-query, not per-row, so collisions are real).
        .withColumn("_rn", F.row_number().over(
            Window.partitionBy("emp_id", "effective_date")
                  .orderBy(F.col("_ingested_at_utc").desc(),
                           F.col("HISTORY_ID").desc()))
        )
        .filter(F.col("_rn") == 1)
        .drop("_rn", "_ingested_at_utc", "_source_table", "_ingest_batch_id",
              # Drop the uppercase originals; we keep only the typed lowercase variants.
              "HISTORY_ID", "EMP_ID", "EFFECTIVE_DATE", "END_DATE",
              "DEPT_ID",    "GRADE_ID", "SALARY", "CHANGE_REASON")
    )

    target = f"{SILVER_CATALOG}.{SILVER_SCHEMA}.employee_history"
    (
        silver.write
              .format("delta")
              .mode("overwrite")
              .option("overwriteSchema", "true")
              .partitionBy("effective_date")
              .saveAsTable(target)
    )
    print(f"Silver employee_history: {silver.count():,} rows -> {target}")


# -----------------------------------------------------------------------------
# employee -- masked at the column level. Unity Catalog applies the mask
# at query time based on the caller's group membership; the underlying
# Delta still holds the cleartext.
# -----------------------------------------------------------------------------
def build_silver_employee():
    bronze = spark.read.table(f"{BRONZE_CATALOG}.{BRONZE_SCHEMA}.employee_raw")

    silver = (
        bronze
        .withColumn("emp_id",      F.col("EMP_ID").cast(T.LongType()))
        .withColumn("emp_number",  F.col("EMP_NUMBER").cast(T.StringType()))
        .withColumn("first_name",  F.col("FIRST_NAME").cast(T.StringType()))
        .withColumn("last_name",   F.col("LAST_NAME").cast(T.StringType()))
        .withColumn("ssn",         F.col("SSN").cast(T.StringType()))
        .withColumn("email",       F.col("EMAIL").cast(T.StringType()))
        .withColumn("hire_date",   F.col("HIRE_DATE").cast(T.DateType()))
        .withColumn("dept_id",     F.col("DEPT_ID").cast(T.IntegerType()))
        .withColumn("grade_id",    F.col("GRADE_ID").cast(T.IntegerType()))
        .select("emp_id","emp_number","first_name","last_name","ssn","email",
                "hire_date","dept_id","grade_id")
    )

    target = f"{SILVER_CATALOG}.{SILVER_SCHEMA}.employee"
    silver.write.format("delta").mode("overwrite").saveAsTable(target)

    # Apply Unity Catalog column mask on ssn. Caller must be in
    # `pii_readers` group to see cleartext; otherwise gets the masked form.
    # Mask function is created in 04_governance_masks.sql (see chapter prose).
    spark.sql(f"""
        ALTER TABLE {target}
          ALTER COLUMN ssn SET MASK hrpro_silver.security.mask_ssn
    """)
    print(f"Silver employee: {silver.count():,} rows -> {target}")


# -----------------------------------------------------------------------------
# Reference dimensions and lightweight time-series tables -- ORR classifies
# these as SILVER_ONLY. Builders are intentionally minimal (no PII to mask,
# no SCD): cast types, drop ingest metadata, write.
# -----------------------------------------------------------------------------
def _build_silver_passthrough(source_table: str, target_table: str,
                              partition_by: list[str] | None = None) -> None:
    bronze = spark.read.table(f"{BRONZE_CATALOG}.{BRONZE_SCHEMA}.{source_table}")
    silver = (
        bronze
        # Drop ingest metadata (Bronze-only).
        .drop("_ingested_at_utc", "_source_table", "_ingest_batch_id")
        # Lowercase column names so downstream Gold queries are case-stable
        # under spark.sql.caseSensitive=true.
        .toDF(*[c.lower() for c in
                [c for c in bronze.columns
                 if c not in {"_ingested_at_utc", "_source_table", "_ingest_batch_id"}]])
    )
    target = f"{SILVER_CATALOG}.{SILVER_SCHEMA}.{target_table}"
    writer = silver.write.format("delta").mode("overwrite").option("overwriteSchema", "true")
    if partition_by:
        writer = writer.partitionBy(*partition_by)
    writer.saveAsTable(target)
    print(f"Silver {target_table}: {silver.count():,} rows -> {target}")


def build_silver_department():
    _build_silver_passthrough("department_raw", "department")


def build_silver_job_grade():
    _build_silver_passthrough("job_grade_raw", "job_grade")


def build_silver_payroll_run():
    _build_silver_passthrough("payroll_run_raw", "payroll_run", partition_by=["run_date"])


if __name__ == "__main__":
    spark.sql(f"CREATE CATALOG IF NOT EXISTS {SILVER_CATALOG}")
    spark.sql(f"CREATE SCHEMA  IF NOT EXISTS {SILVER_CATALOG}.{SILVER_SCHEMA}")

    # Order matters for Gold: Gold reads silver.department + silver.employee,
    # so reference dims must be present before Gold runs. Within a single
    # Silver run order is flexible; we order alphabetically for predictability.
    build_silver_department()
    build_silver_job_grade()
    build_silver_employee()
    build_silver_employee_history()
    build_silver_payroll_run()
    print("Silver transform complete.")
