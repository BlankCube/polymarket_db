"""Shared helpers used by both database/ and chat/.

These two subtrees are run as independent sys.path roots (each has its own
config.py that callers import as `from config import ...`). To share code
between them without restructuring into a package, each config.py prepends
the project root to sys.path and imports from this module.

Keep this module deliberately small — it exists to eliminate duplication,
not to grow into a general utility layer.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def load_dotenv():
    """Populate os.environ from <project_root>/.env for keys that are missing
    OR set to empty string. Shell exports / systemd unit Environment= always
    win — the .env only fills gaps.

    Empty-string check is intentional: some tools (e.g. IDE integrations) set
    env vars to "" rather than leaving them unset, which would otherwise
    cause the .env to be ignored.
    """
    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and not os.environ.get(key):
                os.environ[key] = val
    except OSError:
        pass  # unreadable .env — fall through to env-only mode
