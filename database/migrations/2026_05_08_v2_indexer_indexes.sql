-- V2 indexer migration — partial indexes on (exchange_version).
--
-- Companion to 2026_05_08_v2_indexer.sql; split out because
-- `CREATE INDEX CONCURRENTLY` cannot run inside a transaction block.
-- Run after the column-addition migration and let it finish online — it
-- holds only a SHARE UPDATE EXCLUSIVE lock, so the live indexer's INSERTs
-- proceed throughout. On a 854M-row table it takes 5-15 min depending on
-- cache state.
--
-- The WHERE clause on the partial index keeps the index TINY (only the
-- minority V2-tagged rows are stored). Future-proofs analytics that want
-- to scope to one era explicitly without touching the V1 majority.
--
-- Run with:
--   psql polymarket_db -f database/migrations/2026_05_08_v2_indexer_indexes.sql

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_fills_exchange_version
    ON order_fills(exchange_version)
    WHERE exchange_version <> 1;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_matches_exchange_version
    ON order_matches(exchange_version)
    WHERE exchange_version <> 1;
