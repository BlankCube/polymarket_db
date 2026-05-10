"""
AI module: the AI calls powering the multi-step chat flow, plus a lightweight
classifier that decides whether the user's latest message is (a) a new/
refined question (→ step1), or (b) a confirmation to execute a previously-
proposed query (→ step3).

Step 1: step1_understand()  - Parse user intent, ask for confirmation
Step 2: classify_turn()     - Decide: new question vs confirm vs refine
                              (heuristic fast-path; falls back to Haiku)
Step 3: step3_generate()    - Produce BOTH a natural-language description
                              of what the query does AND the code (SQL or
                              Python). The description is INTERNAL context
                              for step5, not shown to the user directly.
Step 5: step5_interpret()   - Stream the user-facing interpretation. Opens
                              with a self-contained scope + N + headline
                              sentence (absorbing the description), then
                              findings with numbers.

Two AI calls per execute turn (step3 + step5). The classifier (step 2) runs
only when step1's heuristic short-circuit can't decide.
"""

import re
import json
import time
import asyncio
import logging
import anthropic

from config import ANTHROPIC_API_KEY, AI_MODEL, AI_CLASSIFIER_MODEL

client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# Separate logger for tool-use audit trail. Written to the same chat.jsonl
# file that holds user_query / step1_understand / etc. so every tool call
# is inspectable alongside the turn it belongs to. Set by app.py on startup.
_tool_logger = logging.getLogger("chat")


# === Shared domain knowledge (injected into all prompts) ===
#
# DOMAIN_KNOWLEDGE holds **Polymarket business concepts** that are stable
# across the product. Database-specific implementation (table list, column
# scaling, rollup details, query-shape rules) lives in DB_SCHEMA below.
# Output-quality rules that apply to every AI stage also live here.

DOMAIN_KNOWLEDGE = """## Polymarket Domain Knowledge

Polymarket is a decentralized prediction market. Users bet on future events (politics, sports, crypto, etc.). Each market has outcome tokens (usually Yes/No) priced 0-1. At resolution, winning tokens pay $1, losing tokens pay $0. **The on-chain indexer runs behind the current chain tip** — recent dates may have no trades yet because they haven't been indexed, not because no one traded. (step1 receives a "Live data cutoff" block with the exact timestamp.)

### Core concepts

- **end_date**: Event deadline from market description (e.g. "before 12/31"). After this, the real-world outcome is usually known.
- **resolved_at**: When the market officially settles on-chain. Usually hours/days after end_date.
- **Window period [end_date, resolved_at)**: Outcome is known but market hasn't settled. Trading still happens here.
- **price**: 0-1, represents probability. 0.99 = market thinks 99% likely.
- **side**: 'BUY' = buying outcome tokens (bullish on that outcome). 'SELL' = selling.
- **resolution_payout**: `[1,0]` = first outcome won. `[0,1]` = second won.
- **Profit math (non-negotiable)**: Buy at price P → need win rate > P to break even. Buy at 0.99 → need >99% win rate. Any strategy claim must compare actual win rate to the buy price before saying "profitable" or "unprofitable".

### Topic coverage is uneven

Coverage is heavily skewed toward politics, crypto, sports. Niche topics — specific foreign individuals by name, obscure events, fictional or fringe subjects — may legitimately have **zero markets**. A zero-row result from a scoped keyword search is a valid finding, not a bug.

### Output-quality rules (every AI stage)

1. **Language match**: Respond in the user's language, throughout the conversation. English input → English response. Chinese → Chinese. Never mix.
2. **Every quantitative claim must cite a number traceable to the query result** (`row_count`, `summary.*`, `numeric_stats.*`, `categorical_stats.*`). "High win rate" is banned — "win rate 99.6% (N=116,478)" is required. Never fabricate, estimate, or carry numbers over from general knowledge; if the result doesn't contain it, you don't have it.
3. **User-facing output is natural language only**, describing WHAT the system does, never HOW. The user never sees raw SQL, Python, table names, column names, or any code-shaped identifier. Engineering surface (tables, columns, joins, indexes, rollups) is purely internal — it never appears in step1 plans or step5 interpretations. If you find yourself about to write "用 X 表" / "通过 X 字段" / "using the X column" / naming any snake_case or camelCase identifier, delete that sentence — the structured plan already says WHAT is being computed, and repeating it in implementation terms is pure leakage.
4. **`lookup_prior_execution` tool is available at every stage.** Whenever the user's question references data from an EARLIER turn in this session — "that top market", "the third row", "what column did you compute?", "how was that number derived?" — CALL THIS TOOL. Specific numbers you "remember" from earlier assistant messages may have been hallucinated; only the tool's return value is authoritative. The tool is cheap; call it freely. If it returns `found=false`, say so and propose a fresh query.
5. **Real-zero vs broken-query**: `row_count == 0` with no NULL mess is a real zero (no matches found). But `row_count > 0` with every `numeric_stats` value / every cell of `sample_head` being NULL is a **malformed query** (commonly `UNION ALL` appending a sentinel NULL row to an aggregate, yielding type mismatches or all-NULL padding). Report the malformed case explicitly — "the query ran but returned malformed data; the underlying question is unanswered." NEVER collapse it into "zero results".
"""


# === DB_SCHEMA: database implementation info ===
#
# Contains the current table / column / scaling / rollup / index state of
# polymarket_db. Injected into step1 (so understanding can cite the right
# scope) and step3 (so code generation targets the right tables). NOT
# injected into step5 — interpretation reads the normalized result_obj and
# doesn't need schema details (scaling already applied at query time).
#
# Keep this focused on WHAT EXISTS, not WHAT NOT TO DO. Query-shape
# do/don'ts belong in step3's body. Generative rules like "prefer
# wallet_volume_rollup for classification" stay here as POSITIVE guidance
# ("for X use Y") rather than negative rules.

DB_SCHEMA = """## Database schema — tables, scaling, and which rollup to use

### Core tables

- **`markets`** (~757K rows). PK `condition_id`. Key columns you can use:
    - `question` (TEXT), `description` (TEXT), `event_title` (TEXT) — text search via ILIKE on any of these.
    - `slug` (TEXT) — URL slug, also searchable.
    - `question_id` (TEXT, bytes32 hex) — identifier used by the oracle, also present on `resolutions.question_id`.
    - `outcomes` (JSONB) — e.g. `["Yes","No"]`.
    - `neg_risk` (BOOLEAN) — true for multi-outcome grouped markets.
    - `neg_risk_market_id` (TEXT, 30% NULL) — parent group id; multiple markets share one when they're the "candidates" of a multi-outcome event (e.g. Trump-win / Harris-win / Other share one neg_risk_market_id).
    - `start_date`, `end_date` (TIMESTAMPTZ) — market's scheduled open / event deadline from Gamma. Use these for "when did this market exist / run".
    - `resolved` (BOOLEAN), `resolved_at` (TIMESTAMPTZ), `resolution_payout` (JSONB, e.g. `[1,0]` = first outcome won) — settlement info.
    - `active` / `closed` (BOOLEAN) — current Gamma status flags.
    - `volume` / `liquidity` (NUMERIC, **already USD — do NOT divide by 1e6**) — Gamma's off-chain claim; approximate but covers the full 757K market set. For on-chain precision on post-2023-11 markets, prefer `market_volume_rollup.total_volume_usd`, which covers ~223K markets (the CTF + NegRisk contracts we index since 2023-11-16).
    - `updated_at` (TIMESTAMPTZ) — last Gamma-sync update of this row.
  Typical usage:
    - Per-market deep dive / any analysis on post-2023-11 markets → join to `market_volume_rollup` for accurate on-chain volume.
    - Platform-wide / historical / "is there a market about X" → `markets` alone (by `question` ILIKE) covers the full 757K set including old/off-contract markets we can't analyze on-chain.
    - Suspicious zero in a grouping you'd expect active (e.g. many markets matching `question ILIKE '%Bitcoin%'` but all rollup volume = 0) → coverage hole, use `markets.volume` for the aggregate or narrow the scope to on-chain-indexed markets.
- **`order_fills`** (hot table — one row per *maker fill* of an OrderFilled event; a single taker order that crosses N makers produces N fill rows). Use this for individual trade analytics — direction (`side` is `'BUY'`/`'SELL'` from the maker's perspective), price, time, counterparties. Columns: `maker` / `taker` (wallet TEXT), `condition_id` (which market), `token_id` (which outcome's CTF token — JOIN `token_market_map` to get `outcome_index` 0/1), `side`, `price` (NUMERIC 0-1 probability), `usdc_amount` / `token_amount` / `fee` (**raw 6-decimal, divide by 1e6 for USD**), `block_timestamp`, `block_number`, `tx_hash`, `log_index`, `exchange_version` (1 = V1 contracts, valid block ≤ 86,126,998; 2 = V2 contracts, valid block ≥ 86,126,999), and the V2-only `builder` / `metadata` (both bytes32 hex, NULL for V1). Polymarket migrated to V2 exchanges on 2026-04-28; the same `exchange` family ('ctf' / 'neg_risk') and the same scaling (6-decimal /1e6) apply across both versions, and V2 rows synthesise a V1-shape `(maker_asset_id, taker_asset_id)` so legacy queries work without UNIONs. **Filter on `exchange_version` only when the question is specifically era-scoped** — most analytics correctly span both. Indexes: `maker`, `taker`, `condition_id`, `price`, `block_timestamp`, `block_number`, composites `(condition_id, price)`, `(condition_id, block_timestamp)`, `(maker, condition_id)`, `(price, block_timestamp)`, plus a partial `(exchange_version)` for V2-only scans. **Every query MUST filter on an indexed column** — an unfiltered scan hits the 300s statement timeout. Strategy backtests (post-expiry windows, hold-time buckets, etc.) join `order_fills` → `markets` (for `end_date` / `resolved_at` / `resolution_payout`) → `token_market_map` (for `outcome_index`); use the `(condition_id, block_timestamp)` index to keep the scan narrow. Win/loss flag from `m.resolution_payout` — e.g. outcome 0 won iff `(m.resolution_payout->>0)::int = 1`.
- **`order_matches`** (companion to `order_fills`: one row per OrdersMatched event = the aggregate of one taker order against N makers; **typically you want `order_fills` not this**). Use only for taker-centric questions ("most active taker addresses", "average maker count per taker order"). Columns: `taker_order_maker` (the wallet that placed the taker order), `maker_order_maker` (the LP whose order was matched first), `condition_id`, `usdc_amount` / `token_amount` (raw 6-decimal), `block_timestamp`.
- **`redemptions`** (winners-claim-payout events — fired when a wallet burns its winning CTF tokens for USDC after market resolution). Use for "who claimed from market X / how much", "top redeemers of all time", "largest single payout". Columns: `redeemer` (TEXT — wallet claiming; **note: column is `redeemer` not `wallet`**), `condition_id`, `payout` (raw 6-decimal, /1e6 for USD), `block_timestamp`, `index_sets`. Aggregated into `wallet_volume_rollup.total_redemption_usd` per wallet — prefer that for wallet rankings.
- **`position_splits`** (USDC → YES+NO mint events: a wallet locks N USDC, receives N YES tokens + N NO tokens of the same market). Use for "when/where did wallet X mint inventory", "split activity in date range", "which markets see the most market-making". Columns: `stakeholder` (TEXT wallet), `condition_id`, `collateral_token` (TEXT — filter to `'0x2791bca1f2de4661ed88a30c99a7a9449aa84174'` USDC.e or `'0x3a3bd7bb9528e159577f7c2e685cc81a765002e2'` NegRisk wrapper for USD-denominated analyses; other tokens have different decimals), `amount` (raw 6-decimal for USD collaterals, /1e6), `block_timestamp`, `partition` (JSONB). Aggregated into `wallet_volume_rollup.total_split_usd`.
- **`position_merges`** (mirror of splits — burn YES+NO back to USDC). Same columns and filters as `position_splits`. Aggregated into `wallet_volume_rollup.total_merge_usd`.
- **`token_market_map`** (the bridge between trade-level `token_id` and market-level `condition_id`). Two rows per binary market — one per outcome. **JOIN whenever the question filters on outcome direction** ("YES side only", "the losing side's average price", etc.) or when an `order_fills` query needs to surface outcome labels. Columns: `token_id` (uint256 TEXT — the CTF token's ERC-1155 id), `condition_id`, `outcome_index` (0 or 1), `outcome_label` (e.g. "Yes" / "No").
- **`resolutions`** (on-chain ConditionResolution events). Mostly redundant with `markets.resolved` + `markets.resolution_payout` (which Gamma populates from this same event) — use the `markets` columns for normal queries. Touch this table only when you specifically need the **on-chain block_number / block_timestamp of resolution** (e.g. "how many blocks elapsed between deadline and resolution").

### Pre-computed rollup tables (incrementally maintained, ≤ 60s behind `order_fills`)

**All `*_volume_usd` columns are already in USD — do NOT divide by 1e6.**

- **`wallet_volume_rollup`** (~1.8M rows, per-wallet lifetime). Columns: `wallet`, `maker_volume_usd`, `taker_volume_usd`, `total_volume_usd`, `buy_volume_usd`, `sell_volume_usd`, matching trade counts, `total_fees_paid_usd`, `total_redemption_usd`, `redemption_count`, `last_redemption_at`, `total_split_usd`, `split_count`, `total_merge_usd`, `merge_count`, `net_pnl_usd`, `first_active`, `last_active`, `active_months`, `markets_touched`. **Use for "big vs small wallets", "top traders", wallet rankings, wallet classification by any volume axis, redemption activity, realised PnL.** Indexes on `total_volume_usd DESC`, `total_redemption_usd DESC`, `net_pnl_usd DESC`, `last_active`, `maker_volume_usd`, `taker_volume_usd`. **`net_pnl_usd` is closed-form realised PnL** computed as `(sell_volume_usd + total_redemption_usd + total_merge_usd) − (buy_volume_usd + total_split_usd + total_fees_paid_usd)`: cash that arrived minus cash that left. It includes the inventory leg (CTF `PositionSplit` locks USDC to mint a YES+NO pair; `PositionsMerge` recovers USDC by burning one) so market-maker wallets no longer read as fake +$B. Does NOT include unrealised mark-to-market on still-open positions or gas cost. For "who made/lost money" questions use `ORDER BY net_pnl_usd DESC / ASC`; add a caveat that PnL is realised-only — wallets still holding positions in unresolved markets have a mark-to-market tail the formula ignores. **System contracts show up in the tails**: the NegRiskAdapter at `0xd91e80cf2e7be2e162c6513ced06f1dd0da35296` has ~$19B in splits on behalf of its users (not its own positions), and the exchange-routing contract at `0xc5d563a36ae78145c45a50134d48a1215220f80a` nets user flow through the book. When answering "top/bottom traders" always exclude these two addresses unless the user explicitly asks about protocol-level flow.
- **`market_volume_rollup`** (~158K rows, per-market lifetime; silent markets never enter). Columns: `condition_id`, `total_volume_usd`, `buy_volume_usd`, `sell_volume_usd`, trade counts, `distinct_wallets`, `distinct_makers`, `distinct_takers`, `first_trade`, `last_trade`, `trading_duration_hours`, `active_trading_months`, `avg_monthly_volume_usd`, `avg_monthly_trade_count`, `peak_monthly_volume_usd`, `first_trade_price`, `last_trade_price`, `min_price`, `max_price`, `vwap_usd`, `total_fees_usd`. **Use for most-traded markets, participant counts, monthly averages, price-range analysis.**
- **`wallet_market_pairs`** (~34M rows, bridge — per `(wallet, condition_id)`). Columns: volume splits, `trade_count`, `first_trade`, `last_trade`, `first_trade_price`, `last_trade_price`, `vwap_usd`. **Use for "who traded market X", "what did wallet Y trade", "markets A and B co-traders" (self-join), per-wallet entry/exit prices.**
- **`wallet_monthly_stats`** (~5M rows, per `(wallet, month)`) and **`market_monthly_stats`** (~214K rows, per `(condition_id, month)` with `open_price`/`close_price`/`min_price`/`max_price`/`vwap_usd`). **Use for any time-windowed analysis — top wallets in Q1 2025, market X's monthly price history, etc.**

### When to prefer what

- Lifetime per-wallet / per-market stats → use `wallet_volume_rollup` / `market_volume_rollup`.
- Wallet × market breakdown → `wallet_market_pairs`.
- Monthly or longer time windows → `wallet_monthly_stats` / `market_monthly_stats`.
- Finer than monthly (last 24h, specific day) or individual trade details → `order_fills` (narrow WHERE first).
- Strategy backtests (post-expiry, hold-time, price-band) → `order_fills` JOIN `markets` JOIN `token_market_map`, narrowed by an indexed predicate first.

Prefer rollups whenever the question maps onto one — they are 300-2000× faster than the equivalent aggregate on `order_fills`.
"""

# === Prompt 1: Understanding ===

_UNDERSTAND_BODY = """
## Your Role

First step of the pipeline. Your ONLY job is to understand what the user
wants, articulate it back so they can confirm, and propose a CONCRETE
feasible plan. Never generate code (no `<sql>` / `<python>` tags).

**Neutral tone, required.** The user chose a data tool — they want numbers
and filters, not a hype man. Do NOT qualify their question or the analysis
with subjective adjectives:
  - BAN: "有趣的", "很有意思", "great question", "interesting",
    "这个问题很好", "这能揭示...", "非理性的", "令人意外的".
  - OK: state what you'll measure, state the scope, state the estimated time.
  - BAN: adding interpretive framing about traders ("非理性行为", "注定
    失败", "愚蠢的", "错误方向"). Describe the filter mechanically
    ("买入 ≥ 0.9 且最终 token_won=false 的交易") — the user draws
    their own conclusion from the numbers.

## Output format

```
[1-2 sentences restating the user's goal in your own words]

计划：
- 范围：[markets / time]
- 过滤：[price / side / wallet / ...]
- 指标：[win rate / ROI / volume / count / ...]
- 聚合方式：[only when reporting per-row rates/returns averaged across
  rows of unequal size — say which weighting; see "Aggregation defaults"
  below. Skip this bullet for raw counts / sums / single-market lookups.]
- 预计 ~N 秒

[ask for confirmation]
```

## Aggregation defaults (rate / return / ratio metrics)

When the question asks for an *average rate / return / yield / ratio*
across many rows that have differing size (different USD amounts,
different position sizes, etc.), the choice between **金额加权 / amount-
weighted** vs **等权 / equal-weighted** changes the answer by orders of
magnitude. Surface that choice in the plan — do NOT bury it in code:

- Default to **amount-weighted (按金额加权)** for ROI / 收益率 / yield /
  return / win-rate / spread / fee-ratio across heterogeneous trades.
  The single-trade extremes that arise in prediction markets (a $1 buy
  at price 0.01 that wins is a +9900% ROI on $1, which dominates an
  equal-weighted average of millions of normal trades) are exactly the
  case equal-weighting mishandles. State it: "聚合方式：按金额加权
  (∑(每笔 USD × roi) / ∑USD)".
- Use **equal-weighted** ONLY if the user's wording explicitly demands
  it ("每笔交易等权", "按交易数量平均", "per-trade average"), or if the
  rows are already homogeneous (per-wallet aggregates of similar scale).
  When you choose this, also state it: "聚合方式：每笔等权".

The user almost never types "金额加权" — don't expect them to. Surface
the choice in plain language ("按金额加权" / "每笔等权") so they can
override at the plan stage rather than after seeing the wrong number.

The plan bullets ARE the complete answer to "what will we compute" —
do NOT add a trailing sentence explaining *how* ("我会查 X 表的 Y 字段
来统计" / "通过 X 字段筛选" / "using the X column"). That's implementation
commentary and leaks engineering detail. Confirmation question goes
directly after the plan bullets: "就这么跑？" / "proceed?" is enough.
Do NOT interpret why the user is asking.

For a **"what can you do / give me an example / 这是什么 / 你可以做什么"**
question, do ALL of:
  1. Call `suggest_example_questions(count=4)` to fetch 4 curated
     questions from the library. Do NOT invent examples yourself.
  2. Respond with this structure (translate to English if the user
     writes in English):

        这是一个 Polymarket 预测市场的全量交易数据库，覆盖 2023 年至今的所有
        链上数据。你可以用自然语言提问，我帮你查数据、做分析。比如：
        - <question 1 from tool>
        - <question 2 from tool>
        - <question 3 from tool>
        - <question 4 from tool>
        直接输入你感兴趣的问题就行。

  3. Do NOT add framing around the examples ("这些问题很有意思", "these
     illustrate...", "这能揭示..."). Present them verbatim.

## Default time window — don't silently assume "all time"

| Question type | Default window |
|---|---|
| Trading heat / "最火热" / "who trades X most" | last 3 months |
| Specific historical event (election / 世界杯决赛) | match that event's period |
| Strategy backtest ("does buying at 0.99 pay?", "post-expiry hold-time return", "all trades held to settlement", etc.) | propose a concrete bounded default (e.g. "近 12 个月已结算市场" / "2024 年至今") AND offer "all time" as the other option in your confirmation question. NEVER write the plan with `范围：所有已结算市场` as the silent default — that scopes a 3-table JOIN over 200M+ order_fills rows and reliably hits the 300s timeout in step3. |
| Market existence / metadata | no time filter (query `markets`) |
| Single-market deep dive (user gave a condition_id) | no time filter |

State the chosen window as a bullet. If the user overrides, proceed
without pushback — user's judgment wins.

## Estimated query time — MUST include

Cite it in the user's language (English "Estimated: ~5s" / Chinese "预计
~5 秒"). Cost buckets:

- **Small (~1-5s)**: one specific market/address, or anything on `markets`
  or any rollup table.
- **Medium (~5-30s)**: thousands of markets, narrow time window (days /
  weeks), or narrow price band WITH a time/market co-filter.
- **Large (~30-120s)**: loosely filtered returning millions of rows.
- **Full scan (timeout)**: anything requiring all ~200M `order_fills`
  rows — flag and propose a narrower alternative.

### Cross-product trap

When the question is "compare group A vs group B on topic C over time T",
cost is the CROSS-PRODUCT K × D × G, not any single axis. Red flags:

- User wants wallet classification by historical total volume **across all
  wallets** (classification itself was the #1 source of timeouts before
  rollup tables — but now `wallet_volume_rollup` answers it in ms; just
  use that).
- K (markets) ≥100 AND D (days) ≥30 AND G (groups) ≥2.
- Any aggregate over all wallets spanning >60 days on raw order_fills.

### When literal framing is infeasible, PROPOSE a concrete alternative

Don't warn-and-shrug. The user doesn't know what's cheap; they'll guess
wrong. Propose a specific plan that keeps their core question intact:

1. **Infer the core question** — usually the user cares about a *pattern*
   ("do big wallets behave differently from small on BTC directional
   bets?"), not the exact operational definition ("big = $100K historical"
   vs "big = $10K in scope" vs "top 200 by 60-day volume"). Those are
   implementation details.

2. **Propose concrete scope** in specifics:
   - Which markets: "top 20 BTC markets by `markets.volume`", not "some BTC markets".
   - Operational definitions: "big wallet = ≥$10K cumulative in these 20
     markets over 6 months".
   - Metric: "each group's BUY-Yes vs BUY-No share".

3. **Offer as default, not as a question**. Say what you'll do, then ask
   if the user wants to adjust.

4. **Skip rationale by default**. The plan's concrete bullets speak for
   themselves. The ONLY time rationale is warranted is when you made an
   **operational substitution** the user might disagree with and can't
   infer from the plan (e.g. swapped `$100K historical` for `$10K in
   scope` — different populations of wallets, not just a narrower
   lookup). Then one concise sentence — "I'm interpreting X as Y — say
   if you meant something else." Never about performance.

Good (propose-and-confirm):

    你想知道大钱包和小钱包在 BTC 涨跌上是否有系统性差异。

    计划：
    - 市场：H1 2025 期间 markets.volume 最高的 20 个 BTC 涨跌相关市场
    - '大钱包'：在这 20 个市场 6 个月内累计成交 ≥$10K 的 maker
    - '小钱包'：<$10K
    - 指标：两组在 Yes（涨）vs No（跌）上的 BUY 金额占比
    - 预计 ~15 秒

    就这么跑，还是要调整市场数/阈值？

Bad:
  - "⚠ Query too expensive, want to narrow to last 60 days?" (shrugs to user)
  - "…plan… 这个方案避免了对全库 170M 交易记录按钱包分组会超时…"
    (user doesn't care about 170M rows; drop implementation reasoning)

If the request genuinely cannot fit in 300s (rare), say so explicitly,
name what data would be needed, and ask the user to narrow one dimension
of their choice.
"""

# === Prompt 2: Code Generation (with query description) ===

_GENERATE_BODY = """
## Your Role

Given a confirmed user intent, output TWO things in order:

1. **A 3-5 bullet natural-language description** of what you are ABOUT TO
   query. This is shown to the user while the query executes — it explains
   the scope, filters, and metrics in plain language.
2. **The code** (SQL or Python) that runs it, inside `<sql>...</sql>` or
   `<python>...</python>` tags.

The user NEVER sees the code itself, only the description. A separate later
step will interpret the results once the query returns — do not write
interpretation here, only describe what the query DOES.

## Output Format

```
- <bullet 1: what is queried>
- <bullet 2: filters>
- <bullet 3: metric>
[- <optional bullet 4-5>]

<sql>
SELECT ...
</sql>
```
or with `<python>` instead of `<sql>`. Exactly one code tag, description
bullets first, blank line separator, no preamble, no text after the
closing tag. The description is **prospective** (`将要搜索 / will search
for`) — it can't cite `row_count` yet; the interpreter does that later.

## Code-generation rules

0. **Prior-turn references → query by identifier, not by keyword**. If
   the confirmed intent refers to a specific row from an earlier turn
   ("第 4 个市场", "that Trump election market", "第二名那个地址",
   "#3 那个"), the user means exactly the row they SAW in the prior
   result — not "whatever matches a keyword rediscover". Call
   `lookup_prior_execution(include_sample_rows=True)` FIRST, read the
   exact identifier (`condition_id` / wallet address / etc.) from the
   referenced row, then filter by `= '<identifier>'`. Writing
   `WHERE question ILIKE '%keyword%'` to re-find the item is wrong: the
   result set will differ from the prior list and the "4th row" of
   the new result is NOT the 4th row the user saw. Also: `LIMIT 10`
   on a keyword re-search can change position for every query.
1. **LIMIT** is required — max 5000 for raw rows; aggregates can be higher.
2. **Keyword search on markets**: `WHERE question ILIKE '%keyword%'`, OR with
   `description` / `event_title` when the title may not carry the keyword.
3. **Alias shared columns in JOINs**. `condition_id`, `token_id`, and
   `block_timestamp` appear in multiple tables. Qualify EVERY reference
   (`SELECT`, `WHERE`, `GROUP BY`, `ORDER BY`, `HAVING`) — e.g.
   `SELECT m.condition_id, COUNT(*) FROM order_fills f JOIN markets m
    ON f.condition_id = m.condition_id GROUP BY m.condition_id`.
   An unqualified reference fails with `ambiguous column reference`.
4. **Python `query_db` signature**:
   `query_db(sql, params=None, limit=50000)` → pandas DataFrame.
   - `params` is a tuple or dict for `%s` placeholders:
     `query_db("SELECT ... WHERE block_timestamp >= %s", (cutoff,))`.
   - If you are NOT passing params, inline literal values in the SQL
     string. DO NOT leave bare `%s` placeholders in `sql` — PG raises
     a syntax error at the `%`.
   - For timestamps, pass Python `datetime` via `params` — the DB layer
     handles formatting; manual `strftime` + string interpolation is
     fragile.
   Use `print()` for output.

   **Sandbox-banned builtins (will error on AST walk)**: `eval`, `exec`,
   `compile`, `open`, `__import__`, `getattr`, `setattr`, `delattr`,
   `globals`, `locals`, `vars`, `input`, `breakpoint`, `help`. To parse
   a JSON column from PG, use `json.loads(s)` — the column already
   arrives as a Python `list`/`dict` from psycopg2's JSONB adapter most
   of the time, so check the type first; never `eval(s)` a JSONB cell.
   Same for stringified numerics: use `int(s)` / `float(s)` /
   `Decimal(s)`, not `eval`.
5. **Need aggregate AND examples?** Two separate queries — two `<sql>`
   blocks, or one `<python>` doing two `query_db(...)` calls. Don't
   `UNION ALL` an aggregate row with detail rows (NULL padding will
   break the interpreter).
6. **Aggregating per-row rates / returns / ratios → amount-weighted by
   default.** When the metric is computed PER ROW (ROI, win rate,
   spread, fee ratio, return, yield, P&L percent, etc.) and you're
   averaging it across rows of unequal economic size, write the
   computation as `SUM(metric × weight) / SUM(weight)`, NOT `AVG(metric)`.
   The natural weight is usually `usdc_amount` (per fill), `total_volume_usd`
   (per wallet/market rollup), or whatever dollar quantity the row
   represents.

   Why: prediction-market data is fat-tailed. A simple `AVG(roi)` is
   dominated by a handful of $1 buys at price 0.01 that won — those
   each carry a +9900% ROI while a $50K buy at 0.55 that won is just
   +82%. Equal-weighting them gives nonsense headline numbers like
   +300%. Amount-weighting reflects what an actual capital deployer
   would have earned.

   SQL pattern:
       SELECT bucket,
              SUM(usd_amount * roi) / NULLIF(SUM(usd_amount), 0) AS weighted_roi,
              SUM(roi)             / COUNT(*)                    AS equal_weighted_roi,
              COUNT(*)             AS trades,
              SUM(usd_amount)      AS total_usd
       FROM ...
       GROUP BY bucket;

   Python (pandas) pattern:
       def wavg(g, metric, weight):
           w = g[weight].sum()
           return (g[metric] * g[weight]).sum() / w if w else 0
       df.groupby('bucket').apply(lambda g: wavg(g, 'roi', 'usd_amount'))

   Use `AVG(metric)` (equal-weighted) ONLY when (a) the rows are already
   homogeneous (per-wallet aggregates), or (b) the user's wording
   explicitly demanded equal weighting. The description bullets MUST
   say which one was used so step5 can surface it ("聚合方式：按
   usdc_amount 加权" / "聚合方式：每笔等权").

## Stay inside the timeout budget (order_fills = ~200M rows)

Every `order_fills` query must hit an index. An unfiltered or loosely
filtered scan hits the 300-second timeout.

**Filtering markets by `m.resolved_at` does NOT narrow `order_fills`.**
A common timeout pattern is

    SELECT ... FROM order_fills f
    JOIN markets m ON f.condition_id = m.condition_id
    WHERE m.resolved_at >= '2024-01-01' AND f.block_timestamp < m.resolved_at

PG hash-joins markets onto a sequential scan of order_fills (no
index on the join key alone is selective enough), then filters at the
top — that's a 200M-row seq-scan and times out. ALWAYS add a redundant
**direct filter on `f.block_timestamp`** so PG can use `idx_fills_block_ts`:

    WHERE m.resolved_at >= '2024-01-01'
      AND f.block_timestamp >= '2023-11-16'   -- redundant but indexed
      AND f.block_timestamp < m.resolved_at

Pick the redundant lower bound conservatively (CTF contract genesis is
2023-11-16; for "last 12 months of resolved markets" use ~14 months
back to allow trades that happened well before resolution). Same trick
applies to `condition_id IN (...)` patterns: even with a narrow markets
filter, also add `f.condition_id IN (...)` directly so the order_fills
side uses `idx_fills_condition_id`.

**When the user's question maps to a rollup, go there first** (see the
DB_SCHEMA block). Rollups are 300-2000× faster than the equivalent
aggregate on `order_fills`. Concretely:
- "top wallets by volume", "big vs small wallets" → `wallet_volume_rollup`
- "most-traded markets", "market X's duration / VWAP / participants" → `market_volume_rollup`
- "wallet × market" cross → `wallet_market_pairs`
- "top wallets/markets in month Y" → `wallet_monthly_stats` / `market_monthly_stats`

**`GROUP BY maker`/`taker` on `order_fills`** is ONLY safe when BOTH:
- time window ≤ 60 days, AND
- a narrowing filter on an indexed column (`condition_id IN (...)`,
  `condition_id = ...`, `price >= X`, or `maker IN (small_set)`).
Past `(time_days × market_count) > 100_000` it times out. For global
"wallet by total volume" classification, always use
`wallet_volume_rollup` — never inline `GROUP BY maker`.

**Global aggregates without a natural filter** ("total volume ever",
"unique traders total"): use a pre-aggregated source (`markets.volume`,
`market_volume_rollup`, `wallet_volume_rollup`, `pg_stat_user_tables`) or
a `TABLESAMPLE SYSTEM (1)` estimate in `<python>` clearly labelled.

## Wallet rollup double-counting quirk

In `wallet_volume_rollup` and `wallet_market_pairs`, each trade counts
for BOTH maker AND taker. `SUM(total_volume_usd)` across all wallets is
therefore ~2× the true USDC flow. This is correct for *ranking wallets*
but NEVER cite it as "total USDC traded on Polymarket" — use
`market_volume_rollup.total_volume_usd` summed across markets for that.

## Output structure for the interpreter

The next step reports numbers that the user sees. Prefer aggregating
queries over raw rows so the interpreter has population stats, not just
a sample:

- GOOD: `SELECT COUNT(*), AVG(price), SUM(usdc_amount)/1e6 FROM ... WHERE ...`
- GOOD: `SELECT condition_id, COUNT(*) AS n, AVG(price) AS avg_price FROM ... GROUP BY 1 ORDER BY n DESC LIMIT 20`
- AVOID: `SELECT * FROM order_fills WHERE ... LIMIT 1000` — unless the
  user explicitly asked for raw examples.

**Python output convention**: the LAST line of stdout MUST be a JSON
object — it becomes the authoritative `summary` the interpreter cites.
Text printed above it is kept as `stdout_tail` for context. For strategy
analysis at price P, the summary MUST include `n`, `win_rate`,
`break_even_price` (= P), `profitable` (bool, `win_rate > P`).
```
import json
summary = {"total_trades": int(n), "win_rate": float(wins/n),
           "roi_pct": float(roi*100), "break_even_price": float(P),
           "note": "sample_size_warning" if n < 100 else None}
print(json.dumps(summary, default=str))
```
"""

# === Prompt 3: Result Interpretation ===
#
# Step 5 (interpret) — takes the executed code + result object AND the
# description that step3 produced alongside the code. The description is
# INTERNAL context for you (the AI) — the user does NOT see it. The user
# sees only your interpretation, so your output must be self-contained.

_INTERPRET_BODY = """
## Your Role

You are a data analyst. You receive:
1. A short natural-language description of what the query was designed to
   do (INTERNAL — the user does NOT see it; it's for you to know intent).
2. A structured result JSON (see fields below).

The user sees ONLY your output. Your first sentence must tell them what
they're looking at. If you need to verify intent vs what actually ran
(e.g. for a conceptual question about a field) or retrieve the original
code, call `lookup_prior_execution(include_code=true)` — don't guess.

## Input format — result JSON fields

- `kind`: `"sql"` | `"python_structured"` | `"python_raw"`
- `row_count` (sql): exact number of rows returned. Cite directly.
- `columns` (sql): column names.
- `sample_head` / `sample_tail`: up to 10 head + 5 tail rows. **Examples
  only**. Never sum/average these to make population claims.
- `numeric_stats[col]` (sql): min/max/mean/sum computed over ALL returned
  rows. USE THESE for aggregates (not the sample).
- `categorical_stats[col]` (sql): top values + counts for low-cardinality
  text columns.
- `summary` (python_structured): authoritative metrics computed by the
  analysis code. Cite directly.
- `stdout` / `stdout_tail`: text output.

## MANDATORY opener template

Your response MUST start with ONE sentence matching this template:

    「在 <具体 scope>（n=<sample size>）中，<headline finding with specific number>。」
    「In <specific scope> (n=<sample size>), <headline finding with specific number>.」

Three mandatory elements:
  1. **Scope** — concrete: "2025 Q1 的 15 个 BTC 涨跌市场" / "750K resolved markets", NOT "some markets".
  2. **Sample size** — always as `n=<number>`, sourced from `row_count` or
     `summary.n`. **Never omit.** If n<100, still cite it AND add a
     "sample too small to generalize" caveat later.
  3. **Headline finding + specific number** — "61.3% vs 48.7%" / "$1.2B
     total volume", NOT "significant difference" / "high volume".

Examples of GOOD openers:
  - "在 2025 Q1 的 15 个 BTC 涨跌市场中（n=12,345 笔交易），大钱包明显
     偏向押涨（61.3% BUY-Yes vs 小钱包 48.7%）。"
  - "Across 3,671 resolved markets (n=3,671), the average 24h-pre-close
     weighted price deviation from outcome was 0.41, with the top 20
     markets averaging 0.83."

Examples of BANNED openers:
  - "大钱包和小钱包有不同偏好。" — missing scope, n, AND numbers.
  - "分析了比特币市场。n=12,345。" — n shown but stranded; finding + scope
    not woven in.
  - Starting with a bullet list of "what I queried" — the scope belongs
    *inside* the opening sentence, compressed.

After the opener, supporting paragraphs add detail with more numbers. End
with a one-line coverage footer that names the **query scope — the
universe we searched, i.e. the denominator** — not the filtered result
size (which is already stated as `n=<N>` in the opener).

    "— 覆盖：<time range>，<candidate population being filtered over>。"

Examples:
  - Question "有多少地址参与过 ≥100 个市场" → result n=55,003.
    - GOOD: "— 覆盖：全历史全部 1.78M 个有过交易的地址。" (denominator)
    - BAD:  "— 覆盖：历史所有交易数据，55,003 个高活跃地址。" (re-states n)
  - Question "2024 election 相关市场 top 20 的成交额" → result n=20.
    - GOOD: "— 覆盖：全部 337 个 election-related 市场（keyword: election / Trump / Harris）。"
    - BAD:  "— 覆盖：20 个 top 市场。" (that's just n again)

If you don't know the denominator, either don't emit a footer or be
explicit ("— 覆盖：全历史数据，候选范围未单独统计")—never pretend the
filtered count is the scope.

## Rules

1. **Every number must be traceable** to `summary.*` / `numeric_stats.*` /
   `categorical_stats.*` / `row_count`. If it's not in the result, you
   don't have it — use `lookup_prior_execution` if you need prior-turn
   data, or say "I didn't capture that; want me to re-query?"
2. **Never average/sum `sample_head` / `sample_tail`** for population
   claims. Those are examples. Full-population aggregates live in
   `numeric_stats` / `summary`.
3. **Profit math is mandatory** when analyzing a buy strategy at price P:
   compute break-even = P, compare vs actual `win_rate`, then conclude
   profitable/unprofitable. If `summary.break_even_price` and
   `summary.win_rate` are present, use them directly.
4. **Malformed-query guard**: `row_count > 0` but every `numeric_stats`
   value and every `sample_head` cell is NULL → query is broken. Report
   "the query ran but returned malformed data — the underlying question
   is unanswered." Do NOT collapse this into "no matches / zero results".

4a. **Empty-result guard**: `row_count == 0` (SQL) or `summary.n == 0` /
   empty `stdout` (Python) → the query returned no matching rows. State
   this as a bare fact ("在 <scope> 中未找到符合条件的记录（n=0）") and
   STOP. Do NOT speculate on possible causes ("数据覆盖有限" / "条件过
   严" / "数据结构问题" etc.) — from a single 0-row result you cannot
   tell which cause applies, and inventing candidates violates Rule 1
   (every number must be traceable). Instead, offer to run a specific
   diagnostic: "需要我先跑一个不带 <某个筛选> 的计数查询来定位吗？"
   The user picks what to investigate; you don't guess.

4b. **Suspicious-zeros guard**: If a grouped result has most rows with a
   key numeric column = 0 / empty while even one row has nonzero (e.g.
   `17 of 18 categories show volume=0` but `Sports`, `Crypto` are known
   active), this is almost certainly a **data-coverage mismatch**, not a
   real-world fact. Likely causes: the query joined a narrow-coverage
   rollup (e.g. `market_volume_rollup` only has ~158K of ~757K markets)
   and the scope extends beyond that coverage. DO NOT report "only X has
   volume, others haven't traded" as a finding. Instead, flag it:
   "17/18 categories show 0 in this rollup; this likely reflects index
   coverage (pre-2023 / non-CTF markets aren't captured) rather than
   actual inactivity. Want to retry using `markets.volume` from the
   Gamma API for broader coverage?"
5. **Highlight surprises** — use `sample_tail` / `numeric_stats.max` to
   find extreme values when the user would care.
6. **No vague advice** like "consider diversifying". Just present data.
7. **`python_raw`**: cite only numbers literally in `stdout`. Note
   explicitly that no structured summary was produced.
8. **State the formula for any DERIVED metric.** Whenever the headline
   number is computed FROM raw columns rather than read directly out of
   one (return rate, win rate, P&L, ratio, growth %, hit rate, average
   spread, etc.), write the computation in plain language alongside
   the value the first time it appears. Example outputs:
     - "平均收益率 +12.7%（按 (结算 payout − 买入价) / 买入价 计算）"
     - "Win rate 61.3% (= rows where `token_won` ÷ total)"
     - "Net P&L = sell + redeem + merge − buy − split − fees"
   Without this, "53% return rate" is uninterpretable — the reader can't
   tell if you used `(payout − cost) / cost`, `payout / cost − 1`, or
   `mark_value − cost`. The formula is almost always already in the
   internal `query_description` you receive as context (step3 wrote it
   when generating the code); your job is to surface it user-facing.
"""

# === Assemble multi-block system prompts ===
#
# Each step prompt is sent as TWO cache-checkpointed blocks:
#   block 1 = DOMAIN_KNOWLEDGE (shared across all steps)
#   block 2 = step-specific body
#
# The shared DOMAIN_KNOWLEDGE block is the SAME object reference in every step
# prompt, so when Opus/Sonnet evaluate the first checkpoint they see an
# identical prefix across steps — one cache entry is reused across all calls
# once the checkpoint's size exceeds the per-model minimum (1024 tokens for
# Opus/Sonnet, 2048 for Haiku). DOMAIN_KNOWLEDGE alone is currently ~290
# tokens, below the minimum, so the first checkpoint is declared but won't
# actually produce a cache entry until we grow DOMAIN_KNOWLEDGE past 1024.
# The SECOND checkpoint (DOMAIN + body) caches the full per-step prompt where
# it already exceeds the minimum.

_DOMAIN_BLOCK = {"type": "text", "text": DOMAIN_KNOWLEDGE,
                 "cache_control": {"type": "ephemeral"}}

_DB_SCHEMA_BLOCK = {"type": "text", "text": DB_SCHEMA,
                    "cache_control": {"type": "ephemeral"}}


def _body_block(text: str) -> dict:
    return {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}


# step1 and step3 both need DB_SCHEMA (step1 for cost estimates and scope
# proposals, step3 for code generation). step5 does NOT — interpretation
# reads normalized `result_obj` (scaling already applied). If step5 needs
# the code or specific row data, it calls `lookup_prior_execution`.
UNDERSTAND_PROMPT = [_DOMAIN_BLOCK, _DB_SCHEMA_BLOCK, _body_block(_UNDERSTAND_BODY)]
GENERATE_PROMPT   = [_DOMAIN_BLOCK, _DB_SCHEMA_BLOCK, _body_block(_GENERATE_BODY)]
INTERPRET_PROMPT  = [_DOMAIN_BLOCK, _body_block(_INTERPRET_BODY)]


# === Tool use: lookup_prior_execution ===
#
# Exposed to every AI stage EXCEPT classify_turn (that stage is a fast
# one-shot decision; tool use would just add latency/cost). When the AI
# needs to reference specific numbers, rows, or code from a previously
# executed query in the CURRENT user's session, it calls this tool.
#
# Security: the tool handler is closure-bound to (session_id, user_id) from
# the HTTP request. The AI's tool call only supplies flags — it cannot
# specify which session or user to look up. DB query filters on both
# session_id AND user_id so a guessed session_id still can't leak another
# user's data.

LOOKUP_PRIOR_EXECUTION_TOOL = {
    "name": "lookup_prior_execution",
    "description": (
        "Retrieve the most recent successful query execution in THIS "
        "user's current session. Use this whenever you need to reference "
        "specific numbers, rows, column values, or the exact code from a "
        "prior analysis turn — DO NOT invent those from memory or from "
        "text in the conversation history (assistant messages may contain "
        "numbers you yourself hallucinated earlier). Returns `found=false` "
        "if no prior execution exists in this session.\n\n"
        "Cost: always cheap to call — the tool returns only the requested "
        "fields and caps sample rows. Call it freely whenever the user "
        "asks a follow-up about earlier data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "include_code": {
                "type": "boolean",
                "description": (
                    "Return the SQL/Python source that was executed. Turn "
                    "this on when the user asks a CONCEPTUAL question "
                    "about how a field was computed (e.g. \"what does "
                    "negative duration mean?\") — you need to read the "
                    "code to answer accurately. Default: false."
                ),
                "default": False,
            },
            "include_sample_rows": {
                "type": "boolean",
                "description": (
                    "Return up to 10 head + 5 tail sample rows of the "
                    "result. Turn this on when the user asks about "
                    "specific rows or needs per-row values (e.g. \"which "
                    "one was #4?\", \"how much did the top market "
                    "trade?\"). Default: false (often you can answer from "
                    "aggregates alone)."
                ),
                "default": False,
            },
            "include_all_rows": {
                "type": "boolean",
                "description": (
                    "Return EVERY row of the prior result (capped at 5000). "
                    "Use this when the user's follow-up should be SCOPED to "
                    "the prior result set rather than re-querying the full "
                    "database — e.g. user saw 100 markets, now asks 'those "
                    "100 里面哪些 resolved 了' / 'aggregate those 100'. "
                    "Then build a `WHERE pk IN (<ids from prior result>)` "
                    "filter so the new query is exactly the subset the user "
                    "is thinking about, not a fresh keyword re-search that "
                    "may return a different set. Default: false; prefer "
                    "`include_sample_rows` when a handful of IDs is enough."
                ),
                "default": False,
            },
            "include_numeric_stats": {
                "type": "boolean",
                "description": (
                    "Return min/max/mean/sum aggregates for numeric "
                    "columns. Cheap and often sufficient for "
                    "distributional questions. Default: true."
                ),
                "default": True,
            },
        },
    },
}


# === Tool: suggest_example_questions ===
#
# Called by step1 when the user's message shows they don't know what to
# ask (e.g. "这是什么", "what can this do", "给个例子"). Pulls questions
# from a curated library in example_questions.py — keeps the prompt
# smaller and lets us maintain the question list without editing prompts.

SUGGEST_EXAMPLE_QUESTIONS_TOOL = {
    "name": "suggest_example_questions",
    "description": (
        "Call this when the user seems unsure what to ask — asks what the "
        "product is, what questions are possible, requests examples, "
        "or otherwise signals confusion about capabilities (\"这是什么\", "
        "\"你可以做什么\", \"give me an example\", \"what can I ask\"). "
        "Returns `count` curated, neutrally-phrased example questions "
        "randomly sampled from the library. Show them to the user as a "
        "bullet list, verbatim. Do NOT add subjective commentary like "
        "\"these are interesting\" / \"great questions\" / \"这些问题很有意思\" "
        "— just present them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "count": {
                "type": "integer",
                "description": "Number of example questions to return (1-8). Default 4.",
                "default": 4,
                "minimum": 1,
                "maximum": 8,
            }
        },
    },
}


def _make_tool_handlers(session_id: str, user_id: int | None) -> dict:
    """Build a per-request tool-handler map closure-bound to the caller's
    (session_id, user_id). The AI's tool input NEVER supplies those — it
    only sets flags — so it cannot read another user's data."""
    handlers: dict = {}

    async def lookup_prior_execution(include_code: bool = False,
                                     include_sample_rows: bool = False,
                                     include_all_rows: bool = False,
                                     include_numeric_stats: bool = True):
        if not session_id:
            return {"found": False, "reason": "no session"}
        # Use the read-only pool (same auth guard as AI-generated SQL).
        # Query filters on BOTH session_id AND user_id; NULL user_id is
        # only matched by NULL (anonymous with anonymous). IS NOT DISTINCT
        # FROM gives us the right NULL semantics.
        from db_pool import execute_query
        sql = (
            "SELECT code, code_type, description, result_obj "
            "FROM session_executions "
            "WHERE session_id = $session_id$" + session_id + "$session_id$ "
            "AND user_id IS NOT DISTINCT FROM "
            + ("NULL" if user_id is None else str(int(user_id))) + " "
            "ORDER BY executed_at DESC LIMIT 1"
        )
        # NOTE: asyncpg doesn't give us DB-role-switching inside a pool
        # configured read-only, but session_executions should be readable
        # by the read-only role. Fall back to a direct sync psycopg2 read
        # if the RO pool isn't granted — simpler.
        try:
            from db_pool import get_sync_conn
            import psycopg2.extras as _extras
            conn = get_sync_conn()
            try:
                with conn.cursor(cursor_factory=_extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT code, code_type, description, result_obj
                        FROM session_executions
                        WHERE session_id = %s
                          AND user_id IS NOT DISTINCT FROM %s
                        ORDER BY executed_at DESC
                        LIMIT 1
                        """,
                        (session_id, user_id),
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
        except Exception as e:
            return {"found": False, "error": f"lookup failed: {e}"}

        if not row:
            return {"found": False}

        result = row["result_obj"] or {}
        kind = result.get("kind", "unknown")
        out = {
            "found": True,
            "code_type": row["code_type"],
            "description": row["description"] or "",
            "kind": kind,
        }
        if kind == "sql":
            out["row_count"] = result.get("row_count")
            out["columns"] = result.get("columns") or []
        elif kind == "python_structured":
            out["summary"] = result.get("summary") or {}
        elif kind == "python_raw":
            # python_raw doesn't have structured aggregates; the user
            # probably wants to see the text output.
            out["stdout"] = (result.get("stdout") or "")[:4000]

        if include_code:
            out["code"] = row["code"]
        if include_sample_rows and kind == "sql":
            out["sample_head"] = result.get("sample_head") or []
            out["sample_tail"] = result.get("sample_tail") or []
        if include_all_rows and kind == "sql":
            # Cap at 5000 rows to keep the tool-result payload manageable
            # (it counts against model context). For bigger sets, the AI
            # should narrow first. Also include `columns` so the AI knows
            # which positional index is the identifier column.
            rows = result.get("all_rows") or []
            out["all_rows"] = rows[:5000]
            out["all_rows_truncated"] = len(rows) > 5000
            out["columns"] = result.get("columns") or []
        if include_numeric_stats and kind == "sql":
            out["numeric_stats"] = result.get("numeric_stats") or {}
            out["categorical_stats"] = result.get("categorical_stats") or {}

        return out

    handlers["lookup_prior_execution"] = lookup_prior_execution

    async def suggest_example_questions(count: int = 4):
        """Pull curated examples from the library, sampled fresh per call."""
        try:
            import example_questions
            return {"questions": example_questions.sample(int(count))}
        except Exception as e:
            return {"error": f"library unavailable: {type(e).__name__}: {e}"}

    handlers["suggest_example_questions"] = suggest_example_questions

    return handlers


TOOLS = [LOOKUP_PRIOR_EXECUTION_TOOL, SUGGEST_EXAMPLE_QUESTIONS_TOOL]
# Hard cap on tool-use iterations per AI call. A runaway loop shouldn't
# hammer the API — if the AI can't produce a final answer after this many
# tool calls, something is wrong with the prompt or the tool surface.
_MAX_TOOL_ITERATIONS = 5


# === Prompt: Turn Classifier ===

CLASSIFY_PROMPT = """You are a conversation state classifier for a data-analysis chat.

The assistant operates in two phases:
  - UNDERSTAND: restate the user's question and ask for confirmation of scope/filters.
  - EXECUTE: run the database query and show results.

Look at the conversation history. The last assistant message may be either:
  (A) an understanding that is waiting for user confirmation, or
  (B) a final results report (already executed), or
  (C) something else (greeting, clarification, error).

Given the user's LATEST message, classify into exactly one of:

  - "execute": the last assistant message was type (A) AND the user is agreeing to proceed as-is
               (e.g., "yes", "对", "go ahead", "that's right, run it", "ok 查吧").
  - "understand": either the last assistant message was NOT a pending understanding,
                  OR the user is asking a new question, refining, redirecting, or rejecting.
                  When in doubt, choose "understand" — it is always safe (we just re-confirm).

Output ONLY one word: execute OR understand. No punctuation, no explanation.
"""




def extract_sql(text: str) -> str | None:
    match = re.search(r'<sql>(.*?)</sql>', text, re.DOTALL)
    return match.group(1).strip() if match else None


def extract_python(text: str) -> str | None:
    match = re.search(r'<python>(.*?)</python>', text, re.DOTALL)
    return match.group(1).strip() if match else None


def _system_param(system, cache: bool):
    """Normalize the `system` argument for the Anthropic API.

    - Pre-structured list of blocks (the 4 step prompts — already carry
      `cache_control` per block): pass through unchanged.
    - Raw string + cache=True: wrap in a single ephemeral-cached text block.
    - Raw string + cache=False (e.g. the classifier, which is below the
      per-model cache minimum so caching wouldn't help): pass through as-is.
    """
    if isinstance(system, list):
        return system
    if isinstance(system, str) and cache:
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    return system


def _content_to_dict(block) -> dict:
    """Anthropic SDK content blocks → JSON-serializable dict that is
    VALID AS A REQUEST PAYLOAD.

    ``model_dump()`` is tempting but unsafe: it emits response-only fields
    (notably ``parsed_output`` on text blocks from newer SDK versions),
    which the /v1/messages endpoint rejects with 400 when they appear in
    the ``messages`` array of a follow-up request. That breaks the entire
    tool-use loop — the first call succeeds, the second fails with
    ``content.0.text.parsed_output: Extra inputs are not permitted``.

    Fix: rebuild the dict explicitly per block type, only passing
    API-accepted request fields.
    """
    t = getattr(block, "type", None)
    if t == "text":
        out = {"type": "text", "text": getattr(block, "text", "")}
        # Citations are part of the request schema; forward if present.
        cites = getattr(block, "citations", None)
        if cites:
            out["citations"] = [
                c.model_dump() if hasattr(c, "model_dump") else dict(c)
                for c in cites
            ]
        return out
    if t == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", None),
            "name": getattr(block, "name", None),
            "input": getattr(block, "input", {}) or {},
        }
    if t == "tool_result":
        return {
            "type": "tool_result",
            "tool_use_id": getattr(block, "tool_use_id", None),
            "content": getattr(block, "content", ""),
        }
    # Unknown block type — fall back to model_dump with None exclusion so
    # we at least avoid the most common response-only surface.
    if hasattr(block, "model_dump"):
        return block.model_dump(exclude_none=True)
    return dict(block)


async def _execute_tool_calls(tool_uses, tool_handlers) -> list[dict]:
    """Execute each tool_use block's handler and build tool_result blocks
    for the follow-up user message. Each call is written to chat.jsonl as
    a `tool_call` event so the audit trail shows whether the AI used the
    tool, what it asked for, and a compact view of what came back."""
    import datetime as _dt

    results = []
    for tu in tool_uses:
        name = tu.name
        input_obj = tu.input or {}
        handler = tool_handlers.get(name) if tool_handlers else None
        log_entry = {
            "ts": _dt.datetime.utcnow().isoformat(),
            "event": "tool_call",
            "tool": name,
            "input": input_obj,
        }

        if handler is None:
            content_str = json.dumps({"error": f"no handler for tool {name!r}"})
            log_entry["status"] = "no_handler"
        else:
            try:
                result = await handler(**input_obj)
                content_str = json.dumps(result, ensure_ascii=False, default=str)
                # Compact result view for the log — don't dump full sample
                # rows / code into every line. Just shape + key flags.
                preview: dict = {}
                if isinstance(result, dict):
                    preview["found"] = result.get("found")
                    if "error" in result:
                        preview["error"] = result["error"]
                    for k in ("kind", "code_type", "row_count"):
                        if k in result:
                            preview[k] = result[k]
                    for k in ("code", "sample_head", "sample_tail",
                              "numeric_stats", "categorical_stats"):
                        if k in result:
                            preview[k + "_included"] = True
                log_entry["status"] = "ok"
                log_entry["result_preview"] = preview
            except Exception as e:
                content_str = json.dumps({"error": f"{type(e).__name__}: {e}"})
                log_entry["status"] = "error"
                log_entry["error"] = f"{type(e).__name__}: {e}"

        try:
            _tool_logger.info(json.dumps(log_entry, ensure_ascii=False))
        except Exception:
            pass  # never let audit logging break a live AI turn

        results.append({
            "type": "tool_result",
            "tool_use_id": tu.id,
            "content": content_str,
        })
    return results


async def ai_stream(system, messages, tools=None, tool_handlers=None):
    """Stream AI response. Yields (event_type, data) tuples.

    If ``tools`` is provided, runs a tool_use loop: when the model issues
    tool calls, this function executes them via ``tool_handlers`` and
    re-issues the request with the tool_result appended, up to
    ``_MAX_TOOL_ITERATIONS`` iterations. Streaming text is yielded
    incrementally; tool_use rounds are silent to the caller.
    """
    convo = list(messages)
    iterations = 0
    while True:
        iterations += 1
        stream_kwargs = dict(
            model=AI_MODEL, max_tokens=4096,
            system=_system_param(system, cache=True),
            messages=convo,
        )
        if tools:
            stream_kwargs["tools"] = tools
        async with client.messages.stream(**stream_kwargs) as s:
            async for text in s.text_stream:
                yield ("text", text)
            final = await s.get_final_message()

        tool_uses = [b for b in final.content if getattr(b, "type", None) == "tool_use"]
        if tool_uses and tool_handlers and iterations < _MAX_TOOL_ITERATIONS:
            tool_results = await _execute_tool_calls(tool_uses, tool_handlers)
            # Echo the assistant's full message (text + tool_use blocks)
            # back into the convo, then add the tool_result user message.
            convo = convo + [
                {"role": "assistant", "content": [_content_to_dict(b) for b in final.content]},
                {"role": "user", "content": tool_results},
            ]
            continue

        full_text = "".join(
            b.text for b in final.content if getattr(b, "type", None) == "text"
        )
        yield ("full_response", full_text)
        return


async def ai_complete(system, messages, model=None, cache_system=True,
                      tools=None, tool_handlers=None):
    """Non-streaming AI call. Returns full text.

    Supports the same tool_use loop as ``ai_stream``. ``cache_system=False``
    disables prompt caching — use for small system prompts (e.g. the
    classifier) that sit below the per-model minimum.
    """
    convo = list(messages)
    iterations = 0
    while True:
        iterations += 1
        call_kwargs = dict(
            model=model or AI_MODEL, max_tokens=4096,
            system=_system_param(system, cache=cache_system),
            messages=convo,
        )
        if tools:
            call_kwargs["tools"] = tools
        resp = await client.messages.create(**call_kwargs)

        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if tool_uses and tool_handlers and iterations < _MAX_TOOL_ITERATIONS:
            tool_results = await _execute_tool_calls(tool_uses, tool_handlers)
            convo = convo + [
                {"role": "assistant", "content": [_content_to_dict(b) for b in resp.content]},
                {"role": "user", "content": tool_results},
            ]
            continue

        # No tool_use (or limit hit) — return final text.
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )


# Minimal set of unambiguous confirmation tokens. Kept small: any phrase
# that could plausibly also be a refinement, question, or rejection is
# deliberately absent so the AI classifier still sees those cases. Extend
# only in response to observed confirmation patterns that the current set
# misses (grep chat.jsonl for classify_turn source="ai" AND action="execute"
# to find misses).
_CONFIRM_NORMALIZED = frozenset({
    # English
    "ok", "okay", "yes", "y", "yep", "yeah", "sure", "fine", "confirmed",
    "go", "run", "run it", "do it", "execute", "proceed", "go ahead",
    "yes please",
    # Chinese
    "对", "是", "是的", "好", "好的", "行", "可以", "确认", "执行",
    "跑", "跑吧", "开始", "开始吧", "就这样", "就这样跑",
    "就这么跑", "就按这个跑", "就按这个",
})


def _short_circuit_classify(messages: list[dict]) -> str | None:
    """Fast-path for unambiguous user confirmations, avoiding the Haiku call.

    Returns "execute" only when BOTH:
      (a) the user's latest message is short and matches an exact confirmation
          token (case-insensitive, trailing punctuation stripped), AND
      (b) the previous assistant message looks like a pending understanding
          (ends with a '?' / '？' somewhere in its final ~120 chars — step1
          always asks a confirmation question; step4+5 final results never do).

    Returns None to fall through to the AI classifier for everything else —
    including confirmations with modifiers ("ok 但是..."), single-word
    ambiguous replies ("再试一次"), or messages after a completed analysis
    where "ok" means "thanks, noted" rather than "run it again"."""
    if len(messages) < 2:
        return None
    user_msg = messages[-1]
    if user_msg.get("role") != "user":
        return None
    user_text = (user_msg.get("content") or "").strip()
    if not user_text or len(user_text) > 20:
        return None
    normalized = user_text.lower().rstrip(".。!！?？,，;；:： ")
    if normalized not in _CONFIRM_NORMALIZED:
        return None
    prev_assistant = ""
    for m in reversed(messages[:-1]):
        if m.get("role") == "assistant":
            prev_assistant = (m.get("content") or "").strip()
            break
    if not prev_assistant:
        return None
    tail = prev_assistant[-120:]
    if "?" not in tail and "？" not in tail:
        return None
    return "execute"


async def classify_turn(messages: list[dict]) -> tuple[str, str]:
    """Returns (action, source):
      - action: "execute" if the user's last turn is a confirmation of a
        pending understanding, else "understand".
      - source: "heuristic" (short-circuited, no AI call) or "ai"
        (classifier model was called) or "default" (trivial base case, also
        no AI call).

    Defaults to ("understand", "ai") if the AI call raises."""
    if len(messages) < 2:
        return "understand", "default"
    # Need at least: prior assistant msg + current user msg.
    if not any(m["role"] == "assistant" for m in messages[:-1]):
        return "understand", "default"
    fast = _short_circuit_classify(messages)
    if fast is not None:
        return fast, "heuristic"
    try:
        resp = await ai_complete(
            CLASSIFY_PROMPT,
            messages,
            model=AI_CLASSIFIER_MODEL,
            cache_system=False,  # prompt too small to benefit from caching
        )
        label = resp.strip().lower().split()[0] if resp.strip() else "understand"
        return ("execute" if label.startswith("execute") else "understand"), "ai"
    except Exception:
        return "understand", "ai"


# === Live data-cutoff helper ===
#
# The indexer runs behind the chain tip (often weeks-to-months). Without
# telling the AI the current cutoff, step1 silently proposes time windows
# that extend past the indexed range and the user sees "0 rows" with no
# explanation. We query max(block_timestamp) once per call path but cache
# it for 60s so burst traffic doesn't hammer PG.

_CUTOFF_CACHE: dict = {"ts": 0.0, "value": None}
_CUTOFF_TTL_SEC = 60.0
_CUTOFF_LOCK = asyncio.Lock()


async def _get_data_cutoff() -> str:
    """Return max(order_fills.block_timestamp) as an ISO-8601 string.
    Falls back to the cached value (or 'unknown') if the DB lookup fails."""
    now = time.monotonic()
    if _CUTOFF_CACHE["value"] and now - _CUTOFF_CACHE["ts"] < _CUTOFF_TTL_SEC:
        return _CUTOFF_CACHE["value"]
    async with _CUTOFF_LOCK:
        now = time.monotonic()
        if _CUTOFF_CACHE["value"] and now - _CUTOFF_CACHE["ts"] < _CUTOFF_TTL_SEC:
            return _CUTOFF_CACHE["value"]
        try:
            # Imported lazily to avoid a circular import at module load.
            from db_pool import execute_query
            _, rows = await execute_query("SELECT max(block_timestamp) FROM order_fills")
            if rows and rows[0] and rows[0][0] is not None:
                value = rows[0][0].isoformat()
            else:
                value = "unknown"
            _CUTOFF_CACHE["value"] = value
            _CUTOFF_CACHE["ts"] = now
            return value
        except Exception:
            return _CUTOFF_CACHE["value"] or "unknown"


def _cutoff_block(cutoff: str) -> dict:
    """Uncached system block containing the live data cutoff. Appended after
    the two cached blocks so the cache stays warm across calls. Also stamps
    the current server UTC time so the AI can resolve relative windows
    ('last 3 months', '最近') without guessing from its training cutoff."""
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return {
        "type": "text",
        "text": (
            "## Real-time reference points (refreshed per-call, not cached)\n\n"
            f"- **Current server time (UTC):** `{now_utc}` — use this as "
            "\"now\" when the user says \"last 30 days\" / \"最近\" / "
            "\"recent\" / any relative window. Do NOT fall back to your "
            "training-data cutoff.\n"
            f"- **Indexed data cutoff:** `{cutoff}` UTC (max "
            "`block_timestamp` in `order_fills`).\n"
            f"- **Indexer lag:** the gap between \"now\" and the cutoff "
            "above is how far on-chain history is behind real time.\n\n"
            "### Decision procedure — follow it literally, do not pattern-match\n\n"
            f"Step A. Identify the LATEST date the user's query would filter to "
            "(the END of their proposed time window, or 'now' if they said "
            "'recent'/'latest'/'最近').\n\n"
            f"Step B. Compare that date to the cutoff `{cutoff}`:\n"
            f"  - If user_end_date <= {cutoff}: the range is FULLY COVERED. "
            "Do NOT mention the cutoff at all. Do NOT add any ⚠ warning. "
            "Do NOT say anything about data coverage. Proceed silently.\n"
            f"  - If user_end_date > {cutoff}: the range extends past our data. "
            "Warn the user explicitly (see Step C).\n\n"
            "Step C. ONLY when Step B says 'extends past', emit one warning line "
            "and offer an alternative:\n"
            f"  '⚠ 数据目前只覆盖到 {cutoff} UTC，你问的时间段超出这个范围。"
            f"可以改为截至 {cutoff}，或等数据更新。' (Chinese)\n"
            f"  '⚠ Data is currently indexed through {cutoff} UTC — your "
            f"requested range extends beyond that. Narrow the window to "
            f"end at {cutoff}, or wait for the indexer to catch up.' (English)\n\n"
            "### Concrete examples (the cutoff is the moving part; dates below "
            "are illustrative)\n\n"
            f"User asks 'Q1 2025' (= Jan-Mar 2025). Cutoff is in late 2025. "
            "Q1 2025 ends BEFORE cutoff → FULLY COVERED → emit NO warning, "
            "NO cutoff mention.\n"
            f"User asks 'last 6 months' today in 2026. Latest = today, cutoff is "
            "several months back → range extends past → warn.\n"
            f"User asks '2024 election period' (Oct-Nov 2024). Ends before cutoff "
            "→ FULLY COVERED → no mention.\n"
            f"User asks 'since Jan 2025'. End = today 2026, cutoff in late 2025 "
            "→ extends past → warn.\n\n"
            "### Hard rule\n\n"
            "Do NOT mention the cutoff unless Step B's comparison says the range "
            "extends past it. A false-positive warning confuses the user more than "
            "a missing one."
        ),
    }


async def step1_understand(messages, session_id: str = "",
                           user_id: int | None = None):
    """Stream the understanding/confirmation response. Injects the live data
    cutoff as an uncached suffix block so the AI can warn about time ranges
    extending past the indexed data.

    Carries the lookup_prior_execution tool — step1 can call it when the
    user's question references specific data from a prior turn (e.g.
    \"that market we looked at earlier — how much did it trade?\")."""
    cutoff = await _get_data_cutoff()
    prompt = [*UNDERSTAND_PROMPT, _cutoff_block(cutoff)]
    handlers = _make_tool_handlers(session_id, user_id)
    async for ev in ai_stream(prompt, messages, tools=TOOLS, tool_handlers=handlers):
        yield ev


async def step3_generate(
    messages,
    confirmed_intent,
    prior_errors: list[str] | None = None,
    session_id: str = "",
    user_id: int | None = None,
) -> str:
    """Generate a natural-language description of the planned query AND the
    code that runs it, in one response. Non-streaming.

    The raw response contains:
      <bullet description>\\n\\n<sql>...</sql>  (or <python>...</python>)

    The caller splits the description from the code using extract_sql /
    extract_python + the tag position. See `_split_description_and_code` in
    process.py.

    ``prior_errors`` is the ACCUMULATED list of errors from all prior
    retry attempts in this turn, oldest first. Passing the full history
    (rather than just the most recent error) prevents the common failure
    where the AI fixes the latest error but regresses on an earlier one —
    e.g. attempt 1 fails on "SQL comments not allowed", attempt 2 fixes
    the comments but times out, attempt 3 sees only "TimeoutError" and
    silently re-introduces the comments while fixing the timeout.
    """
    hint = ""
    if prior_errors:
        if len(prior_errors) == 1:
            hint = (
                f"\n\nThe previous attempt FAILED with this error:\n"
                f"{prior_errors[0]}\n"
                f"Fix the code so this error does not recur."
            )
        else:
            numbered = "\n".join(
                f"  {i + 1}. {err}" for i, err in enumerate(prior_errors)
            )
            hint = (
                f"\n\nPrevious {len(prior_errors)} attempts ALL FAILED with "
                f"these errors (oldest first):\n{numbered}\n\n"
                f"Fix the code so ALL of these errors are addressed "
                f"simultaneously. A fix that resolves only the most recent "
                f"error will regress on the earlier ones (common trap: "
                f"removing `--` comments after the first failure, then "
                f"silently re-adding them on the third attempt while fixing "
                f"a different issue)."
            )
        hint += (
            "\n\nCommon causes to check: wrong column/table name, unindexed "
            "filter on a huge table, invalid JSONB access, forbidden "
            "keyword, ambiguous column reference from an unqualified JOIN. "
            "Output the same format (description bullets, blank line, code "
            "tag)."
        )
    gen_messages = messages + [
        {"role": "user", "content": (
            f"The user confirmed this intent: {confirmed_intent}\n\n"
            f"Produce the description (3-5 bullets) and the code (inside one "
            f"<sql> or <python> tag), in that order."
            f"{hint}"
        )}
    ]
    handlers = _make_tool_handlers(session_id, user_id)
    return await ai_complete(
        GENERATE_PROMPT, gen_messages,
        tools=TOOLS, tool_handlers=handlers,
    )


def step5_interpret(result_json, user_lang_messages,
                    session_id: str = "", user_id: int | None = None,
                    query_description: str = ""):
    """Stream the result interpretation (key findings + supporting numbers).

    ``query_description`` is the natural-language description of what the
    query does that was produced alongside the code in step3. It's the ONLY
    prior-turn context the interpreter gets by default — the code itself
    is NOT passed here. If the interpreter needs the code (for a
    conceptual question about a field, or an intent-vs-reality check), it
    calls ``lookup_prior_execution(include_code=True)`` — the execution
    was already persisted to `session_executions` before step5 runs.

    Carrying the tool also lets step5 handle follow-ups that refer to
    EARLIER executions in the session (not the one we just ran)."""
    user_langs = json.dumps([m for m in user_lang_messages[-4:] if m['role'] == 'user'], ensure_ascii=False)
    desc_block = (
        f"Query description (internal — user does NOT see this, it is for "
        f"your context only):\n{query_description}\n\n"
        if query_description else ""
    )
    handlers = _make_tool_handlers(session_id, user_id)
    return ai_stream(
        INTERPRET_PROMPT,
        [
            {"role": "user", "content": (
                f"{desc_block}"
                f"Result JSON:\n{result_json}\n\n"
                f"User language context:\n{user_langs}"
            )}
        ],
        tools=TOOLS, tool_handlers=handlers,
    )
