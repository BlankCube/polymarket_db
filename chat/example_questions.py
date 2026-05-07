"""Curated library of example questions shown to users who ask "what can
this do?" / "给个例子" / "这是什么".

Guidelines for adding entries
-----------------------------

1. **Natural language, no jargon.** These are shown to traders and
   researchers, not engineers. Avoid:
     - Internal field / table names (``condition_id``, ``first_trade``,
       ``markets_touched``, ``active_months``, ``total_volume_usd``,
       ``wallet_market_pairs``...).
     - Polymarket-internal concepts (``neg_risk`` multi-outcome,
       ``token_won``, ``resolution_payout``...).
     - Trading-platform lingo only SQL people know (``vwap``, ``OHLC``,
       ``close_price``).
   Write "成交额 / 成交价 / 持仓时长 / 累计成交 / 最低最高价" etc. Use
   "地址" not "wallet".

2. **Neutral.** No value judgments about traders ("愚蠢", "非理性",
   "买错了方向", "出人意料"). Describe the filter mechanically — the
   user draws conclusions from the numbers.

3. **Quantitative.** Answer is a number, distribution, ranking, or
   time series. "It depends" is not an example.

4. **Feasible on our actual data.** We index:
     - Trades (order_fills + rollups).
     - Resolutions + redemptions.
     - Market metadata (via Gamma API sync).
   We do NOT index current on-chain token balances. Questions like
   "wallet X's current open positions / 现在持有什么" are NOT answerable.
   Historical trade activity IS queryable.

5. **Don't use ``close_price`` / "last trade price" as a signal.**
   Polymarket prices converge to 0 or 1 near resolution, so "last trade
   price vs actual payout" is a trivial near-zero — not interesting. For
   "how wrong was the market?" use the VWAP over a meaningful window
   (e.g. last 24h before resolution) vs the actual outcome instead.

6. **Real category names are sparse.** Top-volume markets often have
   no category set at all (Trump election / NBA finals / etc. are
   ``category=NULL``), so "by category" queries drop the biggest
   markets. Either filter to markets-that-have-a-category explicitly,
   or phrase "by topic" with keyword matching on the question text.

7. **Universal phrasing — no topic-specific defaults.** Questions
   appear as clickable chips at the top of a new session. Two failure
   modes to avoid:
   (a) Placeholder-style ("某个 category" / "<关键词>") — clicking sends
       verbatim, forces step1 to clarify, user answers "ok" vaguely,
       step3 pivots scope. Banned outright.
   (b) Hard-coded topic ("Crypto 类别里..." / "有没有关于 trump 的市场?")
       — still concrete so runnable, but the concrete value reads as a
       swap-me placeholder. User either clicks and gets a trivially
       topic-specific answer, or feels obligated to edit before sending.
       **Also banned.** Prefer questions whose answer is meaningful
       WITHOUT any user edit: "按类别汇总成交量，哪个类别总成交最大？"
       or "历史累计成交额最高的那个地址，列出他参与过的所有市场...".
   Universality check: if swapping "trump" → "bitcoin" → "foo" in the
   question changes the answer but not the question's usefulness, it's
   a topic-specific default → remove or rephrase as a distribution /
   ranking / phenomenon question.

Keep ~15-25 entries. The tool picks a random subset per call.
"""


EXAMPLES = [
    # --- Discovery / platform structure (no topic swap needed) ---
    "最近一个月新开的市场里，成交最活跃的 20 个是哪些？",
    "按类别汇总成交量，哪个类别总成交最大？",
    "历史上参与过交易的独立地址总数是多少？",
    "历史成交量最大的那个单独市场是什么？它的价格区间、参与地址数、持续时长分别是多少？",

    # --- Strategy backtests ---
    "在市场给出的事件截止日之后、以 ≥0.9 的价格买入的交易，最终正确的比例是多少？按 0.9 / 0.95 / 0.99 三档分别算。",
    "按持仓时长分（<1 小时 / 1-24 小时 / 1-7 天 / 7 天以上），持有到市场结算的交易各自的平均收益率和样本量。",

    # --- Trader / address behavior ---
    "历史累计成交额 top 20 的地址；每个地址更偏主动挂单方还是主动吃单方？",
    "交易过 100 个以上不同市场的活跃地址有多少个？",
    "只活跃过 1 个月的地址 vs 持续活跃 12 个月以上的长期地址：数量和平均累计成交额对比。",

    # --- Single-entity deep dive (universal — "the top X", not a named X) ---
    "历史累计成交额最高的那个地址，列出他参与过的所有市场：每个市场的首笔成交价、最后一笔成交价、累计成交额。",
]


def sample(count: int = 4, rng=None) -> list[str]:
    """Return ``count`` distinct questions sampled without replacement from
    EXAMPLES. ``rng`` optional (random.Random instance) for deterministic
    sampling in tests; defaults to the module-level random stream."""
    import random
    r = rng or random
    n = max(1, min(count, len(EXAMPLES)))
    return r.sample(EXAMPLES, n)
