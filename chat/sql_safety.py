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


def validate_and_limit(sql: str) -> str:
    """Validate SQL is a safe SELECT and enforce LIMIT. Raises ValueError if unsafe."""
    cleaned = sql.strip().rstrip(';').strip()

    if not cleaned:
        raise ValueError("Empty query")

    # Must start with SELECT or WITH
    first_word = cleaned.split()[0].upper()
    if first_word not in ('SELECT', 'WITH'):
        raise ValueError(f"Only SELECT queries allowed, got: {first_word}")

    # No multiple statements
    # Simple check: no semicolons in the middle (already stripped trailing)
    if ';' in cleaned:
        raise ValueError("Multiple statements not allowed")

    # Check forbidden keywords (word boundary match, case insensitive)
    upper = cleaned.upper()
    for kw in FORBIDDEN_KEYWORDS:
        pattern = r'\b' + kw + r'\b'
        if re.search(pattern, upper):
            raise ValueError(f"Forbidden keyword: {kw}")

    # No comments
    if '--' in cleaned or '/*' in cleaned:
        raise ValueError("SQL comments not allowed")

    # Enforce LIMIT
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
