-- V2 indexer migration — additive only.
--
-- Polymarket migrated to V2 of the CTF Exchange + Neg Risk CTF Exchange
-- on 2026-04-28 at block 86,126,998. The V2 OrderFilled event format adds
-- two new fields (`builder` and `metadata`) and exposes `side` + `tokenId`
-- directly instead of requiring derivation from the V1 `(makerAssetId,
-- takerAssetId)` pair. Underlying ConditionalTokens contract is unchanged,
-- so resolutions, redemptions, splits, and merges are NOT affected by this
-- migration.
--
-- Strategy: extend the existing tables in place with nullable V2-only
-- columns and an exchange_version discriminator, rather than creating
-- parallel `_v2` tables. Reasons:
--   1. Every consumer query (rollups, AI prompts, CSV exports) already
--      targets `order_fills` / `order_matches`. Splitting the tables would
--      force every query to UNION ALL forever.
--   2. The semantically-equivalent V1 and V2 columns (maker, taker, price,
--      usdc_amount, token_amount, condition_id, side) keep their meaning
--      across versions. Only the source-event layout differs, which is a
--      decoder concern, not a storage concern.
--   3. New V2-only columns (builder, metadata) are NULL for V1 rows, which
--      is the natural "did not exist" representation.
--
-- The V2 decoder synthesises `(maker_asset_id, taker_asset_id)` in V1
-- shape (one is '0' for collateral, the other is the CTF tokenId, picked
-- by `side`) so existing queries that join through those columns keep
-- working without a UNION. The new `builder` / `metadata` columns are
-- additive and only populated for V2 rows.
--
-- Idempotent: each statement is `IF NOT EXISTS` / `IF EXISTS` so re-running
-- the migration is a no-op.
--
-- IMPORTANT: this file contains the cheap DDL only (ADD COLUMN +
-- indexer_state row), which holds ACCESS EXCLUSIVE briefly. The partial
-- indexes on (exchange_version) MUST be created with `CREATE INDEX
-- CONCURRENTLY` because both tables are 100s of millions of rows and a
-- non-concurrent CREATE INDEX would scan the whole table under that lock,
-- wedging the live indexer for tens of minutes. Concurrent index creation
-- can't run inside a transaction block, so it lives in a sibling
-- `2026_05_08_v2_indexer_indexes.sql` to be applied separately and online.

BEGIN;

-- order_fills ---------------------------------------------------------------
--
-- PG 11+ optimises `ADD COLUMN ... DEFAULT <constant> NOT NULL` to a
-- metadata-only operation (the default goes into `pg_attribute.atthasmissing`,
-- no rewrite). All three columns finish in milliseconds on a 854M-row table.

ALTER TABLE order_fills
    ADD COLUMN IF NOT EXISTS builder          TEXT,           -- bytes32 hex; V2-only, NULL for V1
    ADD COLUMN IF NOT EXISTS metadata         TEXT,           -- bytes32 hex; V2-only, NULL for V1
    ADD COLUMN IF NOT EXISTS exchange_version SMALLINT NOT NULL DEFAULT 1;

-- order_matches -------------------------------------------------------------
--
-- V2 OrdersMatched dropped `maker_order_maker` (the matched maker side)
-- because per-fill OrderFilled events already identify each maker. V2 rows
-- have an explicit `side` field that V1 had to derive from the asset_id
-- pair. The decoder still populates `(maker_asset_id, taker_asset_id)` in
-- V1 shape so legacy queries keep working unchanged.

ALTER TABLE order_matches
    ADD COLUMN IF NOT EXISTS side             TEXT,
    ADD COLUMN IF NOT EXISTS exchange_version SMALLINT NOT NULL DEFAULT 1;

-- Cutover marker --------------------------------------------------------
--
-- The V2 cutover block is recorded in indexer_state for runtime + ops.
-- This is the LAST V1 block; V2 events start from V2_CUTOVER_BLOCK + 1.
-- (`unified_last_block` keeps moving across the cutover; this row is purely
-- informational so a sysop can grep what era a given block belongs to.)

INSERT INTO indexer_state (key, value, updated_at)
VALUES ('v2_cutover_block', '86126998', NOW())
ON CONFLICT (key) DO UPDATE
    SET value = EXCLUDED.value, updated_at = NOW();

COMMIT;
