"""FastAPI web application for natural language Polymarket database queries."""

import json
import asyncio
import logging
from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse

from db_pool import init_pool, close_pool, execute_query
from sql_safety import validate_and_limit
from ai import chat_stream, extract_sql, extract_python
from python_runner import run_python

# === Logging setup ===
LOG_DIR = Path(__file__).parent / "logs"
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


async def process_chat(messages: list[dict], client_ip: str):
    """Process a chat request: AI -> SQL -> execute -> AI interpret -> stream."""

    # Log user message
    user_msg = messages[-1]["content"] if messages else ""
    log_chat("user_query", {
        "message": user_msg,
        "history_length": len(messages),
    }, client_ip)

    full_response = ""
    sql_query = None

    # Phase 1: Stream AI response (which may contain SQL)
    async for event_type, data in chat_stream(messages):
        if event_type == "text":
            yield f"event: text\ndata: {json.dumps(data)}\n\n"
        elif event_type == "full_response":
            full_response = data

    # Log AI response
    log_chat("ai_response", {
        "response": full_response[:2000],
        "has_sql": "<sql>" in full_response,
        "has_python": "<python>" in full_response,
    }, client_ip)

    # Phase 2: Execute SQL or Python if present
    sql_query = extract_sql(full_response)
    python_code = extract_python(full_response)

    if not sql_query and not python_code:
        yield "event: done\ndata: {}\n\n"
        return

    result_summary = ""

    if python_code:
        # === Python execution path ===
        try:
            yield f"event: python\ndata: {json.dumps(python_code)}\n\n"
            log_chat("python_execute", {"code": python_code[:2000]}, client_ip)

            output = await run_python(python_code)
            yield f"event: python_output\ndata: {json.dumps(output)}\n\n"

            log_chat("python_result", {"output": output[:2000]}, client_ip)
            result_summary = f"Python code output:\n{output}"

        except ValueError as e:
            error_msg = f"Python validation error: {str(e)}"
            yield f"event: error\ndata: {json.dumps({'error': error_msg})}\n\n"
            log_chat("error", {"type": "python_validation", "error": str(e)}, client_ip)
            yield "event: done\ndata: {}\n\n"
            return
        except Exception as e:
            error_msg = f"Python error: {str(e)}"
            yield f"event: error\ndata: {json.dumps({'error': error_msg})}\n\n"
            log_chat("error", {"type": "python_execution", "error": str(e)}, client_ip)
            yield "event: done\ndata: {}\n\n"
            return

    elif sql_query:
        # === SQL execution path ===
        try:
            safe_sql = validate_and_limit(sql_query)
            yield f"event: sql\ndata: {json.dumps(safe_sql)}\n\n"
            log_chat("sql_execute", {"sql": safe_sql}, client_ip)

            columns, rows = await execute_query(safe_sql)
            yield f"event: columns\ndata: {json.dumps(columns)}\n\n"

            for row in rows:
                serialized = [json_serial(v) for v in row]
                yield f"event: row\ndata: {json.dumps(serialized)}\n\n"

            yield f"event: query_done\ndata: {json.dumps({'row_count': len(rows)})}\n\n"
            log_chat("query_result", {"row_count": len(rows), "columns": columns}, client_ip)

            result_summary = f"Query returned {len(rows)} rows.\n"
            if rows:
                display_rows = rows[:20]
                result_summary += "Columns: " + ", ".join(columns) + "\n"
                for row in display_rows:
                    result_summary += " | ".join(str(json_serial(v)) for v in row) + "\n"
                if len(rows) > 20:
                    result_summary += f"... and {len(rows) - 20} more rows\n"

        except ValueError as e:
            yield f"event: error\ndata: {json.dumps({'error': f'SQL validation error: {str(e)}'})}\n\n"
            log_chat("error", {"type": "validation", "error": str(e), "sql": sql_query}, client_ip)
            yield "event: done\ndata: {}\n\n"
            return
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': f'Query error: {str(e)}'})}\n\n"
            log_chat("error", {"type": "execution", "error": str(e)}, client_ip)
            yield "event: done\ndata: {}\n\n"
            return

    # Phase 3: AI interprets results
    if result_summary:
        interpret_messages = messages + [
            {"role": "assistant", "content": full_response},
            {"role": "user", "content": f"Here are the results. Please analyze and interpret them:\n\n{result_summary}"}
        ]

        yield f"event: text\ndata: {json.dumps(chr(10) + chr(10) + '---' + chr(10) + chr(10))}\n\n"

        interpret_text = ""
        async for event_type, data in chat_stream(interpret_messages):
            if event_type == "text":
                yield f"event: text\ndata: {json.dumps(data)}\n\n"
                interpret_text += data

        log_chat("ai_interpretation", {"interpretation": interpret_text[:2000]}, client_ip)

    yield "event: done\ndata: {}\n\n"


@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    client_ip = request.client.host if request.client else "unknown"

    return StreamingResponse(
        process_chat(messages, client_ip),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
