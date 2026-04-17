"""FastAPI web application for natural language Polymarket database queries."""

import json
import uuid
import logging
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles variant that disables browser caching.

    Rationale: during active frontend development we want every browser
    refresh to fetch the latest CSS/JS instead of a stale cached copy.
    Combined with the ?v= query string in index.html this guarantees
    users see new builds immediately. Trade-off: slightly more bandwidth
    on each page load — acceptable for an internal tool.
    """

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

from db_pool import init_pool, close_pool
from auth import register, login, get_user_from_request
import sessions_repo
from process import process_chat

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

# Serve CSS / JS / static assets under /static/* with no-cache headers.
app.mount("/static", NoCacheStaticFiles(directory="static"), name="static")


@app.on_event("startup")
async def startup():
    await init_pool()


@app.on_event("shutdown")
async def shutdown():
    await close_pool()


@app.get("/")
async def index():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-store"})


def log_chat(event_type: str, data: dict, client_ip: str = ""):
    """Write a structured log line to chat.jsonl"""
    entry = {
        "ts": datetime.utcnow().isoformat(),
        "ip": client_ip,
        "event": event_type,
        **data,
    }
    chat_logger.info(json.dumps(entry, ensure_ascii=False, default=str))


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
    try:
        sessions_repo.save_messages(session_id, user_id, messages)
    except Exception as e:
        error_logger.error(f"save_messages failed: {e}")

    return StreamingResponse(
        process_chat(messages, client_ip, log_chat),
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
    try:
        sessions_repo.save_messages(body.get("session_id", ""), user_id, body.get("messages", []))
    except Exception as e:
        error_logger.error(f"end-session save failed: {e}")
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

    sessions_repo.save_feedback(session_id, rating, text)

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
    sessions = sessions_repo.list_sessions(user["user_id"])
    return {"status": "ok", "sessions": sessions}


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: str, request: Request):
    """Load a specific session's conversation."""
    user = get_user_from_request(request)
    if not user:
        return {"status": "error", "message": "not logged in"}
    session = sessions_repo.get_session(session_id, user["user_id"])
    if not session:
        return {"status": "error", "message": "session not found"}
    return {"status": "ok", **session}
