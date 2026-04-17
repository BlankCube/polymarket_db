"""Orchestrator for the 5-step chat flow.

The server is stateless between /api/chat calls — every call receives the
full conversation history. The flow is:

  1. classify_turn()  → "understand" or "execute"
  2. If "understand":  run step1 (stream), end.
  3. If "execute":     generate code → execute → describe → interpret.
                       On exec error, retry step3 with the error as a hint
                       up to STEP3_MAX_RETRIES times before giving up.

Each step is implemented as a separate coroutine that yields SSE events.
The orchestrator only handles state transitions and error branches.
"""

import json

from config import STEP3_MAX_RETRIES
from sql_safety import validate_and_limit
from db_pool import execute_query
from python_runner import run_python
from result_format import (
    normalize_sql_result, normalize_python_result, format_for_ai,
)
from ai import (
    classify_turn,
    step1_understand, step3_generate, step4_describe, step5_interpret,
    extract_sql, extract_python,
)


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _last_assistant(messages: list[dict]) -> str:
    for m in reversed(messages[:-1]):
        if m["role"] == "assistant":
            return m["content"]
    return ""


async def _run_step1(messages, log):
    """Stream the understanding response."""
    full_response = ""
    async for event_type, data in step1_understand(messages):
        if event_type == "text":
            yield _sse("text", data)
        elif event_type == "full_response":
            full_response = data
    log("step1_understand", {"response": full_response[:2000]})


async def _run_step3_with_retry(messages, prev_ai_msg, log):
    """Generate code, execute, and return (code, code_type, result_obj).

    ``result_obj`` is the normalized JSON-serializable dict produced by
    result_format.normalize_*. On execution failure, regenerates with the
    error as a hint, up to STEP3_MAX_RETRIES extra attempts.
    """
    last_error: str | None = None
    attempts = 0
    max_attempts = 1 + max(0, STEP3_MAX_RETRIES)

    while attempts < max_attempts:
        attempts += 1
        code_response = await step3_generate(messages, prev_ai_msg, prior_error=last_error)
        sql_code = extract_sql(code_response)
        python_code = extract_python(code_response)
        code = sql_code or python_code or ""
        code_type = "sql" if sql_code else ("python" if python_code else "")

        log("step3_generate", {"type": code_type, "code": code[:2000], "attempt": attempts})

        if not code:
            last_error = "AI did not emit any <sql> or <python> code block."
            continue

        try:
            if sql_code:
                safe_sql = validate_and_limit(sql_code)
                columns, rows = await execute_query(safe_sql)
                result_obj = normalize_sql_result(columns, rows)
                log("sql_result", {"row_count": result_obj["row_count"], "attempt": attempts})
            else:
                stdout = await run_python(python_code)
                result_obj = normalize_python_result(stdout)
                log("python_result", {
                    "kind": result_obj["kind"],
                    "structured": result_obj["kind"] == "python_structured",
                    "attempt": attempts,
                })
            return code, code_type, result_obj
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            log("step3_error", {"error": last_error, "attempt": attempts})

    # All attempts failed
    raise RuntimeError(last_error or "Code generation/execution failed")


async def _run_step4(code, result_obj, messages, log):
    result_json = format_for_ai(result_obj)
    desc_text = ""
    async for event_type, data in step4_describe(code, result_json, messages):
        if event_type == "text":
            yield _sse("text", data)
            desc_text += data
    log("step4_describe", {"description": desc_text[:2000]})


async def _run_step5(code, result_obj, messages, log):
    result_json = format_for_ai(result_obj)
    interp_text = ""
    async for event_type, data in step5_interpret(code, result_json, messages):
        if event_type == "text":
            yield _sse("text", data)
            interp_text += data
    log("step5_interpret", {"interpretation": interp_text[:2000]})


async def process_chat(messages: list[dict], client_ip: str, log_chat):
    """5-step AI interaction flow. User never sees code."""

    def log(event_type: str, data: dict):
        log_chat(event_type, data, client_ip)

    user_msg = messages[-1]["content"] if messages else ""
    log("user_query", {"message": user_msg, "history_length": len(messages)})

    # === Classify: is this a confirmation to execute, or a new/refined question? ===
    action = await classify_turn(messages)
    log("classify_turn", {"action": action})

    if action != "execute":
        # === STEP 1: Understand (stream) ===
        yield _sse("stage", "understanding")
        async for chunk in _run_step1(messages, log):
            yield chunk
        yield _sse("done", {})
        return

    # === User confirmed. Recover the AI's proposed understanding. ===
    prev_ai_msg = _last_assistant(messages)

    # === STEP 3: Generate + execute (hidden from user, with auto-retry) ===
    yield _sse("stage", "executing")
    # Legacy "status" event kept for any old clients still listening for it.
    yield _sse("status", "Querying database...")
    try:
        code, code_type, result_obj = await _run_step3_with_retry(messages, prev_ai_msg, log)
    except Exception as e:
        yield _sse("error", {"error": f"Query failed after retries: {e}"})
        yield _sse("done", {})
        return

    # === STEP 4: Describe ===
    yield _sse("stage", "describing")
    async for chunk in _run_step4(code, result_obj, messages, log):
        yield chunk

    # Separator between description and interpretation.
    yield _sse("text", "\n\n")

    # === STEP 5: Interpret ===
    yield _sse("stage", "interpreting")
    async for chunk in _run_step5(code, result_obj, messages, log):
        yield chunk

    yield _sse("done", {})
