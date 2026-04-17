#!/usr/bin/env python3
"""
Unified single-pass indexer — THE canonical on-chain indexer.

Scans each block range ONCE and fetches every event (CTF trades,
Neg-Risk trades, ConditionResolution, PayoutRedemption) together so
block-timestamp lookups are shared. This replaced the older parallel
per-exchange indexers.

Run directly:   python unified_indexer.py
Or via the CLI: python run.py index
"""

import time
import traceback
from db import get_conn, get_state, set_state
from indexer import (
    w3, fetch_logs, get_block_timestamp, get_current_block,
    process_order_filled_logs, process_orders_matched_logs,
    process_resolution_logs, process_redemption_logs,
    clear_batch_cache, clear_token_cache,
)
from config import (
    CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE, CONDITIONAL_TOKENS,
    TOPIC_ORDER_FILLED, TOPIC_ORDERS_MATCHED,
    TOPIC_CONDITION_RESOLUTION, TOPIC_PAYOUT_REDEMPTION,
    LOG_BATCH_SIZE,
)

STATE_KEY = "unified_last_block"
# Start from the earliest point any data exists
START_BLOCK = 44_000_000


def run():
    last_block = int(get_state(STATE_KEY, str(START_BLOCK)))
    current = get_current_block()
    from_block = last_block + 1
    conn = get_conn()

    totals = {"ctf_fills": 0, "ctf_matches": 0,
              "neg_fills": 0, "neg_matches": 0,
              "resolutions": 0, "redemptions": 0}

    print(f"Unified indexer: block {from_block:,} -> {current:,} ({current - from_block:,} blocks)")

    try:
        while from_block <= current:
            to_block = min(from_block + LOG_BATCH_SIZE - 1, current)

            try:
                # --- One pass: all events for this block range ---

                # 1) CTF Exchange trades
                ctf_fills = fetch_logs(CTF_EXCHANGE, [TOPIC_ORDER_FILLED], from_block, to_block)
                ctf_matches = fetch_logs(CTF_EXCHANGE, [TOPIC_ORDERS_MATCHED], from_block, to_block)

                # 2) Neg Risk Exchange trades
                neg_fills = fetch_logs(NEG_RISK_CTF_EXCHANGE, [TOPIC_ORDER_FILLED], from_block, to_block)
                neg_matches = fetch_logs(NEG_RISK_CTF_EXCHANGE, [TOPIC_ORDERS_MATCHED], from_block, to_block)

                # 3) Conditional Tokens: resolutions + redemptions
                res_logs = fetch_logs(CONDITIONAL_TOKENS, [TOPIC_CONDITION_RESOLUTION], from_block, to_block)
                redeem_logs = fetch_logs(CONDITIONAL_TOKENS, [TOPIC_PAYOUT_REDEMPTION], from_block, to_block)

                # --- Process all (block timestamps cached & shared across all) ---
                totals["ctf_fills"] += process_order_filled_logs(ctf_fills, "ctf", conn)
                totals["ctf_matches"] += process_orders_matched_logs(ctf_matches, "ctf", conn)
                totals["neg_fills"] += process_order_filled_logs(neg_fills, "neg_risk", conn)
                totals["neg_matches"] += process_orders_matched_logs(neg_matches, "neg_risk", conn)
                totals["resolutions"] += process_resolution_logs(res_logs, conn)
                totals["redemptions"] += process_redemption_logs(redeem_logs, conn)

                # Free memory - no need to cache across batches
                clear_batch_cache()
                clear_token_cache()

            except Exception as e:
                print(f"  Error at {from_block}-{to_block}: {e}")
                traceback.print_exc()
                time.sleep(3)
                from_block = to_block + 1
                set_state(STATE_KEY, str(to_block))
                continue

            set_state(STATE_KEY, str(to_block))

            progress = (to_block - last_block) / max(current - last_block, 1) * 100
            fills = totals['ctf_fills'] + totals['neg_fills']
            matches = totals['ctf_matches'] + totals['neg_matches']
            print(f"  Block {to_block:,}/{current:,} ({progress:.1f}%) | "
                  f"fills={fills:,} matches={matches:,} "
                  f"res={totals['resolutions']:,} redeem={totals['redemptions']:,}")

            from_block = to_block + 1
            time.sleep(0.05)

    finally:
        conn.close()

    print(f"\nDone. Totals: {totals}")


if __name__ == "__main__":
    run()
