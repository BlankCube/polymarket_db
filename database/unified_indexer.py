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

import argparse
import time
import traceback

from db import get_conn, ensure_conn, get_state
from indexer import (
    BatchCache,
    fetch_logs,
    get_current_block,
    process_order_filled_logs,
    process_orders_matched_logs,
    process_resolution_logs,
    process_redemption_logs,
    process_position_split_logs,
    process_positions_merge_logs,
)
from config import (
    CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE, CONDITIONAL_TOKENS,
    TOPIC_ORDER_FILLED, TOPIC_ORDERS_MATCHED,
    TOPIC_CONDITION_RESOLUTION, TOPIC_PAYOUT_REDEMPTION,
    TOPIC_POSITION_SPLIT, TOPIC_POSITIONS_MERGE,
    LOG_BATCH_SIZE,
    INDEXER_MAX_CONSECUTIVE_FAILURES,
    INDEXER_BACKOFF_SECONDS,
)

STATE_KEY = "unified_last_block"
# Earliest point any data exists on either exchange.
START_BLOCK = 44_000_000


def _process_batch(conn, cache, from_block, to_block, totals):
    """Fetch + insert all event types for one block range, then advance
    ``indexer_state`` atomically in the same transaction.

    All six event-type inserts and the ``unified_last_block`` update live in
    one transaction so a crash between them can never leave
    ``max(order_fills.block_number) > indexer_state.unified_last_block``
    (which would cause redundant RPC re-scans on restart and, more importantly,
    violates the invariant that committed data is reflected in the cursor).

    Raises on any error — the outer loop is responsible for rollback +
    backoff/retry.

    NOTE on parallelism: we tried a ThreadPoolExecutor to run the fetches
    concurrently — counter-intuitively it made batches MUCH slower. Empirically
    QuikNode serializes heavy eth_getLogs calls per client; parallel large
    requests end up queuing server-side while also contending for the client
    TCP pool. Sequential with no sleep between batches beats it.

    NOTE on splits/merges: these used to have their own backfill daemon
    (``backfill_splits_merges.py``) with a separate ``splits_merges_synced_block``
    watermark. After the backfill caught up to ``unified_last_block`` on
    2026-04-22 we folded both event types into the unified batch so a single
    cursor drives every on-chain write. The standalone backfill script is
    retained only as a historical reference; do not re-run it.
    """
    ctf_fills = fetch_logs(CTF_EXCHANGE, [TOPIC_ORDER_FILLED], from_block, to_block)
    ctf_matches = fetch_logs(CTF_EXCHANGE, [TOPIC_ORDERS_MATCHED], from_block, to_block)
    neg_fills = fetch_logs(NEG_RISK_CTF_EXCHANGE, [TOPIC_ORDER_FILLED], from_block, to_block)
    neg_matches = fetch_logs(NEG_RISK_CTF_EXCHANGE, [TOPIC_ORDERS_MATCHED], from_block, to_block)
    res_logs = fetch_logs(CONDITIONAL_TOKENS, [TOPIC_CONDITION_RESOLUTION], from_block, to_block)
    redeem_logs = fetch_logs(CONDITIONAL_TOKENS, [TOPIC_PAYOUT_REDEMPTION], from_block, to_block)
    split_logs = fetch_logs(CONDITIONAL_TOKENS, [TOPIC_POSITION_SPLIT], from_block, to_block)
    merge_logs = fetch_logs(CONDITIONAL_TOKENS, [TOPIC_POSITIONS_MERGE], from_block, to_block)

    totals["ctf_fills"] += process_order_filled_logs(ctf_fills, "ctf", conn, cache)
    totals["ctf_matches"] += process_orders_matched_logs(ctf_matches, "ctf", conn, cache)
    totals["neg_fills"] += process_order_filled_logs(neg_fills, "neg_risk", conn, cache)
    totals["neg_matches"] += process_orders_matched_logs(neg_matches, "neg_risk", conn, cache)
    totals["resolutions"] += process_resolution_logs(res_logs, conn, cache)
    totals["redemptions"] += process_redemption_logs(redeem_logs, conn, cache)
    totals["splits"] += process_position_split_logs(split_logs, conn, cache)
    totals["merges"] += process_positions_merge_logs(merge_logs, conn, cache)

    # Stage the cursor update on the same connection so it commits (or rolls
    # back) atomically with the six inserts above.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO indexer_state (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (STATE_KEY, str(to_block)),
        )
    conn.commit()


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
        "splits": 0, "merges": 0,
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

            # Batch succeeded (fills + state were committed atomically inside
            # _process_batch). Reset failure counter and advance the cursor.
            consecutive_failures = 0

            progress = (to_block - last_block) / max(current - last_block, 1) * 100
            fills = totals["ctf_fills"] + totals["neg_fills"]
            matches = totals["ctf_matches"] + totals["neg_matches"]
            print(f"  Block {to_block:,}/{current:,} ({progress:.1f}%) | "
                  f"fills={fills:,} matches={matches:,} "
                  f"res={totals['resolutions']:,} redeem={totals['redemptions']:,} "
                  f"split={totals['splits']:,} merge={totals['merges']:,}")

            from_block = to_block + 1

    finally:
        try:
            conn.close()
        except Exception:
            pass

    print(f"\nDone. Totals: {totals}")


def run_loop(interval_sec: int):
    """Daemon: run() to catch-up, then poll the chain every ``interval_sec``
    seconds for new blocks. Mirrors ``rollup.py --loop``.

    Each pass is one invocation of ``run()``, which exits when caught up to
    the tip observed at pass-start. Between passes we sleep so we don't hammer
    the RPC while the chain is adding ~30 blocks/min.

    On Polygon at 2 s/block, ``interval_sec=30`` keeps us within ~15 new
    blocks behind the head on average (one batch's worth). Go shorter for
    tighter tailing; longer to reduce RPC usage.
    """
    print(f"Unified indexer daemon starting (interval={interval_sec}s)", flush=True)
    while True:
        try:
            run()
        except Exception as e:
            print(f"⚠ run() crashed: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
        time.sleep(interval_sec)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--loop", nargs="?", const=30, type=int,
                   help="Run as daemon; optional interval in seconds "
                        "(default 30). Without --loop, exits on catch-up.")
    args = p.parse_args()

    if args.loop is not None:
        run_loop(args.loop)
    else:
        run()


if __name__ == "__main__":
    main()
