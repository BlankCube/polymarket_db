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
