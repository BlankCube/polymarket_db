"""
AI module: 4 separate AI calls for the 5-step interaction flow.

Step 1: understand() - Parse user intent, ask for confirmation
Step 2: (user confirms)
Step 3: generate_code() - Generate SQL/Python to answer the question
Step 4: describe_query() - Explain what was queried in natural language
Step 5: interpret_results() - Analyze the data with numbers
"""

import os
import re
import json
import anthropic

client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

MODEL = "claude-sonnet-4-20250514"

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

## Output Format

For normal questions, use this structure:

[Your understanding of what they want, stated as bullet points:]
- Target: [what they want to know]
- Scope: [which markets, time range]
- Filters: [price range, trade size, etc.]
- Metric: [what to measure - win rate, ROI, volume, count, etc.]
- Note: [anything important they might not have considered]

[Ask for confirmation]
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
4. order_fills is huge - MUST filter on indexed columns.
5. Amounts: divide by 1e6 for USD values.
6. Search markets: WHERE question ILIKE '%keyword%'
7. Join pattern: order_fills f JOIN markets m ON f.condition_id = m.condition_id LEFT JOIN token_market_map t ON f.token_id = t.token_id
8. For Python: use query_db(sql) which returns a pandas DataFrame. Use print() for output.
9. Make output data-rich: include counts, percentages, totals. The next step needs numbers to interpret.
"""

# === Prompt 3: Query Description ===

DESCRIBE_PROMPT = DOMAIN_KNOWLEDGE + """
## Your Role

You just ran a database query. Describe IN NATURAL LANGUAGE what was searched. The user has never seen the code and never will.

## Rules

1. **Language**: Match the user's language from the conversation.
2. Describe the ACTUAL parameters used: which keywords searched, what price range, what time window, what tables.
3. State the scope: how many rows matched, how many markets involved.
4. Be specific: "searched for markets containing 'election', 'president', 'trump', or 'harris'" not "searched for election-related markets".
5. Keep it brief - 3-5 bullet points max.
6. If the query errored, explain what went wrong in plain language.
"""

# === Prompt 4: Result Interpretation ===

INTERPRET_PROMPT = DOMAIN_KNOWLEDGE + """
## Your Role

You are a data analyst interpreting query results for a prediction market researcher. The user has already seen the query description and knows what was searched.

## Rules

1. **Language**: Match the user's language from the conversation.
2. **Every claim needs a number**: "win rate 99.6% (N=116,478)" not "high win rate".
3. **Profit math is mandatory**: If analyzing a buy strategy at price P, ALWAYS compute break-even (need win rate > P) and compare to actual win rate before concluding profitable/unprofitable.
4. **Acknowledge uncertainty**: If sample size < 100, say so. If data only covers certain time period, say so.
5. **Highlight surprises**: What's unexpected in the data? Biggest winners/losers?
6. **No vague advice**: Don't say "consider diversifying" or "be careful". Just present what the data shows.
7. **Structure your response**: Lead with the key finding, then supporting details.
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
        model=MODEL, max_tokens=4096, system=system, messages=messages,
    ) as s:
        full = ""
        async for text in s.text_stream:
            full += text
            yield ("text", text)
        yield ("full_response", full)


async def ai_complete(system, messages):
    """Non-streaming AI call. Returns full text."""
    resp = await client.messages.create(
        model=MODEL, max_tokens=4096, system=system, messages=messages,
    )
    return resp.content[0].text


def step1_understand(messages):
    """Stream the understanding/confirmation response."""
    return ai_stream(UNDERSTAND_PROMPT, messages)


async def step3_generate(messages, confirmed_intent):
    """Generate code (non-streaming, hidden from user)."""
    gen_messages = messages + [
        {"role": "user", "content": f"The user confirmed this intent: {confirmed_intent}\n\nGenerate the code now. Output ONLY <sql> or <python> tags with code inside."}
    ]
    return await ai_complete(GENERATE_PROMPT, gen_messages)


def step4_describe(code, output, user_lang_messages):
    """Describe what was queried (streaming)."""
    user_langs = json.dumps([m for m in user_lang_messages[-4:] if m['role'] == 'user'], ensure_ascii=False)
    return ai_stream(DESCRIBE_PROMPT, [
        {"role": "user", "content": f"Code executed:\n{code}\n\nOutput:\n{output[:3000]}\n\nUser language context:\n{user_langs}"}
    ])


def step5_interpret(code, output, user_lang_messages):
    """Interpret results (streaming)."""
    user_langs = json.dumps([m for m in user_lang_messages[-4:] if m['role'] == 'user'], ensure_ascii=False)
    return ai_stream(INTERPRET_PROMPT, [
        {"role": "user", "content": f"Code:\n{code}\n\nResults:\n{output[:5000]}\n\nUser language context:\n{user_langs}"}
    ])
