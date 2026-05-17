# Databricks notebook source
# =============================================================================
# 03_gold_aggregate.py - Silver -> Gold business aggregates.
#
# Gold tables replace the Oracle materialized views the source no longer
# computes. The flagship example: mv_headcount_rollup (source) -> Gold
# headcount_rollup_g Delta table, recomputed on a schedule via a Databricks
# Workflow. The same query pattern the source MV used; here it's a Spark
# job over Silver.
# =============================================================================

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

spark = SparkSession.builder.getOrCreate()

SILVER_CATALOG = "hrpro_silver"
GOLD_CATALOG   = "hrpro_gold"
SILVER_SCHEMA  = "clean"
GOLD_SCHEMA    = "facts"


def build_headcount_rollup_g():
    """Gold equivalent of Oracle's MV_HEADCOUNT_ROLLUP.

    Source MV definition (Ch.3.5 hr_pro_schema.sql):
        SELECT d.dept_code, d.dept_name,
               COUNT(*) AS emp_count, AVG(eh.salary) AS avg_salary
          FROM employee e
          JOIN department d ON d.dept_id = e.dept_id
          LEFT JOIN employee_history eh ON eh.emp_id = e.emp_id
                                       AND eh.end_date IS NULL
         GROUP BY d.dept_code, d.dept_name;

    Notes:
      - department is a reference dimension; the OLTP target keeps it (Silver
        only mirrors it). Here we read from Silver to keep the pipeline closed.
      - The Gold table is OPTIMIZE'd and Z-ORDERed on dept_code for
        downstream BI predicate pruning.
    """
    employee         = spark.read.table(f"{SILVER_CATALOG}.{SILVER_SCHEMA}.employee")
    employee_history = spark.read.table(f"{SILVER_CATALOG}.{SILVER_SCHEMA}.employee_history")
    # Reference dim mirrored to Silver in an earlier batch (not shown).
    department       = spark.read.table(f"{SILVER_CATALOG}.{SILVER_SCHEMA}.department")

    eh_current = employee_history.filter(F.col("end_date").isNull())

    gold = (
        employee.alias("e")
        .join(department.alias("d"), F.col("e.dept_id") == F.col("d.dept_id"), "inner")
        .join(eh_current.alias("eh"), F.col("eh.emp_id") == F.col("e.emp_id"), "left")
        .groupBy(F.col("d.dept_code"), F.col("d.dept_name"))
        .agg(
            F.count("*").alias("emp_count"),
            F.avg(F.col("eh.salary")).cast("decimal(14,2)").alias("avg_salary"),
        )
        .withColumn("computed_at_utc", F.current_timestamp())
    )

    target = f"{GOLD_CATALOG}.{GOLD_SCHEMA}.headcount_rollup_g"
    (
        gold.write
            .format("delta")
            .mode("overwrite")
            .option("overwriteSchema", "true")
            .saveAsTable(target)
    )

    # Optimize + Z-ORDER. For a small Gold table this is fast; for
    # large facts it materially improves predicate-pruned BI queries.
    spark.sql(f"OPTIMIZE {target} ZORDER BY (dept_code)")
    print(f"Gold headcount_rollup_g built -> {target}")


def build_payroll_cost_timeseries_g():
    """Time-series of monthly payroll cost -- a Gold table with no source-side
    equivalent. Demonstrates that Gold is the LAYER where new analytics is born,
    not just a port of source MVs."""
    payroll = spark.read.table(f"{SILVER_CATALOG}.{SILVER_SCHEMA}.payroll_run") \
                        if spark.catalog.tableExists(f"{SILVER_CATALOG}.{SILVER_SCHEMA}.payroll_run") \
                        else None
    if payroll is None:
        print("payroll_run not present in Silver; skipping payroll_cost_timeseries_g")
        return

    gold = (
        payroll
        .filter(F.col("status") == "COMPLETED")
        .groupBy(F.date_trunc("month", F.col("run_date")).alias("month"))
        .agg(
            F.sum("total_gross").alias("monthly_gross"),
            F.sum("emp_count").alias("monthly_emp_runs"),
        )
        .orderBy("month")
    )
    target = f"{GOLD_CATALOG}.{GOLD_SCHEMA}.payroll_cost_timeseries_g"
    gold.write.format("delta").mode("overwrite").saveAsTable(target)
    spark.sql(f"OPTIMIZE {target} ZORDER BY (month)")
    print(f"Gold payroll_cost_timeseries_g built -> {target}")


if __name__ == "__main__":
    spark.sql(f"CREATE CATALOG IF NOT EXISTS {GOLD_CATALOG}")
    spark.sql(f"CREATE SCHEMA  IF NOT EXISTS {GOLD_CATALOG}.{GOLD_SCHEMA}")

    build_headcount_rollup_g()
    build_payroll_cost_timeseries_g()
    print("Gold aggregates complete.")
