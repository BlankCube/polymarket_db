"""Async database pool for read-only queries."""

import asyncpg

DB_DSN = "postgresql://polymarket:polymarket123@localhost:5432/polymarket_db"

_pool = None


async def init_pool():
    global _pool
    _pool = await asyncpg.create_pool(
        DB_DSN,
        min_size=1,
        max_size=3,
        command_timeout=300,
        init=_init_connection,
    )


async def _init_connection(conn):
    await conn.execute("SET statement_timeout = '280000'")
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


