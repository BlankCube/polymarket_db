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

**Key files:** chat/app.py, chat/ai.py, chat/sql_safety.py, chat/python_runner.py, chat/db_pool.py, chat/auth.py, chat/static/index.html

#### User-AI Interaction Flow

The user never sees code or raw SQL. Everything is natural language. The flow consists of 5 steps, each backed by a separate AI call with its own focused prompt.

```
Step 1: UNDERSTAND
  User: "在大选后高价买入的人赚钱了吗？"
  AI Call 1 (prompt: understanding)
  → AI: "我理解你想知道的是：
         - 在2024美国大选相关市场中
         - 在事件截止后（end_date之后）
         - 以高价（≥0.95）买入的交易
         - 最终是赚钱还是亏钱
         这样理解对吗？"

Step 2: CONFIRM
  User: "对" / "不对，我是说..." (if wrong, go back to Step 1)

Step 3: GENERATE & EXECUTE (hidden from user)
  AI Call 2 (prompt: code generation)
  → AI generates SQL or Python code
  → Backend executes it, gets raw results
  → User sees nothing during this step (maybe a loading indicator)

Step 4: DESCRIBE THE QUERY
  AI Call 3 (prompt: query description)
  Input: the code that was executed + the parameters used
  → AI: "我查询了以下数据：
         - 范围：所有包含 'election'、'president'、'trump'、'harris' 关键词的市场
         - 时间窗口：每个市场的事件截止日期之后、链上结算之前
         - 价格：买入价 ≥ 0.95
         - 数据量：共找到 23,230 笔符合条件的交易，涉及 2,961 个市场"
  Purpose: 让用户知道 AI 具体查了什么，确认范围是否正确

Step 5: INTERPRET RESULTS
  AI Call 4 (prompt: data interpretation)
  Input: the raw query results (tables, numbers)
  → AI: "结果分析：
         - 0.99 价位：116,478 笔交易，胜率 99.6%，但 ROI 仅 0.86%
           盈亏平衡需要 99% 胜率，当前胜率刚好超过，策略微利
         - 0.95 价位：23,230 笔，胜率 95.7%，盈亏平衡需要 95%
           刚好及格，但样本中有 1,009 笔亏损交易
         - 最大单笔亏损：$62,703（'第三方候选人≥2%选票'市场）
         ..."
  Purpose: 用数据说话，每个结论附带具体数字
```

**What the user sees on screen:**
- Step 1: AI's understanding (text)
- Step 2: Their confirmation (text)
- Step 4: Query description (text) — what was searched, what filters, how much data
- Step 5: Data interpretation (text) — findings with numbers
- Optional: a collapsible "raw data" section if the user wants to see the actual table

**What the user NEVER sees:**
- SQL code
- Python code
- Raw uninterpreted tables (unless they explicitly expand)

**Why 4 separate AI calls instead of 1?**
Each call has a focused prompt optimized for one task. A single call trying to do understanding + code generation + description + interpretation produces worse results on every dimension. Separate calls also allow us to:
- Use different models for different steps (e.g., cheaper model for code gen, better model for interpretation)
- Log and debug each step independently
- Retry one step without redoing everything

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
- `feedback/logs/chat.jsonl` — raw interaction log
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
| **Transient** | State that will self-resolve without intervention (e.g. indexer catching up, Gamma sync lag, migration window) | Do NOT patch. Note and move on — patches for transient states become tech debt that misleads users once the state resolves. |

#### Step 2: Quantify Before Acting

Before anything else, answer **"will this problem still exist in 2 weeks if we do nothing?"** If no, it's Transient — classify it as such in Step 1 and stop. Proceeding only makes sense for structural problems.

Then for structural problems:

- How many users are affected? (check session data)
- What's the severity? (annoying vs blocking)
- What's the blast radius of the fix? (does it affect other scenarios?)
- Is there a pattern? (one user ≠ systemic problem — **unless** the mechanism is predictable: e.g. "any query filtering by non-indexed column on order_fills will time out" is confirmable with N=1 because you can explain why the next user will hit it)

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
