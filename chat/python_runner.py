"""Safe Python code execution for data analysis."""

import subprocess
import tempfile
import json
import os
from pathlib import Path

TIMEOUT = 300  # seconds (5 min, heavy queries allowed)
VENV_PYTHON = str(Path(__file__).parent.parent / "venv" / "bin" / "python3")

CODE_HEADER = '''
import pandas as pd
import numpy as np
import json
import sys
import psycopg2

def query_db(sql, limit=50000):
    """Run a SQL query and return a pandas DataFrame. Auto-adds LIMIT if missing."""
    sql = sql.strip().rstrip(';')
    if 'LIMIT' not in sql.upper():
        sql += f' LIMIT {limit}'
    conn = psycopg2.connect(
        host="localhost", port=5432, dbname="polymarket_db",
        user="polymarket", password="polymarket123",
        options="-c default_transaction_read_only=on -c statement_timeout=280000"
    )
    try:
        df = pd.read_sql_query(sql, conn)
        return df
    finally:
        conn.close()

import io
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


def validate_python(code: str) -> str:
    """Basic safety checks on Python code."""
    dangerous = [
        'os.system', 'subprocess', 'shutil.rmtree', 'open(',
        '__import__', 'eval(', 'exec(', 'compile(',
        'os.remove', 'os.unlink', 'os.rmdir',
        'shutil.move', 'pathlib', 'socket',
    ]
    for d in dangerous:
        if d in code:
            raise ValueError(f"Forbidden operation: {d}")
    # Allow imports only for data libs
    import re
    imports = re.findall(r'^\s*(?:import|from)\s+(\w+)', code, re.MULTILINE)
    allowed = {'pandas', 'pd', 'numpy', 'np', 'json', 'math', 'statistics',
               'collections', 'itertools', 'functools', 'datetime', 're'}
    for imp in imports:
        if imp not in allowed:
            raise ValueError(f"Import not allowed: {imp}. Allowed: {', '.join(sorted(allowed))}")
    return code


async def run_python(code: str) -> str:
    """Execute Python code in a subprocess and return output."""
    validate_python(code)

    # Indent user code by 4 spaces for the template
    indented = '\n'.join('    ' + line for line in code.split('\n'))

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f_code, \
         tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f_out:
        code_file = f_code.name
        output_file = f_out.name

        footer = CODE_FOOTER_TEMPLATE.replace("OUTPUT_FILE_PLACEHOLDER", output_file)
        full_code = CODE_HEADER + indented + "\n" + footer
        f_code.write(full_code)

    try:
        proc = subprocess.run(
            [VENV_PYTHON, code_file],
            capture_output=True, text=True, timeout=TIMEOUT,
            cwd="/home/ubuntu/polymarket-db",
        )

        # Read output file
        if os.path.exists(output_file):
            with open(output_file, 'r') as f:
                result = f.read()
        else:
            result = ""

        if proc.returncode != 0 and not result:
            result = f"Error:\n{proc.stderr[-2000:]}"

        return result.strip() if result else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Code execution timed out (5 min limit)"
    finally:
        os.unlink(code_file)
        if os.path.exists(output_file):
            os.unlink(output_file)
