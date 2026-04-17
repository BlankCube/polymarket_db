"""Persistence for user sessions and feedback.

This is the single place that reads/writes user_sessions. Route handlers
should call these helpers instead of opening their own DB connections.

Kept as sync psycopg2 for now (auth uses sync psycopg2 too). These helpers
are called from async route handlers; treat them as short, bounded calls.
If they ever grow, move them to run_in_executor or port to asyncpg.
"""

import json
from datetime import datetime, date

from db_pool import get_sync_conn


def save_messages(session_id: str, user_id: int | None, messages: list[dict]) -> None:
    """Upsert the conversation for a session. Silent no-op if session_id or messages empty."""
    if not session_id or not messages:
        return
    first_user_msg = next(
        (m["content"][:80] for m in messages if m["role"] == "user"),
        "New conversation",
    )
    conn = get_sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_sessions (session_id, user_id, topic_summary, conversation, started_at, ended_at)
                VALUES (%s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (session_id) DO UPDATE SET
                    conversation = EXCLUDED.conversation,
                    ended_at = NOW(),
                    topic_summary = EXCLUDED.topic_summary,
                    user_id = COALESCE(EXCLUDED.user_id, user_sessions.user_id)
            """, (
                session_id, user_id, first_user_msg,
                json.dumps(messages, ensure_ascii=False, default=str),
            ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_feedback(session_id: str, rating: int | None, text: str) -> None:
    """Attach a rating / free-text feedback to a session. Creates a row if missing."""
    conn = get_sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE user_sessions
                SET user_rating = %s, user_feedback = %s
                WHERE session_id = %s
            """, (rating, text, session_id))
            if cur.rowcount == 0:
                cur.execute("""
                    INSERT INTO user_sessions (session_id, user_rating, user_feedback)
                    VALUES (%s, %s, %s)
                """, (session_id, rating, text))
        conn.commit()
    finally:
        conn.close()


def list_sessions(user_id: int, limit: int = 50) -> list[dict]:
    """Return a user's session history (most recent first)."""
    conn = get_sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT session_id, topic_summary, started_at, ended_at,
                       queries_run, satisfaction, user_rating
                FROM user_sessions
                WHERE user_id = %s
                ORDER BY started_at DESC
                LIMIT %s
            """, (user_id, limit))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
    finally:
        conn.close()

    sessions = []
    for row in rows:
        s = dict(zip(cols, row))
        for k, v in s.items():
            if isinstance(v, (datetime, date)):
                s[k] = v.isoformat()
        sessions.append(s)
    return sessions


def get_session(session_id: str, user_id: int) -> dict | None:
    """Load a specific session's conversation. Returns None if not found or not owned by user."""
    conn = get_sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT session_id, conversation, topic_summary, started_at
                FROM user_sessions
                WHERE session_id = %s AND user_id = %s
            """, (session_id, user_id))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return None

    return {
        "session_id": row[0],
        "conversation": row[1] if row[1] else [],
        "topic_summary": row[2],
        "started_at": row[3].isoformat() if row[3] else None,
    }
