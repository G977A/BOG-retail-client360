-- =====================================================================
-- sql/analytics/03_campaign_effectiveness.sql — the headline analysis
-- =====================================================================
-- Measures what the campaign actually caused, as distinct from what
-- happened after it.
--
-- THE CORE PROBLEM. Counting everyone who took the card after being
-- contacted answers the wrong question, because most of them would have
-- taken it anyway. The randomised holdout gives the missing counterfactual:
-- because assignment was random, treatment and control differ only in
-- contact, so the gap between their take-up rates is the causal effect.
--
--     incremental take-up = treatment rate - control rate
--
-- Every number below that matters is built from that difference, never
-- from the treated group alone.
--
-- CONFIDENCE INTERVALS ARE NOT OPTIONAL. A lift reported as a bare point
-- estimate invites decisions the data cannot support. The standard error
-- of a difference in two proportions is computed inline, so every lift
-- figure carries an interval and it is immediately visible when a
-- segment's apparent lift is indistinguishable from zero.
-- =====================================================================


-- ---------------------------------------------------------------------
-- v_campaign_arms — take-up by assignment arm. Small building block the
-- rest of the file reuses.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_campaign_arms AS
SELECT r.campaign_sk,
       r.assignment,
       COUNT(*)                     AS customers,
       SUM(r.responded)             AS responders,
       -- responded is NUMBER(1), so AVG() over it is the response rate.
       AVG(r.responded)             AS response_rate,
       SUM(r.card_opened)           AS cards_opened,
       SUM(r.annual_value_gel)      AS revenue_gel,
       SUM(r.contact_cost_gel)      AS cost_gel
FROM   fact_campaign_response r
GROUP  BY r.campaign_sk, r.assignment;


-- ---------------------------------------------------------------------
-- v_campaign_lift — the causal read, with interval and economics
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_campaign_lift AS
WITH arms AS (
    SELECT campaign_sk,
           MAX(CASE WHEN assignment = 'treatment' THEN customers END)     AS n_t,
           MAX(CASE WHEN assignment = 'control'   THEN customers END)     AS n_c,
           MAX(CASE WHEN assignment = 'treatment' THEN response_rate END) AS p_t,
           MAX(CASE WHEN assignment = 'control'   THEN response_rate END) AS p_c,
           MAX(CASE WHEN assignment = 'treatment' THEN responders END)    AS resp_t,
           MAX(CASE WHEN assignment = 'treatment' THEN cost_gel END)      AS cost,
           MAX(CASE WHEN assignment = 'treatment' THEN revenue_gel END)   AS gross_revenue
    FROM   v_campaign_arms
    GROUP  BY campaign_sk
),
calc AS (
    SELECT a.*,
           a.p_t - a.p_c AS lift,
           -- SE of a difference in independent proportions.
           SQRT(a.p_t * (1 - a.p_t) / a.n_t
              + a.p_c * (1 - a.p_c) / a.n_c) AS se,
           -- Average value of a responder, used to price the incremental
           -- conversions. Taken from treated responders only.
           CASE WHEN a.resp_t > 0 THEN a.gross_revenue / a.resp_t END AS avg_value
    FROM   arms a
)
SELECT c.campaign_sk,
       c.n_t                                          AS treated,
       c.n_c                                          AS control,
       ROUND(c.p_t, 4)                                AS treatment_rate,
       ROUND(c.p_c, 4)                                AS control_rate,
       ROUND(c.lift, 4)                               AS incremental_rate,
       ROUND(c.se, 4)                                 AS std_error,
       ROUND(c.lift - 1.96 * c.se, 4)                 AS ci_low,
       ROUND(c.lift + 1.96 * c.se, 4)                 AS ci_high,
       -- Significant only when the interval excludes zero.
       CASE WHEN c.lift - 1.96 * c.se > 0 THEN 'YES' ELSE 'NO' END AS significant,

       -- What a naive report would claim: every treated responder counted
       -- as a campaign win.
       c.resp_t                                       AS naive_conversions,
       -- What the campaign actually caused.
       ROUND(c.lift * c.n_t, 0)                       AS incremental_conversions,
       ROUND(c.resp_t / NULLIF(c.lift * c.n_t, 0), 1) AS naive_overstatement_x,

       ROUND(c.cost, 0)                               AS campaign_cost_gel,
       ROUND(c.gross_revenue, 0)                      AS naive_revenue_gel,
       ROUND(c.lift * c.n_t * c.avg_value, 0)         AS incremental_revenue_gel,
       ROUND((c.lift * c.n_t * c.avg_value - c.cost)
             / NULLIF(c.cost, 0), 2)                  AS incremental_roi,
       -- Cost of acquiring one genuinely incremental customer. The number
       -- that should drive budget decisions, and always worse than the
       -- naive cost-per-response.
       ROUND(c.cost / NULLIF(c.lift * c.n_t, 0), 2)   AS cost_per_incremental_gel
FROM   calc c;


-- ---------------------------------------------------------------------
-- Lift by segment — where the budget should go
-- ---------------------------------------------------------------------
-- Segment-level lift is what turns a campaign read into a targeting
-- strategy. A segment whose interval spans zero has no demonstrated
-- effect; a segment with negative lift is actively losing money and
-- should be suppressed, not merely deprioritised.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_campaign_lift_by_segment AS
WITH tagged AS (
    SELECT r.assignment,
           r.responded,
           NVL(s.rfm_segment, 'No History') AS segment
    FROM   fact_campaign_response r
    LEFT   JOIN customer_rfm_snapshot s ON s.customer_sk = r.customer_sk
    WHERE  r.campaign_sk = 1
),
arms AS (
    SELECT segment,
           SUM(CASE WHEN assignment = 'treatment' THEN 1 ELSE 0 END) AS n_t,
           SUM(CASE WHEN assignment = 'control'   THEN 1 ELSE 0 END) AS n_c,
           AVG(CASE WHEN assignment = 'treatment' THEN responded END) AS p_t,
           AVG(CASE WHEN assignment = 'control'   THEN responded END) AS p_c
    FROM   tagged
    GROUP  BY segment
)
SELECT segment,
       n_t + n_c                       AS customers,
       ROUND(p_t, 4)                   AS treatment_rate,
       ROUND(p_c, 4)                   AS control_rate,
       ROUND(p_t - p_c, 4)             AS incremental_rate,
       ROUND(p_t - p_c - 1.96 * SQRT(p_t * (1 - p_t) / NULLIF(n_t, 0)
                                   + p_c * (1 - p_c) / NULLIF(n_c, 0)), 4) AS ci_low,
       ROUND(p_t - p_c + 1.96 * SQRT(p_t * (1 - p_t) / NULLIF(n_t, 0)
                                   + p_c * (1 - p_c) / NULLIF(n_c, 0)), 4) AS ci_high,
       CASE
           WHEN p_t - p_c - 1.96 * SQRT(p_t * (1 - p_t) / NULLIF(n_t, 0)
                                      + p_c * (1 - p_c) / NULLIF(n_c, 0)) > 0
                THEN 'TARGET'
           WHEN p_t - p_c + 1.96 * SQRT(p_t * (1 - p_t) / NULLIF(n_t, 0)
                                      + p_c * (1 - p_c) / NULLIF(n_c, 0)) < 0
                THEN 'SUPPRESS'
           ELSE 'NO EVIDENCE'
       END AS recommendation
FROM   arms
ORDER  BY incremental_rate DESC NULLS LAST;


-- ---------------------------------------------------------------------
-- Response curve — how take-up accumulates after contact
-- ---------------------------------------------------------------------
-- A running total by days-since-contact shows how long the measurement
-- window needs to be. If the curve is still climbing at the window edge,
-- the campaign is being measured too early and its effect understated.
-- ---------------------------------------------------------------------
SELECT days_since_contact,
       responders,
       SUM(responders) OVER (ORDER BY days_since_contact) AS cumulative,
       ROUND(RATIO_TO_REPORT(responders) OVER (), 4)      AS pct_of_total,
       ROUND(SUM(responders) OVER (ORDER BY days_since_contact)
             / SUM(responders) OVER (), 4)                AS cumulative_pct
FROM (
    SELECT TRUNC(r.response_date - r.contact_date) AS days_since_contact,
           COUNT(*)                                AS responders
    FROM   fact_campaign_response r
    WHERE  r.campaign_sk = 1
    AND    r.assignment = 'treatment'
    AND    r.responded = 1
    GROUP  BY TRUNC(r.response_date - r.contact_date)
)
ORDER  BY days_since_contact;
