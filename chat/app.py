"""FastAPI web application for natural language Polymarket database queries."""

import asyncio
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


# Strong references to detached worker tasks so they are not garbage-
# collected while running. asyncio's event loop only holds a weak reference
# to tasks it schedules; without our own set, a worker whose HTTP response
# has been cancelled could be collected mid-flight. Each worker removes
# itself on completion.
_chat_workers: set[asyncio.Task] = set()


def _parse_sse(sse_chunk: str) -> tuple[str, object] | None:
    """Return (event_name, parsed_data) for an SSE chunk produced by
    `process_chat`'s `_sse(...)` helper, else None.

    The chunk format is `event: <name>\\ndata: <json>\\n\\n`."""
    lines = sse_chunk.split("\n", 2)
    if len(lines) < 2 or not lines[0].startswith("event: "):
        return None
    name = lines[0][len("event: "):]
    data_line = lines[1]
    if not data_line.startswith("data: "):
        return None
    try:
        value = json.loads(data_line[len("data: "):])
    except (json.JSONDecodeError, ValueError):
        return None
    return name, value


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    session_id = body.get("session_id", str(uuid.uuid4()))
    client_ip = request.client.host if request.client else "unknown"
    user = get_user_from_request(request)
    user_id = user["user_id"] if user else None

    # Save the conversation up to (and including) the user's new message
    # immediately, so the session row exists before any AI work — a user who
    # reloads before any response arrives still sees their own question.
    try:
        sessions_repo.save_messages(session_id, user_id, messages)
    except Exception as e:
        error_logger.error(f"save_messages failed: {e}")

    # Decouple the AI work from the HTTP response lifecycle. The worker runs
    # process_chat() to completion regardless of whether the client stays
    # connected. When it finishes (success or failure) it writes the final
    # assistant message into the session, so a user who closes the tab
    # mid-stream can come back later and find the response waiting for them.
    queue: asyncio.Queue = asyncio.Queue()

    async def worker():
        text_buf: list[str] = []
        # Carries the `execution` SSE payload (execution_id, csv_url,
        # row_count, truncated) when the turn produced a downloadable SQL
        # result. We attach it to the persisted assistant message so the
        # Download CSV button reappears when the user reloads this session
        # later — without it, only live turns would have working buttons.
        execution_meta: dict | None = None
        try:
            async for sse_chunk in process_chat(
                messages, session_id, user_id, client_ip, log_chat,
            ):
                queue.put_nowait(sse_chunk)
                parsed = _parse_sse(sse_chunk)
                if not parsed:
                    continue
                name, value = parsed
                if name == "text" and isinstance(value, str):
                    text_buf.append(value)
                elif name == "execution" and isinstance(value, dict):
                    execution_meta = value
        except Exception as e:
            error_logger.error(
                f"chat worker crashed (session={session_id}): {type(e).__name__}: {e}"
            )
        finally:
            # Persist whatever was produced, even partial — so a reconnecting
            # user sees the AI's work up to the point of failure rather than
            # a silent missing reply.
            assistant_text = "".join(text_buf)
            if assistant_text:
                try:
                    assistant_msg = {"role": "assistant", "content": assistant_text}
                    if execution_meta:
                        assistant_msg["execution"] = execution_meta
                    final_msgs = messages + [assistant_msg]
                    sessions_repo.save_messages(session_id, user_id, final_msgs)
                except Exception as e:
                    error_logger.error(
                        f"persist final (session={session_id}) failed: {e}"
                    )
            # Sentinel: wake the stream reader so it closes cleanly.
            queue.put_nowait(None)

    task = asyncio.create_task(worker())
    _chat_workers.add(task)
    task.add_done_callback(_chat_workers.discard)

    async def stream_from_queue():
        # If the client disconnects, Starlette cancels this generator. The
        # worker task is independent (owned by _chat_workers) and continues
        # to run, eventually persisting the completed response and exiting.
        while True:
            event = await queue.get()
            if event is None:
                return
            yield event

    return StreamingResponse(
        stream_from_queue(),
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


@app.get("/api/execution/{execution_id}/csv")
async def api_execution_csv(execution_id: int, request: Request):
    """Stream a previously-persisted SQL execution back as CSV.

    Filtered by `(id, user_id)` so a logged-in user can only download
    their own executions even if they guess an integer id. Old executions
    remain downloadable indefinitely (until session_executions is pruned).
    """
    user = get_user_from_request(request)
    if not user:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"status": "error", "message": "not logged in"}, status_code=401,
        )

    from db_pool import get_sync_conn
    import csv
    import io

    conn = get_sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT code_type, result_obj, description
                FROM session_executions
                WHERE id = %s AND user_id = %s
                """,
                (execution_id, user["user_id"]),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"status": "error", "message": "execution not found"},
            status_code=404,
        )

    code_type, result_obj, _description = row
    if code_type != "sql":
        # Python results don't carry tabular all_rows. Could later support
        # python_structured (one-row CSV from summary), but not needed now.
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"status": "error",
             "message": "only SQL executions are downloadable as CSV"},
            status_code=400,
        )

    columns = result_obj.get("columns") or []
    all_rows = result_obj.get("all_rows") or []

    def csv_iter():
        # Stream in chunks so we don't build a full string in memory for big
        # results. Each yield is a complete CSV chunk; client gets bytes.
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        yield buf.getvalue().encode("utf-8")
        # Reset buffer per batch.
        BATCH = 500
        for i in range(0, len(all_rows), BATCH):
            buf = io.StringIO()
            writer = csv.writer(buf)
            for r in all_rows[i:i + BATCH]:
                # csv module won't quote None as "None"; convert to empty.
                writer.writerow(["" if v is None else v for v in r])
            yield buf.getvalue().encode("utf-8")

    truncated = bool(result_obj.get("all_rows_truncated"))
    fname = f"polymarket-explorer-{execution_id}.csv"
    return StreamingResponse(
        csv_iter(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store",
            **({"X-Result-Truncated": "1"} if truncated else {}),
        },
    )


@app.get("/api/example_questions")
async def api_example_questions(count: int = 3):
    """Random sample of curated example questions.

    Frontend calls this on new-session render to show suggestions at the
    top of an empty chat. Also used internally by the AI via the
    ``suggest_example_questions`` tool — both paths share the same
    library in ``chat/example_questions.py``."""
    try:
        import example_questions
        n = max(1, min(int(count), 10))
        return {"status": "ok", "questions": example_questions.sample(n)}
    except Exception as e:
        error_logger.error(f"api_example_questions failed: {e}")
        return {"status": "error", "message": str(e)}
