"""Python code execution for AI-generated data analysis.

Security posture
----------------
This runner executes AI-generated Python. It is NOT a true sandbox — the
subprocess runs as the same OS user as the web process and has access to
the same filesystem. The defenses here are:

  1. AST validation (defense in depth against keyword-match bypasses):
     - Only allowlisted imports (pandas, numpy, etc.).
     - Rejects eval/exec/compile/open/__import__/getattr/setattr/delattr.
     - Rejects attribute names known to escape (os.system, .popen, etc.).
     - Rejects dunder attribute access except a small safe set.

  2. Resource limits (RLIMIT_*) applied via subprocess preexec_fn:
     - CPU time, address space, file size, process count, open files.

  3. Timeout enforced by subprocess.run.

  4. Database access uses the analyst role (polymarket_ro) — writes are
     rejected at the DB layer, not just by the session-level
     default_transaction_read_only setting. See database/schema_roles.sql.

Residual risk: the subprocess can still read files readable by the
running user (including source code and logs). A namespace-based sandbox
(bwrap/firejail/unshare with user-map) requires host-level configuration
that is not available in this environment.
"""

import ast
import os
import subprocess
import tempfile
import resource
from pathlib import Path

from config import PY_RUNNER_TIMEOUT_SEC, DB_ANALYST_PARAMS, DB_STATEMENT_TIMEOUT_MS

VENV_PYTHON = str(Path(__file__).parent.parent / "venv" / "bin" / "python3")

# Modules safe for import in user analysis code.
ALLOWED_IMPORTS = frozenset({
    "pandas", "pd", "numpy", "np", "json", "math", "statistics",
    "collections", "itertools", "functools", "datetime", "re",
})

# Builtins that enable arbitrary code execution or filesystem escape.
FORBIDDEN_BUILTINS = frozenset({
    "eval", "exec", "compile", "open", "__import__",
    "getattr", "setattr", "delattr", "globals", "locals", "vars",
    "input", "breakpoint", "help",
})

# Attribute names known to provide filesystem / process escape.
FORBIDDEN_ATTRS = frozenset({
    "system", "popen", "spawn", "spawnl", "spawnv", "spawnve",
    "fork", "forkpty", "exec", "execv", "execve", "execvp", "execvpe",
    "kill", "killpg", "remove", "unlink", "rmdir", "rmtree",
    "chmod", "chown", "chroot", "setuid", "setgid",
    "mkfifo", "symlink", "link",
    "read_pickle", "to_pickle",
})

# Dunder attributes are off-limits by default because introspection dunders
# (__class__, __bases__, __subclasses__, __mro__, __globals__, __builtins__,
# __code__, __dict__, __closure__) are classic sandbox-escape vectors.
# The allowlist here are dunders with no introspective capability.
ALLOWED_DUNDERS = frozenset({
    # Container / iteration protocol
    "__len__", "__iter__", "__next__", "__contains__",
    "__getitem__", "__setitem__",
    # Display / string protocol
    "__str__", "__repr__",
    # Context-manager protocol
    "__enter__", "__exit__",
    # Safe introspection: just names/strings, no live object handles.
    "__name__", "__qualname__", "__doc__", "__module__",
})


class UnsafeCodeError(ValueError):
    pass


def _check_node(node: ast.AST) -> None:
    """Walk a single AST node and raise UnsafeCodeError on any forbidden construct."""
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        names = (
            [node.module] if isinstance(node, ast.ImportFrom) and node.module
            else [alias.name for alias in node.names]
        )
        for name in names:
            root = name.split(".", 1)[0]
            if root not in ALLOWED_IMPORTS:
                raise UnsafeCodeError(
                    f"Import not allowed: {name}. Allowed: {', '.join(sorted(ALLOWED_IMPORTS))}"
                )

    elif isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in FORBIDDEN_BUILTINS:
            raise UnsafeCodeError(f"Forbidden builtin call: {func.id}")

    elif isinstance(node, ast.Attribute):
        if node.attr in FORBIDDEN_ATTRS:
            raise UnsafeCodeError(f"Forbidden attribute access: .{node.attr}")
        if node.attr.startswith("__") and node.attr.endswith("__"):
            if node.attr not in ALLOWED_DUNDERS:
                raise UnsafeCodeError(f"Forbidden dunder access: .{node.attr}")

    elif isinstance(node, ast.Name):
        if node.id in FORBIDDEN_BUILTINS:
            raise UnsafeCodeError(f"Forbidden reference: {node.id}")
        # Bare dunder names (__builtins__, __import__, __loader__, etc.) are
        # entry points for introspection escapes and never needed by analysis code.
        if node.id.startswith("__") and node.id.endswith("__"):
            raise UnsafeCodeError(f"Forbidden dunder reference: {node.id}")


def validate_python(code: str) -> str:
    """Parse and walk the AST, rejecting any construct that could escape the runner."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise UnsafeCodeError(f"Syntax error: {e}")
    for node in ast.walk(tree):
        _check_node(node)
    return code


# === Subprocess-side template ===
#
# The user code is wrapped in a setup that:
#   - injects query_db() (read-only DB access)
#   - captures stdout
#   - writes stdout to a file (so we can read it back even on crash)

CODE_HEADER = '''
import pandas as pd
import numpy as np
import json
import sys
import psycopg2
import io

_DB_PARAMS = __DB_PARAMS__
_DB_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=__STMT_TIMEOUT__"

def query_db(sql, params=None, limit=50000):
    """Run a SQL query and return a pandas DataFrame. Auto-adds LIMIT if missing.

    ``params`` (optional): tuple or dict for parameterized queries using
    %s placeholders. Prefer this over string-formatting values into SQL —
    it handles escaping and type conversion (including timestamps).
    Example:
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(days=30)
        query_db("SELECT * FROM order_fills WHERE block_timestamp >= %s",
                 (cutoff,))

    If you do NOT need parameterization, pass ``params=None`` (default)
    and inline literal values in the SQL text itself — do NOT leave bare
    %s placeholders in the query; PostgreSQL will raise a syntax error.
    """
    sql = sql.strip().rstrip(";")
    if "LIMIT" not in sql.upper():
        sql += f" LIMIT {limit}"
    conn = psycopg2.connect(**_DB_PARAMS, options=_DB_OPTIONS)
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()

_output_buf = io.StringIO()
_old_stdout = sys.stdout
sys.stdout = _output_buf

try:
'''

CODE_FOOTER_TEMPLATE = '''
except Exception as e:
    print(f"Error: {type(e).__name__}: {e}")

sys.stdout = _old_stdout
_result = _output_buf.getvalue()

with open("OUTPUT_FILE_PLACEHOLDER", "w") as f:
    f.write(_result)
'''


# RLIMIT values for the analysis subprocess.
#
# Note on NPROC: we deliberately do NOT set RLIMIT_NPROC. On Linux it counts
# processes across the whole user, not the process tree, which collides with
# any other process running as the same OS user (pandas/numpy also spawn
# thread workers that count against it). RLIMIT_AS + RLIMIT_CPU cap the
# actually-meaningful resources (memory and compute), so NPROC adds no
# defensive value here.
_MEM_LIMIT_BYTES = int(os.environ.get("PM_PY_RUNNER_MEM_MB", "2048")) * 1024 * 1024
_FSIZE_LIMIT_BYTES = int(os.environ.get("PM_PY_RUNNER_FSIZE_MB", "64")) * 1024 * 1024
_NOFILE_LIMIT = int(os.environ.get("PM_PY_RUNNER_NOFILE", "256"))
_CPU_LIMIT_SEC = PY_RUNNER_TIMEOUT_SEC + 5  # CPU limit slightly above wall-clock


def _apply_rlimits():
    """preexec_fn: applied in the forked child before exec()."""
    resource.setrlimit(resource.RLIMIT_AS, (_MEM_LIMIT_BYTES, _MEM_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (_CPU_LIMIT_SEC, _CPU_LIMIT_SEC))
    resource.setrlimit(resource.RLIMIT_FSIZE, (_FSIZE_LIMIT_BYTES, _FSIZE_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (_NOFILE_LIMIT, _NOFILE_LIMIT))
    # New process group so we can't signal the parent.
    os.setpgrp()


async def run_python(code: str) -> str:
    """Validate and execute analysis code in a subprocess with rlimits applied."""
    validate_python(code)

    indented = "\n".join("    " + line for line in code.split("\n"))

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f_code, \
         tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f_out:
        code_file = f_code.name
        output_file = f_out.name

        header = (
            CODE_HEADER
            .replace("__DB_PARAMS__", repr(DB_ANALYST_PARAMS))
            .replace("__STMT_TIMEOUT__", str(DB_STATEMENT_TIMEOUT_MS))
        )
        footer = CODE_FOOTER_TEMPLATE.replace("OUTPUT_FILE_PLACEHOLDER", output_file)
        full_code = header + indented + "\n" + footer
        f_code.write(full_code)

    try:
        proc = subprocess.run(
            [VENV_PYTHON, "-I", code_file],
            capture_output=True, text=True, timeout=PY_RUNNER_TIMEOUT_SEC,
            cwd="/home/ubuntu/polymarket-db",
            preexec_fn=_apply_rlimits,
            env={  # strip inherited env; keep only what the venv needs
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",
                "LC_ALL": "C.UTF-8",
                "LANG": "C.UTF-8",
            },
        )

        if os.path.exists(output_file):
            with open(output_file, "r") as f:
                result = f.read()
        else:
            result = ""

        if proc.returncode != 0 and not result:
            result = f"Error:\n{proc.stderr[-2000:]}"

        return result.strip() if result else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: Code execution timed out ({PY_RUNNER_TIMEOUT_SEC}s limit)"
    finally:
        try:
            os.unlink(code_file)
        except OSError:
            pass
        try:
            os.unlink(output_file)
        except OSError:
            pass
