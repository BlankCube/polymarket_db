#!/usr/bin/env python3
"""
Polymarket Data Indexer - Main Runner

Usage:
    python run.py sync-markets          # Sync market metadata from Gamma API
    python run.py index-trades          # Index OrderFilled + OrdersMatched events
    python run.py index-resolutions     # Index ConditionResolution + PayoutRedemption
    python run.py index-all             # Run all indexers sequentially
    python run.py status                # Show indexer status
    python run.py backtest-099          # Run the 0.99+ resolution backtest
"""

import sys
import time
from datetime import datetime, timezone
from config import (
    CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE,
    CTF_EXCHANGE_START_BLOCK, NEG_RISK_START_BLOCK
)


def cmd_sync_markets():
    from sync_markets import sync_all_markets
    sync_all_markets()


def cmd_index_trades():
    from indexer import index_exchange_events
    print("=" * 60)
    print("Phase 1: CTF Exchange (standard markets)")
    print("=" * 60)
    index_exchange_events(
        CTF_EXCHANGE, "ctf",
        "ctf_exchange_last_block", CTF_EXCHANGE_START_BLOCK
    )

    print()
    print("=" * 60)
    print("Phase 2: Neg Risk CTF Exchange (multi-outcome markets)")
    print("=" * 60)
    index_exchange_events(
        NEG_RISK_CTF_EXCHANGE, "neg_risk",
        "neg_risk_exchange_last_block", NEG_RISK_START_BLOCK
    )


def cmd_index_resolutions():
    from indexer import index_conditional_token_events
    index_conditional_token_events()


def cmd_index_all():
    print("=" * 60)
    print("Step 1/3: Sync market metadata")
    print("=" * 60)
    cmd_sync_markets()

    print()
    print("=" * 60)
    print("Step 2/3: Index trade events")
    print("=" * 60)
    cmd_index_trades()

    print()
    print("=" * 60)
    print("Step 3/3: Index resolution & redemption events")
    print("=" * 60)
    cmd_index_resolutions()

    print()
    print("All indexing complete!")


def cmd_status():
    from db import get_conn, get_state
    from indexer import get_current_block

    current = get_current_block()
    print(f"Current Polygon block: {current}")
    print()

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # Table counts
            for table in ['markets', 'token_market_map', 'order_fills', 'order_matches', 'resolutions', 'redemptions']:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                print(f"  {table}: {count:,} rows")

            print()

            # Indexer state
            cur.execute("SELECT key, value, updated_at FROM indexer_state ORDER BY key")
            rows = cur.fetchall()
            if rows:
                print("Indexer state:")
                for key, value, updated_at in rows:
                    if 'block' in key:
                        behind = current - int(value)
                        print(f"  {key}: block {value} ({behind:,} blocks behind)")
                    else:
                        print(f"  {key}: {value}")

            print()

            # Market stats
            cur.execute("SELECT COUNT(*) FROM markets WHERE resolved = TRUE")
            resolved = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM markets WHERE closed = TRUE")
            closed = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM markets")
            total = cur.fetchone()[0]
            print(f"Markets: {total} total, {resolved} resolved, {closed} closed")

    finally:
        conn.close()


def cmd_backtest_099():
    """
    Backtest strategy with holding cost and depth constraints:
    - Window: [end_date, resolved_at)
    - Holding cost: 0.01% per day (1 bps/day), precise to the second
    - Depth: only take 1% of each trade's volume
    - Max position: $10,000 per market
    """
    from db import get_conn
    import json

    COST_PER_DAY = 0.0001  # 每日万分之一
    FILL_RATIO = 0.10      # 只吃成交量的10%
    MAX_POSITION = 10000   # 每个市场最多持仓 $10,000

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT m.condition_id, m.question, m.end_date, m.resolved_at,
                       m.resolution_payout, m.clob_token_ids, m.outcomes
                FROM markets m
                WHERE m.resolved = TRUE
                  AND m.resolution_payout IS NOT NULL
                  AND m.end_date IS NOT NULL
                  AND m.resolved_at IS NOT NULL
                  AND m.resolved_at > m.end_date
            """)
            resolved_markets = cur.fetchall()

        print(f"Found {len(resolved_markets)} resolved markets (with end_date < resolved_at)")
        print(f"Parameters: holding cost = {COST_PER_DAY*100:.2f}%/day, "
              f"fill ratio = {FILL_RATIO*100:.0f}% of volume, "
              f"max position = ${MAX_POSITION:,}/market")
        print()

        price_levels = [0.99, 0.98, 0.97, 0.96, 0.95, 0.94, 0.93, 0.92, 0.91, 0.90]
        results = {p: {
            "trades": 0, "wins": 0, "losses": 0,
            "total_invested": 0, "total_payout": 0, "total_cost": 0,
            "total_hold_seconds": 0, "markets_with_trades": set(),
            # Depth-constrained version
            "real_invested": 0, "real_payout": 0, "real_cost": 0,
            "real_trades": 0, "real_markets": set(),
        } for p in price_levels}

        for cond_id, question, end_date, resolved_at, payout_json, clob_ids_json, outcomes_json in resolved_markets:
            if not payout_json or not clob_ids_json:
                continue

            payout = payout_json if isinstance(payout_json, list) else json.loads(payout_json)
            clob_ids = clob_ids_json if isinstance(clob_ids_json, list) else json.loads(clob_ids_json)

            for outcome_idx, token_id in enumerate(clob_ids):
                token_won = (payout[outcome_idx] > 0) if outcome_idx < len(payout) else False
                payout_per_token = 1.0 if token_won else 0.0

                # Single query: get all trades in window, bucket by ROUND(price,2)
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT f.block_timestamp, ROUND(f.price::numeric, 2) as price_bucket,
                               f.usdc_amount / 1e6 as usdc,
                               f.token_amount / 1e6 as tokens
                        FROM order_fills f
                        WHERE f.condition_id = %s
                          AND f.token_id = %s
                          AND f.side = 'BUY'
                          AND f.price >= 0.895
                          AND f.price <= 1.005
                          AND f.block_timestamp > %s
                          AND f.block_timestamp < %s
                        ORDER BY f.block_timestamp
                    """, (cond_id, str(token_id), end_date, resolved_at))
                    all_trades = cur.fetchall()

                if not all_trades:
                    continue

                # Per-market position tracking for depth constraint (per price level)
                market_positions = {p: 0.0 for p in price_levels}

                for trade_ts, price_bucket, raw_usdc, raw_tokens in all_trades:
                    price_level = float(price_bucket)
                    if price_level not in results:
                        continue

                    r = results[price_level]
                    r["markets_with_trades"].add(cond_id)

                    trade_usdc = float(raw_usdc)
                    trade_tokens = float(raw_tokens)
                    hold_seconds = (resolved_at - trade_ts).total_seconds()
                    hold_days = hold_seconds / 86400.0
                    holding_cost_rate = COST_PER_DAY * hold_days

                    # --- Unconstrained stats ---
                    r["trades"] += 1
                    r["total_invested"] += trade_usdc
                    r["total_payout"] += trade_tokens * payout_per_token
                    r["total_cost"] += trade_usdc * holding_cost_rate
                    r["total_hold_seconds"] += hold_seconds
                    if token_won:
                        r["wins"] += 1
                    else:
                        r["losses"] += 1

                    # --- Depth-constrained stats ---
                    my_usdc = trade_usdc * FILL_RATIO
                    my_tokens = trade_tokens * FILL_RATIO

                    if market_positions[price_level] + my_usdc > MAX_POSITION:
                        my_usdc = max(0, MAX_POSITION - market_positions[price_level])
                        if my_usdc <= 0:
                            continue
                        my_tokens = my_usdc / price_level if price_level > 0 else 0

                    market_positions[price_level] += my_usdc
                    r["real_invested"] += my_usdc
                    r["real_payout"] += my_tokens * payout_per_token
                    r["real_cost"] += my_usdc * holding_cost_rate
                    r["real_trades"] += 1
                    r["real_markets"].add(cond_id)

        # === Output: Unconstrained (all volume) ===
        print("=" * 130)
        print("TABLE 1: Unconstrained (all trades, with holding cost)")
        print("  holding cost = 0.01%/day, precise to second")
        print("=" * 130)
        print(f"{'Price':>7} | {'Trades':>8} | {'Mkts':>6} | {'Win%':>6} | {'Invested':>12} | {'Payout':>12} | {'HoldCost':>10} | {'NetPnL':>12} | {'ROI':>7} | {'AvgHold':>8}")
        print("-" * 130)

        for p in price_levels:
            r = results[p]
            gross_pnl = r["total_payout"] - r["total_invested"]
            net_pnl = gross_pnl - r["total_cost"]
            roi = (net_pnl / r["total_invested"] * 100) if r["total_invested"] > 0 else 0
            win_pct = (r["wins"] / r["trades"] * 100) if r["trades"] > 0 else 0
            avg_hold_hrs = (r["total_hold_seconds"] / r["trades"] / 3600) if r["trades"] > 0 else 0
            n_markets = len(r["markets_with_trades"])
            print(f"  {p:.2f}  | {r['trades']:>8,} | {n_markets:>6,} | {win_pct:>5.1f}% | "
                  f"${r['total_invested']:>11,.2f} | ${r['total_payout']:>11,.2f} | "
                  f"${r['total_cost']:>9,.2f} | ${net_pnl:>11,.2f} | {roi:>6.2f}% | {avg_hold_hrs:>6.1f}hr")

        # === Output: Depth-constrained ===
        print()
        print("=" * 130)
        print(f"TABLE 2: Depth-constrained (1% of volume, max ${MAX_POSITION:,}/market, with holding cost)")
        print("=" * 130)
        print(f"{'Price':>7} | {'Trades':>8} | {'Mkts':>6} | {'Invested':>12} | {'Payout':>12} | {'HoldCost':>10} | {'NetPnL':>12} | {'ROI':>7}")
        print("-" * 100)

        for p in price_levels:
            r = results[p]
            gross_pnl = r["real_payout"] - r["real_invested"]
            net_pnl = gross_pnl - r["real_cost"]
            roi = (net_pnl / r["real_invested"] * 100) if r["real_invested"] > 0 else 0
            n_markets = len(r["real_markets"])
            print(f"  {p:.2f}  | {r['real_trades']:>8,} | {n_markets:>6,} | "
                  f"${r['real_invested']:>11,.2f} | ${r['real_payout']:>11,.2f} | "
                  f"${r['real_cost']:>9,.2f} | ${net_pnl:>11,.2f} | {roi:>6.2f}%")

        # === Losing trades ===
        print()
        print("LOSING trades (bought at >= 0.95 in window, token payout = 0):")
        print("-" * 110)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT f.price, f.usdc_amount/1e6 as usdc, f.block_timestamp,
                       m.question, m.end_date, m.resolved_at, m.resolution_payout,
                       t.outcome_label,
                       EXTRACT(EPOCH FROM (m.resolved_at - f.block_timestamp))/3600 as hold_hours
                FROM order_fills f
                JOIN markets m ON f.condition_id = m.condition_id
                LEFT JOIN token_market_map t ON f.token_id = t.token_id
                WHERE f.side = 'BUY'
                  AND f.price >= 0.95
                  AND m.resolved = TRUE
                  AND m.end_date IS NOT NULL
                  AND m.resolved_at IS NOT NULL
                  AND f.block_timestamp > m.end_date
                  AND f.block_timestamp < m.resolved_at
                  AND (
                    (t.outcome_index = 0 AND m.resolution_payout->0 = '0')
                    OR (t.outcome_index = 1 AND m.resolution_payout->1 = '0')
                  )
                ORDER BY f.usdc_amount DESC
                LIMIT 15
            """)
            rows = cur.fetchall()
            for price, usdc, ts, q, ed, ra, payout, label, hold_hrs in rows:
                print(f"  {ts} | {label} @ {price:.4f} | ${usdc:,.2f} | hold {hold_hrs:.1f}hr | {q[:55]}...")
                print(f"    payout={payout}")

        print()

    finally:
        conn.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    commands = {
        "sync-markets": cmd_sync_markets,
        "index-trades": cmd_index_trades,
        "index-resolutions": cmd_index_resolutions,
        "index-all": cmd_index_all,
        "status": cmd_status,
        "backtest-099": cmd_backtest_099,
    }

    if cmd not in commands:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)

    start = time.time()
    commands[cmd]()
    elapsed = time.time() - start
    print(f"\nCompleted in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
