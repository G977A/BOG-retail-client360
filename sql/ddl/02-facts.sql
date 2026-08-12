-- =====================================================================
-- 02_facts.sql — fact tables
-- =====================================================================
-- Design notes
--
-- GRAIN AS A CONSTRAINT. Where a fact has a natural composite key, it is
-- declared as the primary key. The PK is then the grain statement written
-- in DDL: fact_account_monthly is one row per customer x product x month,
-- and the database will refuse anything else. A duplicate-row bug becomes
-- a load failure instead of a silently doubled balance in a dashboard.
--
-- fact_transaction has NO primary key. The source supplies no transaction
-- identifier, and adding a surrogate would mean an extra index on ~34M
-- rows to enforce uniqueness that the ETL already guarantees. Large
-- transaction facts commonly go without one. The cost is that a restarted
-- load can duplicate rows, so the ingest truncates before loading rather
-- than merging.
--
-- PARTITIONING. fact_transaction is range-partitioned by month on txn_date
-- with INTERVAL, so Oracle creates each new monthly partition on demand
-- instead of requiring them to be pre-declared. This buys partition
-- pruning: a query filtered to one month reads one partition, not 34M
-- rows. Partitioning is included in Oracle Database Free 23ai. If your
-- edition rejects it, delete the PARTITION BY clause — everything else
-- works unchanged, just slower.
--
-- FOREIGN KEYS. Created and enabled. Warehouses often disable them for
-- bulk load speed and re-enable afterwards, or declare them RELY
-- DISABLE NOVALIDATE so the optimizer can use them without paying
-- enforcement cost. The ingest script disables and re-validates around
-- the load — the standard pattern, and worth being able to explain.
-- =====================================================================


-- ---------------------------------------------------------------------
-- fact_transaction — one row per transaction
-- ---------------------------------------------------------------------
CREATE TABLE fact_transaction (
    customer_sk  NUMBER(10)        NOT NULL,
    date_sk      NUMBER(8)         NOT NULL,
    merchant_sk  NUMBER(10)        NOT NULL,
    txn_date     DATE              NOT NULL,   -- partition key
    txn_type     VARCHAR2(20 CHAR) NOT NULL,
    direction    VARCHAR2(6 CHAR)  NOT NULL,
    channel      VARCHAR2(20 CHAR) NOT NULL,
    -- Denormalised from dim_merchant. Redundant (merchant_sk resolves it)
    -- but it removes a join from the highest-volume aggregations, which is
    -- the usual trade a warehouse makes: storage is cheap, joins at 34M
    -- rows are not. The cost is that the two must be kept consistent.
    mcc_category VARCHAR2(20 CHAR) NOT NULL,
    amount_gel   NUMBER(12,2)      NOT NULL,

    CONSTRAINT fk_ftxn_customer  FOREIGN KEY (customer_sk) REFERENCES dim_customer (customer_sk),
    CONSTRAINT fk_ftxn_date      FOREIGN KEY (date_sk)     REFERENCES dim_date (date_sk),
    CONSTRAINT fk_ftxn_merchant  FOREIGN KEY (merchant_sk) REFERENCES dim_merchant (merchant_sk),
    CONSTRAINT chk_ftxn_type     CHECK (txn_type  IN ('purchase', 'cash_withdrawal', 'inflow')),
    CONSTRAINT chk_ftxn_dir      CHECK (direction IN ('debit', 'credit')),
    CONSTRAINT chk_ftxn_amount   CHECK (amount_gel >= 0),
    -- Only purchases carry a merchant; cash and inflows use the N/A member.
    CONSTRAINT chk_ftxn_merchant CHECK (txn_type = 'purchase' OR merchant_sk = 0)
)
PARTITION BY RANGE (txn_date)
INTERVAL (NUMTOYMINTERVAL(1, 'MONTH'))
(
    -- Seed partition sits below the first data date; INTERVAL creates the
    -- rest automatically as rows arrive.
    PARTITION p_seed VALUES LESS THAN (DATE '2025-01-01')
);


-- ---------------------------------------------------------------------
-- fact_account_monthly — one row per customer x product x month-end
-- ---------------------------------------------------------------------
CREATE TABLE fact_account_monthly (
    customer_sk       NUMBER(10)   NOT NULL,
    product_sk        NUMBER(10)   NOT NULL,
    date_sk           NUMBER(8)    NOT NULL,
    month_end_date    DATE         NOT NULL,
    -- Liabilities (loans, credit cards) are stored negative, so the range
    -- is signed and wider than a pure balance column would need.
    balance_gel       NUMBER(14,2) NOT NULL,
    opened_this_month NUMBER(1)    NOT NULL,
    is_held           NUMBER(1)    NOT NULL,

    CONSTRAINT pk_fact_account_monthly PRIMARY KEY (customer_sk, product_sk, date_sk),
    CONSTRAINT fk_fam_customer FOREIGN KEY (customer_sk) REFERENCES dim_customer (customer_sk),
    CONSTRAINT fk_fam_product  FOREIGN KEY (product_sk)  REFERENCES dim_product (product_sk),
    CONSTRAINT fk_fam_date     FOREIGN KEY (date_sk)     REFERENCES dim_date (date_sk),
    CONSTRAINT chk_fam_opened  CHECK (opened_this_month IN (0, 1)),
    CONSTRAINT chk_fam_held    CHECK (is_held IN (0, 1))
);


-- ---------------------------------------------------------------------
-- fact_campaign_response — one row per customer x campaign
-- The experiment table. The CHECK constraints below encode the
-- experimental design itself, so a bug in assignment or outcome logic
-- fails the load rather than quietly corrupting the uplift estimate.
-- ---------------------------------------------------------------------
CREATE TABLE fact_campaign_response (
    customer_sk       NUMBER(10)        NOT NULL,
    campaign_sk       NUMBER(10)        NOT NULL,
    assignment        VARCHAR2(10 CHAR) NOT NULL,
    contacted         NUMBER(1)         NOT NULL,
    contact_date      DATE,                        -- NULL for control
    contact_date_sk   NUMBER(8)         NOT NULL,  -- 0 = Unknown member
    responded         NUMBER(1)         NOT NULL,
    response_date     DATE,                        -- NULL for non-responders
    response_date_sk  NUMBER(8)         NOT NULL,  -- 0 = Unknown member
    card_opened       NUMBER(1)         NOT NULL,
    activated         NUMBER(1)         NOT NULL,
    annual_value_gel  NUMBER(12,2)      NOT NULL,
    contact_cost_gel  NUMBER(8,2)       NOT NULL,

    CONSTRAINT pk_fact_campaign_response PRIMARY KEY (customer_sk, campaign_sk),
    CONSTRAINT fk_fcr_customer  FOREIGN KEY (customer_sk)      REFERENCES dim_customer (customer_sk),
    CONSTRAINT fk_fcr_campaign  FOREIGN KEY (campaign_sk)      REFERENCES dim_campaign (campaign_sk),
    CONSTRAINT fk_fcr_cdate     FOREIGN KEY (contact_date_sk)  REFERENCES dim_date (date_sk),
    CONSTRAINT fk_fcr_rdate     FOREIGN KEY (response_date_sk) REFERENCES dim_date (date_sk),

    CONSTRAINT chk_fcr_assignment CHECK (assignment IN ('treatment', 'control')),
    CONSTRAINT chk_fcr_flags      CHECK (contacted   IN (0, 1)
                                     AND responded   IN (0, 1)
                                     AND card_opened IN (0, 1)
                                     AND activated   IN (0, 1)),
    -- The holdout is a holdout: control customers are never contacted, and
    -- every treated customer was. If this ever fails, the randomisation
    -- is broken and the uplift estimate is meaningless.
    CONSTRAINT chk_fcr_holdout    CHECK ((assignment = 'treatment' AND contacted = 1)
                                      OR (assignment = 'control'   AND contacted = 0)),
    -- A card cannot be activated without being opened.
    CONSTRAINT chk_fcr_activation CHECK (activated <= card_opened),
    -- Response is defined as activation (decision record 0007).
    CONSTRAINT chk_fcr_response   CHECK (responded = activated),
    -- Revenue only from responders; contact cost only for the contacted.
    CONSTRAINT chk_fcr_value      CHECK (responded = 1 OR annual_value_gel = 0),
    CONSTRAINT chk_fcr_cost       CHECK (contacted = 1 OR contact_cost_gel = 0),
    -- Dates and their keys agree about whether the event happened.
    CONSTRAINT chk_fcr_cdate_pair CHECK ((contact_date IS NULL  AND contact_date_sk  = 0)
                                      OR (contact_date IS NOT NULL AND contact_date_sk  > 0)),
    CONSTRAINT chk_fcr_rdate_pair CHECK ((response_date IS NULL AND response_date_sk = 0)
                                      OR (response_date IS NOT NULL AND response_date_sk > 0))
);