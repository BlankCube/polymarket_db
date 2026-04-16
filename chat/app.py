"""FastAPI web application for natural language Polymarket database queries."""

import json
import uuid
import logging
import psycopg2
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, FileResponse

from db_pool import init_pool, close_pool, execute_query
from sql_safety import validate_and_limit
from ai import (step1_understand, step3_generate, step4_describe, step5_interpret,
                extract_sql, extract_python)
from python_runner import run_python
from auth import register, login, get_user_from_request

# === Logging setup ===
LOG_DIR = Path(__file__).parent.parent / "feedback" / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Chat log: every conversation turn (user question, AI response, SQL, results, errors)
chat_logger = logging.getLogger("chat")
chat_logger.setLevel(logging.INFO)
chat_handler = logging.FileHandler(LOG_DIR / "chat.jsonl", encoding="utf-8")
chat_logger.addHandler(chat_handler)

# Error log
error_logger = logging.getLogger("errors")
error_logger.setLevel(logging.ERROR)
error_handler = logging.FileHandler(LOG_DIR / "errors.log", encoding="utf-8")
error_handler.setFormatter(logging.Formatter('%(asctime)s | %(message)s'))
error_logger.addHandler(error_handler)

app = FastAPI(title="Polymarket Explorer")


@app.on_event("startup")
async def startup():
    await init_pool()


@app.on_event("shutdown")
async def shutdown():
    await close_pool()


@app.get("/")
async def index():
    return FileResponse("static/index.html")


def json_serial(obj):
    """JSON serializer for types not handled by default."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    return str(obj)


def log_chat(event_type: str, data: dict, client_ip: str = ""):
    """Write a structured log line to chat.jsonl"""
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "ip": client_ip,
        "event": event_type,
        **data,
    }
    chat_logger.info(json.dumps(entry, ensure_ascii=False, default=str))


def _is_confirmation(text: str) -> bool:
    """Check if user message is a confirmation to proceed."""
    t = text.strip().lower()
    confirms = ['对', '没错', '查吧', '是的', '好', '好的', '可以', '行', '嗯', '确认',
                 'yes', 'ok', 'go', 'sure', 'correct', 'right', 'yep', 'yeah', 'do it',
                 'go ahead', 'proceed', 'confirm', 'y']
    return t in confirms or len(t) <= 5 and any(c in t for c in confirms)


async def process_chat(messages: list[dict], client_ip: str):
    """5-step AI interaction flow. User never sees code."""

    user_msg = messages[-1]["content"] if messages else ""
    log_chat("user_query", {"message": user_msg, "history_length": len(messages)}, client_ip)

    # Detect if this is a confirmation of a previous understanding
    is_confirm = len(messages) >= 2 and _is_confirmation(user_msg)

    if not is_confirm:
        # === STEP 1: Understand intent, stream to user ===
        full_response = ""
        stream = step1_understand(messages)
        async for event_type, data in stream:
            if event_type == "text":
                yield f"event: text\ndata: {json.dumps(data)}\n\n"
            elif event_type == "full_response":
                full_response = data

        log_chat("step1_understand", {"response": full_response[:2000]}, client_ip)
        yield "event: done\ndata: {}\n\n"
        return

    # User confirmed. Get the AI's previous understanding from conversation.
    prev_ai_msg = ""
    for m in reversed(messages[:-1]):
        if m["role"] == "assistant":
            prev_ai_msg = m["content"]
            break

    # === STEP 3: Generate code (hidden from user) ===
    yield f"event: status\ndata: {json.dumps('Querying database...')}\n\n"

    try:
        code_response = await step3_generate(messages, prev_ai_msg)
    except Exception as e:
        yield f"event: error\ndata: {json.dumps({'error': f'Code generation failed: {e}'})}\n\n"
        log_chat("error", {"type": "step3_generate", "error": str(e)}, client_ip)
        yield "event: done\ndata: {}\n\n"
        return

    sql_code = extract_sql(code_response)
    python_code = extract_python(code_response)
    code = sql_code or python_code or ""
    code_type = "sql" if sql_code else "python" if python_code else ""

    log_chat("step3_generate", {"type": code_type, "code": code[:2000]}, client_ip)

    if not code:
        yield f"event: error\ndata: {json.dumps({'error': 'Failed to generate query'})}\n\n"
        yield "event: done\ndata: {}\n\n"
        return

    # === Execute the code ===
    output = ""
    try:
        if sql_code:
            safe_sql = validate_and_limit(sql_code)
            columns, rows = await execute_query(safe_sql)
            # Format output as text for AI to interpret
            output = f"Columns: {', '.join(columns)}\n"
            output += f"Rows returned: {len(rows)}\n\n"
            for row in rows[:50]:
                output += " | ".join(str(json_serial(v)) for v in row) + "\n"
            if len(rows) > 50:
                output += f"... ({len(rows) - 50} more rows)\n"
            log_chat("sql_result", {"row_count": len(rows)}, client_ip)
        elif python_code:
            output = await run_python(python_code)
            log_chat("python_result", {"output": output[:2000]}, client_ip)
    except Exception as e:
        yield f"event: error\ndata: {json.dumps({'error': f'Query execution error: {e}'})}\n\n"
        log_chat("error", {"type": "execution", "error": str(e), "code": code[:500]}, client_ip)
        yield "event: done\ndata: {}\n\n"
        return

    # === STEP 4: Describe what was queried (stream to user) ===
    desc_text = ""
    stream = step4_describe(code, output, messages)
    async for event_type, data in stream:
        if event_type == "text":
            yield f"event: text\ndata: {json.dumps(data)}\n\n"
            desc_text += data

    log_chat("step4_describe", {"description": desc_text[:2000]}, client_ip)

    # Separator
    yield f"event: text\ndata: {json.dumps(chr(10) + chr(10))}\n\n"

    # === STEP 5: Interpret results (stream to user) ===
    interp_text = ""
    stream = step5_interpret(code, output, messages)
    async for event_type, data in stream:
        if event_type == "text":
            yield f"event: text\ndata: {json.dumps(data)}\n\n"
            interp_text += data

    log_chat("step5_interpret", {"interpretation": interp_text[:2000]}, client_ip)

    yield "event: done\ndata: {}\n\n"


def _save_messages(session_id, user_id, messages):
    """Save conversation to DB immediately."""
    if not session_id or not messages:
        return
    first_user_msg = next((m["content"][:80] for m in messages if m["role"] == "user"), "New conversation")
    conn = psycopg2.connect(host="localhost", port=5432, dbname="polymarket_db",
                            user="polymarket", password="polymarket123")
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
            """, (session_id, user_id, first_user_msg,
                  json.dumps(messages, ensure_ascii=False, default=str)))
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        conn.close()


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    session_id = body.get("session_id", str(uuid.uuid4()))
    client_ip = request.client.host if request.client else "unknown"
    user = get_user_from_request(request)
    user_id = user["user_id"] if user else None

    # messages contains full conversation history including previous AI replies.
    # Save it now (user's latest message is the new one).
    _save_messages(session_id, user_id, messages)

    return StreamingResponse(
        process_chat(messages, client_ip),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )



@app.post("/api/end-session")
async def end_session(request: Request):
    """Kept for sendBeacon on page close."""
    body = await request.json()
    user = get_user_from_request(request)
    user_id = user["user_id"] if user else body.get("user_id")
    _save_messages(body.get("session_id", ""), user_id, body.get("messages", []))

    return {"status": "saved"}


@app.post("/api/feedback")
async def feedback(request: Request):
    """User submits rating and/or feedback for a session."""
    body = await request.json()
    session_id = body.get("session_id", "")
    rating = body.get("rating")  # 1-5
    text = body.get("feedback", "")

    if not session_id:
        return {"status": "error", "message": "no session_id"}

    conn = psycopg2.connect(
        host="localhost", port=5432, dbname="polymarket_db",
        user="polymarket", password="polymarket123"
    )
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE user_sessions
                SET user_rating = %s, user_feedback = %s
                WHERE session_id = %s
            """, (rating, text, session_id))
            if cur.rowcount == 0:
                # Session not saved yet, create a minimal record
                cur.execute("""
                    INSERT INTO user_sessions (session_id, user_rating, user_feedback)
                    VALUES (%s, %s, %s)
                """, (session_id, rating, text))
        conn.commit()
    finally:
        conn.close()

    log_chat("user_feedback", {
        "session_id": session_id,
        "rating": rating,
        "feedback": text,
    })

    return {"status": "saved"}


# === Auth endpoints ===

@app.post("/api/register")
async def api_register(request: Request):
    body = await request.json()
    result = register(body.get("username", ""), body.get("password", ""), body.get("display_name"))
    if "error" in result:
        return {"status": "error", "message": result["error"]}
    return {"status": "ok", **result}


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    result = login(body.get("username", ""), body.get("password", ""))
    if "error" in result:
        return {"status": "error", "message": result["error"]}
    return {"status": "ok", **result}


@app.get("/api/me")
async def api_me(request: Request):
    user = get_user_from_request(request)
    if not user:
        return {"status": "error", "message": "not logged in"}
    return {"status": "ok", "user_id": user["user_id"], "username": user["username"]}


@app.get("/api/sessions")
async def api_sessions(request: Request):
    """Get user's session history."""
    user = get_user_from_request(request)
    if not user:
        return {"status": "error", "message": "not logged in"}

    conn = psycopg2.connect(**dict(host="localhost", port=5432, dbname="polymarket_db",
                                    user="polymarket", password="polymarket123"))
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT session_id, topic_summary, started_at, ended_at,
                       queries_run, satisfaction, user_rating
                FROM user_sessions
                WHERE user_id = %s
                ORDER BY started_at DESC
                LIMIT 50
            """, (user["user_id"],))
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

    return {"status": "ok", "sessions": sessions}


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str, request: Request):
    """Load a specific session's conversation."""
    user = get_user_from_request(request)
    if not user:
        return {"status": "error", "message": "not logged in"}

    conn = psycopg2.connect(**dict(host="localhost", port=5432, dbname="polymarket_db",
                                    user="polymarket", password="polymarket123"))
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT session_id, conversation, topic_summary, started_at
                FROM user_sessions
                WHERE session_id = %s AND user_id = %s
            """, (session_id, user["user_id"]))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return {"status": "error", "message": "session not found"}

    return {
        "status": "ok",
        "session_id": row[0],
        "conversation": row[1] if row[1] else [],
        "topic_summary": row[2],
        "started_at": row[3].isoformat() if row[3] else None,
    }
