"""
PySpark feature pipeline — 34M transactions to one row per customer.

Builds the wide numeric feature matrix that stages 4 and 5 consume:
clustering needs behaviour expressed as comparable numbers, and the uplift
model needs the same matrix plus the campaign outcome.

DIVISION OF LABOUR WITH SQL. The Oracle layer answers business questions
and feeds Power BI: RFM segments, penetration, campaign lift. This layer
builds model input — dozens of derived columns, no business meaning
required, optimised for a feature matrix rather than a report. Real banks
run both for exactly this reason.

POINT-IN-TIME CORRECTNESS. Every feature is computed strictly before the
campaign start date read from dim_campaign. A feature that peeks past the
decision it informs produces a model that scores brilliantly in backtest
and fails in production. This is enforced once, in AS_OF, and applied to
every source.

SEALED GROUND TRUTH. This pipeline reads only warehouse/. It never touches
ground_truth/, so the features cannot encode the answer (decision 0002).

WINDOWS NOTE. Spark writing to the local filesystem needs winutils.exe and
HADOOP_HOME. This pipeline avoids that entirely: the heavy aggregation runs
distributed in Spark, but the RESULT is one row per customer — 100k rows —
so it is collected and written with pandas. Pushing 100k rows through a
distributed commit protocol buys nothing. If you want native Spark writes
anyway, install winutils for Hadoop 3.3 into D:\\hadoop\\bin and set
HADOOP_HOME=D:\\hadoop.

Usage:
    python -m src.features.build_features
    python -m src.features.build_features --data-dir data/dev --show-plan
"""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

import pandas as pd
from pyspark.sql import SparkSession, Window
from pyspark.sql import functions as F

CATEGORIES = ["groceries", "dining", "ecommerce", "entertainment", "transport",
              "fuel", "utilities", "healthcare", "retail", "travel"]
# 'transfer' is deliberately absent: it carries only inflows, and the channel
# pivot below is filtered to debits, so it would produce an all-zero column.
CHANNELS = ["pos", "ecommerce", "atm", "mobile_app", "internet_bank", "branch"]
DIGITAL_CHANNELS = ["mobile_app", "internet_bank", "ecommerce"]


def build_session(app_name: str = "rc360-features",
                  local_dir: str | None = None,
                  driver_memory: str = "4g") -> SparkSession:
    """Local SparkSession sized for a laptop.

    spark.local.dir matters on a machine with a small system drive: Spark
    spills shuffle data there, and the default lands on C:. Pointing it at
    a roomy drive avoids filling the system disk mid-job.

    shuffle.partitions defaults to 200, which is far too many for a local
    run over tens of millions of rows — each partition becomes a task with
    fixed overhead, and the scheduling cost swamps the work. 16 suits a
    laptop; a cluster would want far more.
    """
    local_dir = local_dir or tempfile.gettempdir()
    return (SparkSession.builder
            .appName(app_name)
            .master("local[*]")
            .config("spark.local.dir", local_dir)
            .config("spark.driver.memory", driver_memory)
            .config("spark.sql.shuffle.partitions", "16")
            .config("spark.sql.session.timeZone", "UTC")
            # Adaptive execution coalesces small shuffle partitions at
            # runtime instead of relying on a static guess.
            .config("spark.sql.adaptive.enabled", "true")
            .getOrCreate())


# ---------------------------------------------------------------- feature blocks
def transaction_features(txn, as_of):
    """RFM plus volume, ticket size and timing shape, from purchases."""
    pur = txn.filter((F.col("txn_type") == "purchase") & (F.col("txn_date") < as_of))

    agg = pur.groupBy("customer_sk").agg(
        F.datediff(F.lit(as_of), F.max("txn_date")).alias("recency_days"),
        F.count("*").alias("purchase_count"),
        F.sum("amount_gel").alias("total_spend_gel"),
        F.avg("amount_gel").alias("avg_ticket_gel"),
        F.stddev("amount_gel").alias("ticket_volatility_gel"),
        F.expr("percentile_approx(amount_gel, 0.5)").alias("median_ticket_gel"),
        F.max("amount_gel").alias("max_ticket_gel"),
        F.countDistinct("merchant_sk").alias("distinct_merchants"),
        F.countDistinct(F.date_format("txn_date", "yyyy-MM")).alias("active_months"),
        # Weekend share separates leisure-led spenders from routine ones.
        F.avg(F.when(F.dayofweek("txn_date").isin(1, 7), 1.0).otherwise(0.0))
            .alias("weekend_txn_share"),
    )
    return agg.withColumn(
        "purchases_per_active_month",
        F.col("purchase_count") / F.greatest(F.col("active_months"), F.lit(1)))


def category_shares(txn, as_of):
    """Share of card spend by MCC category — the behavioural fingerprint.

    Shares rather than amounts: absolute spend mostly measures income, while
    composition separates a student from a pensioner at the same total.
    """
    pur = txn.filter((F.col("txn_type") == "purchase") & (F.col("txn_date") < as_of))

    # Listing the pivot values explicitly lets Spark skip the extra job it
    # would otherwise run to discover them, and guarantees a stable column
    # set even if a category is absent from a slice of data.
    wide = (pur.groupBy("customer_sk")
               .pivot("mcc_category", CATEGORIES)
               .agg(F.sum("amount_gel"))
               .na.fill(0.0))

    total = sum(F.col(c) for c in CATEGORIES)
    out = wide.withColumn("_total", total)
    for c in CATEGORIES:
        out = out.withColumn(f"sh_{c}",
                             F.when(F.col("_total") > 0, F.col(c) / F.col("_total"))
                              .otherwise(0.0))
    return out.select("customer_sk", *[f"sh_{c}" for c in CATEGORIES])


def channel_features(txn, as_of):
    """Channel mix, digital engagement and cash intensity."""
    out = txn.filter((F.col("txn_date") < as_of) & (F.col("direction") == "debit"))

    wide = (out.groupBy("customer_sk")
               .pivot("channel", CHANNELS)
               .agg(F.sum("amount_gel"))
               .na.fill(0.0))

    total = sum(F.col(c) for c in CHANNELS)
    res = wide.withColumn("_total", total)
    for c in CHANNELS:
        res = res.withColumn(f"ch_{c}",
                             F.when(F.col("_total") > 0, F.col(c) / F.col("_total"))
                              .otherwise(0.0))

    res = (res
           .withColumn("digital_value_share",
                       sum(F.col(f"ch_{c}") for c in DIGITAL_CHANNELS))
           .withColumn("cash_value_share", F.col("ch_atm")))

    cash = (out.filter(F.col("txn_type") == "cash_withdrawal")
               .groupBy("customer_sk")
               .agg(F.count("*").alias("withdrawal_count"),
                    F.avg("amount_gel").alias("avg_withdrawal_gel")))

    return (res.select("customer_sk", *[f"ch_{c}" for c in CHANNELS],
                       "digital_value_share", "cash_value_share")
               .join(cash, "customer_sk", "left")
               .na.fill({"withdrawal_count": 0, "avg_withdrawal_gel": 0.0}))


def income_features(txn, as_of):
    """Inflow level and regularity.

    Regularity is as informative as level: a salaried customer and a
    remittance household can receive the same annual total, but one arrives
    monthly and the other in irregular bursts. That difference shows up in
    creditworthiness and in how they respond to offers.
    """
    inflow = txn.filter((F.col("txn_type") == "inflow") & (F.col("txn_date") < as_of))

    monthly = (inflow.groupBy("customer_sk", F.date_format("txn_date", "yyyy-MM").alias("ym"))
                     .agg(F.sum("amount_gel").alias("month_inflow")))

    return monthly.groupBy("customer_sk").agg(
        F.avg("month_inflow").alias("avg_monthly_inflow_gel"),
        F.stddev("month_inflow").alias("inflow_volatility_gel"),
        F.count("*").alias("months_with_inflow"),
        F.min("month_inflow").alias("min_monthly_inflow_gel"),
    ).withColumn(
        # Coefficient of variation: volatility relative to level, so it is
        # comparable across income sizes.
        "inflow_cv",
        F.coalesce(F.col("inflow_volatility_gel"), F.lit(0.0))
        / F.greatest(F.col("avg_monthly_inflow_gel"), F.lit(1.0)))


def momentum_features(txn, as_of):
    """Recent activity versus the preceding period.

    A customer trending up and one trending down can have identical totals.
    Direction of travel is often the stronger predictor, and it is invisible
    to any point-in-time aggregate.
    """
    recent_start = (pd.Timestamp(as_of) - pd.DateOffset(months=3)).date()
    prior_start = (pd.Timestamp(as_of) - pd.DateOffset(months=6)).date()

    pur = txn.filter((F.col("txn_type") == "purchase") & (F.col("txn_date") < as_of))

    agg = pur.groupBy("customer_sk").agg(
        F.sum(F.when(F.col("txn_date") >= F.lit(recent_start), F.col("amount_gel"))
               .otherwise(0.0)).alias("spend_last_3m"),
        F.sum(F.when((F.col("txn_date") >= F.lit(prior_start))
                   & (F.col("txn_date") < F.lit(recent_start)), F.col("amount_gel"))
               .otherwise(0.0)).alias("spend_prior_3m"),
        F.sum(F.when(F.col("txn_date") >= F.lit(recent_start), 1).otherwise(0))
            .alias("txns_last_3m"),
    )
    return agg.withColumn(
        "spend_momentum",
        (F.col("spend_last_3m") - F.col("spend_prior_3m"))
        / F.greatest(F.col("spend_prior_3m"), F.lit(1.0)))


def balance_features(snap, prod, as_of):
    """Current-account level, volatility and trend from the monthly snapshot.

    Volatility separates customers who run their account to zero from those
    who hold a buffer, even when average balances match.
    """
    ca_sk = (prod.filter(F.col("product_name") == "Current Account")
                 .select("product_sk").first()[0])

    ca = snap.filter((F.col("product_sk") == ca_sk)
                     & (F.col("month_end_date") < as_of))

    # Windows over each customer's own history let first and last month be
    # picked without a self-join.
    w_asc = Window.partitionBy("customer_sk").orderBy(F.col("month_end_date"))
    w_desc = Window.partitionBy("customer_sk").orderBy(F.col("month_end_date").desc())

    ranked = (ca.withColumn("rn_asc", F.row_number().over(w_asc))
                .withColumn("rn_desc", F.row_number().over(w_desc)))

    return ranked.groupBy("customer_sk").agg(
        F.avg("balance_gel").alias("avg_balance_gel"),
        F.stddev("balance_gel").alias("balance_volatility_gel"),
        F.min("balance_gel").alias("min_balance_gel"),
        F.max("balance_gel").alias("max_balance_gel"),
        F.sum(F.when(F.col("balance_gel") < 0, 1).otherwise(0)).alias("months_negative"),
        F.max(F.when(F.col("rn_desc") == 1, F.col("balance_gel"))).alias("_last_bal"),
        F.max(F.when(F.col("rn_asc") == 1, F.col("balance_gel"))).alias("_first_bal"),
    ).withColumn("balance_trend_gel", F.col("_last_bal") - F.col("_first_bal")
    ).withColumn("balance_cv",
                 F.coalesce(F.col("balance_volatility_gel"), F.lit(0.0))
                 / F.greatest(F.abs(F.col("avg_balance_gel")), F.lit(1.0))
    ).drop("_last_bal", "_first_bal")


def holding_features(snap, prod, as_of):
    """Which products the customer held at the last snapshot before the
    campaign. Product ownership is a strong signal for cross-sell appetite."""
    latest = (snap.filter(F.col("month_end_date") < as_of)
                  .agg(F.max("month_end_date")).first()[0])

    held = (snap.filter(F.col("month_end_date") == F.lit(latest))
                .join(F.broadcast(prod.select("product_sk", "product_name")), "product_sk"))

    return (held.groupBy("customer_sk").agg(
        F.max(F.when(F.col("product_name") == "Savings Account", 1).otherwise(0))
            .alias("has_savings"),
        F.max(F.when(F.col("product_name") == "Term Deposit", 1).otherwise(0))
            .alias("has_term_deposit"),
        F.max(F.when(F.col("product_name") == "Consumer Loan", 1).otherwise(0))
            .alias("has_loan"),
        F.max(F.when(F.col("product_name") == "Credit Card", 1).otherwise(0))
            .alias("has_credit_card"),
        F.count("*").alias("products_held"),
        F.sum(F.when(F.col("balance_gel") > 0, F.col("balance_gel")).otherwise(0.0))
            .alias("total_assets_gel"),
        F.abs(F.sum(F.when(F.col("balance_gel") < 0, F.col("balance_gel")).otherwise(0.0)))
            .alias("total_liabilities_gel"),
    ))


# ------------------------------------------------------------------- pipeline
def build(spark: SparkSession, data_dir: Path, show_plan: bool = False) -> pd.DataFrame:
    wh = data_dir / "warehouse"

    txn = spark.read.parquet(str(wh / "fact_transaction"))
    snap = spark.read.parquet(str(wh / "fact_account_monthly.parquet"))
    cust = spark.read.parquet(str(wh / "dim_customer.parquet"))
    prod = spark.read.parquet(str(wh / "dim_product.parquet"))
    camp = spark.read.parquet(str(wh / "dim_campaign.parquet"))

    as_of = camp.select("start_date").first()[0]
    print(f"  as-of date (nothing after this enters a feature): {as_of}")

    # Cached because six feature blocks scan it. Without this, Spark re-reads
    # and re-decodes the whole transaction set once per block.
    txn = txn.cache()
    print(f"  transactions: {txn.count():,}")

    blocks = [
        transaction_features(txn, as_of),
        category_shares(txn, as_of),
        channel_features(txn, as_of),
        income_features(txn, as_of),
        momentum_features(txn, as_of),
        balance_features(snap, prod, as_of),
        holding_features(snap, prod, as_of),
    ]

    # dim_customer is the spine: every customer gets a row even if some
    # block has nothing for them. Left joins throughout, nulls filled after.
    # dim_customer.digital_engagement_flag is EXCLUDED on purpose. The
    # generator derives it from a per-persona constant, so it is a
    # deterministic function of the hidden persona — a ground-truth label
    # sitting in an observable table. Including it would let the clustering
    # recover the personas trivially and prove nothing. The honest version of
    # the same idea is digital_value_share, computed above from actual
    # transactions, and that one stays.
    #
    # Worth noting the general shape: a leaked feature does not announce
    # itself. It was caught by an ANOVA F-statistic of infinity against the
    # sealed labels — zero variance within each group. Any feature that
    # separates a target perfectly deserves suspicion before celebration.
    features = cust.select(
        "customer_sk", "age", "tenure_years", "existing_product_count",
        "income_band", "employment", "city",
    )
    for b in blocks:
        features = features.join(b, "customer_sk", "left")

    if show_plan:
        print("\n--- physical plan ---")
        features.explain(mode="simple")

    pdf = features.toPandas()
    txn.unpersist()

    num = pdf.select_dtypes("number").columns
    pdf[num] = pdf[num].fillna(0.0)
    return pdf


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the customer feature matrix.")
    ap.add_argument("--data-dir", default="data/parquet")
    ap.add_argument("--out", default=None, help="default: <data-dir>/features")
    ap.add_argument("--local-dir", default=None,
                    help="Spark scratch space; point at a roomy drive on Windows")
    ap.add_argument("--driver-memory", default="4g")
    ap.add_argument("--show-plan", action="store_true")
    a = ap.parse_args()

    data_dir = Path(a.data_dir)
    out_dir = Path(a.out) if a.out else data_dir / "features"
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    spark = build_session(local_dir=a.local_dir, driver_memory=a.driver_memory)
    spark.sparkContext.setLogLevel("ERROR")
    print(f"Spark {spark.version}\n")

    pdf = build(spark, data_dir, show_plan=a.show_plan)

    # Written with pandas, not Spark: the aggregation was distributed, but
    # the result is one row per customer. See the module docstring.
    path = out_dir / "customer_features.parquet"
    pdf.to_parquet(path, index=False)

    spark.stop()
    print(f"\n{len(pdf):,} customers x {pdf.shape[1]} features -> {path}")
    print(f"done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()