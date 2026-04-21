#!/usr/bin/env python3
"""
Incremental rollup updater for analytical pre-aggregated tables.

See database/ROLLUPS.md for the design.

Tables maintained (all in the same polymarket_db database):
  A. wallet_volume_rollup    — per-wallet lifetime activity
  B. market_volume_rollup    — per-market lifetime activity
  C. wallet_market_pairs     — (wallet, market) bridge
  D. wallet_monthly_stats    — per-wallet per-month
  E. market_monthly_stats    — per-market per-month (monthly OHLCV-style)

Sync state
----------
Watermark lives in ``indexer_state.rollup_synced_block``. On each update we
process trades in the range (watermark, unified_last_block], do all 5
rollup upserts in one transaction, then advance the watermark. Re-running
with the same range is NOT safe (UPSERTs are additive) — the transaction
boundary protects against double-counting.

Modes
-----
  python rollup.py                 — one incremental pass, then exit
  python rollup.py --loop [SECS]   — run forever, sleeping SECS between
                                     passes (default 60)
  python rollup.py --rebuild       — reset watermark to 0 and do a full
                                     rebuild in one long transaction.
                                     Use on first install.

Notes on double-counting
------------------------
Each order_fills row contributes to BOTH the maker and the taker in
wallet-centric tables (A, C, D). Self-trades (maker == taker) are counted
once (maker side only). Consequence: summing ``total_volume_usd`` across
all wallets is ~2× the true USDC flow — known quirk, documented to the AI.
"""

import sys
import time
import argparse

from db import get_conn, get_state, set_state

WATERMARK_KEY = "rollup_synced_block"
INDEXER_KEY = "unified_last_block"


# ============================================================
# Schema
# ============================================================

CREATE_TABLES_SQL = """
-- A. wallet_volume_rollup ---------------------------------------------------
CREATE TABLE IF NOT EXISTS wallet_volume_rollup (
    wallet                  TEXT PRIMARY KEY,
    maker_volume_usd        NUMERIC NOT NULL DEFAULT 0,
    taker_volume_usd        NUMERIC NOT NULL DEFAULT 0,
    total_volume_usd        NUMERIC NOT NULL DEFAULT 0,
    buy_volume_usd          NUMERIC NOT NULL DEFAULT 0,
    sell_volume_usd         NUMERIC NOT NULL DEFAULT 0,
    maker_trade_count       BIGINT NOT NULL DEFAULT 0,
    taker_trade_count       BIGINT NOT NULL DEFAULT 0,
    total_trade_count       BIGINT NOT NULL DEFAULT 0,
    buy_trade_count         BIGINT NOT NULL DEFAULT 0,
    sell_trade_count        BIGINT NOT NULL DEFAULT 0,
    total_fees_paid_usd     NUMERIC NOT NULL DEFAULT 0,
    -- Redemptions delta — updated incrementally by UPDATE_F_SQL. Numbers
    -- are direct from `redemptions` table; trustworthy.
    --
    -- PnL is intentionally NOT stored here yet. The naive formula
    -- `sell − buy + redemption − fees` undercounts costs for any wallet
    -- that uses CTF `PositionSplit` (USDC → YES+NO pair) or `PositionsMerge`
    -- (YES+NO → USDC) — i.e. every market maker. The indexer doesn't track
    -- those events. Until it does, any "PnL" column would be misleading
    -- (the top wallet's split-and-sell-spread strategy reads as +$4B).
    total_redemption_usd    NUMERIC NOT NULL DEFAULT 0,
    redemption_count        INTEGER NOT NULL DEFAULT 0,
    last_redemption_at      TIMESTAMPTZ,
    first_active            TIMESTAMPTZ,
    last_active             TIMESTAMPTZ,
    active_months           INTEGER NOT NULL DEFAULT 0,
    markets_touched         INTEGER NOT NULL DEFAULT 0,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- ALTERs for upgrading an existing deployment that pre-dates the
-- redemption columns. IF NOT EXISTS makes these idempotent.
ALTER TABLE wallet_volume_rollup
    ADD COLUMN IF NOT EXISTS total_redemption_usd NUMERIC NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS redemption_count     INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_redemption_at   TIMESTAMPTZ;
-- net_pnl_usd was added in an earlier iteration but the formula is
-- unreliable until split/merge events are indexed (see column-block comment
-- above). Drop the column + its index so we don't expose a misleading number.
ALTER TABLE wallet_volume_rollup DROP COLUMN IF EXISTS net_pnl_usd;
CREATE INDEX IF NOT EXISTS idx_wvr_total_vol    ON wallet_volume_rollup (total_volume_usd DESC);
CREATE INDEX IF NOT EXISTS idx_wvr_last_active  ON wallet_volume_rollup (last_active DESC);
CREATE INDEX IF NOT EXISTS idx_wvr_maker_vol    ON wallet_volume_rollup (maker_volume_usd DESC);
CREATE INDEX IF NOT EXISTS idx_wvr_taker_vol    ON wallet_volume_rollup (taker_volume_usd DESC);
CREATE INDEX IF NOT EXISTS idx_wvr_redemption   ON wallet_volume_rollup (total_redemption_usd DESC);
-- Index on redemptions.block_number so the delta range scan in UPDATE_F_SQL
-- doesn't full-scan 16M rows. The existing redemptions indexes cover redeemer
-- and condition_id; block_number wasn't covered before the rollup was added.
CREATE INDEX IF NOT EXISTS idx_redemptions_block ON redemptions (block_number);

-- B. market_volume_rollup ---------------------------------------------------
CREATE TABLE IF NOT EXISTS market_volume_rollup (
    condition_id            TEXT PRIMARY KEY,
    total_volume_usd        NUMERIC NOT NULL DEFAULT 0,
    buy_volume_usd          NUMERIC NOT NULL DEFAULT 0,
    sell_volume_usd         NUMERIC NOT NULL DEFAULT 0,
    total_trade_count       BIGINT NOT NULL DEFAULT 0,
    buy_trade_count         BIGINT NOT NULL DEFAULT 0,
    sell_trade_count        BIGINT NOT NULL DEFAULT 0,
    distinct_wallets        INTEGER NOT NULL DEFAULT 0,
    distinct_makers         INTEGER NOT NULL DEFAULT 0,
    distinct_takers         INTEGER NOT NULL DEFAULT 0,
    first_trade             TIMESTAMPTZ,
    last_trade              TIMESTAMPTZ,
    trading_duration_hours  NUMERIC,
    active_trading_months   INTEGER NOT NULL DEFAULT 0,
    avg_monthly_volume_usd  NUMERIC,
    avg_monthly_trade_count NUMERIC,
    peak_monthly_volume_usd NUMERIC,
    first_trade_price       NUMERIC,
    last_trade_price        NUMERIC,
    min_price               NUMERIC,
    max_price               NUMERIC,
    vwap_numerator          NUMERIC NOT NULL DEFAULT 0,
    vwap_usd                NUMERIC,
    total_fees_usd          NUMERIC NOT NULL DEFAULT 0,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mvr_total_vol    ON market_volume_rollup (total_volume_usd DESC);
CREATE INDEX IF NOT EXISTS idx_mvr_last_trade   ON market_volume_rollup (last_trade DESC);
CREATE INDEX IF NOT EXISTS idx_mvr_avg_monthly  ON market_volume_rollup (avg_monthly_volume_usd DESC NULLS LAST);

-- C. wallet_market_pairs ----------------------------------------------------
CREATE TABLE IF NOT EXISTS wallet_market_pairs (
    wallet                  TEXT,
    condition_id            TEXT,
    maker_volume_usd        NUMERIC NOT NULL DEFAULT 0,
    taker_volume_usd        NUMERIC NOT NULL DEFAULT 0,
    total_volume_usd        NUMERIC NOT NULL DEFAULT 0,
    buy_volume_usd          NUMERIC NOT NULL DEFAULT 0,
    sell_volume_usd         NUMERIC NOT NULL DEFAULT 0,
    trade_count             INTEGER NOT NULL DEFAULT 0,
    first_trade             TIMESTAMPTZ,
    last_trade              TIMESTAMPTZ,
    first_trade_price       NUMERIC,
    last_trade_price        NUMERIC,
    vwap_numerator          NUMERIC NOT NULL DEFAULT 0,
    vwap_usd                NUMERIC,
    PRIMARY KEY (wallet, condition_id)
);
CREATE INDEX IF NOT EXISTS idx_wmp_wallet      ON wallet_market_pairs (wallet);
CREATE INDEX IF NOT EXISTS idx_wmp_cond        ON wallet_market_pairs (condition_id);
CREATE INDEX IF NOT EXISTS idx_wmp_wallet_vol  ON wallet_market_pairs (wallet, total_volume_usd DESC);

-- D. wallet_monthly_stats ---------------------------------------------------
CREATE TABLE IF NOT EXISTS wallet_monthly_stats (
    wallet                  TEXT,
    month                   DATE,
    maker_volume_usd        NUMERIC NOT NULL DEFAULT 0,
    taker_volume_usd        NUMERIC NOT NULL DEFAULT 0,
    total_volume_usd        NUMERIC NOT NULL DEFAULT 0,
    buy_volume_usd          NUMERIC NOT NULL DEFAULT 0,
    sell_volume_usd         NUMERIC NOT NULL DEFAULT 0,
    trade_count             INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (wallet, month)
);
CREATE INDEX IF NOT EXISTS idx_wms_month_vol    ON wallet_monthly_stats (month, total_volume_usd DESC);
CREATE INDEX IF NOT EXISTS idx_wms_wallet_month ON wallet_monthly_stats (wallet, month DESC);

-- E. market_monthly_stats ---------------------------------------------------
CREATE TABLE IF NOT EXISTS market_monthly_stats (
    condition_id            TEXT,
    month                   DATE,
    total_volume_usd        NUMERIC NOT NULL DEFAULT 0,
    buy_volume_usd          NUMERIC NOT NULL DEFAULT 0,
    sell_volume_usd         NUMERIC NOT NULL DEFAULT 0,
    trade_count             INTEGER NOT NULL DEFAULT 0,
    open_price              NUMERIC,
    close_price             NUMERIC,
    min_price               NUMERIC,
    max_price               NUMERIC,
    vwap_numerator          NUMERIC NOT NULL DEFAULT 0,
    vwap_usd                NUMERIC,
    PRIMARY KEY (condition_id, month)
);
CREATE INDEX IF NOT EXISTS idx_mms_month_vol    ON market_monthly_stats (month, total_volume_usd DESC);
CREATE INDEX IF NOT EXISTS idx_mms_cond_month   ON market_monthly_stats (condition_id, month DESC);
"""


def ensure_tables(conn):
    with conn.cursor() as cur:
        cur.execute(CREATE_TABLES_SQL)
    conn.commit()


# ============================================================
# Incremental updates — one function per table
#
# Each uses parameters %(from)s (exclusive) and %(to)s (inclusive) on
# order_fills.block_number. Updates are UPSERT-additive. Derived columns
# (distinct_*, active_months, etc.) are recomputed in a second pass after
# the additive updates — see recompute_derived().
# ============================================================


UPDATE_D_SQL = """
-- wallet_monthly_stats: per (wallet, month). Each trade contributes to BOTH
-- maker and taker (wallet perspective); self-trades counted once.
WITH sides AS (
    SELECT maker AS wallet,
           DATE_TRUNC('month', block_timestamp)::date AS month,
           usdc_amount / 1e6 AS total_usd,
           CASE WHEN side='BUY'  THEN usdc_amount / 1e6 ELSE 0 END AS buy_usd,
           CASE WHEN side='SELL' THEN usdc_amount / 1e6 ELSE 0 END AS sell_usd,
           usdc_amount / 1e6 AS maker_usd,
           0::numeric          AS taker_usd
    FROM order_fills
    WHERE block_number > %(from)s AND block_number <= %(to)s
      AND usdc_amount > 0
    UNION ALL
    SELECT taker,
           DATE_TRUNC('month', block_timestamp)::date,
           usdc_amount / 1e6,
           CASE WHEN side='SELL' THEN usdc_amount / 1e6 ELSE 0 END,
           CASE WHEN side='BUY'  THEN usdc_amount / 1e6 ELSE 0 END,
           0::numeric,
           usdc_amount / 1e6
    FROM order_fills
    WHERE block_number > %(from)s AND block_number <= %(to)s
      AND usdc_amount > 0
      AND taker != maker
)
INSERT INTO wallet_monthly_stats (
    wallet, month, maker_volume_usd, taker_volume_usd, total_volume_usd,
    buy_volume_usd, sell_volume_usd, trade_count
)
SELECT wallet, month,
       SUM(maker_usd), SUM(taker_usd), SUM(total_usd),
       SUM(buy_usd), SUM(sell_usd),
       COUNT(*)::int
FROM sides
GROUP BY wallet, month
ON CONFLICT (wallet, month) DO UPDATE SET
    maker_volume_usd = wallet_monthly_stats.maker_volume_usd + EXCLUDED.maker_volume_usd,
    taker_volume_usd = wallet_monthly_stats.taker_volume_usd + EXCLUDED.taker_volume_usd,
    total_volume_usd = wallet_monthly_stats.total_volume_usd + EXCLUDED.total_volume_usd,
    buy_volume_usd   = wallet_monthly_stats.buy_volume_usd   + EXCLUDED.buy_volume_usd,
    sell_volume_usd  = wallet_monthly_stats.sell_volume_usd  + EXCLUDED.sell_volume_usd,
    trade_count      = wallet_monthly_stats.trade_count      + EXCLUDED.trade_count
"""


UPDATE_E_SQL = """
-- market_monthly_stats: per (condition_id, month) with OHLCV-style prices.
-- open/close derived via window functions on the delta. Since new trades in
-- a given month always arrive later than any previously processed trades in
-- that month (block_number is monotonic), ON CONFLICT keeps the existing
-- open_price (COALESCE) and overwrites close_price with the delta's close.
WITH trades AS (
    SELECT condition_id,
           DATE_TRUNC('month', block_timestamp)::date AS month,
           block_timestamp, log_index, price, usdc_amount, side
    FROM order_fills
    WHERE block_number > %(from)s AND block_number <= %(to)s
      AND usdc_amount > 0
      AND price IS NOT NULL
      AND condition_id IS NOT NULL
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY condition_id, month
                              ORDER BY block_timestamp ASC, log_index ASC) AS rn_asc,
           ROW_NUMBER() OVER (PARTITION BY condition_id, month
                              ORDER BY block_timestamp DESC, log_index DESC) AS rn_desc
    FROM trades
)
INSERT INTO market_monthly_stats (
    condition_id, month, total_volume_usd, buy_volume_usd, sell_volume_usd,
    trade_count, open_price, close_price, min_price, max_price,
    vwap_numerator
)
SELECT condition_id, month,
       SUM(usdc_amount) / 1e6,
       SUM(CASE WHEN side='BUY'  THEN usdc_amount ELSE 0 END) / 1e6,
       SUM(CASE WHEN side='SELL' THEN usdc_amount ELSE 0 END) / 1e6,
       COUNT(*)::int,
       MAX(CASE WHEN rn_asc  = 1 THEN price END),
       MAX(CASE WHEN rn_desc = 1 THEN price END),
       MIN(price),
       MAX(price),
       SUM(price * usdc_amount / 1e6)
FROM ranked
GROUP BY condition_id, month
ON CONFLICT (condition_id, month) DO UPDATE SET
    total_volume_usd = market_monthly_stats.total_volume_usd + EXCLUDED.total_volume_usd,
    buy_volume_usd   = market_monthly_stats.buy_volume_usd   + EXCLUDED.buy_volume_usd,
    sell_volume_usd  = market_monthly_stats.sell_volume_usd  + EXCLUDED.sell_volume_usd,
    trade_count      = market_monthly_stats.trade_count      + EXCLUDED.trade_count,
    open_price       = COALESCE(market_monthly_stats.open_price, EXCLUDED.open_price),
    close_price      = EXCLUDED.close_price,
    min_price        = LEAST(market_monthly_stats.min_price, EXCLUDED.min_price),
    max_price        = GREATEST(market_monthly_stats.max_price, EXCLUDED.max_price),
    vwap_numerator   = market_monthly_stats.vwap_numerator + EXCLUDED.vwap_numerator,
    vwap_usd         = (market_monthly_stats.vwap_numerator + EXCLUDED.vwap_numerator)
                       / NULLIF(market_monthly_stats.total_volume_usd + EXCLUDED.total_volume_usd, 0)
"""


UPDATE_C_SQL = """
-- wallet_market_pairs: per (wallet, condition_id). Each trade contributes
-- for BOTH maker and taker (self-trades counted once).
WITH sides AS (
    SELECT maker AS wallet,
           condition_id,
           block_timestamp, log_index, price, usdc_amount, side,
           usdc_amount / 1e6 AS total_usd,
           CASE WHEN side='BUY'  THEN usdc_amount / 1e6 ELSE 0 END AS buy_usd,
           CASE WHEN side='SELL' THEN usdc_amount / 1e6 ELSE 0 END AS sell_usd,
           usdc_amount / 1e6 AS maker_usd,
           0::numeric          AS taker_usd
    FROM order_fills
    WHERE block_number > %(from)s AND block_number <= %(to)s
      AND usdc_amount > 0
      AND condition_id IS NOT NULL
    UNION ALL
    SELECT taker, condition_id,
           block_timestamp, log_index, price, usdc_amount, side,
           usdc_amount / 1e6,
           CASE WHEN side='SELL' THEN usdc_amount / 1e6 ELSE 0 END,
           CASE WHEN side='BUY'  THEN usdc_amount / 1e6 ELSE 0 END,
           0::numeric, usdc_amount / 1e6
    FROM order_fills
    WHERE block_number > %(from)s AND block_number <= %(to)s
      AND usdc_amount > 0
      AND condition_id IS NOT NULL
      AND taker != maker
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (PARTITION BY wallet, condition_id
                              ORDER BY block_timestamp ASC, log_index ASC) AS rn_asc,
           ROW_NUMBER() OVER (PARTITION BY wallet, condition_id
                              ORDER BY block_timestamp DESC, log_index DESC) AS rn_desc
    FROM sides
)
INSERT INTO wallet_market_pairs (
    wallet, condition_id,
    maker_volume_usd, taker_volume_usd, total_volume_usd,
    buy_volume_usd, sell_volume_usd, trade_count,
    first_trade, last_trade, first_trade_price, last_trade_price,
    vwap_numerator
)
SELECT wallet, condition_id,
       SUM(maker_usd), SUM(taker_usd), SUM(total_usd),
       SUM(buy_usd), SUM(sell_usd),
       COUNT(*)::int,
       MIN(block_timestamp), MAX(block_timestamp),
       MAX(CASE WHEN rn_asc  = 1 THEN price END),
       MAX(CASE WHEN rn_desc = 1 THEN price END),
       SUM(COALESCE(price, 0) * usdc_amount / 1e6)
FROM ranked
GROUP BY wallet, condition_id
ON CONFLICT (wallet, condition_id) DO UPDATE SET
    maker_volume_usd  = wallet_market_pairs.maker_volume_usd  + EXCLUDED.maker_volume_usd,
    taker_volume_usd  = wallet_market_pairs.taker_volume_usd  + EXCLUDED.taker_volume_usd,
    total_volume_usd  = wallet_market_pairs.total_volume_usd  + EXCLUDED.total_volume_usd,
    buy_volume_usd    = wallet_market_pairs.buy_volume_usd    + EXCLUDED.buy_volume_usd,
    sell_volume_usd   = wallet_market_pairs.sell_volume_usd   + EXCLUDED.sell_volume_usd,
    trade_count       = wallet_market_pairs.trade_count       + EXCLUDED.trade_count,
    first_trade       = LEAST(wallet_market_pairs.first_trade, EXCLUDED.first_trade),
    last_trade        = GREATEST(wallet_market_pairs.last_trade, EXCLUDED.last_trade),
    first_trade_price = COALESCE(wallet_market_pairs.first_trade_price, EXCLUDED.first_trade_price),
    last_trade_price  = EXCLUDED.last_trade_price,
    vwap_numerator    = wallet_market_pairs.vwap_numerator + EXCLUDED.vwap_numerator,
    vwap_usd          = (wallet_market_pairs.vwap_numerator + EXCLUDED.vwap_numerator)
                        / NULLIF(wallet_market_pairs.total_volume_usd + EXCLUDED.total_volume_usd, 0)
"""


UPDATE_A_SQL = """
-- wallet_volume_rollup: per-wallet lifetime (additive cols + first/last).
-- Fee only attributed to maker side; order_fills.fee is the maker fee.
WITH sides AS (
    SELECT maker AS wallet,
           block_timestamp,
           usdc_amount / 1e6 AS total_usd,
           CASE WHEN side='BUY'  THEN usdc_amount / 1e6 ELSE 0 END AS buy_usd,
           CASE WHEN side='SELL' THEN usdc_amount / 1e6 ELSE 0 END AS sell_usd,
           usdc_amount / 1e6 AS maker_usd,
           0::numeric          AS taker_usd,
           fee / 1e6           AS fee_usd,
           1 AS maker_trade,
           0 AS taker_trade,
           CASE WHEN side='BUY'  THEN 1 ELSE 0 END AS buy_trade,
           CASE WHEN side='SELL' THEN 1 ELSE 0 END AS sell_trade
    FROM order_fills
    WHERE block_number > %(from)s AND block_number <= %(to)s
      AND usdc_amount > 0
    UNION ALL
    SELECT taker, block_timestamp, usdc_amount / 1e6,
           CASE WHEN side='SELL' THEN usdc_amount / 1e6 ELSE 0 END,
           CASE WHEN side='BUY'  THEN usdc_amount / 1e6 ELSE 0 END,
           0::numeric, usdc_amount / 1e6,
           0::numeric,  -- taker pays no fee in current schema
           0, 1,
           CASE WHEN side='SELL' THEN 1 ELSE 0 END,
           CASE WHEN side='BUY'  THEN 1 ELSE 0 END
    FROM order_fills
    WHERE block_number > %(from)s AND block_number <= %(to)s
      AND usdc_amount > 0
      AND taker != maker
)
INSERT INTO wallet_volume_rollup (
    wallet,
    maker_volume_usd, taker_volume_usd, total_volume_usd,
    buy_volume_usd, sell_volume_usd,
    maker_trade_count, taker_trade_count, total_trade_count,
    buy_trade_count, sell_trade_count,
    total_fees_paid_usd,
    first_active, last_active,
    updated_at
)
SELECT wallet,
       SUM(maker_usd), SUM(taker_usd), SUM(total_usd),
       SUM(buy_usd), SUM(sell_usd),
       SUM(maker_trade)::bigint, SUM(taker_trade)::bigint, COUNT(*)::bigint,
       SUM(buy_trade)::bigint, SUM(sell_trade)::bigint,
       SUM(fee_usd),
       MIN(block_timestamp), MAX(block_timestamp),
       NOW()
FROM sides
GROUP BY wallet
ON CONFLICT (wallet) DO UPDATE SET
    maker_volume_usd     = wallet_volume_rollup.maker_volume_usd     + EXCLUDED.maker_volume_usd,
    taker_volume_usd     = wallet_volume_rollup.taker_volume_usd     + EXCLUDED.taker_volume_usd,
    total_volume_usd     = wallet_volume_rollup.total_volume_usd     + EXCLUDED.total_volume_usd,
    buy_volume_usd       = wallet_volume_rollup.buy_volume_usd       + EXCLUDED.buy_volume_usd,
    sell_volume_usd      = wallet_volume_rollup.sell_volume_usd      + EXCLUDED.sell_volume_usd,
    maker_trade_count    = wallet_volume_rollup.maker_trade_count    + EXCLUDED.maker_trade_count,
    taker_trade_count    = wallet_volume_rollup.taker_trade_count    + EXCLUDED.taker_trade_count,
    total_trade_count    = wallet_volume_rollup.total_trade_count    + EXCLUDED.total_trade_count,
    buy_trade_count      = wallet_volume_rollup.buy_trade_count      + EXCLUDED.buy_trade_count,
    sell_trade_count     = wallet_volume_rollup.sell_trade_count     + EXCLUDED.sell_trade_count,
    total_fees_paid_usd  = wallet_volume_rollup.total_fees_paid_usd  + EXCLUDED.total_fees_paid_usd,
    first_active         = LEAST(wallet_volume_rollup.first_active, EXCLUDED.first_active),
    last_active          = GREATEST(wallet_volume_rollup.last_active, EXCLUDED.last_active),
    updated_at           = NOW()
RETURNING wallet
"""


UPDATE_B_SQL = """
-- market_volume_rollup: per-market lifetime (additive + first/last + price).
WITH ranked AS (
    SELECT condition_id, block_timestamp, log_index, price, usdc_amount, side, fee,
           ROW_NUMBER() OVER (PARTITION BY condition_id
                              ORDER BY block_timestamp ASC, log_index ASC) AS rn_asc,
           ROW_NUMBER() OVER (PARTITION BY condition_id
                              ORDER BY block_timestamp DESC, log_index DESC) AS rn_desc
    FROM order_fills
    WHERE block_number > %(from)s AND block_number <= %(to)s
      AND usdc_amount > 0
      AND condition_id IS NOT NULL
)
INSERT INTO market_volume_rollup (
    condition_id,
    total_volume_usd, buy_volume_usd, sell_volume_usd,
    total_trade_count, buy_trade_count, sell_trade_count,
    first_trade, last_trade,
    first_trade_price, last_trade_price, min_price, max_price,
    vwap_numerator, total_fees_usd,
    updated_at
)
SELECT condition_id,
       SUM(usdc_amount) / 1e6,
       SUM(CASE WHEN side='BUY'  THEN usdc_amount ELSE 0 END) / 1e6,
       SUM(CASE WHEN side='SELL' THEN usdc_amount ELSE 0 END) / 1e6,
       COUNT(*)::bigint,
       SUM(CASE WHEN side='BUY'  THEN 1 ELSE 0 END)::bigint,
       SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END)::bigint,
       MIN(block_timestamp), MAX(block_timestamp),
       MAX(CASE WHEN rn_asc  = 1 THEN price END),
       MAX(CASE WHEN rn_desc = 1 THEN price END),
       MIN(price), MAX(price),
       SUM(COALESCE(price, 0) * usdc_amount / 1e6),
       SUM(fee) / 1e6,
       NOW()
FROM ranked
GROUP BY condition_id
ON CONFLICT (condition_id) DO UPDATE SET
    total_volume_usd    = market_volume_rollup.total_volume_usd    + EXCLUDED.total_volume_usd,
    buy_volume_usd      = market_volume_rollup.buy_volume_usd      + EXCLUDED.buy_volume_usd,
    sell_volume_usd     = market_volume_rollup.sell_volume_usd     + EXCLUDED.sell_volume_usd,
    total_trade_count   = market_volume_rollup.total_trade_count   + EXCLUDED.total_trade_count,
    buy_trade_count     = market_volume_rollup.buy_trade_count     + EXCLUDED.buy_trade_count,
    sell_trade_count    = market_volume_rollup.sell_trade_count    + EXCLUDED.sell_trade_count,
    first_trade         = LEAST(market_volume_rollup.first_trade, EXCLUDED.first_trade),
    last_trade          = GREATEST(market_volume_rollup.last_trade, EXCLUDED.last_trade),
    first_trade_price   = COALESCE(market_volume_rollup.first_trade_price, EXCLUDED.first_trade_price),
    last_trade_price    = EXCLUDED.last_trade_price,
    min_price           = LEAST(market_volume_rollup.min_price, EXCLUDED.min_price),
    max_price           = GREATEST(market_volume_rollup.max_price, EXCLUDED.max_price),
    vwap_numerator      = market_volume_rollup.vwap_numerator      + EXCLUDED.vwap_numerator,
    total_fees_usd      = market_volume_rollup.total_fees_usd      + EXCLUDED.total_fees_usd,
    updated_at          = NOW()
RETURNING condition_id
"""


# ============================================================
# Derived-column recompute
#
# Only recompute rows whose primary key appears in THIS batch's delta.
# The set is collected from UPDATE_A/B's RETURNING clause. When no set is
# passed (``%(wallets)s`` / ``%(markets)s`` is NULL — used during initial
# full-rebuild), the query falls through to updating every row.
#
# Per-cycle cost BEFORE this filter: ~1.67M wallet rows + ~182K market rows
# re-updated EVERY 4.5 min, almost all redundantly. After filter: only the
# ~10K wallets and ~3K markets actually touched by the batch.
# ============================================================


UPDATE_F_SQL = """
-- F. Redemption delta into wallet_volume_rollup. Each redemptions row is
-- attributed to one wallet (redeemer); no double-counting issue. Uses the
-- same block_number watermark range as order_fills since the unified
-- indexer commits both in lock-step.
INSERT INTO wallet_volume_rollup (
    wallet, total_redemption_usd, redemption_count, last_redemption_at,
    updated_at
)
SELECT redeemer,
       SUM(payout) / 1e6,
       COUNT(*)::int,
       MAX(block_timestamp),
       NOW()
FROM redemptions
WHERE block_number > %(from)s AND block_number <= %(to)s
  AND payout > 0
GROUP BY redeemer
ON CONFLICT (wallet) DO UPDATE SET
    total_redemption_usd = wallet_volume_rollup.total_redemption_usd + EXCLUDED.total_redemption_usd,
    redemption_count     = wallet_volume_rollup.redemption_count     + EXCLUDED.redemption_count,
    last_redemption_at   = GREATEST(wallet_volume_rollup.last_redemption_at, EXCLUDED.last_redemption_at),
    updated_at           = NOW()
RETURNING wallet
"""


RECOMPUTE_A_DERIVED_SQL = """
-- A derived columns: markets_touched (from C), active_months (from D).
-- net_pnl_usd was here briefly but removed: see the column-block comment
-- at the top of CREATE_TABLES_SQL for why (split/merge unindexed).
-- When %(wallets)s is NULL → update every wallet (used by full-rebuild).
-- Otherwise → only wallets whose PK appears in the delta.
UPDATE wallet_volume_rollup w SET
    markets_touched = COALESCE((SELECT COUNT(*) FROM wallet_market_pairs p WHERE p.wallet = w.wallet), 0),
    active_months   = COALESCE((SELECT COUNT(*) FROM wallet_monthly_stats d WHERE d.wallet = w.wallet), 0)
WHERE %(wallets)s::text[] IS NULL
   OR w.wallet = ANY(%(wallets)s::text[])
"""


RECOMPUTE_B_DERIVED_SQL = """
-- B derived columns from C (distinct_* counts) and E (active months, avg,
-- peak monthly volume) and self (trading_duration_hours, vwap_usd).
-- When %(markets)s is NULL → update every market (used by full-rebuild).
-- Otherwise → only markets whose PK appears in the delta.
UPDATE market_volume_rollup m SET
    distinct_wallets        = COALESCE(sub.distinct_wallets, 0),
    distinct_makers         = COALESCE(sub.distinct_makers, 0),
    distinct_takers         = COALESCE(sub.distinct_takers, 0),
    active_trading_months   = COALESCE(monthly.active_months, 0),
    avg_monthly_volume_usd  = CASE WHEN COALESCE(monthly.active_months, 0) > 0
                                   THEN m.total_volume_usd / monthly.active_months
                                   ELSE NULL END,
    avg_monthly_trade_count = CASE WHEN COALESCE(monthly.active_months, 0) > 0
                                   THEN m.total_trade_count::numeric / monthly.active_months
                                   ELSE NULL END,
    peak_monthly_volume_usd = monthly.peak_vol,
    trading_duration_hours  = CASE WHEN m.first_trade IS NOT NULL AND m.last_trade IS NOT NULL
                                   THEN EXTRACT(EPOCH FROM (m.last_trade - m.first_trade)) / 3600.0
                                   ELSE NULL END,
    vwap_usd                = m.vwap_numerator / NULLIF(m.total_volume_usd, 0)
FROM (
    -- distinct wallet participants, aggregated only for affected markets
    -- (or all markets when %(markets)s is NULL).
    SELECT m2.condition_id,
           COUNT(DISTINCT p.wallet) AS distinct_wallets,
           COUNT(DISTINCT CASE WHEN p.maker_volume_usd > 0 THEN p.wallet END) AS distinct_makers,
           COUNT(DISTINCT CASE WHEN p.taker_volume_usd > 0 THEN p.wallet END) AS distinct_takers
    FROM market_volume_rollup m2
    LEFT JOIN wallet_market_pairs p ON p.condition_id = m2.condition_id
    WHERE %(markets)s::text[] IS NULL
       OR m2.condition_id = ANY(%(markets)s::text[])
    GROUP BY m2.condition_id
) sub
LEFT JOIN (
    -- monthly rollup-derived stats per market, same scope.
    SELECT condition_id,
           COUNT(*) AS active_months,
           MAX(total_volume_usd) AS peak_vol
    FROM market_monthly_stats
    WHERE %(markets)s::text[] IS NULL
       OR condition_id = ANY(%(markets)s::text[])
    GROUP BY condition_id
) monthly ON monthly.condition_id = sub.condition_id
WHERE m.condition_id = sub.condition_id
  AND (%(markets)s::text[] IS NULL OR m.condition_id = ANY(%(markets)s::text[]))
"""


def _read_blocks(conn):
    """Return (synced, current) block numbers."""
    with conn.cursor() as cur:
        cur.execute("SELECT value::bigint FROM indexer_state WHERE key = %s", (WATERMARK_KEY,))
        row = cur.fetchone()
        synced = row[0] if row else 0
        cur.execute("SELECT value::bigint FROM indexer_state WHERE key = %s", (INDEXER_KEY,))
        row = cur.fetchone()
        current = row[0] if row else 0
    return synced, current


def _update_watermark(conn, block: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO indexer_state (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (WATERMARK_KEY, str(block)),
        )


def _run_sql(conn, label, sql, params):
    """Execute one update query and print timing."""
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.rowcount
    elapsed = time.time() - t0
    print(f"  [{label:>10s}] {rows:>12,} rows  in {elapsed:6.1f}s", flush=True)


def _run_upsert_collect_pks(conn, label, sql, params) -> list[str]:
    """Run an UPSERT whose tail is `RETURNING <pk_column>` and return the
    set of affected primary-key values (as a list, distinct preserved).

    Used for UPDATE_A (returns wallet) and UPDATE_B (returns condition_id)
    so that the derived recompute can filter to ONLY the rows whose
    source-table data changed in this batch — skipping 1.6M+ redundant
    no-op updates per cycle.
    """
    t0 = time.time()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        pks = [row[0] for row in cur.fetchall()]
    elapsed = time.time() - t0
    print(f"  [{label:>10s}] {len(pks):>12,} rows  in {elapsed:6.1f}s", flush=True)
    # Deduplicate while preserving order (same PK can appear only once per
    # upsert anyway — GROUP BY PK — so this is belt-and-suspenders).
    seen = set()
    out = []
    for pk in pks:
        if pk not in seen:
            seen.add(pk)
            out.append(pk)
    return out


def run_once(full_rebuild: bool = False):
    """One update pass over (synced, current]. Returns True if any work done."""
    conn = get_conn()
    try:
        ensure_tables(conn)

        synced, current = _read_blocks(conn)

        if full_rebuild:
            print(f"FULL REBUILD: resetting watermark {synced:,} → 0", flush=True)
            synced = 0

        if current <= synced:
            print(f"up to date (synced={synced:,} current={current:,})", flush=True)
            return False

        span = current - synced
        print(f"Updating rollups over blocks ({synced:,}, {current:,}] — "
              f"{span:,} blocks", flush=True)

        # Raise limits for the whole transaction. Default statement_timeout
        # would abort long backfill aggregates; default work_mem (64MB) would
        # force hash-aggregate spills for the bigger GROUP BYs.
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 0")
            cur.execute("SET LOCAL work_mem = '2GB'")

        params = {"from": synced, "to": current}
        total_t0 = time.time()

        # Order: additive first, derived second. Each statement is part of
        # the same transaction so rollup state is atomic.
        _run_sql(conn, "D wallet_m", UPDATE_D_SQL, params)
        _run_sql(conn, "E market_m", UPDATE_E_SQL, params)
        _run_sql(conn, "C w_m_pair", UPDATE_C_SQL, params)
        # A and B return the set of PKs they touched so the derived step
        # can filter to only those rows. During full-rebuild we skip the
        # filter entirely (pass NULL) — the cost of shipping 1.6M PKs
        # through an array param would beat the benefit.
        affected_wallets = _run_upsert_collect_pks(conn, "A wallet_v", UPDATE_A_SQL, params)
        affected_markets = _run_upsert_collect_pks(conn, "B market_v", UPDATE_B_SQL, params)
        # F: redemption delta into wallet_volume_rollup. Returns wallets
        # whose redemption sums changed — these may differ from A's set
        # (a wallet can redeem in a block range without trading in it).
        # Union with A's set so net_pnl_usd recompute covers both.
        affected_redeemers = _run_upsert_collect_pks(conn, "F redemption", UPDATE_F_SQL, params)

        derived_a_wallet_set = (
            None if full_rebuild
            else list(set(affected_wallets) | set(affected_redeemers))
        )
        derived_a_params = {
            "wallets": derived_a_wallet_set,
        }
        derived_b_params = {
            "markets": None if full_rebuild else affected_markets,
        }
        _run_sql(conn, "A derived ", RECOMPUTE_A_DERIVED_SQL, derived_a_params)
        _run_sql(conn, "B derived ", RECOMPUTE_B_DERIVED_SQL, derived_b_params)

        _update_watermark(conn, current)
        conn.commit()

        total_elapsed = time.time() - total_t0
        scope_note = (
            "FULL" if full_rebuild
            else f"affected wallets={len(affected_wallets):,} "
                 f"markets={len(affected_markets):,}"
        )
        print(f"Done: synced → {current:,} in {total_elapsed:.1f}s ({scope_note})",
              flush=True)
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_loop(interval_sec: int):
    """Daemon: run_once() every interval_sec seconds, forever."""
    print(f"Rollup daemon starting (interval={interval_sec}s)", flush=True)
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"⚠ run_once failed: {type(e).__name__}: {e}", flush=True)
            import traceback
            traceback.print_exc()
        time.sleep(interval_sec)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--loop", nargs="?", const=60, type=int,
                   help="Run as daemon; optional interval in seconds (default 60).")
    p.add_argument("--rebuild", action="store_true",
                   help="Reset watermark to 0 and do a full rebuild.")
    args = p.parse_args()

    if args.rebuild and args.loop is not None:
        p.error("--rebuild and --loop cannot be combined")

    if args.loop is not None:
        run_loop(args.loop)
    else:
        run_once(full_rebuild=args.rebuild)


if __name__ == "__main__":
    main()
