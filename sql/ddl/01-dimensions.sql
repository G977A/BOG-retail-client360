-- =====================================================================
-- 01_dimensions.sql — dimension tables for the retail-client360 star
-- =====================================================================
-- Conventions
--   *_sk            surrogate keys, NUMBER, supplied by the source (see below)
--   pk_/fk_/chk_    every constraint is named — system-generated names like
--                   SYS_C0011423 make production errors unreadable
--   VARCHAR2(n CHAR) character semantics, not bytes. Georgian text is 3 bytes
--                   per character in UTF-8, so VARCHAR2(20) would hold only
--                   ~6 Georgian letters. CHAR semantics makes the length mean
--                   what it says regardless of script.
--   NUMBER(1)       booleans. Oracle 23ai has a native BOOLEAN type, but 19c
--                   (what most banks run) does not, and BI tool drivers are
--                   inconsistent with it. NUMBER(1) + CHECK is portable and
--                   maps cleanly to Power BI.
--   DATE            not TIMESTAMP — no source column carries a time component.
--
-- NO IDENTITY COLUMNS. Surrogate keys arrive from upstream (the generator
-- assigns customer_sk 1..N, and the fact tables already reference those
-- values). Identity is correct when the WAREHOUSE mints keys during ETL;
-- here it would overwrite keys the facts depend on. Two cases where it
-- breaks outright: date_sk is a smart key (20250101, not a sequence) and
-- merchant_sk starts at 0.
--
-- CHECK constraint value lists are the DDL half of a data contract. They
-- mirror src/generator/config.py — PERSONAS defines income_band and
-- employment, PRODUCTS defines product names and groups, MCC_CATEGORIES
-- defines merchant categories. If that file changes, these change with it.
--
-- Ground truth (gt_customer_persona, gt_customer_uplift) is deliberately
-- NOT loaded into this database. It stays in Parquet under
-- data/parquet/ground_truth/ and is read only at validation time, so the
-- seal in decision record 0002 is enforced by the database boundary itself.
-- =====================================================================


-- ---------------------------------------------------------------------
-- dim_date — calendar. Loaded first: other tables reference it.
-- ---------------------------------------------------------------------
CREATE TABLE dim_date (
    date_sk        NUMBER(8)        NOT NULL,   -- smart key: YYYYMMDD
    full_date      DATE,                        -- NULL only for the Unknown member
    year           NUMBER(4),
    quarter        NUMBER(1),
    month          NUMBER(2),
    month_name     VARCHAR2(12 CHAR),           -- 'September' = 9 chars
    day_of_month   NUMBER(2),
    day_of_week    NUMBER(1),                   -- 1 = Monday
    day_name       VARCHAR2(12 CHAR),
    is_weekend     NUMBER(1),
    is_month_end   NUMBER(1),
    year_month     VARCHAR2(7 CHAR),            -- 'YYYY-MM'

    CONSTRAINT pk_dim_date          PRIMARY KEY (date_sk),
    CONSTRAINT uq_dim_date_full     UNIQUE (full_date),
    CONSTRAINT chk_date_quarter     CHECK (quarter      BETWEEN 1 AND 4),
    CONSTRAINT chk_date_month       CHECK (month        BETWEEN 1 AND 12),
    CONSTRAINT chk_date_dom         CHECK (day_of_month BETWEEN 1 AND 31),
    CONSTRAINT chk_date_dow         CHECK (day_of_week  BETWEEN 1 AND 7),
    CONSTRAINT chk_date_weekend     CHECK (is_weekend   IN (0, 1)),
    CONSTRAINT chk_date_month_end   CHECK (is_month_end IN (0, 1)),
    -- LIKE is not a format mask: 'YYYY-MM' would match only that literal
    -- string. REGEXP_LIKE actually validates the shape.
    CONSTRAINT chk_date_year_month  CHECK (REGEXP_LIKE(year_month, '^[0-9]{4}-[0-9]{2}$'))
);


-- ---------------------------------------------------------------------
-- dim_customer
-- ---------------------------------------------------------------------
CREATE TABLE dim_customer (
    customer_sk             NUMBER(10)        NOT NULL,
    age                     NUMBER(3)         NOT NULL,
    gender                  VARCHAR2(1 CHAR)  NOT NULL,
    city                    VARCHAR2(50 CHAR) NOT NULL,
    income_band             VARCHAR2(10 CHAR) NOT NULL,
    employment              VARCHAR2(20 CHAR) NOT NULL,
    tenure_years            NUMBER(3)         NOT NULL,
    relationship_start_date DATE              NOT NULL,
    existing_product_count  NUMBER(3)         NOT NULL,
    digital_engagement_flag NUMBER(1)         NOT NULL,

    CONSTRAINT pk_dim_customer      PRIMARY KEY (customer_sk),
    -- Values are matched exactly as stored, not wrapped in UPPER(). A check
    -- like UPPER(gender) IN ('M','F') would accept both 'm' and 'M' and let
    -- both into the column, so GROUP BY gender then returns two rows for one
    -- real value — which defeats the purpose of constraining it.
    CONSTRAINT chk_cust_gender      CHECK (gender IN ('M', 'F')),
    CONSTRAINT chk_cust_income      CHECK (income_band IN ('low', 'low_mid', 'mid', 'high')),
    CONSTRAINT chk_cust_employment  CHECK (employment IN ('employed', 'self_employed',
                                                          'retired', 'student')),
    CONSTRAINT chk_cust_age         CHECK (age BETWEEN 18 AND 120),
    CONSTRAINT chk_cust_tenure      CHECK (tenure_years >= 0),
    CONSTRAINT chk_cust_products    CHECK (existing_product_count >= 0),
    CONSTRAINT chk_cust_digital     CHECK (digital_engagement_flag IN (0, 1))
);


-- ---------------------------------------------------------------------
-- dim_product
-- ---------------------------------------------------------------------
CREATE TABLE dim_product (
    product_sk         NUMBER(10)        NOT NULL,
    product_name       VARCHAR2(50 CHAR) NOT NULL,
    product_group      VARCHAR2(30 CHAR) NOT NULL,
    is_campaign_target NUMBER(1)         NOT NULL,

    CONSTRAINT pk_dim_product       PRIMARY KEY (product_sk),
    CONSTRAINT uq_dim_product_name  UNIQUE (product_name),
    CONSTRAINT chk_prod_group       CHECK (product_group IN ('daily_banking',
                                                             'lending', 'deposits')),
    CONSTRAINT chk_prod_target      CHECK (is_campaign_target IN (0, 1))
);


-- ---------------------------------------------------------------------
-- dim_merchant
-- merchant_sk = 0 is the 'Not Applicable' member, used by cash withdrawals
-- and inflows. A star schema uses an N/A dimension row rather than a NULL
-- foreign key, so every fact-to-dimension join stays an inner join and row
-- counts never silently drop.
-- ---------------------------------------------------------------------
CREATE TABLE dim_merchant (
    merchant_sk   NUMBER(10)        NOT NULL,
    merchant_name VARCHAR2(60 CHAR) NOT NULL,
    mcc_code      NUMBER(4)         NOT NULL,   -- real MCCs are 4 digits (5411, 8011)
    mcc_category  VARCHAR2(20 CHAR) NOT NULL,

    CONSTRAINT pk_dim_merchant      PRIMARY KEY (merchant_sk),
    CONSTRAINT chk_merch_category   CHECK (mcc_category IN (
        'groceries', 'dining', 'ecommerce', 'entertainment', 'transport',
        'fuel', 'utilities', 'healthcare', 'retail', 'travel', 'n/a')),
    CONSTRAINT chk_merch_sk         CHECK (merchant_sk >= 0)
);


-- ---------------------------------------------------------------------
-- dim_campaign
-- ---------------------------------------------------------------------
CREATE TABLE dim_campaign (
    campaign_sk          NUMBER(10)        NOT NULL,
    campaign_name        VARCHAR2(100 CHAR) NOT NULL,
    target_product_sk    NUMBER(10)        NOT NULL,
    start_date           DATE              NOT NULL,
    end_date             DATE              NOT NULL,
    measurement_end_date DATE              NOT NULL,
    contact_channel      VARCHAR2(20 CHAR) NOT NULL,
    cost_per_contact_gel NUMBER(8,2)       NOT NULL,
    treatment_share      NUMBER(4,3)       NOT NULL,

    CONSTRAINT pk_dim_campaign      PRIMARY KEY (campaign_sk),
    CONSTRAINT fk_campaign_product  FOREIGN KEY (target_product_sk)
                                    REFERENCES dim_product (product_sk),
    CONSTRAINT chk_camp_dates       CHECK (end_date >= start_date
                                       AND measurement_end_date >= end_date),
    CONSTRAINT chk_camp_share       CHECK (treatment_share BETWEEN 0 AND 1),
    CONSTRAINT chk_camp_cost        CHECK (cost_per_contact_gel >= 0)
);


-- ---------------------------------------------------------------------
-- Unknown date member.
-- fact_campaign_response stores contact_date_sk = 0 for control customers
-- (never contacted) and response_date_sk = 0 for non-responders. Without
-- this row those foreign keys have nothing to point at. Seeding an
-- 'Unknown' member is the standard alternative to nullable dimension keys.
-- ---------------------------------------------------------------------
INSERT INTO dim_date (date_sk, full_date, year, quarter, month, month_name,
                      day_of_month, day_of_week, day_name,
                      is_weekend, is_month_end, year_month)
VALUES (0, NULL, NULL, NULL, NULL, 'Unknown',
        NULL, NULL, 'Unknown', NULL, NULL, NULL);

COMMIT;