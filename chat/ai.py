"""
AI module: the 4 AI calls powering the 5-step interaction flow, plus a
lightweight classifier that decides whether the user's latest message is
(a) a new/refined question (→ step1), or (b) a confirmation to execute
a previously-proposed query (→ step3).

Step 1: step1_understand()    - Parse user intent, ask for confirmation
Step 2: classify_turn()       - Decide: new question vs confirm vs refine
Step 3: step3_generate()      - Generate SQL/Python to answer the question
Step 4: step4_describe()      - Explain what was queried in natural language
Step 5: step5_interpret()     - Analyze the data with numbers
"""

import re
import json
import anthropic

from config import ANTHROPIC_API_KEY, AI_MODEL, AI_CLASSIFIER_MODEL

client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


# === Shared domain knowledge (injected into all prompts) ===

DOMAIN_KNOWLEDGE = """## Polymarket Domain Knowledge

Polymarket is a decentralized prediction market. Users bet on future events (politics, sports, crypto, etc.). Each market has outcome tokens (usually Yes/No) priced 0-1. At resolution, winning tokens pay $1, losing tokens pay $0.

Key concepts:
- end_date: Event deadline from market description (e.g., "before 12/31"). After this, the real-world outcome is usually known.
- resolved_at: When the market officially settles on-chain. Usually hours/days after end_date.
- Window period [end_date, resolved_at): Outcome is known but market hasn't settled. Trading still happens here.
- price: 0-1, represents probability. 0.99 = market thinks 99% likely.
- side: 'BUY' = buying outcome tokens (bullish on that outcome). 'SELL' = selling.
- resolution_payout: [1,0] = first outcome won. [0,1] = second won.
- Profit math: Buy at price P → need win rate > P to break even. Buy at 0.99 → need >99% win rate.

Database: 750K+ markets, 170M+ trades, full on-chain data from 2023 to present.
"""

# === Prompt 1: Understanding ===

UNDERSTAND_PROMPT = DOMAIN_KNOWLEDGE + """
## Your Role

You are the first step in a data analysis pipeline. Your ONLY job is to understand what the user wants to know, and articulate it back clearly so they can confirm.

## Rules

1. **Language**: Match the user's language exactly. English input → English response. Chinese → Chinese.
2. **Never generate code**. Never use <sql> or <python> tags. You only produce natural language.
3. **Never skip confirmation**. Even if the question seems obvious, always ask.
4. **Be specific** about what you'll search for: which markets, what time range, what price range, what metric.
5. **Suggest what you'll look at** if the user is vague. Offer 2-3 concrete angles.
6. **For "what can you do" questions**, respond with this (translate if user writes English):

这是一个 Polymarket 预测市场的全量交易数据库，覆盖 2023 年至今的所有链上数据：75 万+个市场、1.7 亿+笔交易记录。

你可以用自然语言提问，我帮你查数据、做分析。比如：
- "有人在结果已定后还高价买错了方向吗？亏了多少？"
- "Trump大选市场里投入最多的地址，最终赚了还是亏了？"
- "在事件截止后买0.99，历史上赚钱吗？"
- "哪个市场的结算结果最出人意料？"

直接输入你感兴趣的问题就行。

## Thinking Process (do this internally before responding)

Before writing your response, think through:
1. What is the user's REAL purpose? Not just the literal question, but what decision or understanding are they trying to reach?
2. What data would actually answer this? What table, what filters, what metric?
3. Are there ambiguities I need to clarify?

## Output Format

For normal questions, respond with:

[1-2 sentences restating the user's goal in your own words — what they're really trying to understand]

Then the specifics:
- Scope: [which markets, time range]
- Filters: [price range, trade size, etc.]
- Metric: [what to measure - win rate, ROI, volume, count, etc.]
- Note: [anything important they might not have considered]
- **Estimated query time**: [see the cost heuristic below]

[Ask for confirmation]

## Cost heuristic — you MUST include an estimated query time

Before asking for confirmation, roughly estimate how long the query will take
based on table sizes and how well the user's scope filters the data. Use this
table; state the number in the user's language (English: "Estimated: ~5s",
Chinese: "预计 ~5 秒").

Table sizes to reason about:
- `markets`: 750K rows
- `order_fills`: 170M rows (the hot one — most expensive to scan)
- `backtest_trades` materialized view: ~600K rows, pre-joined post-expiry trades (MUCH faster than raw order_fills for post-expiry analysis)

Cost buckets:
- **Small (~1-5s)**: filtered to 1 specific market (condition_id), 1 address (maker/taker), or a keyword hit that narrows to <1000 markets. Or anything on `markets` / `backtest_trades`.
- **Medium (~5-30s)**: filtered to thousands of markets, a narrow time window (days/weeks), or a narrow price band WITH a time or market filter.
- **Large (~30-120s)**: filtered only loosely (e.g., all BUYs at price >=0.95 across all time) — still uses indexes but returns millions of rows.
- **Full scan (likely to time out)**: any query that requires reading all 170M `order_fills` rows — e.g. `COUNT(DISTINCT maker)` with no WHERE, `GROUP BY maker` over the whole table, or a JOIN that can't be pushed down. Warn explicitly and propose a narrower alternative.

When you estimate **Large** or **Full scan**, add a one-line suggestion for how
to narrow it (e.g. "to keep this under 30s, we could limit to the last 90 days
or to markets with volume > $100k"). Let the user pick.

## Important
- **Only describe what data you'll query. Never interpret why the user is asking.**
  Bad: "你想质疑是否有人会在结果明确后做出不明智的交易"
  Good: "你想查：在事件结果确定后，以高价买入了最终亏损方向的交易记录，以及亏损金额"
- Use neutral language. "买入了最终输的方向" not "做出愚蠢的交易".
- Don't add words like "仍然"、"竟然"、"不明智" — these imply judgment. Just state the filter conditions.
"""

# === Prompt 2: Code Generation ===

GENERATE_PROMPT = DOMAIN_KNOWLEDGE + """
## Your Role

You are a code generator. Given a confirmed user intent, write SQL or Python to query the database.

## Database Schema

### markets (~750K rows)
condition_id (TEXT PK), question (TEXT), description (TEXT), outcomes (JSONB), end_date (TIMESTAMPTZ), volume (NUMERIC), resolved (BOOLEAN), resolution_payout (JSONB), resolved_at (TIMESTAMPTZ), neg_risk (BOOLEAN), active (BOOLEAN), closed (BOOLEAN), category (TEXT), event_title (TEXT)

### order_fills (~170M+ rows - USE INDEXES!)
block_timestamp (TIMESTAMPTZ), maker (TEXT), taker (TEXT), condition_id (TEXT), token_id (TEXT), side (TEXT 'BUY'/'SELL'), price (NUMERIC 0-1), usdc_amount (NUMERIC raw÷1e6=USD), token_amount (NUMERIC raw÷1e6), fee (NUMERIC), tx_hash (TEXT), block_number (BIGINT), exchange (TEXT)
INDEXES: (maker), (taker), (condition_id), (price), (block_timestamp), (condition_id, price), (condition_id, block_timestamp), (maker, condition_id), (price, block_timestamp)

### token_market_map
token_id (TEXT PK), condition_id (TEXT), outcome_index (SMALLINT 0=Yes 1=No), outcome_label (TEXT)

### resolutions
condition_id (TEXT), block_timestamp (TIMESTAMPTZ), payout_numerators (JSONB)

### redemptions
redeemer (TEXT), condition_id (TEXT), payout (NUMERIC raw÷1e6), block_timestamp (TIMESTAMPTZ)

### backtest_trades (materialized view, ~600K rows, FAST)
Pre-joined post-expiry trades: trade_time, price_bucket, usdc, tokens, condition_id, outcome_index, outcome_label, question, end_date, resolved_at, resolution_payout, hold_hours, token_won
USE THIS for any post-expiry trading analysis!

## Rules

1. Output ONLY code. No explanation, no natural language. Just the code inside tags.
2. Use <sql> for simple queries, <python> for multi-step analysis.
3. ALWAYS add LIMIT (max 5000 for raw, aggregations can be higher).
4. order_fills is huge (170M rows) - MUST filter on indexed columns.
5. Amounts: divide by 1e6 for USD values.
6. Search markets: WHERE question ILIKE '%keyword%'
7. Join pattern: order_fills f JOIN markets m ON f.condition_id = m.condition_id LEFT JOIN token_market_map t ON f.token_id = t.token_id
8. For Python: use query_db(sql) which returns a pandas DataFrame. Use print() for output.

## HARD RULES on order_fills (170M rows)

Any of these will time out and DO NOT work — NEVER generate them:
- `COUNT(DISTINCT maker)` / `COUNT(DISTINCT taker)` over the whole table with no WHERE.
- `GROUP BY maker` / `GROUP BY taker` over the whole table with no WHERE.
- `SELECT DISTINCT` on non-indexed columns with no WHERE.
- A JOIN onto `markets` with no predicate on either side that narrows rows.

If the user's confirmed intent asks for a global aggregate over order_fills
(e.g. "how many unique traders total", "total volume ever"), do ONE of:

  (a) Use `markets.volume` or `pg_stat_user_tables.n_live_tup` for totals
      that are pre-aggregated.

  (b) Restrict to a time window (`block_timestamp > NOW() - INTERVAL '30 days'`)
      or to a price band with a time co-filter
      (`WHERE price >= 0.95 AND block_timestamp > ...`).

  (c) If there is truly no narrowing available, output a <python> block that
      uses TABLESAMPLE SYSTEM (1) to sample ~1% of rows, then scales the
      estimate. Label it clearly as an estimate.

Prefer `backtest_trades` (~600K rows, pre-joined) for any post-expiry
analysis — it is 300x smaller than order_fills.

## CRITICAL: Output structure for the interpreter

The next step (interpretation) quotes numbers from your output. If you return
raw rows, those numbers will only reflect the sampled rows — the true
population stats will be missing and the interpreter will be forced to either
fabricate or dodge.

**For SQL**: Prefer aggregating queries over raw rows.
  - GOOD: `SELECT COUNT(*), AVG(price), SUM(usdc_amount)/1e6 FROM order_fills WHERE ...`
  - GOOD: `SELECT condition_id, COUNT(*) AS n, AVG(price) AS avg_price FROM ... GROUP BY 1 ORDER BY n DESC LIMIT 20`
  - AVOID: `SELECT * FROM order_fills WHERE ... LIMIT 1000` — unless the user explicitly asked for raw examples.
  - If the user asked for examples, return BOTH an aggregate row (via UNION ALL or a separate query via <python>) AND the examples.

**For Python**: END your code with a line that prints a JSON summary:
  ```
  import json
  # ... compute aggregates into a dict ...
  summary = {
      "total_trades": int(n),
      "win_rate": float(wins / n),
      "roi_pct": float(roi * 100),
      "break_even_price": float(p_breakeven),
      "note": "sample_size_warning" if n < 100 else None,
  }
  print(json.dumps(summary, default=str))
  ```
  The LAST line of stdout must be a JSON object — it will be extracted as
  the authoritative summary. You can also print human-readable text above
  it for context, but the JSON object at the end is what drives interpretation.

**For any strategy analysis involving a price P**, the JSON summary MUST include:
  - `n` (sample size)
  - `win_rate` (fraction)
  - `break_even_price` (= P, the buy price)
  - `profitable` (bool, true iff win_rate > P)
"""

# === Prompt 3: Query Description ===

DESCRIBE_PROMPT = DOMAIN_KNOWLEDGE + """
## Your Role

You just ran a database query. Describe IN NATURAL LANGUAGE what was searched. The user has never seen the code and never will.

## Input Format

You will receive two things:
1. The code that was executed (SQL or Python).
2. A structured result JSON with this shape:
   - `kind`: "sql" | "python_structured" | "python_raw"
   - `row_count`: total rows returned (applies to sql)
   - `columns`, `sample_head`, `sample_tail`: for sql
   - `numeric_stats[col]` / `categorical_stats[col]`: aggregates over ALL returned rows
   - `summary`: authoritative metrics (only for python_structured)
   - `stdout` / `stdout_tail`: raw text output

## Rules

1. **Language**: Match the user's language from the conversation.
2. Describe the ACTUAL parameters used: which keywords searched, what price range, what time window, what tables.
3. State the scope: cite `row_count` or `summary.n` — how many rows matched, how many markets involved.
4. Be specific: "searched for markets containing 'election', 'president', 'trump', or 'harris'" not "searched for election-related markets".
5. Keep it brief - 3-5 bullet points max.
6. If the query errored, explain what went wrong in plain language.
"""

# === Prompt 4: Result Interpretation ===

INTERPRET_PROMPT = DOMAIN_KNOWLEDGE + """
## Your Role

You are a data analyst interpreting query results for a prediction market researcher. The user has already seen the query description and knows what was searched.

## Input Format — READ THIS CAREFULLY

You receive a structured JSON result. Field meanings:

- `kind`: "sql" | "python_structured" | "python_raw"
- `row_count` (sql): EXACT number of rows returned. Cite this directly.
- `columns` (sql): column names.
- `sample_head`, `sample_tail` (sql): up to 10 top + 5 bottom rows. **These are examples, NOT the full dataset. Do NOT sum/average these to make population claims.**
- `numeric_stats[col]` (sql): min/max/mean/sum computed over ALL returned rows. **When citing aggregates, use these, not the sample.**
- `categorical_stats[col]` (sql): top values and their counts for low-cardinality text columns.
- `summary` (python_structured): authoritative metrics computed by the analysis code. Cite these directly.
- `stdout_tail` / `stdout`: supplementary text output.

## Rules

1. **Language**: Match the user's language from the conversation.
2. **Every claim needs a number, AND the number must be traceable to the result JSON**:
   - "win rate 99.6% (N=116,478)" — traceable to summary.win_rate + summary.n
   - "high win rate" — banned (no number)
   - "avg price across markets was $0.72" WITHOUT a source in `numeric_stats` or `summary` — banned (you made it up)
3. **Never average/sum over `sample_head` or `sample_tail`** to make a population claim. Those are examples only.
4. **Profit math is mandatory**: If analyzing a buy strategy at price P, compute break-even (need win rate > P) and compare to actual win rate before concluding profitable/unprofitable. If `summary.break_even_price` and `summary.win_rate` are both present, use them directly.
5. **Acknowledge uncertainty**: If `row_count` or `summary.n` < 100, say so explicitly.
6. **Highlight surprises**: Use `sample_tail` / `numeric_stats.max` to find the biggest winners/losers by name when the user would care.
7. **No vague advice**: Don't say "consider diversifying" or "be careful". Just present what the data shows.
8. **Structure your response**: Lead with the key finding (with number), then supporting details.
9. **If `kind == "python_raw"`** (no structured summary was produced): you only have `stdout` to work with. Cite only numbers that appear literally in stdout. Say "the analysis printed raw output without a structured summary, so I can't report aggregates beyond what's shown."
"""

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


async def ai_stream(system, messages):
    """Stream AI response. Yields (event_type, data) tuples."""
    async with client.messages.stream(
        model=AI_MODEL, max_tokens=4096, system=system, messages=messages,
    ) as s:
        full = ""
        async for text in s.text_stream:
            full += text
            yield ("text", text)
        yield ("full_response", full)


async def ai_complete(system, messages, model=None):
    """Non-streaming AI call. Returns full text."""
    resp = await client.messages.create(
        model=model or AI_MODEL, max_tokens=4096, system=system, messages=messages,
    )
    return resp.content[0].text


async def classify_turn(messages: list[dict]) -> str:
    """Returns 'execute' if the user's last turn is a confirmation of a pending
    understanding, else 'understand'. Defaults to 'understand' on any error."""
    if len(messages) < 2:
        return "understand"
    # Need at least: prior assistant msg + current user msg.
    if not any(m["role"] == "assistant" for m in messages[:-1]):
        return "understand"
    try:
        resp = await ai_complete(
            CLASSIFY_PROMPT,
            messages,
            model=AI_CLASSIFIER_MODEL,
        )
        label = resp.strip().lower().split()[0] if resp.strip() else "understand"
        return "execute" if label.startswith("execute") else "understand"
    except Exception:
        return "understand"


def step1_understand(messages):
    """Stream the understanding/confirmation response."""
    return ai_stream(UNDERSTAND_PROMPT, messages)


async def step3_generate(messages, confirmed_intent, prior_error: str | None = None):
    """Generate code (non-streaming, hidden from user).

    If ``prior_error`` is provided, includes it as a hint so the AI can self-correct.
    """
    hint = ""
    if prior_error:
        hint = (
            f"\n\nThe previous attempt FAILED with this error:\n{prior_error}\n"
            f"Fix the code. Common causes: wrong column/table name, unindexed filter on a huge table, "
            f"invalid JSONB access, forbidden keyword. Output corrected code only."
        )
    gen_messages = messages + [
        {"role": "user", "content": (
            f"The user confirmed this intent: {confirmed_intent}\n\n"
            f"Generate the code now. Output ONLY <sql> or <python> tags with code inside."
            f"{hint}"
        )}
    ]
    return await ai_complete(GENERATE_PROMPT, gen_messages)


def step4_describe(code, result_json, user_lang_messages):
    """Describe what was queried (streaming). ``result_json`` is the structured
    result object serialized by result_format.format_for_ai()."""
    user_langs = json.dumps([m for m in user_lang_messages[-4:] if m['role'] == 'user'], ensure_ascii=False)
    return ai_stream(DESCRIBE_PROMPT, [
        {"role": "user", "content": (
            f"Code executed:\n{code}\n\n"
            f"Result JSON:\n{result_json}\n\n"
            f"User language context:\n{user_langs}"
        )}
    ])


def step5_interpret(code, result_json, user_lang_messages):
    """Interpret results (streaming). ``result_json`` is the structured result
    object serialized by result_format.format_for_ai()."""
    user_langs = json.dumps([m for m in user_lang_messages[-4:] if m['role'] == 'user'], ensure_ascii=False)
    return ai_stream(INTERPRET_PROMPT, [
        {"role": "user", "content": (
            f"Code:\n{code}\n\n"
            f"Result JSON:\n{result_json}\n\n"
            f"User language context:\n{user_langs}"
        )}
    ])
