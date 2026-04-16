# Product Improvement Methodology

## Core Principle

Every change must improve the experience for the **majority** of users. A fix that helps 5% but hurts 50% is a net negative. When in doubt, don't change.

## Step 1: Classify the Feedback

Every piece of feedback falls into one of these categories:

| Category | Description | Action |
|----------|-------------|--------|
| **Systemic Bug** | Feature is broken for everyone (e.g., Python runner crashes) | Fix immediately |
| **Systemic Quality** | AI consistently gives bad output in common scenarios | Improve prompt/logic |
| **Edge Case Bug** | Broken only in rare conditions | Fix if low-risk, otherwise backlog |
| **Preference** | User wants it different, but current behavior is reasonable | Track frequency, change only if pattern emerges |
| **Misuse** | User expects something the tool isn't designed for | Improve onboarding/docs, don't change core behavior |

## Step 2: Quantify Before Acting

Before proposing any change, answer:

1. **How many users are affected?** (check session data: how often does this scenario occur?)
2. **What's the severity?** (annoying vs completely blocks the user)
3. **What's the blast radius of the fix?** (does changing this affect other scenarios?)
4. **Is there evidence of a pattern?** (one angry user ≠ systemic problem)

Minimum threshold for action:
- Systemic bugs: 1 occurrence is enough
- Quality improvements: need ≥3 independent users hitting the same issue, OR the issue is trivially reproducible in common scenarios
- Preference changes: need clear majority pattern in feedback data

## Step 3: Propose Changes with Impact Assessment

Format:
```
PROPOSED CHANGE: [what to change]
CATEGORY: [from Step 1]
EVIDENCE: [data showing this is a real problem]
AFFECTED USERS: [who benefits, who might be hurt]
RISK: [what could go wrong]
RECOMMENDATION: [do it / don't do it / need more data]
```

## Step 4: Validate After Deployment

After any change, monitor:
- Did the feedback pattern disappear?
- Did new complaints emerge?
- Did session satisfaction scores change?

## What NOT to do

- Don't add special-case rules in the prompt for one user's complaint
- Don't make the AI more verbose to be "more helpful" — most users want concise output
- Don't restrict AI behavior because one user misused it
- Don't over-engineer the prompt — every added instruction dilutes the others
- Don't "fix" things that aren't broken just because one user was unhappy
