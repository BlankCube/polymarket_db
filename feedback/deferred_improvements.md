# Deferred Improvements Queue

Change proposals that were identified but deliberately **not** implemented yet.
Each entry records the evidence, the reasoning for deferral, and the trigger
that should cause us to revisit it.

Format per entry:
- **What**: the proposed change
- **Evidence so far**: what we've actually observed (N, dates)
- **Why deferred**: reason we're not doing it yet
- **Revisit when**: concrete trigger to reopen

---

## 1. Split the `÷ 1e6` rule in `GENERATE_PROMPT`

**What**
In `chat/ai.py::GENERATE_PROMPT`, replace the current blanket rule

> Amounts: divide by 1e6 for USD values.

with a field-level table:

| Column | Scaling |
|---|---|
| `order_fills.usdc_amount` / `token_amount` / `fee` | raw, divide by 1e6 |
| `order_matches.usdc_amount` / `token_amount` | raw, divide by 1e6 |
| `redemptions.payout` | raw, divide by 1e6 |
| `markets.volume` / `markets.liquidity` | **already USD** — do NOT divide |

**Evidence so far (as of 2026-04-17)**
- 1 session: 2026-04-17T09:47 — "交易得最多的政治市场是哪个？"
  - step3 SQL used `m.volume / 1e6`, producing Trump-2024 volume ≈ 15M USD in
    output, when the real figure is multi-billion.
  - step5 presented this as fact (violating "every claim needs a number" trust).
- User didn't give explicit 👎 for this turn (no rating recorded).

**Why deferred**
- N=1, no downvote → methodology says we should wait for a second occurrence
  before adding a prompt rule (avoids one-off special-casing).
- Prompt budget is scarce; a field-level scaling table adds tokens to every
  code-gen call.
- Fix is trivial (~5 min) once we commit.

**Revisit when**
- Any of:
  - A second session produces clearly wrong numbers on `markets.volume` or
    `markets.liquidity` (scan chat.jsonl for `volume / 1e6` or `volume/1e6`).
  - A user submits 👎 citing wrong totals on market-level aggregates.
  - We're about to give the URL to external users — pre-launch scrub.

---

## 2. `wallet_volume_rollup` materialized view

**What**
Add a nightly-refreshed materialized view that pre-aggregates every wallet's
lifetime trading footprint, so queries that want to classify "big vs small
wallets" become O(view-size) instead of O(order_fills).

```sql
CREATE MATERIALIZED VIEW wallet_volume_rollup AS
SELECT
    maker,
    SUM(usdc_amount) / 1e6       AS total_volume_usd,
    COUNT(*)                     AS trade_count,
    MIN(block_timestamp)         AS first_active,
    MAX(block_timestamp)         AS last_active,
    COUNT(DISTINCT condition_id) AS markets_touched
FROM order_fills
GROUP BY maker;
CREATE INDEX ON wallet_volume_rollup (total_volume_usd DESC);
CREATE INDEX ON wallet_volume_rollup (maker);
```

Refresh via `REFRESH MATERIALIZED VIEW CONCURRENTLY wallet_volume_rollup`
from the indexer's nightly hook (or a dedicated cron), after indexer has
committed its latest batch.

Then `DOMAIN_KNOWLEDGE` can replace the 2-pass wallet pattern with:
"To classify wallets by total volume, query `wallet_volume_rollup` directly."

**Evidence so far (as of 2026-04-18)**
- 1 user, 2 attempts at the same question (pre- and post-EC2-restart):
  2026-04-18T01:12 and 2026-04-18T02:53 —
  "大钱包和小钱包在赌btc涨跌上有显著的不同偏好吗"
  - Both attempts timed out at the 300s statement_timeout.
  - Generated Python tried to classify wallets by historical volume inline;
    even with a `block_timestamp >= '2024-01-01'` prefilter, the
    `GROUP BY maker` step was too expensive.
- The prompt-level fix (2-pass pattern + HARD RULE in ai.py, landed
  2026-04-18) teaches the AI to shortlist first, but each shortlist pass
  still scans a 60-day slice of order_fills per user question — that's the
  wasted work this materialized view would eliminate.

**Why deferred**
- Still N=1 user, no 👎 recorded. Methodology says wait for a second
  occurrence of the *mechanism* before doing schema work.
- Schema + refresh-cron is a real commitment: ~5-10 min refresh time on
  200M rows, ~2-5 GB storage, plus monitoring that the refresh actually ran.
- Prompt fix alone should cut the visible failure mode; if users still hit
  timeouts AFTER the prompt fix, the materialized view becomes the obvious
  next step.

**Revisit when**
- Any of:
  - A second distinct user (or same user, different question) times out on
    a wallet-classification-shaped query EVEN WITH the 2-pass prompt rule
    active.
  - We decide to open the URL to external users — big-wallet questions will
    be a common first thing people try, and 300s failures are a bad first
    impression.
  - Analytics on chat.jsonl show ≥ 3 sessions triggered the new
    "wallet classification by historical volume" HARD RULE message from
    step3.

---

## 3. Heuristic classifier misfires on "ok" after information-seeking "?"

**What**
In `chat/ai.py::_short_circuit_classify`, the fast-path triggers
`execute` when (a) user's message is a short confirmation token and
(b) the prior assistant message has a `?` in its last ~120 chars. But
the heuristic cannot distinguish two kinds of `?`:

- "Does this plan look right? Run as-is?" → user "ok" correctly means
  execute.
- "Which category / address / timeframe do you want?" → user "ok" is
  a vague non-answer; the correct routing is back to step1 for
  re-clarification, not step3.

The heuristic lacks context to tell them apart.

**Evidence so far (as of 2026-04-20)**
- 1 session: 2026-04-20T06:59 — step1 asked "请告诉我你想看哪个类别？",
  user replied "ok", heuristic returned
  `action=execute source=heuristic`, step3 silently pivoted from "top
  markets in a category" to "top categories overall".
  See `chat.jsonl` lines 83-91.

**Why deferred**
- N=1, no downvote.
- Fix candidates each have tradeoffs:
  - (a) Require step1 never to end an info-seeking question at the very
    end of its message when the goal is just plan-confirmation — keep
    the `?` only when confirming the plan. This reshapes step1 output
    style, which may have downstream effects.
  - (b) Scan the prior assistant message for plan-shaped keywords
    (`计划`, `plan`, `预计`, `estimated`) as a proxy for
    plan-confirmation `?`. Fragile.
  - (c) Remove the heuristic entirely; always call the AI classifier.
    Small latency/cost hit (Haiku call per turn). Safer but not free.
- The cost of this misfire is meaningful but rare so far.

**Revisit when**
- A second session shows the same misfire. Grep pattern:
  `chat.jsonl` where `classify_turn source=heuristic action=execute`
  is followed by a `step3_generate` whose description diverges from
  the prior `step1_understand` response.
- Or if we add a third classify outcome for genuinely-ambiguous
  follow-ups (at which point the heuristic surface naturally expands).

Bundle the fix with #4 (step3 scope-pivot) — they compound on each other.

---

## 4. step3 silently changes scope when intent is underspecified

**What**
When step3 receives a "confirmed" intent that still has an unresolved
variable (e.g. "某个 category 里 top 20" where `category` was never
picked), the AI currently picks SOME query — in the observed case it
pivoted to a completely different one (listing categories instead of
listing markets in a category). It should instead refuse / emit no
code / return an error, which would trigger the retry loop with
"intent underspecified" as the hint, and eventually bubble back up to
step1 for re-clarification.

**Evidence so far (as of 2026-04-20)**
- 1 session, same 06:59 session as #3: after heuristic misfire routed
  to execute, step3's `description` says "查询所有可用的市场类别以供选择"
  while the user's original question was "top 20 markets in a
  category." step3 changed the question.

**Why deferred**
- N=1, mechanism abstractly predictable (ambiguous intent → AI picks
  path of least resistance) but the concrete fix ("teach step3 to
  self-abort") is subtle and risks over-triggering (spurious refusal
  on slightly-ambiguous-but-answerable intents).
- Related to #3: if #3's heuristic fires wrongly, #4's robustness
  matters more. Fixing together is cleaner than piecemeal.

**Revisit when**
- A second session shows step3's `description` materially diverging
  from step1's prior `response` for the same user intent. Bundle with
  #3's revisit.

**Interim mitigation that was done in this turn (2026-04-20)**
- example_questions library guidelines #7 now require concrete values
  (no "某个 X" / "<placeholder>" style), so at least the CHIP path
  doesn't feed step3 an underspecified intent. AI-authored
  underspecified intents can still happen.

---

## 5. CSV download is unhelpful for summary-aggregate queries

**What**
When a question's natural answer is a small aggregate ("有多少地址 ≥
100 个市场" → 1-row `COUNT(*) + MIN/MAX/AVG/MEDIAN`), the Download CSV
button still appears but contains only the 1 summary row, which is
useless for a user who wanted the underlying wallet list.

Candidate fixes (pick ONE when we act — they're different philosophies):

1. **UI-side**: strengthen the "Result: 1 rows" label so it's obvious
   the CSV is a summary, not a dataset. Or hide the button entirely for
   `row_count <= 1` single-summary results (less generous but clearer).
2. **Prompt-side**: flip the default for count-style questions to
   "return detail rows + `COUNT(*) OVER ()` window column" so CSV has
   the wallet list AND the interpreter still sees the true total. Risk:
   over-fits to the CSV use case; the aggregate answer was exactly what
   the user literally asked (no drift from intent), and changing the
   default forces larger payloads and different interpreter shape for
   every count-style question.
3. **Feature**: emit two queries in step3 — one for the headline (what
   the AI wanted anyway), one for detail (for CSV). Bigger lift; needs
   result_obj to hold two row sets.

**Evidence so far (as of 2026-04-21)**
- 1 session: 2026-04-21T03:45 — "交易过 100 个以上不同市场的活跃地址
  有多少个？" → step3 generated `SELECT COUNT(*), MIN, MAX, AVG,
  PERCENTILE_CONT...` → CSV = 1 summary row (55003, 100, 168313,
  423.95, 173). User flagged it as unhelpful, said the aggregate
  answer itself was fine but the CSV wasn't.

**Why deferred**
- N=1. User themselves said "统计数据也挺好的，毕竟那其实就是用户问的
  原文要求"—the SQL is a legitimate response to the literal question;
  the CSV gap is a UX mismatch, not a query-quality bug.
- Each of the 3 fix paths has real trade-offs (see above). Wrong move
  would be to commit to one before we see more cases.
- Implemented once during 2026-04-21 session (flipped to detail-rows
  + `COUNT(*) OVER ()`), then reverted immediately on user review —
  the revert is what left evidence here.

**Revisit when**
- Any of:
  - A second session where a user actively wants the detail rows behind
    a count-style question but has no way to get them short of re-asking.
  - We expose the URL to external users — first-time visitors who click
    the Download CSV button and get 1 summary row will bounce. Add the
    label clarity + a "Refine query to see the N rows behind this?"
    prompt before launch.
  - 3+ sessions with `row_count == 1` where the user clicks Download
    CSV (in log: `/api/execution/<id>/csv` GET followed by no further
    session activity — proxy for "they downloaded and left").
