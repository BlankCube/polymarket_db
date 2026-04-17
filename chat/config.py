"""Centralized configuration for the chat webapp.

All secrets and connection parameters are read from environment variables
with fallback defaults for local development. Do NOT hardcode credentials
in other modules — import from here.
"""

import os
import sys
import secrets
from pathlib import Path

# Make the project-root shared helpers importable. chat/ is run with its
# own dir as CWD (uvicorn app:app), so we have to put the parent on
# sys.path before we can import _pm_common.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _pm_common import load_dotenv  # noqa: E402

load_dotenv()


# === Database ===

DB_HOST = os.environ.get("PM_DB_HOST", "localhost")
DB_PORT = int(os.environ.get("PM_DB_PORT", "5432"))
DB_NAME = os.environ.get("PM_DB_NAME", "polymarket_db")
DB_USER = os.environ.get("PM_DB_USER", "polymarket")
DB_PASSWORD = os.environ.get("PM_DB_PASSWORD", "polymarket123")

DB_DSN = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

DB_PARAMS = dict(
    host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
    user=DB_USER, password=DB_PASSWORD,
)

# Analyst (read-only) credentials. AI-generated SQL and AI-generated Python
# analysis code must connect with this role so that any attempt to write
# is rejected at the DB layer, not just the application layer.
DB_ANALYST_USER = os.environ.get("PM_DB_ANALYST_USER", "polymarket_ro")
DB_ANALYST_PASSWORD = os.environ.get("PM_DB_ANALYST_PASSWORD", "polymarket_ro_secret_change_me")

DB_ANALYST_DSN = f"postgresql://{DB_ANALYST_USER}:{DB_ANALYST_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

DB_ANALYST_PARAMS = dict(
    host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
    user=DB_ANALYST_USER, password=DB_ANALYST_PASSWORD,
)

# Pool sizing for the async read-only query pool used by AI-generated SQL.
DB_POOL_MIN = int(os.environ.get("PM_DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.environ.get("PM_DB_POOL_MAX", "3"))
DB_STATEMENT_TIMEOUT_MS = int(os.environ.get("PM_DB_STATEMENT_TIMEOUT_MS", "280000"))


# === Auth / JWT ===

# In production, PM_JWT_SECRET must be set. If it's missing we generate an
# ephemeral secret per process so tokens at least don't use a known value;
# this invalidates existing tokens on restart, which is the correct failure
# mode for a missing-secret deployment.
JWT_SECRET = os.environ.get("PM_JWT_SECRET") or secrets.token_urlsafe(48)
JWT_ALGORITHM = "HS256"
JWT_TOKEN_EXPIRE_DAYS = int(os.environ.get("PM_JWT_EXPIRE_DAYS", "30"))


# === AI ===

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL = os.environ.get("PM_AI_MODEL", "claude-sonnet-4-20250514")
AI_CLASSIFIER_MODEL = os.environ.get("PM_AI_CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")


# === Python runner sandbox ===

# Max wall time for a user-generated python analysis (seconds).
PY_RUNNER_TIMEOUT_SEC = int(os.environ.get("PM_PY_RUNNER_TIMEOUT_SEC", "300"))
# Max number of step3 auto-retries on execution failure.
STEP3_MAX_RETRIES = int(os.environ.get("PM_STEP3_MAX_RETRIES", "2"))
