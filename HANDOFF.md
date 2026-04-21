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

## What's live as of 2026-04-21

Four daemons + PG; see `OPERATIONS.md` for details:

1. `unified_indexer.py` — incremental on-chain scan, ~1500 blocks/min
   when RPC is free, advancing toward chain tip
2. `rollup.py --loop 60` — 5-table aggregate daemon, ≤ 60 s behind indexer
3. `backfill_splits_merges.py --batch-size 2500` — **one-shot**, running
   since 2026-04-20 evening. Currently ~81% done, ETA ~12-15 h more
4. `uvicorn app:app` — webapp on `:8080` with SSL

Current data cutoff: `max(order_fills.block_timestamp)` is **2026-01-06**
(indexer is ~100 days behind chain tip; catching up at ~1 chain-day per
wall-clock day). Step1's prompt has explicit cutoff-awareness logic — it
warns users when their window extends past the indexed range.

---

## What we're in the middle of

**PnL infrastructure rebuild.** The old `net_pnl_usd` column in
`wallet_volume_rollup` was computed as `sell − buy + redemption − fees`,
which undercounts cost for every market maker who uses
`PositionSplit` / `PositionsMerge` to mint inventory. Those events
weren't indexed, so split cost = 0 in the formula → top wallets showed
fake +$4 B "PnL". That column was **dropped** until we can compute it
correctly.

**Current backfill** is scanning `CONDITIONAL_TOKENS` on Polygon for
`PositionSplit` + `PositionsMerge` events from block 44 M to chain tip
(~42 M blocks). Watermark: `indexer_state.splits_merges_synced_block`.
Running in parallel with unified_indexer; they compete for QuikNode
RPC slots, which is why batch was reduced from 10 000 → 2 500 (see
`splits_merges_backfill.log`).

**Next steps after catch-up** (queued as todos):

1. Merge the 2 topic filters into `unified_indexer.py::_process_batch`
   so incremental sync does split/merge alongside fills/matches. Drop
   the separate `splits_merges_synced_block` watermark.
2. Add a "G" stage to `rollup.py` that sums `position_splits.amount`
   and `position_merges.amount` per wallet into new columns
   (`total_split_usd`, `total_merge_usd`).
3. Restore `net_pnl_usd` with the correct formula:
   `sell + redemption + merge − buy − split − fees`.
4. Re-document in `DB_SCHEMA` and lift the "PnL is NOT computable"
   guard we currently have in the prompt.

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
