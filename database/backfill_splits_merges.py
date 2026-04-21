#!/usr/bin/env python3
"""
One-shot backfill for PositionSplit / PositionsMerge events.

Why this exists
---------------
The unified indexer only learned about split/merge AFTER ~80M blocks of
history was already on chain. The "right" way to fill that gap is to scan
from the CTF token contract's effective start (44M) up to where the
unified indexer was when split/merge were added — and then have the
unified indexer take over the tail incremental work.

This script does the one-shot scan ONLY. It writes its own watermark in
``indexer_state.splits_merges_synced_block`` so it's resumable. It does
NOT touch ``unified_last_block``; the unified indexer owns that.

Run modes
---------
  python backfill_splits_merges.py --speed-test
       Scan a few hundred-thousand blocks, report blocks/min and event
       throughput, do NOT advance the watermark. Use this to project
       total time before committing.

  python backfill_splits_merges.py
       Continuous backfill from the current watermark up to the chain
       tip. Resumable: if interrupted, resume from the last committed
       watermark on next run. Safe to run alongside the unified indexer
       and the rollup daemon (different tables, different events, no
       lock contention).

  python backfill_splits_merges.py --to BLOCK
       Stop at a specific block (useful for handing off to unified
       indexer at a chosen merge point).

Speed
-----
The unified indexer fetches 6 event types per batch and is tuned for
LOG_BATCH_SIZE=1250 blocks on QuikNode. This script fetches only 2 event
types, both from one contract, so larger batches are safe — we use
``--batch-size`` (default 5000). Past that QuikNode starts capping log
counts per response, which forces us to bisect the range and hurts
throughput.
"""

import argparse
import time
import sys

from db import get_conn, ensure_conn, get_state
from indexer import (
    BatchCache, fetch_logs, get_current_block,
    process_position_split_logs, process_positions_merge_logs,
)
from config import (
    CONDITIONAL_TOKENS,
    TOPIC_POSITION_SPLIT, TOPIC_POSITIONS_MERGE,
    CONDITIONAL_TOKENS_START_BLOCK,
)

WATERMARK_KEY = "splits_merges_synced_block"
DEFAULT_BATCH_SIZE = 5000


def _read_watermark(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value::bigint FROM indexer_state WHERE key = %s",
            (WATERMARK_KEY,),
        )
        row = cur.fetchone()
    return row[0] if row else (CONDITIONAL_TOKENS_START_BLOCK - 1)


def _write_watermark(conn, block: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO indexer_state (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (WATERMARK_KEY, str(block)),
        )


def _do_batch(conn, from_block: int, to_block: int) -> tuple[int, int]:
    """Fetch + insert split + merge logs for one block range. Returns
    ``(splits_count, merges_count)`` and commits on success.

    Both inserts and the watermark advance live in one transaction so a
    crash leaves no orphaned cursor."""
    cache = BatchCache(conn)
    splits = fetch_logs(
        CONDITIONAL_TOKENS, [TOPIC_POSITION_SPLIT], from_block, to_block,
    )
    merges = fetch_logs(
        CONDITIONAL_TOKENS, [TOPIC_POSITIONS_MERGE], from_block, to_block,
    )
    n_splits = process_position_split_logs(splits, conn, cache)
    n_merges = process_positions_merge_logs(merges, conn, cache)
    _write_watermark(conn, to_block)
    conn.commit()
    return n_splits, n_merges


def _format_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


def run_speed_test(batch_size: int, num_batches: int):
    """Scan a few batches without advancing the watermark, report
    blocks/min throughput so the user can decide before committing to a
    multi-hour backfill."""
    conn = get_conn()
    try:
        watermark = _read_watermark(conn)
        current = get_current_block()
        from_block = watermark + 1
        if from_block > current:
            print("Already caught up — no speed test needed.")
            return
        # Don't run past the tip.
        scan_end = min(from_block + batch_size * num_batches - 1, current)

        print(f"Speed test: {num_batches} batches × {batch_size} blocks "
              f"starting at {from_block:,} (chain tip = {current:,})")
        print(f"  watermark will NOT advance.\n")

        # psycopg2 starts an implicit transaction on first execute(); we
        # never commit during speed test, then rollback at the end so any
        # inserted rows + watermark writes evaporate.
        total_blocks = 0
        total_splits = 0
        total_merges = 0
        total_t0 = time.time()

        for i in range(num_batches):
            b_from = from_block + i * batch_size
            b_to = min(b_from + batch_size - 1, scan_end)
            if b_from > scan_end:
                break
            t0 = time.time()
            cache = BatchCache(conn)
            splits = fetch_logs(
                CONDITIONAL_TOKENS, [TOPIC_POSITION_SPLIT], b_from, b_to,
            )
            merges = fetch_logs(
                CONDITIONAL_TOKENS, [TOPIC_POSITIONS_MERGE], b_from, b_to,
            )
            n_splits = process_position_split_logs(splits, conn, cache)
            n_merges = process_positions_merge_logs(merges, conn, cache)
            elapsed = time.time() - t0
            total_blocks += (b_to - b_from + 1)
            total_splits += n_splits
            total_merges += n_merges
            print(f"  batch {i + 1:>2d}: blocks {b_from:,}-{b_to:,} | "
                  f"splits={n_splits:>6,} merges={n_merges:>6,} | {elapsed:5.1f}s")

        # Roll back so watermark + any inserted rows go away.
        conn.rollback()

        total_elapsed = time.time() - total_t0
        bpm = total_blocks / total_elapsed * 60.0
        events_per_sec = (total_splits + total_merges) / max(total_elapsed, 1e-3)
        remaining = current - watermark
        eta_sec = remaining / max(bpm, 1) * 60.0

        print(f"\n=== Speed test summary ===")
        print(f"  scanned: {total_blocks:,} blocks in {total_elapsed:.1f}s")
        print(f"  throughput: {bpm:,.0f} blocks/min "
              f"({events_per_sec:,.0f} events/sec)")
        print(f"  events: {total_splits:,} splits + {total_merges:,} merges = "
              f"{total_splits + total_merges:,}")
        print(f"  remaining: {remaining:,} blocks "
              f"({watermark + 1:,} → {current:,})")
        print(f"  projected ETA at this rate: {_format_eta(eta_sec)}")
        print(f"\n  watermark unchanged at {watermark:,}.")
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


def run_backfill(batch_size: int, stop_at: int | None):
    """Resumable backfill. Advances watermark per batch.

    Adaptive batch sizing: on QuikNode HTTP 413 (Request Entity Too Large)
    we halve the batch size and retry the same range — densest historical
    ranges (election peak, recent activity) emit so many split/merge events
    per block that a 10K-block window can blow past the RPC's response
    size limit. The smaller size persists for the rest of the run; we
    don't try to ramp back up because it only matters for sparse early
    ranges that already finish in seconds anyway.
    """
    conn = get_conn()
    try:
        watermark = _read_watermark(conn)
        current = get_current_block()
        target = min(stop_at, current) if stop_at else current
        if watermark >= target:
            print(f"Already at or past target ({watermark:,} >= {target:,})")
            return

        from_block = watermark + 1
        total_remaining = target - watermark
        print(f"Backfill splits/merges: {from_block:,} → {target:,} "
              f"({total_remaining:,} blocks, batch_size={batch_size:,})")

        total_splits = 0
        total_merges = 0
        run_t0 = time.time()
        batches_done = 0
        # Floor on adaptive shrink — below this it's almost certainly not a
        # payload-size problem and we should fail loudly instead.
        MIN_BATCH = 250

        while from_block <= target:
            conn = ensure_conn(conn)
            to_block = min(from_block + batch_size - 1, target)
            t0 = time.time()
            try:
                n_splits, n_merges = _do_batch(conn, from_block, to_block)
            except Exception as e:
                # 413 = response payload too big → halve and retry SAME range.
                # Anything else → wait + retry as before (transient RPC issue).
                msg = str(e)
                is_413 = "413" in msg or "Request Entity Too Large" in msg
                if is_413 and batch_size > MIN_BATCH:
                    new_size = max(MIN_BATCH, batch_size // 2)
                    print(f"  ⚠ HTTP 413 at blocks {from_block:,}-{to_block:,} "
                          f"(payload too big) — shrinking batch_size "
                          f"{batch_size:,} → {new_size:,} for the rest of the run")
                    batch_size = new_size
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    continue
                print(f"  ⚠ batch {from_block:,}-{to_block:,} failed: "
                      f"{type(e).__name__}: {e} — retrying in 10s")
                try:
                    conn.rollback()
                except Exception:
                    pass
                time.sleep(10)
                continue

            elapsed = time.time() - t0
            total_splits += n_splits
            total_merges += n_merges
            batches_done += 1

            # Cumulative throughput → ETA for the REMAINING blocks.
            cum_elapsed = time.time() - run_t0
            blocks_done = to_block - watermark
            bpm = blocks_done / max(cum_elapsed, 1) * 60.0
            remaining = target - to_block
            eta_sec = remaining / max(bpm, 1) * 60.0

            # Print every batch so a tail -f shows live progress.
            print(f"  {to_block:,}/{target:,} "
                  f"({100.0 * blocks_done / total_remaining:5.1f}%) | "
                  f"+{n_splits:>5,}s/+{n_merges:>5,}m in {elapsed:5.1f}s | "
                  f"avg {bpm:,.0f} blk/min | ETA {_format_eta(eta_sec)}",
                  flush=True)

            from_block = to_block + 1

        total_elapsed = time.time() - run_t0
        print(f"\nDone. {batches_done:,} batches in {_format_eta(total_elapsed)}.")
        print(f"  inserted: {total_splits:,} splits + {total_merges:,} merges")
    finally:
        conn.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--speed-test", action="store_true",
                   help="Scan a few batches without advancing the watermark "
                        "and report throughput + projected ETA.")
    p.add_argument("--speed-test-batches", type=int, default=10,
                   help="Number of batches for the speed test (default 10).")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                   help=f"Blocks per eth_getLogs call (default {DEFAULT_BATCH_SIZE}).")
    p.add_argument("--to", type=int, default=None,
                   help="Stop at this block (default: chain tip).")
    args = p.parse_args()

    if args.speed_test:
        run_speed_test(args.batch_size, args.speed_test_batches)
    else:
        run_backfill(args.batch_size, args.to)


if __name__ == "__main__":
    main()
