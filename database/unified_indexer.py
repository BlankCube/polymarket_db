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
from concurrent.futures import ThreadPoolExecutor

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


# ===========================================================================
# Parallel variant
# ===========================================================================
#
# Single-threaded _process_batch is CPU-bound per batch: one PG backend pinned
# at 100% doing B-tree maintenance and one Python process pinned decoding logs.
# On an 8-vCPU box this uses 2 cores out of 8. Parallelising the INSERT phase
# across worker connections lets 4-6 cores actually work.
#
# Safety: every INSERT uses ``ON CONFLICT (tx_hash, log_index) DO NOTHING``, so
# re-inserting the same row is a no-op. That makes the old "one atomic
# transaction for all 8 event types + watermark" invariant unnecessary — we
# can commit each worker independently and still have correctness:
#
#   * If any worker fails, the whole batch is considered failed and the
#     watermark does NOT advance; the outer retry loop re-runs the same range
#     and the already-committed rows get ON CONFLICT-skipped.
#   * If Python crashes after some workers commit but before the watermark
#     update, same story on restart.
#
# Jobs are grouped BY TARGET TABLE so no two workers ever write the same table
# concurrently (avoids B-tree leaf contention):
#
#   W1  ctf_fills + neg_fills           → order_fills
#   W2  ctf_matches + neg_matches       → order_matches
#   W3  resolution + redemption         → resolutions + redemptions
#   W4  split + merge                   → position_splits + position_merges
#
# Fetches stay sequential — QuikNode serializes heavy eth_getLogs per client,
# parallel RPC makes things slower (verified before, see _process_batch
# docstring).


def _job_order_fills(conn, ctf_logs, neg_logs):
    cache = BatchCache(conn)
    n_ctf = process_order_filled_logs(ctf_logs, "ctf", conn, cache)
    n_neg = process_order_filled_logs(neg_logs, "neg_risk", conn, cache)
    conn.commit()
    return {"ctf_fills": n_ctf, "neg_fills": n_neg}


def _job_order_matches(conn, ctf_logs, neg_logs):
    cache = BatchCache(conn)
    n_ctf = process_orders_matched_logs(ctf_logs, "ctf", conn, cache)
    n_neg = process_orders_matched_logs(neg_logs, "neg_risk", conn, cache)
    conn.commit()
    return {"ctf_matches": n_ctf, "neg_matches": n_neg}


def _job_ct_events(conn, res_logs, redeem_logs):
    cache = BatchCache(conn)
    n_res = process_resolution_logs(res_logs, conn, cache)
    n_red = process_redemption_logs(redeem_logs, conn, cache)
    conn.commit()
    return {"resolutions": n_res, "redemptions": n_red}


def _job_positions(conn, split_logs, merge_logs):
    cache = BatchCache(conn)
    n_s = process_position_split_logs(split_logs, conn, cache)
    n_m = process_positions_merge_logs(merge_logs, conn, cache)
    conn.commit()
    return {"splits": n_s, "merges": n_m}


def _process_batch_parallel(main_conn, worker_conns, pool,
                            from_block, to_block, totals):
    """Parallel variant of ``_process_batch``. Four worker connections handle
    the INSERTs concurrently, one per target-table group. Watermark still
    advances on the main connection, ONLY after all 4 workers commit.

    Raises on any worker failure — outer loop handles rollback/retry.
    """
    # Fetch all 8 event types sequentially (parallel RPC hurts QuikNode).
    ctf_fills = fetch_logs(CTF_EXCHANGE, [TOPIC_ORDER_FILLED], from_block, to_block)
    ctf_matches = fetch_logs(CTF_EXCHANGE, [TOPIC_ORDERS_MATCHED], from_block, to_block)
    neg_fills = fetch_logs(NEG_RISK_CTF_EXCHANGE, [TOPIC_ORDER_FILLED], from_block, to_block)
    neg_matches = fetch_logs(NEG_RISK_CTF_EXCHANGE, [TOPIC_ORDERS_MATCHED], from_block, to_block)
    res_logs = fetch_logs(CONDITIONAL_TOKENS, [TOPIC_CONDITION_RESOLUTION], from_block, to_block)
    redeem_logs = fetch_logs(CONDITIONAL_TOKENS, [TOPIC_PAYOUT_REDEMPTION], from_block, to_block)
    split_logs = fetch_logs(CONDITIONAL_TOKENS, [TOPIC_POSITION_SPLIT], from_block, to_block)
    merge_logs = fetch_logs(CONDITIONAL_TOKENS, [TOPIC_POSITIONS_MERGE], from_block, to_block)

    # Ensure each worker conn is alive before handing it to a thread.
    for i in range(4):
        worker_conns[i] = ensure_conn(worker_conns[i])

    futures = [
        pool.submit(_job_order_fills,   worker_conns[0], ctf_fills,   neg_fills),
        pool.submit(_job_order_matches, worker_conns[1], ctf_matches, neg_matches),
        pool.submit(_job_ct_events,     worker_conns[2], res_logs,    redeem_logs),
        pool.submit(_job_positions,     worker_conns[3], split_logs,  merge_logs),
    ]
    # .result() re-raises any worker exception; as_completed is tempting but
    # we want to wait for ALL to finish (so partial commits already landed
    # can be accounted for) before deciding the batch failed.
    errors = []
    partial_totals = {}
    for f in futures:
        try:
            partial_totals.update(f.result())
        except Exception as e:
            errors.append(e)
    if errors:
        # Merge whatever deltas did succeed into totals so logging is honest,
        # then raise. Outer loop won't advance the watermark → retry next run
        # will re-insert these rows as ON CONFLICT no-ops.
        for k, v in partial_totals.items():
            totals[k] += v
        raise errors[0]
    for k, v in partial_totals.items():
        totals[k] += v

    # All workers committed. Advance watermark on main conn.
    with main_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO indexer_state (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (STATE_KEY, str(to_block)),
        )
    main_conn.commit()


def _backoff(attempt: int) -> int:
    """Return seconds to wait before retry ``attempt`` (1-indexed)."""
    idx = min(attempt - 1, len(INDEXER_BACKOFF_SECONDS) - 1)
    return INDEXER_BACKOFF_SECONDS[idx]


def run(workers: int = 1, stop_at: int | None = None):
    """Catch-up scan from ``unified_last_block`` to chain tip.

    ``workers``:
        1 → single-threaded path (historical behaviour).
        ≥2 → parallel path: all 8 event fetches sequential (QuikNode
             constraint), INSERTs fanned out across 4 worker connections
             grouped by target table. See ``_process_batch_parallel`` for
             the safety argument.

    ``stop_at``:
        If set, exit cleanly after committing the batch that contains this
        block — even if the chain tip is further ahead. Used to halt at the
        2026-04-28 V1→V2 cutover (block 86_126_998) until the V2 decoders
        are in place; without it the indexer would advance the watermark
        across cutover into V2-only territory and silently miss those
        events. Combined with ``--loop``, the loop also exits once stop_at
        is hit so the daemon doesn't busy-poll an idle target.
    """
    last_block = int(get_state(STATE_KEY, str(START_BLOCK)))
    current = get_current_block()
    if stop_at is not None:
        current = min(current, stop_at)
    from_block = last_block + 1
    conn = get_conn()

    # Parallel path uses 4 persistent worker connections + a thread pool.
    # Fixed at 4 because we group work by target table (see
    # _process_batch_parallel); more workers would just sit idle.
    parallel = workers >= 2
    worker_conns = [get_conn() for _ in range(4)] if parallel else []
    pool = ThreadPoolExecutor(max_workers=4) if parallel else None

    totals = {
        "ctf_fills": 0, "ctf_matches": 0,
        "neg_fills": 0, "neg_matches": 0,
        "resolutions": 0, "redemptions": 0,
        "splits": 0, "merges": 0,
    }

    mode = f"parallel x4 workers" if parallel else "single-threaded"
    print(f"Unified indexer ({mode}): block {from_block:,} -> {current:,} "
          f"({current - from_block:,} blocks)")

    consecutive_failures = 0

    try:
        while from_block <= current:
            to_block = min(from_block + LOG_BATCH_SIZE - 1, current)

            # Check-and-reconnect at the top of each batch so a dead PG
            # connection (restart / idle timeout / network blip) costs one
            # iteration, not the whole process.
            conn = ensure_conn(conn)

            try:
                if parallel:
                    _process_batch_parallel(
                        conn, worker_conns, pool,
                        from_block, to_block, totals,
                    )
                else:
                    # Fresh cache per batch keeps memory bounded.
                    cache = BatchCache(conn)
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
                # Worker conns may have committed partial data; that's OK
                # (ON CONFLICT DO NOTHING on retry). But roll back any stray
                # in-progress transaction so the conn is usable next iter.
                for wc in worker_conns:
                    try:
                        wc.rollback()
                    except Exception:
                        pass
                time.sleep(wait)
                # IMPORTANT: do NOT advance from_block or set_state on failure.
                # The same range will be retried, guaranteeing no silent gaps.
                continue

            # Batch succeeded. Reset failure counter and advance the cursor.
            consecutive_failures = 0

            progress = (to_block - last_block) / max(current - last_block, 1) * 100
            fills = totals["ctf_fills"] + totals["neg_fills"]
            matches = totals["ctf_matches"] + totals["neg_matches"]
            print(f"  Block {to_block:,}/{current:,} ({progress:.1f}%) | "
                  f"fills={fills:,} matches={matches:,} "
                  f"res={totals['resolutions']:,} redeem={totals['redemptions']:,} "
                  f"split={totals['splits']:,} merge={totals['merges']:,}",
                  flush=True)

            from_block = to_block + 1

    finally:
        if pool is not None:
            pool.shutdown(wait=False)
        for wc in worker_conns:
            try:
                wc.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

    print(f"\nDone. Totals: {totals}")


def run_loop(interval_sec: int, workers: int = 1, stop_at: int | None = None):
    """Daemon: run() to catch-up, then poll the chain every ``interval_sec``
    seconds for new blocks. Mirrors ``rollup.py --loop``.

    Each pass is one invocation of ``run()``, which exits when caught up to
    the tip observed at pass-start. Between passes we sleep so we don't hammer
    the RPC while the chain is adding ~30 blocks/min.

    On Polygon at 2 s/block, ``interval_sec=30`` keeps us within ~15 new
    blocks behind the head on average (one batch's worth). Go shorter for
    tighter tailing; longer to reduce RPC usage.

    If ``stop_at`` is set and the current watermark already meets/exceeds it,
    the loop exits — pointless to busy-poll for a frozen target.
    """
    print(f"Unified indexer daemon starting (interval={interval_sec}s, "
          f"workers={workers}, stop_at={stop_at})", flush=True)
    while True:
        try:
            run(workers=workers, stop_at=stop_at)
        except Exception as e:
            print(f"⚠ run() crashed: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
        # Exit the loop once we've hit the stop-at boundary, so we don't
        # spin every ``interval_sec`` against a target that won't move.
        if stop_at is not None:
            try:
                synced = int(get_state(STATE_KEY, str(START_BLOCK)))
                if synced >= stop_at:
                    print(f"Reached stop_at={stop_at:,} (synced={synced:,}); "
                          f"exiting daemon.", flush=True)
                    return
            except Exception:
                pass
        time.sleep(interval_sec)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--loop", nargs="?", const=30, type=int,
                   help="Run as daemon; optional interval in seconds "
                        "(default 30). Without --loop, exits on catch-up.")
    p.add_argument("--workers", type=int, default=1,
                   help="Number of parallel INSERT workers (default 1 = "
                        "single-threaded historical behaviour). Set to 2+ to "
                        "fan INSERTs out across 4 worker connections grouped "
                        "by target table. See _process_batch_parallel for "
                        "the safety argument.")
    p.add_argument("--stop-at", type=int, default=None,
                   help="Exit cleanly after committing the batch containing "
                        "this block. Used to halt at the V1→V2 cutover "
                        "(block 86_126_998) until V2 decoders ship.")
    args = p.parse_args()

    if args.loop is not None:
        run_loop(args.loop, workers=args.workers, stop_at=args.stop_at)
    else:
        run(workers=args.workers, stop_at=args.stop_at)


if __name__ == "__main__":
    main()
