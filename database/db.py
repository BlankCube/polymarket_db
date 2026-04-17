"""Database connection helpers."""

import psycopg2
import psycopg2.extras
from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD


def get_conn():
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
        user=DB_USER, password=DB_PASSWORD
    )


def ensure_conn(conn):
    """Return a usable connection; transparently reconnect if the current one
    is dead (PG restarted, network blip, idle timeout).

    Call this at the top of every indexer loop iteration so a connection-level
    failure costs you one batch, not the whole process.
    """
    try:
        # Lightweight round-trip that fails fast on a broken connection.
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return conn
    except (psycopg2.InterfaceError, psycopg2.OperationalError):
        try:
            conn.close()
        except Exception:
            pass
        return get_conn()


def get_state(key, default=None):
    """Get indexer state value."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM indexer_state WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else default
    finally:
        conn.close()


def set_state(key, value):
    """Set indexer state value."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO indexer_state (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = NOW()
            """, (key, str(value), str(value)))
        conn.commit()
    finally:
        conn.close()


def delete_state(keys):
    """Remove one or more stale indexer_state rows by key."""
    if not keys:
        return 0
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM indexer_state WHERE key = ANY(%s)", (list(keys),))
            deleted = cur.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


def bulk_upsert(table, rows, conflict_cols, update_cols=None):
    """Generic bulk upsert using psycopg2.extras.execute_values."""
    if not rows:
        return 0
    cols = list(rows[0].keys())
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            values = [[row[c] for c in cols] for row in rows]
            col_str = ", ".join(cols)
            conflict_str = ", ".join(conflict_cols)
            if update_cols:
                update_str = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
                sql = f"""
                    INSERT INTO {table} ({col_str}) VALUES %s
                    ON CONFLICT ({conflict_str}) DO UPDATE SET {update_str}
                """
            else:
                sql = f"""
                    INSERT INTO {table} ({col_str}) VALUES %s
                    ON CONFLICT ({conflict_str}) DO NOTHING
                """
            psycopg2.extras.execute_values(cur, sql, values, page_size=500)
        conn.commit()
        return len(rows)
    finally:
        conn.close()
