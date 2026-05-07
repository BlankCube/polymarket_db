# Session Handoff

Paste-in prompt for a new Claude session picking up this project. Last
updated **2026-04-21**.

---

## Start-of-session checklist (read in order)

1. **`PRODUCT.md`** — the vision + the Improvement Methodology (how we
   decide when a log issue is worth a fix: classify → 2-week horizon →
   N≥2 check → impact weighing → structured proposal).
2. **`OPERATIONS.md`** — control / runbook: what daemons run, how to
   restart them, log locations, the golden rules (track PIDs via `ps`
   not wrapper, truncate in place, bump `v=` on static asset changes).
3. **`database/ROLLUPS.md`** — rollup tables A–E schema + update rules.
4. **`feedback/deferred_improvements.md`** — the real backlog. 5
   entries queued; each says when to revisit. **Grep this before
   proposing any prompt/code fix** — half the "new bugs" are usually
   already queued.
5. **`feedback/logs/chat.archive.jsonl`** — cold archive of analyzed
   sessions. Only read if you need historical context; default is to
   skip and look at active `chat.jsonl`.
6. **`.claude/agents/improvement-analyst.md`** — subagent for log
   review. Use it instead of manually greping chat.jsonl.

---

## One-paragraph project summary

**Polymarket Explorer** is a natural-language chat over a PostgreSQL DB
indexing all on-chain Polymarket trading on Polygon. Users ask
questions in Chinese or English ("历史累计成交额 top 20 的地址..."),
Claude Sonnet translates to SQL or Python, runs it read-only against
the indexed DB, and returns a data-grounded interpretation. Auth-gated
webapp on `https://<host>:8080` (self-signed cert for now).

Architecture: 5-step AI pipeline per user turn — classify → step1
understand → step3 generate (code+description) → execute → step5
interpret. Steps 3 and 5 use Claude Sonnet 4.5; classify uses Haiku
4.5. Prompts share a `DOMAIN_KNOWLEDGE` + `DB_SCHEMA` cached prefix.
Tool-use loop gives step1/3/5 access to `lookup_prior_execution` (for
referring to previous turn's data) and `suggest_example_questions` (for
"what can this do?" discovery).

---

## What's live as of 2026-04-22

Three daemons + PG; see `OPERATIONS.md` for details:

1. `unified_indexer.py` — incremental on-chain scan of **8 event types**
   (CTF/Neg-Risk fills+matches, resolution, redemption, split, merge),
   advancing toward chain tip. Single watermark: `unified_last_block`.
2. `rollup.py --loop 60` — 5-table aggregate daemon, ≤ 60 s behind indexer
3. `uvicorn app:app` — webapp on `:8080` with SSL

**Splits/merges were folded into the unified indexer on 2026-04-22**
after the one-shot `backfill_splits_merges.py` caught up past
`unified_last_block`. The standalone backfill script + its
`splits_merges_synced_block` watermark are retired; don't re-run it.

Current data cutoff: `max(order_fills.block_timestamp)` is **2026-01-12**
(indexer is ~100 days behind chain tip; catching up at ~1 chain-day per
wall-clock day). Step1's prompt has explicit cutoff-awareness logic — it
warns users when their window extends past the indexed range.

---

## What we're in the middle of

**PnL infrastructure rebuild — complete 2026-04-22.** The old
`net_pnl_usd` column used `sell − buy + redemption − fees`, which
undercounts cost for every market maker who uses `PositionSplit` /
`PositionsMerge` to mint inventory (not indexed at the time), so top
wallets read as fake +$4 B. Fix in three steps, all now done:

1. ~~Merge split/merge into `unified_indexer` and drop separate
   watermark.~~ **Done.** `_process_batch` fetches 8 event types; the
   standalone `backfill_splits_merges.py` is retired.
2. ~~Add "G" stage to `rollup.py` aggregating `position_splits.amount`
   / `position_merges.amount` per wallet into new columns
   (`total_split_usd`, `split_count`, `total_merge_usd`,
   `merge_count`).~~ **Done.** G runs every rollup cycle alongside F.
3. ~~Restore `net_pnl_usd` with `sell + redemption + merge − buy −
   split − fees`.~~ **Done.** Backfilled for the entire history via
   `rollup.py --backfill-g`. Indexed `DESC` for "top/bottom PnL"
   queries. DOMAIN_KNOWLEDGE updated, "PnL not computable" guard
   lifted. Caveat kept: realised PnL only (no M2M on open positions,
   no gas).

**`backtest_trades` matview retired — 2026-05-07.** It was a project-
launch fossil: an over-specialised materialised view (`order_fills`
JOIN `markets` JOIN `token_market_map` filtered to BUY trades with
price ∈ [0.895, 1.005] in markets resolved AFTER the deadline) created
for the original "post-expiry high-price arbitrage" research question
and never generalised. Two of the four canonical example questions
were routed at it, and AI generation kept hallucinating column names
(`price` / `trade_price`) and treating `token_won` as integer instead
of BOOLEAN — every retry exhausted on those two questions. Fix:
dropped the matview (158 MB recovered), removed all references from
`chat/ai.py`, `chat/example_questions.py`, `PRODUCT.md`, and
`OPERATIONS.md`, and extended the `order_fills` schema doc in the
prompt to spell out how to derive the win flag (`m.resolution_payout`)
and which composite index to lean on (`(condition_id, block_timestamp)`).
The two strategy questions in `example_questions.py` stay — they're
now answered via raw `order_fills` JOIN, slower but consistent and
not pinned to one hard-coded price band.

---

## Prompt + code design rules (learned the hard way)

- **Never let AI name an identifier to the user.** `DOMAIN_KNOWLEDGE`
  rule 3 forbids any snake_case / camelCase token in user-facing text.
  Step1 plan is WHAT, not HOW; no "我会查 X 表的 Y 字段" sentences.
- **step3 rule 0**: when the user references a prior-turn row ("第 4
  个"), AI must call `lookup_prior_execution(include_sample_rows=true)`
  and query by the row's exact identifier, NOT re-search by keyword.
  Keyword re-search returns a different set and "the 4th row" of the
  new result silently points to a different market.
- **`include_all_rows` tool option** (added 2026-04-21): for follow-ups
  that should scope to the prior result set ("那 100 个里哪些 resolved
  了"), AI pulls up to 5000 PKs from prior execution + writes
  `WHERE pk IN (…)`. Do NOT write a fresh keyword filter.
- **Mandatory step5 opener**: `在 <具体 scope>（n=<N>）中，<headline
  finding>`. No "significant difference"; every claim needs a
  traceable number from `numeric_stats` / `summary` / `row_count`.
- **Coverage footer** (`— 覆盖: ...`): names the **denominator**
  (search scope), NOT the filtered result count. `n=` already
  conveys the numerator. Don't repeat.
- **`_content_to_dict` in ai.py**: never `block.model_dump()` blindly
  — emits response-only fields (`parsed_output`) that the Anthropic
  API rejects in follow-up request bodies. Rebuild per-type with a
  whitelist.
- **Anonymous users don't exist.** The UI gates every call behind
  login. Any `user_id IS NULL` handling is dead defensive code; don't
  add more.

---

## Recent session-log findings (already addressed, don't re-flag)

See `deferred_improvements.md` for the full archive. Short list that
the improvement-analyst will confirm as "already addressed / queued":

- SQL comments on retry (2026-04-18) — fixed by `sql_safety._strip_comments`
- %s placeholders on retry (2026-04-18) — documented in `_GENERATE_BODY`
- `wallet_volume_rollup` table-name leak (2026-04-20) — fixed by rule 3
- heuristic classifier misfire on "ok" after info-seeking `?` (queued #3)
- step3 silent scope pivot (queued #4)
- CSV has only 1 summary row on aggregate queries (queued #5)
- Prompt-injection attempt (2026-04-21) — AI defended correctly by
  routing to "what can this do" + example_questions tool

---

## How to talk to me (the user's preferences)

- Chinese primary, English allowed when referring to code / API
- Terse. Give me the answer; don't explain my own system back to me
- When I surface a bug from a log, **use the improvement-analyst
  subagent** — you'll catch already-queued issues I don't want
  re-proposed
- When you commit, use HEREDOC for the message per standard format.
  Do NOT push without me asking
- Numbers first, prose second. If you're unsure about a number, say
  so explicitly — never fabricate

---

## Open questions you might hit early

- **Real HTTPS + domain** — still on self-signed `:8080`. `MEMORY.md`
  has a reminder to replace with Let's Encrypt (Caddy/nginx) before
  exposing to external users
- **`polymarket_ro` password rotation** — still on dev placeholder per
  `MEMORY.md`. Rotate before production
- **CSV for non-SQL results** — Python / structured-output executions
  don't currently emit CSV downloads. Low priority until someone asks
