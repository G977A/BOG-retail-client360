-- =====================================================================
-- 00_drop_all.sql — tear down in FK-safe order so the DDL is re-runnable
-- =====================================================================
-- Facts first (they hold the foreign keys), then dimensions.
-- CASCADE CONSTRAINTS drops dependent FKs; PURGE skips the recycle bin.
--
-- IF EXISTS on DROP TABLE requires Oracle 23ai. On 19c and earlier, wrap
-- each drop in a PL/SQL block that swallows ORA-00942 instead:
--
--   BEGIN EXECUTE IMMEDIATE 'DROP TABLE x'; EXCEPTION WHEN OTHERS THEN
--     IF SQLCODE != -942 THEN RAISE; END IF; END;
--   /
-- =====================================================================

DROP TABLE IF EXISTS fact_campaign_response CASCADE CONSTRAINTS PURGE;
DROP TABLE IF EXISTS fact_account_monthly   CASCADE CONSTRAINTS PURGE;
DROP TABLE IF EXISTS fact_transaction       CASCADE CONSTRAINTS PURGE;

DROP TABLE IF EXISTS dim_campaign           CASCADE CONSTRAINTS PURGE;
DROP TABLE IF EXISTS dim_merchant           CASCADE CONSTRAINTS PURGE;
DROP TABLE IF EXISTS dim_product            CASCADE CONSTRAINTS PURGE;
DROP TABLE IF EXISTS dim_customer           CASCADE CONSTRAINTS PURGE;
DROP TABLE IF EXISTS dim_date               CASCADE CONSTRAINTS PURGE;