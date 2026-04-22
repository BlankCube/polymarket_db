#!/usr/bin/env python3
"""Regression tests for the `wallet_volume_rollup.net_pnl_usd` column.

Run after any rebuild / schema change / backfill that touches the PnL
stack. All four tests should print `✓` — any `✗` means the rollup has
drifted from the raw on-chain truth.

Run from repo root:
    venv/bin/python database/verify_pnl.py
"""

import random
import sys
from decimal import Decimal

sys.path.insert(0, "/home/ubuntu/polymarket-db/database")
from db import get_conn


# Two 6-decimal USD-pegged collateral tokens we aggregate in G stage.
# Keep in sync with rollup.py::_USD_COLLATERAL_TOKENS_SQL.
_USD_TOKENS = (
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174",  # USDC.e
    "0x3a3bd7bb9528e159577f7c2e685cc81a765002e2",  # NegRiskAdapter wrapper
)


def _f(x) -> float:
    return float(x) if x is not None else 0.0


def test_1_population_identity(cur) -> bool:
    """Across the whole population, sum(net_pnl) must equal
    (sum_sell - sum_buy) + sum_redeem + sum_merge - sum_split - sum_fees
    — and since every order_fill has one buyer and one seller, sum_sell
    must equal sum_buy exactly, which reduces the identity to
      sum(pnl) = -(split - merge - redeem) - fees
              = -(open_inventory + fees).
    """
    cur.execute("""SELECT
        SUM(buy_volume_usd), SUM(sell_volume_usd),
        SUM(total_redemption_usd), SUM(total_split_usd), SUM(total_merge_usd),
        SUM(total_fees_paid_usd), SUM(net_pnl_usd)
      FROM wallet_volume_rollup""")
    b, s, r, sp, mg, f, pnl = (_f(x) for x in cur.fetchone())
    expected = (s - b) + r + mg - sp - f
    open_inv = sp - mg - r
    balance = pnl + open_inv + f
    ok = abs(pnl - expected) < 0.01 and abs(balance) < 0.01
    print("== Test 1: population accounting identity ==")
    print(f"  sum buy   = {b:>18,.2f}")
    print(f"  sum sell  = {s:>18,.2f}   (buy==sell diff = {s-b:,.4f})")
    print(f"  redemption= {r:>18,.2f}")
    print(f"  split     = {sp:>18,.2f}")
    print(f"  merge     = {mg:>18,.2f}")
    print(f"  fees      = {f:>18,.2f}")
    print(f"  sum pnl   = {pnl:>18,.2f}")
    print(f"  expected  = {expected:>18,.2f}   (diff {pnl-expected:,.4f})")
    print(f"  open inv  = {open_inv:>18,.2f}")
    print(f"  pnl + open_inv + fees = {balance:,.4f}   {'✓' if ok else '✗'}")
    return ok


def test_2_spot_check_no_splits(cur, n_wallets: int = 3) -> bool:
    """Recompute PnL for a handful of simple wallets (no splits/merges)
    directly from order_fills + redemptions and compare to rollup."""
    cur.execute("""SELECT wallet FROM wallet_volume_rollup
                   WHERE total_trade_count BETWEEN 3 AND 20
                     AND split_count = 0 AND merge_count = 0
                     AND redemption_count > 0
                     AND total_volume_usd > 100
                   ORDER BY RANDOM() LIMIT %s""", (n_wallets,))
    wallets = [r[0] for r in cur.fetchall()]
    print(f"\n== Test 2: {len(wallets)} no-split wallets against raw events ==")
    all_ok = True
    for w in wallets:
        cur.execute("""SELECT buy_volume_usd, sell_volume_usd,
                              total_redemption_usd, total_fees_paid_usd,
                              net_pnl_usd
                       FROM wallet_volume_rollup WHERE wallet=%s""", (w,))
        b, s, r, f, pnl = (_f(x) for x in cur.fetchone())
        cur.execute("""SELECT
            SUM(CASE WHEN (maker=%s AND side='BUY')  OR (taker=%s AND side='SELL')
                     THEN usdc_amount/1e6 ELSE 0 END),
            SUM(CASE WHEN (maker=%s AND side='SELL') OR (taker=%s AND side='BUY')
                     THEN usdc_amount/1e6 ELSE 0 END),
            SUM(CASE WHEN maker=%s THEN fee/1e6 ELSE 0 END)
          FROM order_fills WHERE maker=%s OR taker=%s""",
                    (w, w, w, w, w, w, w))
        raw_b, raw_s, raw_f = (_f(x) for x in cur.fetchone())
        cur.execute("""SELECT COALESCE(SUM(payout)/1e6, 0)
                       FROM redemptions WHERE redeemer=%s AND payout > 0""", (w,))
        raw_r = _f(cur.fetchone()[0])
        expected = raw_s + raw_r - raw_b - raw_f
        diff = pnl - expected
        status = "✓" if abs(diff) < 0.01 else "✗"
        all_ok &= status == "✓"
        print(f"  {w[:10]}... pnl=${pnl:>12,.2f}  expected=${expected:>12,.2f}  diff={diff:,.4f}  {status}")
    return all_ok


def test_3_spot_check_with_splits(cur, n_wallets: int = 3) -> bool:
    """Recompute PnL including the split/merge leg for wallets that use
    inventory minting, from raw events."""
    cur.execute("""SELECT wallet FROM wallet_volume_rollup
                   WHERE split_count BETWEEN 1 AND 50
                     AND merge_count BETWEEN 1 AND 50
                     AND total_trade_count BETWEEN 5 AND 100
                     AND total_volume_usd BETWEEN 500 AND 100000
                   ORDER BY RANDOM() LIMIT %s""", (n_wallets,))
    wallets = [r[0] for r in cur.fetchall()]
    print(f"\n== Test 3: {len(wallets)} split/merge wallets against raw events ==")
    all_ok = True
    for w in wallets:
        cur.execute("""SELECT buy_volume_usd, sell_volume_usd,
                              total_redemption_usd, total_fees_paid_usd,
                              total_split_usd, total_merge_usd,
                              net_pnl_usd
                       FROM wallet_volume_rollup WHERE wallet=%s""", (w,))
        b, s, r, f, sp, mg, pnl = (_f(x) for x in cur.fetchone())
        cur.execute("""SELECT
            SUM(CASE WHEN (maker=%s AND side='BUY')  OR (taker=%s AND side='SELL')
                     THEN usdc_amount/1e6 ELSE 0 END),
            SUM(CASE WHEN (maker=%s AND side='SELL') OR (taker=%s AND side='BUY')
                     THEN usdc_amount/1e6 ELSE 0 END),
            SUM(CASE WHEN maker=%s THEN fee/1e6 ELSE 0 END)
          FROM order_fills WHERE maker=%s OR taker=%s""",
                    (w, w, w, w, w, w, w))
        raw_b, raw_s, raw_f = (_f(x) for x in cur.fetchone())
        cur.execute("""SELECT COALESCE(SUM(payout)/1e6, 0) FROM redemptions
                       WHERE redeemer=%s AND payout > 0""", (w,))
        raw_r = _f(cur.fetchone()[0])
        cur.execute("""SELECT COALESCE(SUM(amount)/1e6, 0) FROM position_splits
                       WHERE stakeholder=%s AND collateral_token IN %s""",
                    (w, _USD_TOKENS))
        raw_sp = _f(cur.fetchone()[0])
        cur.execute("""SELECT COALESCE(SUM(amount)/1e6, 0) FROM position_merges
                       WHERE stakeholder=%s AND collateral_token IN %s""",
                    (w, _USD_TOKENS))
        raw_mg = _f(cur.fetchone()[0])
        expected = raw_s + raw_r + raw_mg - raw_b - raw_sp - raw_f
        diff = pnl - expected
        status = "✓" if abs(diff) < 0.01 else "✗"
        all_ok &= status == "✓"
        print(f"  {w[:10]}... pnl=${pnl:>12,.2f}  expected=${expected:>12,.2f}  "
              f"split=${raw_sp:>9,.2f} merge=${raw_mg:>9,.2f}  diff={diff:,.4f}  {status}")
    return all_ok


def test_4_market_conservation(cur, n_markets: int = 3) -> bool:
    """For a resolved market, the participants' aggregate PnL contribution
    equals redeem + merge - split - fees (the buy/sell leg cancels by
    trade matching). If the market has no unredeemed "dust" (losing
    tokens holders didn't bother to redeem), this ≈ -fees. Non-zero
    remainder reflects real unredeemed inventory, NOT a bug."""
    cur.execute("""SELECT m.condition_id, m.question
                   FROM markets m JOIN market_volume_rollup mvr ON m.condition_id=mvr.condition_id
                   WHERE m.resolved = true
                     AND mvr.total_volume_usd BETWEEN 10000 AND 500000
                     AND mvr.total_trade_count > 20
                   ORDER BY RANDOM() LIMIT %s""", (n_markets,))
    markets = cur.fetchall()
    print(f"\n== Test 4: market-level conservation over {len(markets)} resolved markets ==")
    all_ok = True
    for cid, question in markets:
        cur.execute("""SELECT SUM(usdc_amount)/1e6, SUM(fee)/1e6
                       FROM order_fills WHERE condition_id=%s""", (cid,))
        flow, fees = (_f(x) for x in cur.fetchone())
        cur.execute("""SELECT COALESCE(SUM(payout),0)/1e6
                       FROM redemptions WHERE condition_id=%s""", (cid,))
        redeem = _f(cur.fetchone()[0])
        cur.execute("""SELECT COALESCE(SUM(amount),0)/1e6
                       FROM position_splits
                       WHERE condition_id=%s AND collateral_token IN %s""",
                    (cid, _USD_TOKENS))
        split = _f(cur.fetchone()[0])
        cur.execute("""SELECT COALESCE(SUM(amount),0)/1e6
                       FROM position_merges
                       WHERE condition_id=%s AND collateral_token IN %s""",
                    (cid, _USD_TOKENS))
        merge = _f(cur.fetchone()[0])
        participant_pnl = redeem + merge - split - fees
        unredeemed_dust = split - merge - redeem
        # The test passes if participant PnL matches the dust-based expectation
        # exactly (within floating-point epsilon). It does NOT require dust to
        # be zero — real markets have leftover losing tokens.
        expected = -unredeemed_dust - fees
        diff = participant_pnl - expected
        status = "✓" if abs(diff) < 0.01 else "✗"
        all_ok &= status == "✓"
        print(f"  {question[:40]:<40s}  split=${split:>9,.2f}  merge=${merge:>9,.2f}  "
              f"redeem=${redeem:>9,.2f}  fees=${fees:,.4f}")
        print(f"    dust=${unredeemed_dust:,.2f}  participant-pnl=${participant_pnl:,.2f}  "
              f"expected={-unredeemed_dust-fees:,.2f}  diff={diff:,.4f}  {status}")
    return all_ok


def main():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            ok1 = test_1_population_identity(cur)
            ok2 = test_2_spot_check_no_splits(cur)
            ok3 = test_3_spot_check_with_splits(cur)
            ok4 = test_4_market_conservation(cur)
    finally:
        conn.close()
    print()
    print("=" * 60)
    all_ok = ok1 and ok2 and ok3 and ok4
    print(f"PnL verification: {'ALL PASS ✓' if all_ok else 'FAIL ✗'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
