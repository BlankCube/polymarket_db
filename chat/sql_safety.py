"""SQL validation and safety layer. Only allows SELECT queries with limits."""

import re

FORBIDDEN_KEYWORDS = [
    'INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'CREATE', 'TRUNCATE',
    'GRANT', 'REVOKE', 'COPY', 'EXECUTE', 'CALL', 'DO',
    'pg_read_file', 'pg_ls_dir', 'lo_import', 'lo_export',
    'pg_sleep', 'dblink', 'pg_terminate_backend',
]

MAX_LIMIT = 5000
DEFAULT_LIMIT = 1000


def _strip_comments(sql: str) -> str:
    """Remove SQL line (`-- ...`) and block (`/* ... */`) comments.

    Used INTERNALLY by the validator so that keyword / semicolon checks
    inspect the code as PG will actually see it (comments are whitespace to
    the SQL engine). The returned SQL that is actually executed still
    contains the original comments — they're harmless at runtime and useful
    for readability when reviewing logs of AI-generated code.
    """
    no_block = re.sub(r'/\*.*?\*/', ' ', sql, flags=re.DOTALL)
    no_line = re.sub(r'--[^\n]*', '', no_block)
    return no_line


def validate_and_limit(sql: str) -> str:
    """Validate SQL is a safe SELECT and enforce LIMIT. Raises ValueError if
    unsafe. Comments are permitted in the returned SQL; all safety checks are
    run against a comment-stripped view so a malicious `-- DROP TABLE` cannot
    sneak past the forbidden-keyword filter."""
    cleaned = sql.strip().rstrip(';').strip()

    if not cleaned:
        raise ValueError("Empty query")

    # Strip comments for ALL safety checks — the DB engine treats comments as
    # whitespace, so any keyword hiding inside a comment can't execute anyway.
    # We still return the ORIGINAL `cleaned` (with comments) for execution so
    # the logged/debugged SQL stays readable.
    check_src = _strip_comments(cleaned).strip()

    if not check_src:
        raise ValueError("Query has only comments, no statement")

    # Must start with SELECT or WITH
    first_word = check_src.split()[0].upper()
    if first_word not in ('SELECT', 'WITH'):
        raise ValueError(f"Only SELECT queries allowed, got: {first_word}")

    # No multiple statements (inspect the comment-stripped view so that
    # `-- ; comment` doesn't trip the check).
    if ';' in check_src:
        raise ValueError("Multiple statements not allowed")

    # Check forbidden keywords on the comment-stripped view.
    upper = check_src.upper()
    for kw in FORBIDDEN_KEYWORDS:
        pattern = r'\b' + kw + r'\b'
        if re.search(pattern, upper):
            raise ValueError(f"Forbidden keyword: {kw}")

    # Enforce LIMIT (check comment-stripped, rewrite on the returned SQL).
    limit_match = re.search(r'\bLIMIT\s+(\d+)', upper)
    if limit_match:
        limit_val = int(limit_match.group(1))
        if limit_val > MAX_LIMIT:
            cleaned = re.sub(
                r'\bLIMIT\s+\d+', f'LIMIT {MAX_LIMIT}', cleaned,
                flags=re.IGNORECASE
            )
    else:
        cleaned += f'\nLIMIT {DEFAULT_LIMIT}'

    return cleaned
