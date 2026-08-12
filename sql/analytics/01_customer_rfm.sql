-- =====================================================================
-- sql/analytics/01_customer_rfm.sql — RFM segmentation
-- =====================================================================
-- Recency / Frequency / Monetary, scored into quintiles within the
-- population and mapped to named segments.
--
-- POINT-IN-TIME CORRECTNESS. Every measure is computed strictly BEFORE
-- the campaign start date, taken from dim_campaign rather than hardcoded.
-- Features used for targeting must not see anything that happened after
-- the decision they inform, or the model looks brilliant in backtest and
-- useless in production. This is the single most common way an otherwise
-- sound model fails.
--
-- WHY QUINTILES, NOT FIXED THRESHOLDS. NTILE ranks customers against each
-- other, so the segmentation adapts as the portfolio shifts. Fixed cutoffs
-- ("spend > 5000 GEL = high value") go stale with inflation and portfolio
-- mix, and are unusable across markets. The trade is that scores are
-- relative: a 5 means top fifth of THIS population, not an absolute level.
--
-- TIES. NTILE splits ties arbitrarily across bucket boundaries — two
-- customers with identical frequency can land in different quintiles.
-- That is acceptable for a marketing segmentation but would not be for
-- anything with a regulatory or contractual consequence, where NTILE
-- should be replaced by an explicit banded CASE.
--
-- KNOWN WEAKNESS IN THIS DATASET. Recency has very little variance here:
-- every generated customer stays active, so last-purchase dates cluster
-- within about a week of each other and r_score is close to noise. In a
-- real portfolio recency spreads widely because customers go dormant, and
-- it is usually the strongest of the three. Kept for completeness and
-- flagged rather than quietly dropped; the frequency and monetary
-- dimensions carry the real signal in this data.
-- =====================================================================


-- ---------------------------------------------------------------------
-- v_customer_rfm — the logic, as a view
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_customer_rfm AS
WITH ref AS (
    SELECT start_date AS as_of
    FROM   dim_campaign
    WHERE  campaign_sk = 1
),
base AS (
    SELECT t.customer_sk,
           -- Oracle date subtraction returns days as a NUMBER, so no
           -- DATEDIFF is needed. TRUNC guards against fractional days if a
           -- time component ever appears.
           TRUNC(MIN(r.as_of) - MAX(t.txn_date)) AS recency_days,
           COUNT(*)                              AS frequency,
           SUM(t.amount_gel)                     AS monetary_gel,
           AVG(t.amount_gel)                     AS avg_ticket_gel
    FROM   fact_transaction t
    CROSS  JOIN ref r
    WHERE  t.txn_type = 'purchase'
    AND    t.txn_date < r.as_of
    GROUP  BY t.customer_sk
),
scored AS (
    SELECT b.*,
           -- Recency is reversed: fewer days since last purchase is better,
           -- so DESC puts the most recent customers in quintile 5 and keeps
           -- "higher score = better" true for all three dimensions.
           NTILE(5) OVER (ORDER BY recency_days DESC) AS r_score,
           NTILE(5) OVER (ORDER BY frequency)         AS f_score,
           NTILE(5) OVER (ORDER BY monetary_gel)      AS m_score
    FROM   base b
)
SELECT s.customer_sk,
       s.recency_days,
       s.frequency,
       ROUND(s.monetary_gel, 2)   AS monetary_gel,
       ROUND(s.avg_ticket_gel, 2) AS avg_ticket_gel,
       s.r_score,
       s.f_score,
       s.m_score,
       s.r_score * 100 + s.f_score * 10 + s.m_score AS rfm_cell,
       CASE
           WHEN s.r_score >= 4 AND s.f_score >= 4 AND s.m_score >= 4 THEN 'Champions'
           WHEN s.r_score >= 3 AND s.f_score >= 3                    THEN 'Loyal'
           WHEN s.r_score >= 4 AND s.f_score <= 2                    THEN 'New / Promising'
           WHEN s.r_score <= 2 AND s.f_score >= 3                    THEN 'At Risk'
           WHEN s.r_score <= 2 AND s.f_score <= 2                    THEN 'Hibernating'
           ELSE 'Needs Attention'
       END AS rfm_segment
FROM   scored s;


-- ---------------------------------------------------------------------
-- customer_rfm_snapshot — materialised result
-- ---------------------------------------------------------------------
-- The view recomputes a full scan and sort of fact_transaction every time
-- it is queried, which is fine for exploration and wrong for a dashboard
-- refreshed by many users. Persisting the result once per load and
-- pointing Power BI at the table costs storage and buys predictable
-- response times.
--
-- A materialized view would express this more compactly, but requires the
-- CREATE MATERIALIZED VIEW privilege, which the application user may not
-- hold. A plain table refreshed by MERGE needs no extra grants and is what
-- most warehouse ETL actually does.
-- ---------------------------------------------------------------------
CREATE TABLE customer_rfm_snapshot (
    customer_sk    NUMBER(10)        NOT NULL,
    snapshot_date  DATE              NOT NULL,
    recency_days   NUMBER(6),
    frequency      NUMBER(10),
    monetary_gel   NUMBER(14,2),
    avg_ticket_gel NUMBER(12,2),
    r_score        NUMBER(1),
    f_score        NUMBER(1),
    m_score        NUMBER(1),
    rfm_cell       NUMBER(3),
    rfm_segment    VARCHAR2(20 CHAR),
    CONSTRAINT pk_customer_rfm_snapshot PRIMARY KEY (customer_sk),
    CONSTRAINT fk_rfm_customer FOREIGN KEY (customer_sk)
               REFERENCES dim_customer (customer_sk)
);


-- ---------------------------------------------------------------------
-- Refresh. MERGE is the right tool: one statement handles both the first
-- population and every later refresh, updating rows that exist and
-- inserting those that do not. The alternative — DELETE then INSERT —
-- empties the table for the duration of the transaction, so anything
-- reading it mid-refresh sees nothing.
-- ---------------------------------------------------------------------
MERGE INTO customer_rfm_snapshot tgt
USING (
    SELECT v.*, (SELECT start_date FROM dim_campaign WHERE campaign_sk = 1) AS snapshot_date
    FROM   v_customer_rfm v
) src
ON (tgt.customer_sk = src.customer_sk)
WHEN MATCHED THEN UPDATE SET
    tgt.snapshot_date  = src.snapshot_date,
    tgt.recency_days   = src.recency_days,
    tgt.frequency      = src.frequency,
    tgt.monetary_gel   = src.monetary_gel,
    tgt.avg_ticket_gel = src.avg_ticket_gel,
    tgt.r_score        = src.r_score,
    tgt.f_score        = src.f_score,
    tgt.m_score        = src.m_score,
    tgt.rfm_cell       = src.rfm_cell,
    tgt.rfm_segment    = src.rfm_segment
WHEN NOT MATCHED THEN INSERT (
    customer_sk, snapshot_date, recency_days, frequency, monetary_gel,
    avg_ticket_gel, r_score, f_score, m_score, rfm_cell, rfm_segment
) VALUES (
    src.customer_sk, src.snapshot_date, src.recency_days, src.frequency,
    src.monetary_gel, src.avg_ticket_gel, src.r_score, src.f_score,
    src.m_score, src.rfm_cell, src.rfm_segment
);

COMMIT;


-- ---------------------------------------------------------------------
-- Segment profile — what each segment is worth
-- ---------------------------------------------------------------------
SELECT rfm_segment,
       COUNT(*)                                     AS customers,
       ROUND(RATIO_TO_REPORT(COUNT(*)) OVER (), 3)  AS pct_of_base,
       ROUND(AVG(recency_days), 1)                  AS avg_recency_days,
       ROUND(AVG(frequency), 0)                     AS avg_purchases,
       ROUND(AVG(monetary_gel), 0)                  AS avg_spend_gel,
       ROUND(SUM(monetary_gel), 0)                  AS total_spend_gel,
       ROUND(RATIO_TO_REPORT(SUM(monetary_gel)) OVER (), 3) AS pct_of_spend
FROM   customer_rfm_snapshot
GROUP  BY rfm_segment
ORDER  BY total_spend_gel DESC;
