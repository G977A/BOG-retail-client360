-- =====================================================================
-- 03_indexes.sql — run AFTER the data load, not before
-- =====================================================================
-- Building an index once over a finished table is substantially faster
-- than maintaining it row by row during a 34M-row insert. Primary key and
-- unique constraints already created their indexes in the DDL; these are
-- the additional access paths the analytics layer needs.
--
-- B-TREE vs BITMAP
--   B-tree suits high-cardinality columns (customer_sk: 100k distinct
--   values) and is the default choice.
--   Bitmap suits low-cardinality columns (channel: 7 values, txn_type: 3)
--   and combines efficiently across predicates — Oracle can AND several
--   bitmaps together before touching the table, which is exactly the shape
--   of warehouse filtering ("mobile app purchases in the dining category").
--   Bitmaps are compact but lock large row ranges on DML, so they belong
--   in batch-loaded warehouses and never in an OLTP system. This table is
--   loaded once and read many times, which is the case they exist for.
--
-- LOCAL means one index segment per partition. On a partitioned table this
-- keeps index maintenance partition-scoped: loading a new month rebuilds
-- only that month's index pieces, and dropping an old partition drops its
-- index with it.
-- =====================================================================


-- --- fact_transaction -------------------------------------------------
-- Per-customer feature building scans one customer's whole history; this
-- is the workhorse index for the PySpark feature layer and for RFM.
CREATE INDEX ix_ftxn_customer ON fact_transaction (customer_sk) LOCAL;

-- Joins to dim_date when the filter is on a date attribute (quarter,
-- weekend) rather than a raw date range.
CREATE INDEX ix_ftxn_date ON fact_transaction (date_sk) LOCAL;

-- Low-cardinality filters, combined constantly in the analytics layer.
CREATE BITMAP INDEX bix_ftxn_channel  ON fact_transaction (channel)      LOCAL;
CREATE BITMAP INDEX bix_ftxn_category ON fact_transaction (mcc_category) LOCAL;
CREATE BITMAP INDEX bix_ftxn_type     ON fact_transaction (txn_type)     LOCAL;


-- --- fact_account_monthly --------------------------------------------
-- The PK (customer_sk, product_sk, date_sk) already covers customer-led
-- access. These serve the other two directions: product penetration over
-- time, and whole-portfolio snapshots at a given month end.
CREATE INDEX ix_fam_product ON fact_account_monthly (product_sk, date_sk);
CREATE INDEX ix_fam_date    ON fact_account_monthly (date_sk);


-- --- fact_campaign_response ------------------------------------------
-- The PK covers customer lookups. Campaign-level reads slice by
-- assignment arm, which is every uplift query.
CREATE INDEX ix_fcr_campaign ON fact_campaign_response (campaign_sk, assignment);


-- --- optimiser statistics --------------------------------------------
-- Without current statistics the optimiser guesses cardinalities and will
-- happily pick a nested loop over 34M rows. Gathering stats after a bulk
-- load is not optional.
BEGIN
    DBMS_STATS.GATHER_SCHEMA_STATS(
        ownname          => USER,
        cascade          => TRUE,          -- include indexes
        degree           => DBMS_STATS.AUTO_DEGREE,
        estimate_percent => DBMS_STATS.AUTO_SAMPLE_SIZE
    );
END;
