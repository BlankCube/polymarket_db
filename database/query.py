"""
Query utilities for Polymarket database.

Example usage:
    python query.py high-price-trades 0.95    # All trades at price >= 0.95
    python query.py market-trades <cond_id>   # All trades for a market
    python query.py trader <address>          # All trades by a trader
    python query.py resolved-stats            # Stats on resolved markets
    python query.py post-expiry-trades 0.99   # Trades at price >= X after market end_date
"""

import sys
import json
from db import get_conn


def high_price_trades(min_price=0.95, limit=50):
    """Find trades at high prices (near 1.0)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT f.block_timestamp, f.maker, f.taker, f.side, f.price,
                       f.usdc_amount/1e6 as usdc, f.token_amount/1e6 as tokens,
                       m.question, m.end_date, m.resolved, m.resolution_payout
                FROM order_fills f
                LEFT JOIN markets m ON f.condition_id = m.condition_id
                WHERE f.price >= %s AND f.price <= 1.0
                  AND f.side = 'BUY'
                ORDER BY f.block_timestamp DESC
                LIMIT %s
            """, (min_price, limit))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            for row in rows:
                print(dict(zip(cols, row)))
            print(f"\nTotal: {len(rows)} trades at price >= {min_price}")
    finally:
        conn.close()


def market_trades(condition_id, limit=100):
    """All trades for a specific market."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT m.question, m.end_date, m.resolved, m.resolution_payout, m.outcomes
                FROM markets m WHERE m.condition_id = %s
            """, (condition_id,))
            market = cur.fetchone()
            if market:
                print(f"Market: {market[0]}")
                print(f"End date: {market[1]}, Resolved: {market[2]}, Payout: {market[3]}")
                print()

            cur.execute("""
                SELECT f.block_timestamp, f.side, f.price,
                       f.usdc_amount/1e6 as usdc, f.token_amount/1e6 as tokens,
                       f.maker, f.taker, t.outcome_label
                FROM order_fills f
                LEFT JOIN token_market_map t ON f.token_id = t.token_id
                WHERE f.condition_id = %s
                ORDER BY f.block_timestamp
                LIMIT %s
            """, (condition_id, limit))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            for row in rows:
                print(dict(zip(cols, row)))
            print(f"\nTotal: {len(rows)} trades")
    finally:
        conn.close()


def trader_trades(address, limit=100):
    """All trades by a specific trader."""
    addr = address.lower()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT f.block_timestamp, f.side, f.price,
                       f.usdc_amount/1e6 as usdc, f.token_amount/1e6 as tokens,
                       f.condition_id, m.question, t.outcome_label
                FROM order_fills f
                LEFT JOIN markets m ON f.condition_id = m.condition_id
                LEFT JOIN token_market_map t ON f.token_id = t.token_id
                WHERE f.maker = %s OR f.taker = %s
                ORDER BY f.block_timestamp DESC
                LIMIT %s
            """, (addr, addr, limit))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            for row in rows:
                print(dict(zip(cols, row)))
            print(f"\nTotal: {len(rows)} trades for {address}")
    finally:
        conn.close()


def resolved_stats():
    """Statistics on resolved markets."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) as total_markets,
                    COUNT(*) FILTER (WHERE resolved = TRUE) as resolved,
                    COUNT(*) FILTER (WHERE closed = TRUE) as closed,
                    COUNT(*) FILTER (WHERE end_date < NOW() AND resolved = FALSE) as past_expiry_unresolved,
                    COUNT(*) FILTER (WHERE end_date < NOW()) as past_expiry_total
                FROM markets
            """)
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
            stats = dict(zip(cols, row))
            for k, v in stats.items():
                print(f"  {k}: {v:,}")

            print("\nMarkets past expiry, not yet resolved (potential 0.99 strategy targets):")
            cur.execute("""
                SELECT m.condition_id, m.question, m.end_date, m.volume
                FROM markets m
                WHERE m.end_date < NOW()
                  AND m.resolved = FALSE
                  AND m.active = TRUE
                ORDER BY m.volume DESC
                LIMIT 20
            """)
            rows = cur.fetchall()
            for cid, q, ed, vol in rows:
                print(f"  [{ed}] vol=${vol:,.0f} - {q[:80]}")
    finally:
        conn.close()


def post_expiry_trades(min_price=0.99, limit=100):
    """Find trades that happened after market end_date at high prices."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT f.block_timestamp, f.side, f.price,
                       f.usdc_amount/1e6 as usdc, f.token_amount/1e6 as tokens,
                       f.maker, m.question, m.end_date, m.resolved,
                       m.resolution_payout, t.outcome_label
                FROM order_fills f
                JOIN markets m ON f.condition_id = m.condition_id
                LEFT JOIN token_market_map t ON f.token_id = t.token_id
                WHERE f.price >= %s AND f.price <= 1.0
                  AND f.side = 'BUY'
                  AND m.end_date IS NOT NULL
                  AND f.block_timestamp > m.end_date
                ORDER BY f.block_timestamp DESC
                LIMIT %s
            """, (min_price, limit))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            for row in rows:
                d = dict(zip(cols, row))
                print(f"  {d['block_timestamp']} | {d['price']:.4f} | ${d['usdc']:.2f} | {d['outcome_label']} | {d['question'][:60]}...")
            print(f"\nTotal: {len(rows)} post-expiry high-price trades")
    finally:
        conn.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "high-price-trades":
        price = float(sys.argv[2]) if len(sys.argv) > 2 else 0.95
        high_price_trades(price)
    elif cmd == "market-trades":
        market_trades(sys.argv[2])
    elif cmd == "trader":
        trader_trades(sys.argv[2])
    elif cmd == "resolved-stats":
        resolved_stats()
    elif cmd == "post-expiry-trades":
        price = float(sys.argv[2]) if len(sys.argv) > 2 else 0.99
        post_expiry_trades(price)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
