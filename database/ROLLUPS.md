# Pre-computed Rollup Tables — Design

## Goal

Pre-compute common aggregations over `order_fills` and store them as
incrementally-maintained tables, so quantitative analysts asking the AI
get results in <1 s instead of timing out at 300 s.

Target users: **quants doing research-grade analysis**. Columns are
chosen for analytical breadth — splits by role (maker / taker),
direction (buy / sell), outcome (Yes / No), and time axis — not for
minimum schema size.

- **AI side**: one block of schema description added to `_GENERATE_BODY`
  so the AI knows the rollups exist and when to use them. No code changes.
- **DB side**: each rollup is a regular table (not a `MATERIALIZED VIEW`)
  so we can update *incrementally* — tracking the last consumed
  `order_fills.block_number` via a sync-watermark in `indexer_state`,
  then only aggregating new trades on each run.

## Shared mechanics

1. Every rollup has a sync-watermark in `indexer_state`:
   `<rollup>_synced_block` = highest `block_number` already folded in.
2. One update pass: compute the delta over
   `(synced_block, unified_last_block]`, UPSERT into the rollup, advance
   the watermark — all in one transaction so a crash is either all-or-
   nothing.
3. Each rollup can be backfilled from scratch by setting the watermark
   to 0 and running the update (will take minutes-to-an-hour for the
   first pass; incrementals are seconds).

## Conventions

- **"wallet"** = the union of `order_fills.maker` and `order_fills.taker`.
  Every trade contributes to both sides.
- **"maker side" vs "taker side"**: a wallet is the *maker* when its
  address appears in `order_fills.maker`, *taker* when in
  `order_fills.taker`. Per-role splits (`maker_volume_usd`,
  `taker_volume_usd`) let quants distinguish makers (price-setters,
  capital providers) from takers (flow / momentum traders).
- **"buy" vs "sell" from *this wallet's* perspective**:
  `order_fills.side` is recorded from the maker's viewpoint. We
  re-derive buy/sell from the wallet's own standpoint:
    - wallet is **maker** and `side='BUY'` → wallet **bought** tokens
    - wallet is **maker** and `side='SELL'` → wallet **sold** tokens
    - wallet is **taker** and `side='BUY'` → wallet **sold** tokens
    - wallet is **taker** and `side='SELL'` → wallet **bought** tokens
  This gives an accurate "net long / net short exposure" view per wallet.
- **"USD"** columns are pre-divided by 1e6 at rollup time so the AI
  doesn't have to remember the scaling.
- All rollup tables are append-only via `INSERT ... ON CONFLICT DO UPDATE`.
  No row is ever deleted.

---

## Proposals

### A. `wallet_volume_rollup` — per-wallet lifetime activity

| column | type | what it is |
|---|---|---|
| `wallet` | TEXT PK | maker or taker address |
| **Volume by role** | | |
| `maker_volume_usd` | NUMERIC | USD where this wallet was the maker |
| `taker_volume_usd` | NUMERIC | USD where this wallet was the taker |
| `total_volume_usd` | NUMERIC | = maker_volume + taker_volume (stored for easy ORDER BY; known caveat: sum across all wallets ≈ 2× true USDC flow since each trade is counted for both parties) |
| **Volume by direction** (wallet's own perspective) | | |
| `buy_volume_usd` | NUMERIC | USD spent buying tokens |
| `sell_volume_usd` | NUMERIC | USD received selling tokens |
| **Trade counts** | | |
| `maker_trade_count` | BIGINT | trades as maker |
| `taker_trade_count` | BIGINT | trades as taker |
| `total_trade_count` | BIGINT | maker + taker |
| `buy_trade_count` | BIGINT | — |
| `sell_trade_count` | BIGINT | — |
| **Costs** | | |
| `total_fees_paid_usd` | NUMERIC | fees paid by this wallet when it was maker (`order_fills.fee/1e6`). Taker fees aren't in the schema; only maker pays. |
| **Time span** | | |
| `first_active` | TIMESTAMPTZ | earliest trade |
| `last_active` | TIMESTAMPTZ | latest trade |
| `active_days` | INTEGER | distinct UTC days with ≥1 trade (derived from `wallet_daily_stats`, updated on rollup refresh) |
| `markets_touched` | INTEGER | distinct `condition_id` traded (derived from `wallet_market_pairs`) |
| `updated_at` | TIMESTAMPTZ | bookkeeping |

Indexes: `(total_volume_usd DESC)`, `(last_active DESC)`,
`(maker_volume_usd DESC)`, `(taker_volume_usd DESC)`,
`(total_redemption_usd DESC)`, `(net_pnl_usd DESC) WHERE net_pnl_usd IS NOT NULL`.

**Extensions added after initial build (stages F and G):**

| column | type | source | added |
|---|---|---|---|
| `total_redemption_usd` | NUMERIC | `redemptions.payout / 1e6` | stage F (2026-04) |
| `redemption_count` | INTEGER | `COUNT(redemptions)` | stage F |
| `last_redemption_at` | TIMESTAMPTZ | `MAX(redemptions.block_timestamp)` | stage F |
| `total_split_usd` | NUMERIC | `position_splits.amount / 1e6` | stage G (2026-04-22) |
| `split_count` | INTEGER | `COUNT(position_splits)` | stage G |
| `total_merge_usd` | NUMERIC | `position_merges.amount / 1e6` | stage G |
| `merge_count` | INTEGER | `COUNT(position_merges)` | stage G |
| `net_pnl_usd` | NUMERIC | derived (see below) | stage G |

**`net_pnl_usd` formula**:
`(sell_volume_usd + total_redemption_usd + total_merge_usd) − (buy_volume_usd + total_split_usd + total_fees_paid_usd)`

Realised cash-flow PnL only. Does NOT mark open positions to market and
excludes gas. Recomputed inside `RECOMPUTE_A_DERIVED_SQL` for the
affected-wallet set each cycle (plus every wallet once via `--backfill-g`).

- **Size**: ~1.8M wallets × ~320 bytes = **~580 MB**.
- **Initial backfill**: `GROUP BY wallet` over 200M rows with buy/sell
  CASE aggregates. With `work_mem = 1 GB`, hash-aggregate fits entirely
  in RAM. Estimated **10-20 min**. One-shot G backfill over 136 M
  splits + 23 M merges adds ~3-5 min.
- **Incremental**: seconds per run.

Answers:
- "big wallets" / "small wallets" classification by any volume column
- "pure makers vs pure takers vs mixed"
- "net-long vs net-short biased wallets"
- "most fee-generating wallets"
- "wallets active in last N days"
- "wallets that traded ≥ X distinct markets"
- "top / bottom realised PnL wallets" (`ORDER BY net_pnl_usd DESC / ASC`)

---

### B. `market_volume_rollup` — per-market lifetime activity

| column | type | what it is |
|---|---|---|
| `condition_id` | TEXT PK | — |
| **Volume** | | |
| `total_volume_usd` | NUMERIC | sum of `usdc_amount/1e6` |
| `yes_volume_usd` | NUMERIC | volume on Yes-token trades (binary markets only; NULL for non-binary) |
| `no_volume_usd` | NUMERIC | volume on No-token trades |
| `buy_volume_usd` | NUMERIC | trades where maker side was BUY |
| `sell_volume_usd` | NUMERIC | trades where maker side was SELL |
| **Trade counts** | | |
| `total_trade_count` | BIGINT | — |
| `buy_trade_count` | BIGINT | — |
| `sell_trade_count` | BIGINT | — |
| **Participants** | | |
| `distinct_wallets` | INTEGER | COUNT(DISTINCT wallet) via `wallet_market_pairs` |
| `distinct_makers` | INTEGER | — |
| `distinct_takers` | INTEGER | — |
| **Time / duration** | | |
| `first_trade` | TIMESTAMPTZ | earliest on-chain trade |
| `last_trade` | TIMESTAMPTZ | latest on-chain trade |
| `trading_duration_hours` | NUMERIC | `(last_trade - first_trade)` in hours |
| `active_trading_days` | INTEGER | distinct UTC days with ≥1 trade (from `market_daily_stats`) |
| **Daily / rate** | | |
| `avg_daily_volume_usd` | NUMERIC | `total_volume_usd / NULLIF(active_trading_days, 0)` |
| `avg_daily_trade_count` | NUMERIC | `total_trade_count / NULLIF(active_trading_days, 0)` |
| `peak_daily_volume_usd` | NUMERIC | max single-day volume (from `market_daily_stats`) |
| **Price** | | |
| `first_trade_price` | NUMERIC | price of the earliest trade ("open") |
| `last_trade_price` | NUMERIC | price of the latest trade ("close"); for resolved markets this is typically near 0 or 1 |
| `min_price` | NUMERIC | lowest price ever traded |
| `max_price` | NUMERIC | highest price ever traded |
| `vwap_usd` | NUMERIC | volume-weighted avg price = `SUM(price*usdc_amount) / SUM(usdc_amount)` over the whole market |
| **Fees** | | |
| `total_fees_usd` | NUMERIC | sum of `fee/1e6` |
| `updated_at` | TIMESTAMPTZ | — |

Indexes: `(total_volume_usd DESC)`, `(last_trade DESC)`,
`(avg_daily_volume_usd DESC)`.

Note on overlap with `markets.volume`: that column is sourced from
Polymarket's Gamma API (off-chain) and can drift from on-chain reality.
`market_volume_rollup.total_volume_usd` is the on-chain truth.

- **Size**: ~750K markets × ~350 bytes = **~260 MB**.
- **Initial backfill**: GROUP BY `condition_id` over 200M rows; extra
  cost for the token_market_map JOIN (to split Yes/No volumes).
  Estimated **15-25 min**.
- **Incremental**: seconds per run.

Answers:
- "most-traded markets" (by any volume axis)
- "most-active markets by unique-wallet count"
- "markets that traded for the longest / shortest time"
- "markets with highest daily average activity"
- "markets with biggest peak-day volume spike"
- "markets where the closing price differed most from 0/1" (anomalies at
  resolution)
- "markets with most lopsided Yes vs No volume"

---

### C. `wallet_market_pairs` — bridge: per wallet per market

| column | type | what it is |
|---|---|---|
| `wallet` | TEXT | — |
| `condition_id` | TEXT | — |
| PRIMARY KEY `(wallet, condition_id)` | | |
| `maker_volume_usd` | NUMERIC | — |
| `taker_volume_usd` | NUMERIC | — |
| `total_volume_usd` | NUMERIC | — |
| `buy_volume_usd` | NUMERIC | — |
| `sell_volume_usd` | NUMERIC | — |
| `trade_count` | INTEGER | — |
| `first_trade` | TIMESTAMPTZ | — |
| `last_trade` | TIMESTAMPTZ | — |
| `first_trade_price` | NUMERIC | entry price |
| `last_trade_price` | NUMERIC | exit price |
| `vwap_usd` | NUMERIC | wallet's VWAP in this market |

Indexes: `(wallet)`, `(condition_id)`, `(wallet, total_volume_usd DESC)`.

- **Size estimate**: assume avg wallet touched ~20 markets → 150K × 20
  = **~3M rows × ~180 bytes = ~540 MB**. (Will be validated on the
  initial build.)
- **Initial backfill**: ~20-30 min.
- **Incremental**: a few seconds.

Answers on its own:
- "wallets that traded in both market A and market B" (self-join)
- "top 20 markets for wallet X" (`WHERE wallet=X ORDER BY total_volume_usd`)
- "top 20 wallets for market X"
- "wallet X's entry and exit price in each market they touched"

Also acts as the source for `markets_touched` / `distinct_wallets`
counts on the other rollups.

---

### D. `wallet_daily_stats` — per-wallet per-day

| column | type | what it is |
|---|---|---|
| `wallet` | TEXT | — |
| `day` | DATE | UTC calendar day |
| PRIMARY KEY `(wallet, day)` | | |
| `maker_volume_usd` | NUMERIC | — |
| `taker_volume_usd` | NUMERIC | — |
| `total_volume_usd` | NUMERIC | — |
| `buy_volume_usd` | NUMERIC | — |
| `sell_volume_usd` | NUMERIC | — |
| `trade_count` | INTEGER | — |
| `markets_touched_today` | INTEGER | distinct `condition_id` traded on this day |

Indexes: `(wallet, day)` (PK), `(day, total_volume_usd DESC)` (for
"top wallets on day X"), `(wallet, day DESC)` (for "recent history of
wallet X").

- **Size estimate**: 150K wallets × avg ~15-30 active days = **~3-5M
  rows × ~140 bytes = ~450-700 MB**.
- **Initial backfill**: ~15-25 min.
- **Incremental**: seconds.

Answers:
- "top 100 wallets in January 2025"
- "wallets that were very active in H1 but went dormant in H2"
- "moving average / rolling volume per wallet"
- "weekly / monthly aggregates" (cheap roll-up from this table)
- Backing store for `wallet_volume_rollup.active_days`.

---

### E. `market_daily_stats` — per-market per-day

| column | type | what it is |
|---|---|---|
| `condition_id` | TEXT | — |
| `day` | DATE | UTC calendar day |
| PRIMARY KEY `(condition_id, day)` | | |
| `total_volume_usd` | NUMERIC | — |
| `yes_volume_usd` | NUMERIC | binary markets only; NULL otherwise |
| `no_volume_usd` | NUMERIC | — |
| `buy_volume_usd` | NUMERIC | — |
| `sell_volume_usd` | NUMERIC | — |
| `trade_count` | INTEGER | — |
| `distinct_wallets_today` | INTEGER | — |
| `open_price` | NUMERIC | first trade of the day |
| `close_price` | NUMERIC | last trade of the day |
| `min_price` | NUMERIC | — |
| `max_price` | NUMERIC | — |
| `vwap_usd` | NUMERIC | — |

Indexes: `(condition_id, day)` (PK), `(day, total_volume_usd DESC)`,
`(condition_id, day DESC)`.

- **Size estimate**: 750K markets × avg ~20 active days = **~15M rows ×
  ~220 bytes = ~3.3 GB**.
- **Initial backfill**: ~25-40 min.
- **Incremental**: seconds.

Answers:
- "OHLCV time series for market X" (daily bars)
- "top markets in specific time window"
- "daily volatility / price range per market"
- "time-of-resolution price history" (JOIN with `markets.resolved_at`)
- Backing store for `market_volume_rollup.active_trading_days`,
  `avg_daily_volume_usd`, `peak_daily_volume_usd`.

---

## Storage summary

| rollup | rows | size |
|---|---|---|
| A. wallet_volume_rollup | ~150K | ~38 MB |
| B. market_volume_rollup | ~750K | ~260 MB |
| C. wallet_market_pairs | ~3M | ~540 MB |
| D. wallet_daily_stats | ~3-5M | ~450-700 MB |
| E. market_daily_stats | ~15M | ~3.3 GB |
| **TOTAL** | ~22M | **~4.6 GB** |

Disk used today: 295 GB. Adding 4.6 GB is a ~1.5% bump. Current
available: 480 GB. No storage pressure.

Initial backfill wall-clock: each table 10-40 min; can be run sequentially
or in parallel. Full backfill ~2 hours if sequential, ~45 min if parallel.
Incremental updates afterward: all tables combined ~10-30 s per cycle.

---

## Refresh cadence — 3 options

| option | staleness | complexity |
|---|---|---|
| **(a) Post-indexer hook** | ≤1 batch (seconds) | slows indexer by 10-30 s per batch |
| **(b) Separate daemon, 60 s loop** | ≤60 s | decoupled; most flexible |
| **(c) Cron every 10 min** | ≤10 min | simplest |

Recommendation: **(b)**. Decoupled from indexer (rollup failure doesn't
stop indexing), simple to reason about (a `while True: update(); sleep
60` loop), latency is fine for interactive AI queries.

---

## Things I intentionally did NOT propose (for your call)

1. **Per-hour rollups.** Hourly granularity would let queries like
   "trading activity in the 6 hours around event deadline" run cheaply.
   But it's ~24× the row count of daily, so ~80 GB for market_hourly.
   Tempting but a big commitment. Skip unless we see the use case.

2. **Concentration metrics (Gini, HHI, top-N-wallet share).** These can
   be computed on demand from `wallet_market_pairs` in seconds. Adding
   pre-computed columns means more maintenance; I prefer to let them
   be derived.

3. **Windowed price metrics** (`vwap_last_24h_before_resolution`,
   `vwap_first_hour`). Specific windows like this are many, and each
   requires its own rollup pass. Quants can compute any window cheaply
   from `market_daily_stats`; that's the scalable answer.

4. **Net position / P&L per wallet per market.** Requires joining
   redemption payouts with trade entries — more complex than pure
   volume rollups. Valid quant use case, but deserves its own design
   pass. Skip this round.

5. **Taker fees.** Not in the schema (`order_fills.fee` is only the
   maker fee on current Polymarket contracts). If this changes, we'd
   add a column.

---

## Open questions for confirmation

1. **Scope of first pass**: all 5 tables (A–E), or subset?
   - My recommendation: **all 5**. Quant users want time-windowed
     queries; D+E are essential for that. A+B+C together don't answer
     "top wallets in Q1 2025". Total cost is still only ~4.6 GB and
     ~45 min parallel backfill.

2. **Refresh**: option (a) / (b) / (c)?
   - My recommendation: **(b) separate daemon, 60 s loop**.

3. **Naming**: OK with the names above?
   - Open to renaming before they get baked into prompt + scripts.

4. **Should `active_days` and `markets_touched` be materialized on
   `wallet_volume_rollup`, or derived on query from D / C?**
   - My default is "materialized" so a simple `ORDER BY markets_touched
     DESC` on the main table works. It does add a small refresh-time
     cost (~1 s) but makes the table more self-sufficient.

5. **Non-binary markets** (`outcome_slot_count > 2`): how do we split
   volume? Current proposal has `yes_volume_usd` / `no_volume_usd` as
   NULL for non-binary. Alternatives:
   - Skip non-binary (small minority of markets)
   - Add `outcome_N_volume_usd` as JSONB `{"outcome_index": volume_usd}`
   - Build a separate bridge `(condition_id, outcome_index) → volume`
   - My default: leave Yes/No-split NULL for non-binary and not
     complicate v1. If needed, add the bridge later.

6. **Things I left out (section above)**: any you actually want now?
   - hourly rollups?
   - concentration metrics?
   - windowed price (24h-before-resolution VWAP)?
   - P&L per wallet per market?

Once confirmed, the build order is:
1. `database/rollup.py` — one script, handles all chosen rollups in one
   pass, shared connection, one transaction per table.
2. Initial backfill (run once, report timings per table).
3. Refresh driver per your choice.
4. Update `ai.py::_GENERATE_BODY` with a schema block for the new
   tables and usage hints.
