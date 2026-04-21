"""Orchestrator for the chat flow.

The server is stateless between /api/chat calls — every call receives the
full conversation history. The flow is:

  1. classify_turn()  → "understand" or "execute" (heuristic fast-path;
                       falls back to a Haiku call when ambiguous).
  2. If "understand":  run step1 (stream), end.
  3. If "execute":     generate (code + internal description) → execute →
                       interpret (the description is fed to the interpreter
                       as context — the user does NOT see it directly;
                       they see only the interpretation, which opens with a
                       self-contained scope + N + headline sentence).
                       On exec error, retry step3 with the error as a hint
                       up to STEP3_MAX_RETRIES times before giving up.

Each step is implemented as a separate coroutine that yields SSE events.
The orchestrator only handles state transitions and error branches.
"""

import json
import re
import logging

from config import STEP3_MAX_RETRIES
from sql_safety import validate_and_limit
from db_pool import execute_query, get_sync_conn
from python_runner import run_python
from result_format import (
    normalize_sql_result, normalize_python_result, format_for_ai,
)
from ai import (
    classify_turn,
    step1_understand, step3_generate, step5_interpret,
    extract_sql, extract_python,
)

_err = logging.getLogger("errors")


def _sanitize_for_ai(messages: list[dict]) -> list[dict]:
    """Strip frontend-only fields from messages before sending to the AI.

    Persisted assistant messages can carry an `execution` field (so the
    Download CSV button can reappear when the session is reloaded). The
    Anthropic API only knows `role` / `content` — passing extra fields can
    error or get silently ignored. Strip them defensively here so every AI
    call sees clean message dicts."""
    return [{"role": m["role"], "content": m["content"]} for m in messages]


def _persist_execution(session_id: str, user_id: int | None, code: str,
                       code_type: str, description: str,
                       result_obj: dict) -> int | None:
    """Write a successful step3 execution to `session_executions` so that
    later turns in the same session (this one OR a future reload of the
    session) can look it up via the lookup_prior_execution tool. Returns
    the new row's `id` (or None on error / no session) so callers can
    surface a stable handle for the CSV-download endpoint.

    Scoped by (session_id, user_id) — the tool handler will only return
    rows where BOTH match the current request, so a user who guesses
    another user's session_id still can't read their results."""
    if not session_id:
        return None
    try:
        conn = get_sync_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO session_executions (
                        session_id, user_id, code, code_type,
                        description, result_obj
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (session_id, user_id, code, code_type, description,
                     json.dumps(result_obj, default=str, ensure_ascii=False)),
                )
                row = cur.fetchone()
            conn.commit()
            return int(row[0]) if row else None
        finally:
            conn.close()
    except Exception as e:
        _err.error(f"persist_execution failed (session={session_id}): {e}")
        return None


# Matches the error line that python_runner.CODE_FOOTER_TEMPLATE prints
# when user code raises an exception:
#     print(f"Error: {type(e).__name__}: {e}")
# Requires start-of-string or newline before, then `Error: `, then a Python
# identifier (exception class name), then `: `. Tolerates multi-line error
# messages (PG errors span lines).
_PYTHON_ERROR_LINE = re.compile(r"(?:^|\n)Error: ([A-Za-z_][A-Za-z0-9_]*): ")


def _detect_python_error(stdout: str) -> str | None:
    """Return a short error message if the Python run failed, else None.

    Two shapes trigger a retry:
      1. run_python fallback when the subprocess exited non-zero without
         writing the output file: stdout starts with `Error:\\n{stderr}` or
         `Error: Code execution timed out`.
      2. Wrapper-caught user-code exception: CODE_FOOTER_TEMPLATE prints
         `Error: <ExceptionClassName>: <message>` after the user code,
         usually at the tail of stdout.
    """
    if not stdout:
        return None
    s = stdout.lstrip()
    if s.startswith("Error:\n") or "Error: Code execution timed out" in stdout:
        return stdout.strip()[:500]
    tail = stdout[-3000:]
    m = _PYTHON_ERROR_LINE.search(tail)
    if m:
        return tail[m.start():].lstrip("\n")[:500]
    return None


def _split_description_and_code(raw: str) -> tuple[str, str, str]:
    """Split step3's raw response into (description, code, code_type).

    The response format is a few bullets, then a blank line, then a single
    `<sql>...</sql>` or `<python>...</python>` tag. We locate the tag and
    take everything before it as the description. If no tag is present,
    we return ("", "", "") and the caller will treat this as a failure to
    trigger retry.
    """
    sql_match = re.search(r'<sql>(.*?)</sql>', raw, re.DOTALL)
    python_match = re.search(r'<python>(.*?)</python>', raw, re.DOTALL)
    if sql_match:
        return raw[:sql_match.start()].strip(), sql_match.group(1).strip(), "sql"
    if python_match:
        return raw[:python_match.start()].strip(), python_match.group(1).strip(), "python"
    return "", "", ""


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _last_assistant(messages: list[dict]) -> str:
    for m in reversed(messages[:-1]):
        if m["role"] == "assistant":
            return m["content"]
    return ""


async def _run_step1(messages, log, session_id: str, user_id: int | None):
    """Stream the understanding response. Passes session scope so step1 can
    call the lookup_prior_execution tool when the user's question refers to
    a prior turn's data."""
    full_response = ""
    async for event_type, data in step1_understand(
        messages, session_id=session_id, user_id=user_id,
    ):
        if event_type == "text":
            yield _sse("text", data)
        elif event_type == "full_response":
            full_response = data
    log("step1_understand", {"response": full_response[:2000]})


async def _run_step3_with_retry(messages, prev_ai_msg, log,
                                session_id: str, user_id: int | None):
    """Generate a description + code, execute, and return
    ``(description, code, code_type, result_obj)``.

    ``result_obj`` is the normalized JSON-serializable dict produced by
    result_format.normalize_*. On execution failure, regenerates with the
    error as a hint, up to STEP3_MAX_RETRIES extra attempts. The description
    is produced in the same call as the code and is shown to the user only
    after the code has executed successfully (so retries don't leak half-
    written descriptions).
    """
    # prior_errors accumulates ALL errors from previous attempts in this turn
    # and is passed to step3_generate every retry. This prevents the "AI fixes
    # the latest error but regresses on an earlier one" failure mode (e.g.
    # attempt 1 fails on "SQL comments not allowed" → attempt 2 removes the
    # comment but times out → attempt 3 sees only "TimeoutError" and fixes
    # the timeout while silently re-introducing comments). See step3_generate
    # docstring for the hint format.
    prior_errors: list[str] = []
    attempts = 0
    max_attempts = 1 + max(0, STEP3_MAX_RETRIES)

    while attempts < max_attempts:
        attempts += 1
        raw_response = await step3_generate(
            messages, prev_ai_msg, prior_errors=prior_errors,
            session_id=session_id, user_id=user_id,
        )
        description, code, code_type = _split_description_and_code(raw_response)

        log("step3_generate", {
            "type": code_type, "code": code[:2000],
            "description": description[:1000],
            "attempt": attempts,
        })

        if not code:
            err = "AI did not emit any <sql> or <python> code block."
            prior_errors.append(err)
            log("step3_error", {"error": err, "attempt": attempts})
            continue

        try:
            if code_type == "sql":
                safe_sql = validate_and_limit(code)
                columns, rows = await execute_query(safe_sql)
                result_obj = normalize_sql_result(columns, rows)
                log("sql_result", {"row_count": result_obj["row_count"], "attempt": attempts})
            else:
                stdout = await run_python(code)
                result_obj = normalize_python_result(stdout)
                # The python_runner wrapper catches user-code exceptions and
                # prints them to stdout instead of propagating — so a SQL
                # syntax error or ambiguous-column error looks like a clean
                # return here. Detect that shape and treat it as a failure so
                # the retry loop sees the error message as a hint.
                py_error = _detect_python_error(stdout)
                if py_error:
                    prior_errors.append(py_error)
                    log("step3_error", {
                        "error": py_error, "attempt": attempts,
                        "source": "python_runner_caught",
                    })
                    continue
                log("python_result", {
                    "kind": result_obj["kind"],
                    "structured": result_obj["kind"] == "python_structured",
                    "attempt": attempts,
                })
            log("step3_summary", {
                "attempts_used": attempts, "outcome": "success",
                "retries": attempts - 1,
            })
            return description, code, code_type, result_obj
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            prior_errors.append(err)
            log("step3_error", {"error": err, "attempt": attempts})

    # All attempts failed
    log("step3_summary", {
        "attempts_used": attempts, "outcome": "exhausted",
        "retries": attempts - 1,
        "errors": prior_errors,
    })
    raise RuntimeError(
        prior_errors[-1] if prior_errors else "Code generation/execution failed"
    )


async def _run_step5(result_obj, messages, log,
                     session_id: str, user_id: int | None,
                     query_description: str = ""):
    """Stream the result interpretation (findings + numbers).

    The executed code is NOT passed to step5 directly — if the interpreter
    needs to read it (for conceptual questions about a field, or to verify
    intent vs what actually ran), it calls the lookup_prior_execution tool.
    By this point the execution has already been persisted to
    session_executions so the tool will find it.

    ``query_description`` is passed as internal context (user doesn't see
    it). The interpretation opens with a mandatory scope + N + headline
    sentence so the user always knows what sample they're looking at."""
    result_json = format_for_ai(result_obj)
    interp_text = ""
    async for event_type, data in step5_interpret(
        result_json, messages,
        session_id=session_id, user_id=user_id,
        query_description=query_description,
    ):
        if event_type == "text":
            yield _sse("text", data)
            interp_text += data
    log("step5_interpret", {"interpretation": interp_text[:4000]})


async def process_chat(messages: list[dict], session_id: str,
                       user_id: int | None, client_ip: str, log_chat):
    """Multi-step AI interaction flow. User never sees code.

    ``session_id`` and ``user_id`` are threaded to every AI stage so the
    tools (notably ``lookup_prior_execution``) can fetch this user's
    prior turn data — and ONLY this user's, scoped by (session_id,
    user_id) in the DB handler so a user who guesses another's session_id
    still can't read their results."""

    def log(event_type: str, data: dict):
        log_chat(event_type, data, client_ip)

    # Strip persistence-only fields (e.g. `execution` metadata for the CSV
    # download button) before any AI call. Carrying extras through to the
    # Anthropic API is at best wasteful, at worst a validation error.
    messages = _sanitize_for_ai(messages)

    user_msg = messages[-1]["content"] if messages else ""
    log("user_query", {
        "message": user_msg, "history_length": len(messages),
        "session_id": session_id,
    })

    # === Classify: is this a confirmation to execute, or a new/refined question? ===
    # Classifier has no tools by design: fast decision, no tool-use loop.
    action, source = await classify_turn(messages)
    log("classify_turn", {"action": action, "source": source})

    if action != "execute":
        # === STEP 1: Understand (stream) ===
        yield _sse("stage", "understanding")
        async for chunk in _run_step1(messages, log, session_id, user_id):
            yield chunk
        yield _sse("done", {})
        return

    # === User confirmed. Recover the AI's proposed understanding. ===
    prev_ai_msg = _last_assistant(messages)

    # === STEP 3: Generate (code + description) + execute, with auto-retry ===
    yield _sse("stage", "executing")
    # Legacy "status" event kept for any old clients still listening for it.
    yield _sse("status", "Querying database...")
    try:
        description, code, code_type, result_obj = await _run_step3_with_retry(
            messages, prev_ai_msg, log, session_id, user_id,
        )
    except Exception as e:
        yield _sse("error", {"error": f"Query failed after retries: {e}"})
        yield _sse("done", {})
        return

    # Persist this successful execution so future turns (this session OR a
    # later reload) can look it up via the lookup_prior_execution tool.
    # The returned id is sent to the client as an `execution` SSE event so
    # the UI can render a Download CSV button for SQL results that have
    # downloadable rows. Python results don't carry `all_rows`, so the UI
    # only shows the button for code_type=='sql'.
    execution_id = _persist_execution(
        session_id, user_id, code, code_type, description, result_obj,
    )
    if execution_id is not None and code_type == "sql":
        row_count = result_obj.get("row_count", 0)
        truncated = bool(result_obj.get("all_rows_truncated"))
        if row_count > 0:
            yield _sse("execution", {
                "execution_id": execution_id,
                "row_count": row_count,
                "truncated": truncated,
                "csv_url": f"/api/execution/{execution_id}/csv",
            })

    # === STEP 5: Interpret results (streamed) ===
    # `code` is NOT passed — step5 calls lookup_prior_execution if it needs
    # to read the code. The execution was persisted above, so the tool will
    # find it. `description` IS passed as internal context.
    yield _sse("stage", "interpreting")
    async for chunk in _run_step5(
        result_obj, messages, log, session_id, user_id,
        query_description=description,
    ):
        yield chunk

    yield _sse("done", {})
