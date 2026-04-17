"""Configuration for Polymarket data collection.

Secrets (DB password, RPC URL with API key) are read from env vars with
fallback defaults for local dev. A sibling .env file at the project root is
auto-loaded on import, so running `python unified_indexer.py` picks up the
same credentials as the webapp.
"""

import os
import sys
from pathlib import Path

# Make the project-root shared helpers importable. database/ is run with
# its own dir as CWD (`python run.py ...`), so we have to put the parent on
# sys.path before we can import _pm_common.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _pm_common import load_dotenv  # noqa: E402

load_dotenv()


# PostgreSQL
DB_HOST = os.environ.get("PM_DB_HOST", "localhost")
DB_PORT = int(os.environ.get("PM_DB_PORT", "5432"))
DB_NAME = os.environ.get("PM_DB_NAME", "polymarket_db")
DB_USER = os.environ.get("PM_DB_USER", "polymarket")
DB_PASSWORD = os.environ.get("PM_DB_PASSWORD", "polymarket123")
DB_DSN = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Polygon RPC (full node). This URL contains an API key — never commit the
# real value. .env or env var overrides the dev placeholder below.
POLYGON_RPC = os.environ.get(
    "POLYGON_RPC",
    "https://broken-small-fire.matic.quiknode.pro/84ee9f62000fd3743f66098da514f2364fd73622/",
)

# Polymarket contract addresses (Polygon mainnet)
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Event topic hashes
TOPIC_ORDER_FILLED = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"
TOPIC_ORDERS_MATCHED = "0x63bf4d16b7fa898ef4c4b2b6d90fd201e9c56313b65638af6088d149d2ce956c"
TOPIC_CONDITION_RESOLUTION = "0xb44d84d3289691f71497564b85d4233648d9dbae8cbdbb4329f301c3a0185894"
TOPIC_PAYOUT_REDEMPTION = "0x2682012a4a4f1973119f1c9b90745d1bd91fa2bab387344f044cb3586864d18d"
TOPIC_CONDITION_PREPARATION = "0xab3760c3bd2bb38b5bcf54dc79802ed67338b4cf29f3054ded67ed24661e4177"

# Gamma API (market metadata)
GAMMA_API = "https://gamma-api.polymarket.com"

# Indexing settings
LOG_BATCH_SIZE = int(os.environ.get("PM_LOG_BATCH_SIZE", "500"))  # blocks per eth_getLogs call
GAMMA_PAGE_SIZE = 100  # markets per API page

# Indexer error-handling parameters.
# On failure, we retry the same block range with exponential backoff and
# abort the process after too many consecutive failures so a stuck indexer
# never silently advances past missing data.
INDEXER_MAX_CONSECUTIVE_FAILURES = int(os.environ.get("PM_INDEXER_MAX_FAILURES", "5"))
INDEXER_BACKOFF_SECONDS = (3, 10, 30, 60, 120)  # retry #1, #2, #3, #4, #5+

# CTF Exchange deployment block (approximate - skip to where activity starts)
CTF_EXCHANGE_START_BLOCK = 44_000_000  # ~June 2023, activity picks up here
NEG_RISK_START_BLOCK = 51_000_000  # ~Jan 2024
CONDITIONAL_TOKENS_START_BLOCK = 44_000_000
