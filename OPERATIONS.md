# Operations Manual

Day-to-day runbook for the Polymarket Explorer stack. **Read this before
restarting anything**—several daemons have subtle lifecycle issues (PID
tracking via bash wrappers, port 8080 reuse, etc.) that the patterns
below work around.

See `PRODUCT.md` for the product vision + improvement methodology.
See `database/ROLLUPS.md` for the rollup schema deep-dive.
See `HANDOFF.md` for the "what was I working on last time" pointer.

---

## The five processes

All run as plain `nohup python … &` under user `ubuntu`. **No systemd,
no cron.** Reboots require manual restart (see `Cold start` below).

| # | Process | PID file mechanism | Logs | Purpose |
|---|---|---|---|---|
| 1 | `unified_indexer.py` | none (track via `ps`) | `indexer_unified.log` | Incrementally pulls CTF / Neg-Risk trades, matches, resolutions, redemptions, **splits, merges** from Polygon RPC into PG (8 event types) |
| 2 | `rollup.py --loop 60` | none | `rollup.log` | Every 60 s, rolls new `order_fills` + `redemptions` into 5 aggregate tables (A–F) |
| 3 | `uvicorn app:app --port 8080 --ssl-*` | none | `webapp.log` | FastAPI chat UI + `/api/execution/<id>/csv` download endpoint |
| 4 | PostgreSQL 16 | system service | `/var/log/postgresql/…` | `polymarket_db` |

**Retired 2026-04-22:** `backfill_splits_merges.py` (one-shot) finished catching `position_splits`/`position_merges` up past `unified_last_block`; the 2 topic filters are now part of `_process_batch` and the `splits_merges_synced_block` watermark row has been deleted from `indexer_state`. The script is kept for reference only — do not re-run.

All Python daemons use `/home/ubuntu/polymarket-db/venv/bin/python`.
**Never use system `python3`** — it lacks psycopg2 / anthropic / web3.

---

## Status snapshot

Copy-paste this block to get one-page health:

```bash
echo "=== daemons ==="
ps -ef | grep -v grep | grep -E 'python.*(unified_indexer|rollup\.py|uvicorn app)' \
    | awk '{printf "  %-7s %-9s %s %s %s\n", $2, $7, $8, $9, $10}'

echo
echo "=== watermarks ==="
/home/ubuntu/polymarket-db/venv/bin/python <<'PY'
import sys, datetime
sys.path.insert(0, '/home/ubuntu/polymarket-db/database')
from db import get_conn
c = get_conn()
with c.cursor() as cur:
    cur.execute("""SELECT key, value, updated_at FROM indexer_state
                   WHERE key IN ('unified_last_block','rollup_synced_block')
                   ORDER BY key""")
    for k, v, t in cur.fetchall():
        age = (datetime.datetime.now(t.tzinfo) - t).total_seconds()
        age_s = f'{age:.0f}s' if age < 90 else (f'{age/60:.1f}m' if age < 3600 else f'{age/3600:.1f}h')
        print(f'  {k:30s} = {int(v):>13,}   updated {age_s:>6s} ago')
c.close()
PY

echo
echo "=== logs (last line of each) ==="
for f in indexer_unified.log rollup.log webapp.log; do
    echo "--- $f ---"
    tail -1 /home/ubuntu/polymarket-db/$f
done
```

Healthy indicators:

- `unified_last_block` age < ~5 min (daemon still making forward progress)
- `rollup_synced_block` within 1–2 batches of `unified_last_block` (≤ 2500 blocks)
- webapp `GET /api/example_questions?count=1` → HTTP 200 in < 100 ms

---

## Starting / restarting each process

### Golden rule: `pgrep -f` matches the bash wrapper too

`nohup python …` leaves a bash wrapper PID and a python PID. `pgrep -f
'uvicorn app:app'` matches both. The **bash wrapper exits on its own**
after the Python is launched — killing it is harmless but doesn't stop
the daemon. Target the Python PID directly:

```bash
ps -ef | grep -v grep | grep 'python.*uvicorn app:app' | awk '{print $2}'
# or for the indexer / rollup / backfill:
ps -ef | grep -v grep | grep 'python.*unified_indexer' | awk '{print $2}'
```

### Port 8080 release on webapp restart

After `kill -9 <python_pid>`, port 8080 can linger for ~1 s. If you
`nohup uvicorn …` too fast, it dies with `[Errno 98] address already in
use`. Always verify the port is free before relaunch:

```bash
ss -tlnp 2>/dev/null | grep :8080 || echo "port free"
```

### Cold start (after reboot or full shutdown)

Run in order, one at a time, check each came up before the next:

```bash
cd /home/ubuntu/polymarket-db

# 1. Indexer
nohup venv/bin/python -u database/unified_indexer.py >> indexer_unified.log 2>&1 &
sleep 5 && tail -3 indexer_unified.log

# 2. Rollup daemon
nohup venv/bin/python -u database/rollup.py --loop 60 >> rollup.log 2>&1 &
sleep 5 && tail -3 rollup.log

# 3. Webapp (retired: the standalone splits/merges backfill — unified_indexer
#    now handles those two event types itself)
cd /home/ubuntu/polymarket-db/chat
nohup ../venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080 \
    --ssl-keyfile ../certs/key.pem --ssl-certfile ../certs/cert.pem \
    >> ../webapp.log 2>&1 &
sleep 5 && tail -3 ../webapp.log && \
    curl -sk https://localhost:8080/ -o /dev/null -w "  GET / → %{http_code}\n"
```

### Webapp-only restart (the common case — prompt / code changes)

```bash
# Kill
PID=$(ps -ef | grep -v grep | grep 'python.*uvicorn app:app' | awk '{print $2}')
[ -n "$PID" ] && kill -9 $PID
sleep 2
ss -tlnp 2>/dev/null | grep :8080 && echo "WAIT — port still held" || echo "port free"

# Restart
cd /home/ubuntu/polymarket-db/chat
nohup ../venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080 \
    --ssl-keyfile ../certs/key.pem --ssl-certfile ../certs/cert.pem \
    >> ../webapp.log 2>&1 &

sleep 3
curl -sk https://localhost:8080/ -o /dev/null -w "  GET / → %{http_code} in %{time_total}s\n"
```

When you change static assets (`app.js`, `app.css`), also **bump the
`v=` cache-bust query** in `chat/static/index.html`
(e.g. `v=2026-04-21-1 → 2026-04-21-2`). The `NoCacheStaticFiles` mount
sets `no-cache` headers but bumping the version forces immediate reload
even for misbehaving proxies.

### Indexer / rollup restart

```bash
# Indexer
kill -TERM $(ps -ef | grep -v grep | grep 'python.*unified_indexer' | awk '{print $2}')
sleep 2
cd /home/ubuntu/polymarket-db
nohup venv/bin/python -u database/unified_indexer.py >> indexer_unified.log 2>&1 &

# Rollup
kill -TERM $(ps -ef | grep -v grep | grep 'python.*rollup\.py' | awk '{print $2}')
sleep 2
nohup venv/bin/python -u database/rollup.py --loop 60 >> rollup.log 2>&1 &
```

Both are **crash-safe**: they read watermarks from `indexer_state` and
resume. Never write data past their watermark.

---

## Log files — what's where

| File | Writer | Format | Size management |
|---|---|---|---|
| `indexer_unified.log` | unified_indexer | plain text, one batch per line | Unbounded. `echo -n > file` to truncate while running (preserves fd) |
| `rollup.log` | rollup daemon | plain text, per-cycle | Same |
| `webapp.log` | uvicorn stdout | Starlette INFO lines + tracebacks | Same truncation rule |
| `feedback/logs/chat.jsonl` | app.py logger | JSONL — one event per AI stage / tool call | Truncate **in place** with `> file` so the live uvicorn fd stays valid (see `MEMORY.md`: `mv` breaks fd, writes orphan) |
| `feedback/logs/chat.archive.jsonl` | manual | append-only | Cold archive of analyzed sessions. Keep forever |
| `feedback/logs/errors.log` | error_logger | one traceback per line | Truncate in place |

**Log-rotation hard rule** (bitten by this before): for files held open by a
running process, always `> file` (truncate). `mv file file.old && touch file`
breaks the fd and subsequent writes land in the orphan inode.

### Archiving chat sessions after analysis

After analyzing sessions with the `improvement-analyst` subagent, move
them to `chat.archive.jsonl` so the next analysis doesn't re-flag them:

```bash
cat /home/ubuntu/polymarket-db/feedback/logs/chat.jsonl >> \
    /home/ubuntu/polymarket-db/feedback/logs/chat.archive.jsonl
> /home/ubuntu/polymarket-db/feedback/logs/chat.jsonl
```

---

## Database (brief; deep-dive in `database/ROLLUPS.md`)

- Host: `localhost:5432`, DB: `polymarket_db`
- Writer role: `polymarket` (password in `.env`)
- Read-only role: `polymarket_ro` (used by AI-generated SQL via `db_pool.py`)
- Creds: `/home/ubuntu/polymarket-db/.env` (mode 600; never commit)

### Tables, live sizes

- `markets` ~757 K rows (Gamma metadata; full platform coverage)
- `order_fills` ~200 M rows (CTF + Neg-Risk fills)
- `order_matches` ~50 M rows
- `redemptions` ~16 M rows
- `position_splits` ~136 M rows / `position_merges` ~23 M rows (kept current by `unified_indexer`)
- 5 rollups: `wallet_volume_rollup` (1.8 M) / `market_volume_rollup` (~223 K) / `wallet_market_pairs` (~34 M) / `wallet_monthly_stats` (~5 M) / `market_monthly_stats` (~214 K)
- App tables: `users`, `user_sessions`, `session_executions`, `indexer_state`

### Ad-hoc SQL

```bash
PGPASSWORD=$(grep PM_DB_PASSWORD /home/ubuntu/polymarket-db/.env | cut -d= -f2) \
    psql -h localhost -U polymarket polymarket_db
```

---

## Gamma-API metadata sync (manual / on-demand)

Not a daemon. Run occasionally (or when markets look stale):

```bash
cd /home/ubuntu/polymarket-db/database
../venv/bin/python run.py sync-markets       # refresh markets table
../venv/bin/python sync_categories.py        # refresh market.category from /events
```

Safe to run alongside indexer + rollup; only touches `markets` upserts.

---

## Instance resize checklist

The host (currently **r6i.2xlarge**, 8 vCPU / 64 GB RAM) is sized for
catch-up indexing — Polymarket-active block ranges drive nvme to ~90%
utilization, while CPU sits at ~50% idle. Once we're caught up to chain
tip and only doing live-tail (~30 blk/min on Polygon), the instance is
massively oversized.

**Don't downsize during catch-up.** EBS baseline IOPS / throughput is
proportional to instance size — going smaller cuts disk-bandwidth
exactly when we need it most.

**After catch-up + steady live-tail for 1-2 weeks**, evaluate by
measuring real working set:

```bash
# CPU + iowait
mpstat 1 60 | tail -2 | head -1
# Steady-state memory + cache
free -h
# Disk pressure
iostat -x 1 30 | grep nvme0n1 | tail -10
```

If sustained: <20% user CPU, <30% iowait, <40 GB working set, then
downsize is safe. Recommended steps (us-west-2 on-demand, approx):

| target          | vCPU/RAM   | $/mo  | save | required tweaks |
|-----------------|------------|-------|------|----------------|
| r6i.2xlarge     | 8 / 64 GB  | $363  | —    | (current)       |
| **r6i.xlarge**  | 4 / 32 GB  | $182  | 50%  | drop `shared_buffers` 24→12 GB; restart PG |
| m6i.xlarge      | 4 / 16 GB  | $138  | 62%  | drop `shared_buffers` 24→6 GB; raises miss rate, only OK if total DB hot set < 16 GB |

**Procedure for r6i.2xlarge → r6i.xlarge (the safe step):**

1. `kill -TERM` indexer + rollup; let webapp drain.
2. Stop the EC2 instance.
3. AWS console: change instance type → `r6i.xlarge`.
4. Start instance.
5. Edit `postgresql.conf`: `shared_buffers = 12GB`, `effective_cache_size = 24GB`.
6. `sudo systemctl restart postgresql`.
7. Restart daemons per the "Cold start" section above.
8. Watch nvme + load for 30 min. If iowait stays <50% and rollup keeps
   `lag = 0`, we're fine. If not, resize back up.

Reverse path (xlarge → 2xlarge) is the same flow without the
`postgresql.conf` edit; bigger `shared_buffers` is set on next planned
restart.

---

## Deferred work — read before making changes

`feedback/deferred_improvements.md` is the honest backlog. Each entry
has evidence, reason-for-defer, and a "revisit when" trigger. Before
proposing a fix, grep this file; if the issue is already logged, the
answer is "already queued" until the trigger fires.

As of 2026-04-21 the queue holds 5 entries (÷1e6 split; wallet volume
rollup — done; heuristic misfire on `ok`; step3 scope pivot; CSV vs
summary aggregate).

---

## The `improvement-analyst` subagent — use it for log review

Location: `.claude/agents/improvement-analyst.md`.

**Always use this agent**, not inline grep, when the user asks to "look
at the log" / "analyze the latest session" / "see what's going wrong".
Inline analysis skips the deferred-queue check and mis-flags already-
known issues as new bugs.

Usage from Claude: `Agent(subagent_type="improvement-analyst", prompt="...")`.

The agent reads `PRODUCT.md` methodology, then `deferred_improvements.md`,
then `chat.jsonl`. It applies the N≥2 rule, the 2-week horizon check, and
returns a structured verdict per issue (do-now / wait / defer / queued /
decline). It does NOT write code.

After running the agent + acting on its verdicts, archive the analyzed
sessions (see above) so the next run sees only fresh logs.
