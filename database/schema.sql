-- Polymarket Trading Database Schema
-- Optimized for fast filtering by trader, market, price, time

-- Markets metadata (from Gamma API + on-chain)
CREATE TABLE IF NOT EXISTS markets (
    condition_id        TEXT PRIMARY KEY,          -- bytes32 hex
    question_id         TEXT,                      -- bytes32 hex
    question            TEXT,
    description         TEXT,
    slug                TEXT,
    outcomes            JSONB,                     -- ["Yes","No"]
    outcome_prices      JSONB,                     -- current prices
    clob_token_ids      JSONB,                     -- [token_id_yes, token_id_no]
    neg_risk            BOOLEAN DEFAULT FALSE,
    neg_risk_market_id  TEXT,                      -- parent group for multi-outcome
    end_date            TIMESTAMPTZ,               -- scheduled resolution date
    start_date          TIMESTAMPTZ,
    active              BOOLEAN,
    closed              BOOLEAN,
    volume              NUMERIC,
    liquidity           NUMERIC,
    resolution_source   TEXT,
    resolved            BOOLEAN DEFAULT FALSE,
    resolution_payout   JSONB,                     -- payout numerators after resolution
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Token ID to market mapping (for fast lookup from trade events)
CREATE TABLE IF NOT EXISTS token_market_map (
    token_id            TEXT PRIMARY KEY,          -- ERC-1155 token ID (decimal string)
    condition_id        TEXT REFERENCES markets(condition_id),
    outcome_index       SMALLINT,                  -- 0=Yes, 1=No (or outcome index for multi)
    outcome_label       TEXT                       -- "Yes", "No", or custom label
);

-- OrderFilled events from CTF Exchange and Neg Risk CTF Exchange
CREATE TABLE IF NOT EXISTS order_fills (
    id                  BIGSERIAL PRIMARY KEY,
    tx_hash             TEXT NOT NULL,
    log_index           INTEGER NOT NULL,
    block_number        BIGINT NOT NULL,
    block_timestamp     TIMESTAMPTZ NOT NULL,
    exchange            TEXT NOT NULL,             -- 'ctf' or 'neg_risk'
    order_hash          TEXT NOT NULL,             -- bytes32 hex
    maker               TEXT NOT NULL,             -- address
    taker               TEXT NOT NULL,             -- address
    maker_asset_id      TEXT NOT NULL,             -- token ID or '0' for USDC
    taker_asset_id      TEXT NOT NULL,
    maker_amount_filled NUMERIC NOT NULL,          -- raw amount (6 decimals)
    taker_amount_filled NUMERIC NOT NULL,
    fee                 NUMERIC NOT NULL,
    -- Derived fields for fast querying
    token_id            TEXT,                      -- the non-USDC token involved
    condition_id        TEXT,                      -- resolved from token_id
    side                TEXT,                      -- 'BUY' or 'SELL' (from maker perspective)
    price               NUMERIC,                   -- USDC per token (derived)
    usdc_amount         NUMERIC,                   -- USDC side of trade
    token_amount        NUMERIC,                   -- token side of trade
    UNIQUE(tx_hash, log_index)
);

-- OrdersMatched events (one per match, avoids double-counting)
CREATE TABLE IF NOT EXISTS order_matches (
    id                  BIGSERIAL PRIMARY KEY,
    tx_hash             TEXT NOT NULL,
    log_index           INTEGER NOT NULL,
    block_number        BIGINT NOT NULL,
    block_timestamp     TIMESTAMPTZ NOT NULL,
    exchange            TEXT NOT NULL,
    taker_order_hash    TEXT NOT NULL,
    taker_order_maker   TEXT NOT NULL,             -- taker's address
    maker_asset_id      TEXT NOT NULL,
    taker_asset_id      TEXT NOT NULL,
    maker_amount_filled NUMERIC NOT NULL,
    taker_amount_filled NUMERIC NOT NULL,
    -- Derived
    token_id            TEXT,
    condition_id        TEXT,
    price               NUMERIC,
    usdc_amount         NUMERIC,
    token_amount        NUMERIC,
    UNIQUE(tx_hash, log_index)
);

-- ConditionResolution events
CREATE TABLE IF NOT EXISTS resolutions (
    id                  BIGSERIAL PRIMARY KEY,
    tx_hash             TEXT NOT NULL,
    log_index           INTEGER NOT NULL,
    block_number        BIGINT NOT NULL,
    block_timestamp     TIMESTAMPTZ NOT NULL,
    condition_id        TEXT NOT NULL REFERENCES markets(condition_id) ON DELETE CASCADE,
    oracle              TEXT NOT NULL,
    question_id         TEXT NOT NULL,
    outcome_slot_count  INTEGER NOT NULL,
    payout_numerators   JSONB NOT NULL,            -- [1,0] means outcome 0 won
    UNIQUE(tx_hash, log_index)
);

-- PayoutRedemption events
CREATE TABLE IF NOT EXISTS redemptions (
    id                  BIGSERIAL PRIMARY KEY,
    tx_hash             TEXT NOT NULL,
    log_index           INTEGER NOT NULL,
    block_number        BIGINT NOT NULL,
    block_timestamp     TIMESTAMPTZ NOT NULL,
    redeemer            TEXT NOT NULL,
    collateral_token    TEXT NOT NULL,
    condition_id        TEXT NOT NULL,
    index_sets          JSONB NOT NULL,
    payout              NUMERIC NOT NULL,          -- USDC amount redeemed
    UNIQUE(tx_hash, log_index)
);

-- Indexer state: track how far we've synced
CREATE TABLE IF NOT EXISTS indexer_state (
    key                 TEXT PRIMARY KEY,
    value               TEXT NOT NULL,
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

-- Users (auth)
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,
    display_name    TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- User sessions (conversation history + feedback)
CREATE TABLE IF NOT EXISTS user_sessions (
    id              SERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL UNIQUE,
    user_id         INTEGER REFERENCES users(id),
    client_ip       TEXT,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    topic_summary   TEXT,
    queries_run     INTEGER DEFAULT 0,
    errors_hit      INTEGER DEFAULT 0,
    satisfaction    TEXT,
    user_rating     SMALLINT,
    user_feedback   TEXT,
    conversation    JSONB,
    ai_notes        TEXT
);

-- ============ INDEXES ============

-- order_fills: the core table for backtesting queries
CREATE INDEX IF NOT EXISTS idx_fills_maker ON order_fills(maker);
CREATE INDEX IF NOT EXISTS idx_fills_taker ON order_fills(taker);
CREATE INDEX IF NOT EXISTS idx_fills_token_id ON order_fills(token_id);
CREATE INDEX IF NOT EXISTS idx_fills_condition_id ON order_fills(condition_id);
CREATE INDEX IF NOT EXISTS idx_fills_price ON order_fills(price);
CREATE INDEX IF NOT EXISTS idx_fills_block_ts ON order_fills(block_timestamp);
CREATE INDEX IF NOT EXISTS idx_fills_block_num ON order_fills(block_number);
CREATE INDEX IF NOT EXISTS idx_fills_order_hash ON order_fills(order_hash);
-- Composite indexes for common backtesting queries
CREATE INDEX IF NOT EXISTS idx_fills_cond_price ON order_fills(condition_id, price);
CREATE INDEX IF NOT EXISTS idx_fills_cond_ts ON order_fills(condition_id, block_timestamp);
CREATE INDEX IF NOT EXISTS idx_fills_maker_cond ON order_fills(maker, condition_id);
CREATE INDEX IF NOT EXISTS idx_fills_price_ts ON order_fills(price, block_timestamp);

-- order_matches
CREATE INDEX IF NOT EXISTS idx_matches_token_id ON order_matches(token_id);
CREATE INDEX IF NOT EXISTS idx_matches_condition_id ON order_matches(condition_id);
CREATE INDEX IF NOT EXISTS idx_matches_block_ts ON order_matches(block_timestamp);
CREATE INDEX IF NOT EXISTS idx_matches_price ON order_matches(price);

-- resolutions
CREATE INDEX IF NOT EXISTS idx_resolutions_condition ON resolutions(condition_id);
CREATE INDEX IF NOT EXISTS idx_resolutions_ts ON resolutions(block_timestamp);

-- redemptions
CREATE INDEX IF NOT EXISTS idx_redemptions_redeemer ON redemptions(redeemer);
CREATE INDEX IF NOT EXISTS idx_redemptions_condition ON redemptions(condition_id);
CREATE INDEX IF NOT EXISTS idx_redemptions_ts ON redemptions(block_timestamp);

-- markets
CREATE INDEX IF NOT EXISTS idx_markets_end_date ON markets(end_date);
CREATE INDEX IF NOT EXISTS idx_markets_resolved ON markets(resolved);
CREATE INDEX IF NOT EXISTS idx_markets_neg_risk ON markets(neg_risk);
CREATE INDEX IF NOT EXISTS idx_markets_slug ON markets(slug);

-- token_market_map
CREATE INDEX IF NOT EXISTS idx_token_map_condition ON token_market_map(condition_id);

-- user_sessions
CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_time ON user_sessions(started_at);

-- markets (category)
CREATE INDEX IF NOT EXISTS idx_markets_category ON markets(category);
