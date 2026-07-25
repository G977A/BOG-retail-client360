# 00 — Project charter

## Why this exists

Two goals, in this order:

1. **Learn.** Deliberately build the skills the target role lists that I have
   least production exposure to: Oracle-specific SQL, PySpark at meaningful
   volume, clustering for customer segmentation, and causal measurement of
   campaigns.
2. **Demonstrate.** Produce one artefact that shows analytical judgement, not
   just tool familiarity — something that ends in a business decision with a
   number attached.

## Scope

**In scope**
- Synthetic retail banking data at ~20 M transactions
- Oracle star schema with partitioning and analytic SQL
- PySpark feature store
- Behavioural segmentation, validated against planted ground truth
- Campaign design with a control group; uplift modelling; Qini evaluation
- Power BI reporting layer with a what-if budget simulator
- A one-page executive summary

**Explicitly out of scope**
- Real or scraped customer data of any kind
- Production orchestration (Airflow/dbt) — noted as "what I'd add next"
- Deep learning; the point is interpretable segments and honest measurement

## Success criteria

| # | Criterion | How it's measured |
|---|---|---|
| 1 | Segmentation recovers planted personas | Adjusted Rand Index vs `RBA_TRUTH` |
| 2 | Naive campaign read shown to be biased | Naive lift vs true uplift, quantified |
| 3 | Uplift targeting beats propensity targeting | Qini AUC; incremental revenue at equal budget |
| 4 | A stakeholder can act on it without me in the room | Power BI report + 1-page summary stand alone |

## Anti-goals

- No 40-cell notebook as the deliverable.
- No segment called "Cluster 3". Segments get business names or they are not done.
- No accuracy metric without a business number attached to it.
