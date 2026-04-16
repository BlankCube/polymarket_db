# Polymarket Explorer - Product Foundation

## What This Is

A web tool that lets anyone explore Polymarket's full on-chain trading history (2023-present, 170M+ trades, 750K+ markets) using natural language. Users don't need SQL or database knowledge. An AI analyst converts their questions into queries, executes them, and interprets results.

## Who It Serves

**Primary users**: Prediction market traders and researchers who have hypotheses about market behavior but lack the technical ability to query blockchain data directly.

Examples:
- A trader who wants to know if buying at 0.99 after event deadline is profitable
- A researcher studying how large wallets behave during US elections
- A casual user curious about which markets had surprise outcomes

**What they need**: The ability to express a research idea in plain language and get back data-supported answers. They value accuracy over speed, and raw data over hand-wavy conclusions.

---

## System Architecture (3 Parts)

### Part 1: Database

The data layer. Contains all Polymarket on-chain trading data.

**What it does:**
- Indexes blockchain events (trades, resolutions, redemptions) from Polygon via full node RPC
- Syncs market metadata (questions, dates, categories) from Polymarket Gamma API
- Stores everything in PostgreSQL with indexes optimized for analytical queries

**Key files:** config.py, db.py, schema.sql, indexer.py, unified_indexer.py, sync_markets.py, sync_categories.py

**Key tables:** markets, order_fills (170M+ rows), token_market_map, resolutions, redemptions, backtest_trades (materialized view)

**The AI accesses this via:** SQL queries (through asyncpg pool, read-only) and Python code (through query_db() function in subprocess)

### Part 2: AI Chat System

A standard AI chat interface with a specific system prompt and tool access. Not fundamentally different from any other AI chat product — the differentiation is in the prompt engineering and the database it connects to.

**What it does:**
- Takes natural language input from users
- Confirms understanding before acting (3-step flow: understand → confirm → execute)
- Generates SQL or Python code to query Part 1
- Executes queries safely (SQL validation, read-only connections, timeouts)
- Interprets results in plain language with data-backed conclusions

**Key files:** webapp/app.py, webapp/ai.py, webapp/sql_safety.py, webapp/python_runner.py, webapp/db_pool.py, webapp/auth.py, webapp/static/index.html

**The system prompt defines:** domain knowledge (what Polymarket is, how prices/payouts work, profit math), behavior rules (confirm first, match language, every claim needs a number), and tool instructions (SQL/Python format, table schemas, index hints)

### Part 3: Self-Improvement System

Collects user behavior data, enables periodic review, and provides a framework for making changes.

**What it does:**
- Logs every interaction (user queries, AI responses, SQL/Python executed, errors) to chat.jsonl
- Saves full conversation history per session to user_sessions table (with user_id, ratings, feedback)
- Provides feedback buttons (thumbs up/down + free text) on every AI response

**How improvement works:**
1. User data accumulates in logs + database
2. Developer (or scheduled process) reviews the data periodically
3. Issues are analyzed using the Improvement Methodology (below)
4. Approved changes are applied to the system prompt or code
5. Impact is monitored after deployment

**Key data sources for review:**
- `webapp/logs/chat.jsonl` — raw interaction log
- `user_sessions` table — per-session conversations, ratings, feedback
- Server logs — errors, timeouts, failed queries

---

## Core Principles

### 1. Users Are Smarter Than The AI

Users have domain knowledge the AI lacks. The AI's job is to be a skilled database operator, not a strategy advisor. Show data, let users draw conclusions. Never present vague analysis as insight.

### 2. Confirm Before Acting

Users' questions are often ambiguous. "Show me election data" could mean 50 different queries. The AI must articulate its interpretation and wait for confirmation before executing anything. This costs one extra message but prevents wasted compute and user frustration.

### 3. Match The User's Language

If the user writes in English, respond in English. If Chinese, respond in Chinese. Never mix.

### 4. Every Claim Needs A Number

Never say "market X performed well" — say "market X: 99.2% win rate (N=1,234), ROI +0.31%". If the sample size is too small to be meaningful, say so explicitly.

### 5. Understand The Math Before Concluding

Buying at price P requires win rate > P to break even. This is non-negotiable knowledge. The AI must compute break-even thresholds before labeling anything as "profitable" or "unprofitable".

---

## Improvement Methodology

### How We Process User Feedback

#### Step 1: Classify

| Category | Description | Action Threshold |
|----------|-------------|-----------------|
| **Systemic Bug** | Broken for everyone | Fix immediately, 1 occurrence enough |
| **Systemic Quality** | AI consistently wrong in common scenarios | Fix if reproducible in common use cases |
| **Edge Case Bug** | Broken only in rare conditions | Fix if low-risk, otherwise backlog |
| **Preference** | User wants it different, current behavior is reasonable | Change only if pattern emerges across multiple users |
| **Misuse** | User expects something the tool isn't designed for | Improve onboarding, don't change core |

#### Step 2: Quantify Before Acting

- How many users are affected? (check session data)
- What's the severity? (annoying vs blocking)
- What's the blast radius of the fix? (does it affect other scenarios?)
- Is there a pattern? (one user ≠ systemic problem)

#### Step 3: Evaluate Impact

For every proposed change, answer:
- **Who benefits?** (what % of users hit this scenario)
- **Who gets hurt?** (does the fix degrade other experiences)
- **Net impact**: benefit_coverage × severity > harm_coverage × severity?

**If a fix helps 5% but hurts 50%, don't do it.**

#### Step 4: Propose With Structure

```
CHANGE: [what]
CATEGORY: [from Step 1]
EVIDENCE: [data]
BENEFITS: [who, how many]
RISKS: [who gets hurt]
VERDICT: [do it / don't / need more data]
```

#### Step 5: Validate After Deployment

- Monitor session satisfaction scores
- Check if the feedback pattern disappears
- Watch for new complaints caused by the change

### What NOT To Do

- Don't add special-case prompt rules for one user's complaint
- Don't make AI more verbose to seem "more helpful" — most users want concise
- Don't restrict AI behavior because one user misused it
- Don't over-stuff the system prompt — every instruction dilutes the others
