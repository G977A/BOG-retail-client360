-- =====================================================================
-- sql/checks/01_data_quality.sql — run after every load
-- =====================================================================
-- One query, one row per check, violation count in a column. Returning all
-- checks together rather than as separate scripts means a single glance
-- tells you whether the warehouse is trustworthy, and the output is easy
-- to schedule and diff between loads.
--
-- Constraints in the DDL already prevent most structural problems. These
-- checks cover what constraints cannot: cross-table consistency, business
-- plausibility, and the "loaded successfully but wrong" class of failure
-- that produces a confident, incorrect dashboard.
--
-- Anything with severity ERROR must be zero before the data is used.
-- =====================================================================

WITH checks AS (

    -- 1. Every customer should have transaction history. A customer with
    --    none is either a generator bug or a partial load.
    SELECT 'customers_without_transactions' AS check_name,
           'ERROR' AS severity,
           COUNT(*) AS violations
    FROM   dim_customer c
    WHERE  NOT EXISTS (SELECT 1 FROM fact_transaction t
                       WHERE t.customer_sk = c.customer_sk)

    UNION ALL
    -- 2. Amounts are stored unsigned; direction carries the sign.
    SELECT 'negative_transaction_amounts', 'ERROR', COUNT(*)
    FROM   fact_transaction WHERE amount_gel < 0

    UNION ALL
    -- 3. Only purchases have a merchant. Cash and inflows must use the
    --    'Not Applicable' member (merchant_sk = 0).
    SELECT 'non_purchase_with_merchant', 'ERROR', COUNT(*)
    FROM   fact_transaction
    WHERE  txn_type <> 'purchase' AND merchant_sk <> 0

    UNION ALL
    -- 4. The denormalised category on the fact must agree with the
    --    merchant dimension it was copied from. This is the price of the
    --    denormalisation in fact_transaction, so it gets checked.
    SELECT 'category_disagrees_with_merchant', 'ERROR', COUNT(*)
    FROM   fact_transaction t
    JOIN   dim_merchant m ON m.merchant_sk = t.merchant_sk
    WHERE  t.mcc_category <> m.mcc_category

    UNION ALL
    -- 5. The holdout must be a holdout. If a control customer was
    --    contacted, the experiment is broken and every uplift number
    --    downstream is meaningless.
    SELECT 'control_customers_contacted', 'ERROR', COUNT(*)
    FROM   fact_campaign_response
    WHERE  assignment = 'control' AND (contacted = 1 OR contact_date IS NOT NULL)

    UNION ALL
    -- 6. Campaign eligibility: nobody in the campaign should have held a
    --    credit card before it started.
    SELECT 'campaign_targeted_existing_cardholders', 'ERROR', COUNT(*)
    FROM   fact_campaign_response r
    WHERE  r.responded = 0
    AND    EXISTS (
             SELECT 1
             FROM   fact_account_monthly f
             JOIN   dim_product p ON p.product_sk = f.product_sk
             JOIN   dim_campaign c ON c.campaign_sk = r.campaign_sk
             WHERE  f.customer_sk = r.customer_sk
             AND    p.product_name = 'Credit Card'
             AND    f.month_end_date < c.start_date)

    UNION ALL
    -- 7. Revenue is only recognised on activated cards (decision 0007).
    SELECT 'revenue_without_response', 'ERROR', COUNT(*)
    FROM   fact_campaign_response
    WHERE  responded = 0 AND annual_value_gel <> 0

    UNION ALL
    -- 8. Transactions must fall inside the generation window. Rows outside
    --    it mean a date-handling bug, which silently distorts every
    --    time-based measure.
    SELECT 'transactions_outside_window', 'ERROR', COUNT(*)
    FROM   fact_transaction t
    WHERE  NOT EXISTS (SELECT 1 FROM dim_date d WHERE d.date_sk = t.date_sk)

    UNION ALL
    -- 9. Current accounts below the overdraft floor. Not a load error —
    --    a modelling warning that spending is outrunning income.
    SELECT 'current_accounts_below_floor', 'WARNING', COUNT(*)
    FROM   fact_account_monthly f
    JOIN   dim_product p ON p.product_sk = f.product_sk
    WHERE  p.product_name = 'Current Account' AND f.balance_gel < -500

    UNION ALL
    -- 10. Randomisation sanity: the realised treatment share should sit
    --     close to the configured one. Drift means assignment is not
    --     random, which biases the whole causal read.
    SELECT 'treatment_share_off_target', 'WARNING',
           CASE WHEN ABS(AVG(CASE WHEN assignment = 'treatment' THEN 1 ELSE 0 END)
                         - MAX(c.treatment_share)) > 0.02
                THEN 1 ELSE 0 END
    FROM   fact_campaign_response r
    JOIN   dim_campaign c ON c.campaign_sk = r.campaign_sk
)
SELECT check_name,
       severity,
       violations,
       CASE WHEN violations = 0 THEN 'PASS' ELSE 'FAIL' END AS status
FROM   checks
ORDER  BY CASE severity WHEN 'ERROR' THEN 1 ELSE 2 END,
          violations DESC,
          check_name;
