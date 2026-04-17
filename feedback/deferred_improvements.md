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
