-- ---------------------------------------------------------------------------
-- Runs ONCE, automatically, the first time the container initialises.
-- (gvenzl/oracle-free executes everything in /container-entrypoint-initdb.d
--  in alphabetical order, connected as SYSDBA.)
--
-- The APP_USER from docker-compose.yml is already created. This script adds
-- the second schema: RBA_TRUTH.
--
-- Why two schemas? RBA_TRUTH holds the hidden persona labels and the true
-- per-customer campaign uplift used to *generate* the data. Keeping it in a
-- separate schema that the modelling code never connects to makes it
-- structurally impossible to leak ground truth into a feature. It is opened
-- only at evaluation time.
-- ---------------------------------------------------------------------------

ALTER SESSION SET CONTAINER = FREEPDB1;

CREATE USER rba_truth IDENTIFIED BY change_me_truth
  QUOTA UNLIMITED ON USERS;

GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW, CREATE SEQUENCE TO rba_truth;

-- the app user may read truth tables, but only via explicit grants added later
-- (see sql/10_ddl/04_grants.sql) — nothing is readable by default.

-- Give the app user room to work.
ALTER USER rba QUOTA UNLIMITED ON USERS;
GRANT CREATE VIEW, CREATE MATERIALIZED VIEW, CREATE SEQUENCE, CREATE PROCEDURE TO rba;

-- Session-level defaults that make analytic SQL less painful.
ALTER SYSTEM SET nls_date_format = 'YYYY-MM-DD' SCOPE = SPFILE;

EXIT;
