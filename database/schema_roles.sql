-- Roles for the Polymarket Explorer.
--
-- Run this ONCE as a superuser on the polymarket_db database:
--   sudo -u postgres psql -d polymarket_db -f database/schema_roles.sql
--
-- There are two application roles:
--
--   polymarket    — full owner. Used by the indexer, sync scripts, auth,
--                   and session persistence. Creates and maintains tables.
--
--   polymarket_ro — read-only analyst. Used by AI-generated SQL (via the
--                   asyncpg pool in chat/db_pool.py) and AI-generated
--                   Python (via query_db in chat/python_runner.py).
--                   Writes are rejected at the DB layer.
--
-- In production, set the password via env (PM_DB_ANALYST_PASSWORD) and
-- change the placeholder below before running.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'polymarket_ro') THEN
        CREATE ROLE polymarket_ro LOGIN PASSWORD 'polymarket_ro_secret_change_me';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE polymarket_db TO polymarket_ro;
GRANT USAGE ON SCHEMA public TO polymarket_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO polymarket_ro;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO polymarket_ro;

-- Future tables/sequences created by the owner role are auto-granted to the analyst.
ALTER DEFAULT PRIVILEGES FOR ROLE polymarket IN SCHEMA public
    GRANT SELECT ON TABLES TO polymarket_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE polymarket IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO polymarket_ro;
