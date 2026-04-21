"""Normalize step3 execution output into a structured object for step4/5.

Rationale
---------
Previously, step4/5 received a truncated text blob (`columns: a,b,c\n50 rows\nrow1|row2...`)
and had to extract numbers from it. This violated the "every claim needs a
number" principle in two ways:

  1. Aggregates reported by the AI could only be re-derived from the 50-row
     sample, even when the query returned 500K rows.
  2. Interpretation text was truncated mid-row by a crude char-count limit.

This module replaces the text blob with a JSON object:

  {
    "kind": "sql" | "python_structured" | "python_raw",
    "row_count": int,                  # true count of rows returned (<= LIMIT)
    "columns": [str],                  # column names
    "sample_head": [[...]],            # up to 10 rows (from the top)
    "sample_tail": [[...]],            # up to 5 rows (from the bottom, if > 15)
    "numeric_stats": {col: {...}},     # min/max/mean/sum computed over ALL returned rows
    "categorical_stats": {col: {...}}, # for low-cardinality TEXT columns: top values

    # For python_structured (AI code ended with print(json.dumps(summary, ...))):
    "summary": {...},                  # the parsed JSON — authoritative metrics
    "stdout_tail": str,                # any non-JSON trailing stdout

    # For python_raw (AI just printed text):
    "stdout": str,                     # the printed output, capped
  }
"""

import json
from datetime import datetime, date
from decimal import Decimal


MAX_HEAD_ROWS = 10
MAX_TAIL_ROWS = 5
MAX_CATEGORICAL_DISTINCT = 20
MAX_TOP_CATEGORICAL = 5
MAX_STDOUT_CHARS = 8000
# Hard cap on rows persisted in `all_rows` for CSV download. Above this, the
# CSV is truncated and the download endpoint emits a warning header. Keep
# below the SQL LIMIT (5000 raw / aggregates higher) so most queries fit
# entirely. JSONB max is 1GB; 50K rows × 50 cols × 50 chars ≈ 125MB worst
# case — safe.
MAX_ALL_ROWS = 50000


def _to_jsonable(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        # NaN/Inf aren't valid JSON; normalize.
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


def _is_numeric(v) -> bool:
    if isinstance(v, bool):
        return False  # bools are ints in Python, but treat as categorical
    return isinstance(v, (int, float, Decimal)) and v is not None


def _numeric_stats(values: list) -> dict:
    nums = [float(v) for v in values if _is_numeric(v) and (v == v)]
    if not nums:
        return None
    return {
        "non_null": len(nums),
        "min": min(nums),
        "max": max(nums),
        "mean": sum(nums) / len(nums),
        "sum": sum(nums),
    }


def _categorical_stats(values: list) -> dict | None:
    """Return top-N counts for a low-cardinality column; None if high-cardinality."""
    counts = {}
    for v in values:
        if v is None:
            continue
        key = v if isinstance(v, str) else str(v)
        counts[key] = counts.get(key, 0) + 1
        if len(counts) > MAX_CATEGORICAL_DISTINCT:
            return None  # too many distinct values — skip
    if not counts:
        return None
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:MAX_TOP_CATEGORICAL]
    return {"distinct": len(counts), "top": [[k, v] for k, v in top]}


def normalize_sql_result(columns: list[str], rows: list[list]) -> dict:
    """Build a structured result object for SQL output.

    `all_rows` (capped at MAX_ALL_ROWS) is included for CSV download — it is
    NOT shown to the AI (format_for_ai strips it). The AI sees only
    sample_head / sample_tail + aggregate stats."""
    n = len(rows)
    capped_rows = rows[:MAX_ALL_ROWS]
    obj = {
        "kind": "sql",
        "row_count": n,
        "columns": list(columns),
        "sample_head": [[_to_jsonable(v) for v in r] for r in rows[:MAX_HEAD_ROWS]],
        "sample_tail": (
            [[_to_jsonable(v) for v in r] for r in rows[-MAX_TAIL_ROWS:]]
            if n > MAX_HEAD_ROWS + MAX_TAIL_ROWS else []
        ),
        "all_rows": [[_to_jsonable(v) for v in r] for r in capped_rows],
        "all_rows_truncated": n > MAX_ALL_ROWS,
        "numeric_stats": {},
        "categorical_stats": {},
    }

    for i, col in enumerate(columns):
        column_values = [r[i] for r in rows]
        ns = _numeric_stats(column_values)
        if ns is not None:
            obj["numeric_stats"][col] = ns
        else:
            cs = _categorical_stats(column_values)
            if cs is not None:
                obj["categorical_stats"][col] = cs

    return obj


def normalize_python_result(stdout: str) -> dict:
    """Build a structured result object from Python subprocess stdout.

    Convention: if the last non-empty line of stdout is a JSON object, treat
    it as the authoritative summary. Anything before it is preserved as
    stdout_tail.
    """
    stdout = stdout or ""
    stripped = stdout.rstrip()
    if not stripped:
        return {"kind": "python_raw", "stdout": ""}

    lines = stripped.splitlines()
    last = lines[-1].strip() if lines else ""
    if last.startswith("{") and last.endswith("}"):
        try:
            summary = json.loads(last)
            tail = "\n".join(lines[:-1])[-MAX_STDOUT_CHARS:]
            return {
                "kind": "python_structured",
                "summary": summary,
                "stdout_tail": tail,
            }
        except json.JSONDecodeError:
            pass

    return {"kind": "python_raw", "stdout": stdout[-MAX_STDOUT_CHARS:]}


def format_for_ai(result_obj: dict) -> str:
    """Serialize the result object into a prompt-friendly JSON blob.

    Strips `all_rows` — that field is for CSV download only and would blow
    out the AI's context window for any large query (and it's redundant
    with sample_head / sample_tail / numeric_stats which the AI already
    cites from)."""
    pruned = {k: v for k, v in result_obj.items()
              if k not in ("all_rows", "all_rows_truncated")}
    return json.dumps(pruned, ensure_ascii=False, indent=2, default=str)
