#!/usr/bin/env python3
"""
Unified single-pass indexer — THE canonical on-chain indexer.

Scans each block range ONCE and fetches every event (CTF trades, Neg-Risk
trades, ConditionResolution, PayoutRedemption) together so block-timestamp
lookups are shared via a per-batch cache.

Run directly:   python unified_indexer.py
Or via the CLI: python run.py index

Error handling
--------------
If a batch fails (RPC error, DB error, ...), we do NOT advance the
``unified_last_block`` marker past the failed range. Instead we retry the
same range with exponential backoff. After INDEXER_MAX_CONSECUTIVE_FAILURES
consecutive failures we abort with a loud error, so a stuck indexer never
silently skips blocks and leaves holes in the data.
"""

import time
import traceback

from db import get_conn, ensure_conn, get_state, set_state
from indexer import (
    BatchCache,
    fetch_logs,
    get_current_block,
    process_order_filled_logs,
    process_orders_matched_logs,
    process_resolution_logs,
    process_redemption_logs,
)
from config import (
    CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE, CONDITIONAL_TOKENS,
    TOPIC_ORDER_FILLED, TOPIC_ORDERS_MATCHED,
    TOPIC_CONDITION_RESOLUTION, TOPIC_PAYOUT_REDEMPTION,
    LOG_BATCH_SIZE,
    INDEXER_MAX_CONSECUTIVE_FAILURES,
    INDEXER_BACKOFF_SECONDS,
)

STATE_KEY = "unified_last_block"
# Earliest point any data exists on either exchange.
START_BLOCK = 44_000_000


def _process_batch(conn, cache, from_block, to_block, totals):
    """Fetch + insert all event types for one block range.

    Raises on any error — the outer loop is responsible for backoff/retry.
    """
    ctf_fills = fetch_logs(CTF_EXCHANGE, [TOPIC_ORDER_FILLED], from_block, to_block)
    ctf_matches = fetch_logs(CTF_EXCHANGE, [TOPIC_ORDERS_MATCHED], from_block, to_block)
    neg_fills = fetch_logs(NEG_RISK_CTF_EXCHANGE, [TOPIC_ORDER_FILLED], from_block, to_block)
    neg_matches = fetch_logs(NEG_RISK_CTF_EXCHANGE, [TOPIC_ORDERS_MATCHED], from_block, to_block)
    res_logs = fetch_logs(CONDITIONAL_TOKENS, [TOPIC_CONDITION_RESOLUTION], from_block, to_block)
    redeem_logs = fetch_logs(CONDITIONAL_TOKENS, [TOPIC_PAYOUT_REDEMPTION], from_block, to_block)

    totals["ctf_fills"] += process_order_filled_logs(ctf_fills, "ctf", conn, cache)
    totals["ctf_matches"] += process_orders_matched_logs(ctf_matches, "ctf", conn, cache)
    totals["neg_fills"] += process_order_filled_logs(neg_fills, "neg_risk", conn, cache)
    totals["neg_matches"] += process_orders_matched_logs(neg_matches, "neg_risk", conn, cache)
    totals["resolutions"] += process_resolution_logs(res_logs, conn, cache)
    totals["redemptions"] += process_redemption_logs(redeem_logs, conn, cache)


def _backoff(attempt: int) -> int:
    """Return seconds to wait before retry ``attempt`` (1-indexed)."""
    idx = min(attempt - 1, len(INDEXER_BACKOFF_SECONDS) - 1)
    return INDEXER_BACKOFF_SECONDS[idx]


def run():
    last_block = int(get_state(STATE_KEY, str(START_BLOCK)))
    current = get_current_block()
    from_block = last_block + 1
    conn = get_conn()

    totals = {
        "ctf_fills": 0, "ctf_matches": 0,
        "neg_fills": 0, "neg_matches": 0,
        "resolutions": 0, "redemptions": 0,
    }

    print(f"Unified indexer: block {from_block:,} -> {current:,} "
          f"({current - from_block:,} blocks)")

    consecutive_failures = 0

    try:
        while from_block <= current:
            to_block = min(from_block + LOG_BATCH_SIZE - 1, current)

            # Check-and-reconnect at the top of each batch so a dead PG
            # connection (restart / idle timeout / network blip) costs one
            # iteration, not the whole process.
            conn = ensure_conn(conn)

            # Fresh cache per batch keeps memory bounded; there is no value
            # in caching block timestamps across batches.
            cache = BatchCache(conn)

            try:
                _process_batch(conn, cache, from_block, to_block, totals)
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures > INDEXER_MAX_CONSECUTIVE_FAILURES:
                    print(f"✗ Aborting after {INDEXER_MAX_CONSECUTIVE_FAILURES} "
                          f"consecutive failures. Last error: {e}")
                    traceback.print_exc()
                    raise

                wait = _backoff(consecutive_failures)
                print(f"⚠ Failure #{consecutive_failures} at blocks "
                      f"{from_block:,}-{to_block:,}: {e} — retrying in {wait}s")
                traceback.print_exc()
                try:
                    conn.rollback()
                except Exception:
                    pass
                time.sleep(wait)
                # IMPORTANT: do NOT advance from_block or set_state on failure.
                # The same range will be retried, guaranteeing no silent gaps.
                continue

            # Batch succeeded: reset failure counter, advance cursor + state.
            consecutive_failures = 0
            set_state(STATE_KEY, str(to_block))

            progress = (to_block - last_block) / max(current - last_block, 1) * 100
            fills = totals["ctf_fills"] + totals["neg_fills"]
            matches = totals["ctf_matches"] + totals["neg_matches"]
            print(f"  Block {to_block:,}/{current:,} ({progress:.1f}%) | "
                  f"fills={fills:,} matches={matches:,} "
                  f"res={totals['resolutions']:,} redeem={totals['redemptions']:,}")

            from_block = to_block + 1
            time.sleep(0.05)  # gentle rate limit on the RPC

    finally:
        try:
            conn.close()
        except Exception:
            pass

    print(f"\nDone. Totals: {totals}")


if __name__ == "__main__":
    run()
