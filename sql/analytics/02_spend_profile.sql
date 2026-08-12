-- =====================================================================
-- sql/analytics/02_spend_profile.sql — behavioural fingerprint
-- =====================================================================
-- Category spend shares, channel mix and cash intensity per customer.
-- This is the behavioural signal the segmentation depends on: two
-- customers can spend the same total and look nothing alike once the
-- composition is broken out.
--
-- SHARES, NOT AMOUNTS. Absolute spend mostly measures income. Dividing by
-- the customer's own total removes that and leaves behaviour, which is
-- what separates a student from a pensioner at the same spend level. Both
-- go into the feature set — the shares describe habit, the totals describe
-- value.
--
-- Same point-in-time rule as the RFM view: nothing after campaign start.
-- =====================================================================


-- ---------------------------------------------------------------------
-- v_customer_spend_profile
-- PIVOT turns one row per customer x category into one row per customer
-- with a column per category — the shape a feature matrix needs. Written
-- with PIVOT rather than ten CASE expressions because the category list
-- lives in one place and the intent is explicit.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_customer_spend_profile AS
WITH ref AS (
    SELECT start_date AS as_of FROM dim_campaign WHERE campaign_sk = 1
),
purchases AS (
    SELECT t.customer_sk,
           t.mcc_category,
           SUM(t.amount_gel) AS amt
    FROM   fact_transaction t
    CROSS  JOIN ref r
    WHERE  t.txn_type = 'purchase'
    AND    t.txn_date < r.as_of
    GROUP  BY t.customer_sk, t.mcc_category
),
shares AS (
    SELECT customer_sk,
           mcc_category,
           -- RATIO_TO_REPORT is Oracle's built-in "share of the window
           -- total" — equivalent to amt / SUM(amt) OVER (PARTITION BY ...)
           -- but says what it means and handles the division once.
           RATIO_TO_REPORT(amt) OVER (PARTITION BY customer_sk) AS share
    FROM   purchases
)
SELECT *
FROM   shares
PIVOT (
    SUM(share) FOR mcc_category IN (
        'groceries'     AS sh_groceries,
        'dining'        AS sh_dining,
        'ecommerce'     AS sh_ecommerce,
        'entertainment' AS sh_entertainment,
        'transport'     AS sh_transport,
        'fuel'          AS sh_fuel,
        'utilities'     AS sh_utilities,
        'healthcare'    AS sh_healthcare,
        'retail'        AS sh_retail,
        'travel'        AS sh_travel
    )
);


-- ---------------------------------------------------------------------
-- v_customer_channel_profile
-- Digital engagement and cash intensity. Cash is the single most
-- discriminating behaviour in this portfolio: a customer who withdraws
-- and spends offline is nearly invisible on the card rails, which changes
-- both how you segment them and how you reach them.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_customer_channel_profile AS
WITH ref AS (
    SELECT start_date AS as_of FROM dim_campaign WHERE campaign_sk = 1
),
activity AS (
    SELECT t.customer_sk,
           t.channel,
           t.txn_type,
           t.amount_gel
    FROM   fact_transaction t
    CROSS  JOIN ref r
    WHERE  t.txn_date < r.as_of
    AND    t.direction = 'debit'          -- outflows only; inflows are income
)
SELECT customer_sk,
       COUNT(*)                                              AS debit_txn_count,
       ROUND(SUM(amount_gel), 2)                             AS total_outflow_gel,
       -- SUM(CASE ...) / COUNT(*) is the portable way to express a
       -- conditional share. Oracle has no FILTER clause.
       ROUND(SUM(CASE WHEN channel IN ('mobile_app', 'internet_bank', 'ecommerce')
                      THEN amount_gel ELSE 0 END) / NULLIF(SUM(amount_gel), 0), 4)
                                                             AS digital_value_share,
       ROUND(SUM(CASE WHEN txn_type = 'cash_withdrawal'
                      THEN amount_gel ELSE 0 END) / NULLIF(SUM(amount_gel), 0), 4)
                                                             AS cash_value_share,
       ROUND(SUM(CASE WHEN channel = 'branch' THEN 1 ELSE 0 END) / COUNT(*), 4)
                                                             AS branch_txn_share,
       COUNT(DISTINCT channel)                               AS channels_used,
       ROUND(AVG(CASE WHEN txn_type = 'cash_withdrawal' THEN amount_gel END), 2)
                                                             AS avg_withdrawal_gel
FROM   activity
GROUP  BY customer_sk;


-- ---------------------------------------------------------------------
-- v_customer_balance_profile
-- Level, volatility and trend from the monthly snapshot. Volatility
-- separates customers who run their account to zero from those who hold a
-- buffer, even when average balances match.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_customer_balance_profile AS
WITH ref AS (
    SELECT start_date AS as_of FROM dim_campaign WHERE campaign_sk = 1
),
ca AS (
    SELECT f.customer_sk,
           f.month_end_date,
           f.balance_gel,
           -- Position within the customer's own history, so first and last
           -- month can be picked without a correlated subquery.
           ROW_NUMBER() OVER (PARTITION BY f.customer_sk
                              ORDER BY f.month_end_date)      AS mth_asc,
           ROW_NUMBER() OVER (PARTITION BY f.customer_sk
                              ORDER BY f.month_end_date DESC) AS mth_desc
    FROM   fact_account_monthly f
    JOIN   dim_product p ON p.product_sk = f.product_sk
    CROSS  JOIN ref r
    WHERE  p.product_name = 'Current Account'
    AND    f.month_end_date < r.as_of
)
SELECT customer_sk,
       ROUND(AVG(balance_gel), 2)             AS avg_balance_gel,
       ROUND(STDDEV(balance_gel), 2)          AS balance_volatility_gel,
       ROUND(MIN(balance_gel), 2)             AS min_balance_gel,
       ROUND(MAX(balance_gel), 2)             AS max_balance_gel,
       -- Coefficient of variation: volatility relative to level, so a
       -- 500 GEL swing counts differently for a student and a millionaire.
       ROUND(STDDEV(balance_gel) / NULLIF(ABS(AVG(balance_gel)), 0), 4)
                                              AS balance_cv,
       ROUND(MAX(CASE WHEN mth_desc = 1 THEN balance_gel END)
           - MAX(CASE WHEN mth_asc  = 1 THEN balance_gel END), 2)
                                              AS balance_trend_gel,
       SUM(CASE WHEN balance_gel < 0 THEN 1 ELSE 0 END) AS months_negative
FROM   ca
GROUP  BY customer_sk;


-- ---------------------------------------------------------------------
-- Sanity check: do the profiles actually separate customers?
-- If these columns look flat across income bands, the features carry no
-- signal and clustering will find nothing worth naming.
-- ---------------------------------------------------------------------
SELECT c.income_band,
       COUNT(*)                       AS customers,
       ROUND(AVG(s.sh_groceries), 3)  AS groceries,
       ROUND(AVG(s.sh_dining), 3)     AS dining,
       ROUND(AVG(s.sh_travel), 3)     AS travel,
       ROUND(AVG(s.sh_healthcare), 3) AS healthcare,
       ROUND(AVG(ch.digital_value_share), 3) AS digital,
       ROUND(AVG(ch.cash_value_share), 3)    AS cash,
       ROUND(AVG(b.avg_balance_gel), 0)      AS avg_balance
FROM   dim_customer c
JOIN   v_customer_spend_profile   s  ON s.customer_sk  = c.customer_sk
JOIN   v_customer_channel_profile ch ON ch.customer_sk = c.customer_sk
JOIN   v_customer_balance_profile b  ON b.customer_sk  = c.customer_sk
GROUP  BY c.income_band
ORDER  BY avg_balance DESC;
