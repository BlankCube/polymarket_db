"""Database connections for the chat webapp.

Two access patterns live here:

1. Async read-only pool (asyncpg) using the analyst (polymarket_ro) role —
   used by AI-generated SQL. Writes are denied at the database layer, not
   just by session-level default_transaction_read_only. The session setting
   is kept as a belt-and-braces measure.

2. Sync writer connection factory (psycopg2) using the owner role — used by
   auth.py and sessions_repo.py for short, bounded operations called from
   async route handlers. See sessions_repo.py docstring for the rationale
   for keeping these sync.
"""

import asyncpg
import psycopg2

from config import (
    DB_ANALYST_DSN, DB_POOL_MIN, DB_POOL_MAX, DB_STATEMENT_TIMEOUT_MS,
    DB_PARAMS,
)

_pool = None


async def init_pool():
    global _pool
    _pool = await asyncpg.create_pool(
        DB_ANALYST_DSN,
        min_size=DB_POOL_MIN,
        max_size=DB_POOL_MAX,
        command_timeout=DB_STATEMENT_TIMEOUT_MS / 1000 + 20,
        init=_init_connection,
    )


async def _init_connection(conn):
    await conn.execute(f"SET statement_timeout = '{DB_STATEMENT_TIMEOUT_MS}'")
    await conn.execute("SET default_transaction_read_only = on")


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()


async def execute_query(sql: str) -> tuple[list[str], list[list]]:
    """Execute a read-only query. Returns (column_names, rows)."""
    async with _pool.acquire() as conn:
        stmt = await conn.prepare(sql)
        columns = [attr.name for attr in stmt.get_attributes()]
        rows = await stmt.fetch()
        return columns, [list(r.values()) for r in rows]


def get_sync_conn():
    """Open a sync psycopg2 connection with the owner (writer) role.

    Callers are responsible for closing the connection. Used by auth.py and
    sessions_repo.py.
    """
    return psycopg2.connect(**DB_PARAMS)
