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
#
# V1 (active 2023-11-16 → 2026-04-28, block 86,126,998 = last V1 block):
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
# V2 (active 2026-04-29 onward; new exchange addresses, same CTF backend):
CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_CTF_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
# Unchanged across V1/V2 (CTF lives below the exchange layer):
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# V2 collateral wrapper (PMCT / pUSD). 6 decimals, pegged 1:1 to USDC by the
# CollateralOnramp/Offramp pair. V2 OrderFilled.makerAmountFilled is
# denominated in pUSD, but since decimals match USDC the existing /1e6
# scaling in the decoders works unchanged. At the CTF layer (PositionSplit
# etc.) the wrapper is unwrapped first, so collateral_token there is still
# USDC.e or the NegRisk-adapter wrapper — no schema change needed for those.
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"

# V2 cutover. The unified indexer fetches V1 topics for blocks <= this and
# V2 topics for blocks >. Only one batch can ever straddle the boundary;
# `_process_batch_dispatch` in unified_indexer.py handles that case by
# fetching both topic sets for the straddling range and discarding logs on
# the wrong side of the cutover. Mirrored into indexer_state by the
# 2026_05_08_v2_indexer.sql migration so ops queries can grep era directly.
V2_CUTOVER_BLOCK = 86_126_998

# Event topic hashes — V1 (legacy, pre-cutover only)
TOPIC_ORDER_FILLED = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"
TOPIC_ORDERS_MATCHED = "0x63bf4d16b7fa898ef4c4b2b6d90fd201e9c56313b65638af6088d149d2ce956c"

# V2 topics — verified on-chain 2026-05-08 against actual logs from
# CTF_EXCHANGE_V2 and NEG_RISK_CTF_EXCHANGE_V2 (both V2 contracts emit the
# same set). Computed locally from the V2 source-of-truth signatures:
#   OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)
#   OrdersMatched(bytes32,address,uint8,uint256,uint256,uint256)
#   FeeCharged(address,uint256)
TOPIC_ORDER_FILLED_V2   = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
TOPIC_ORDERS_MATCHED_V2 = "0x174b3811690657c217184f89418266767c87e4805d09680c39fc9c031c0cab7c"
TOPIC_FEE_CHARGED_V2    = "0x55bb3cade9d43b798a4fe5ffdd05024b2d7870df53920673bfc7e68047cd0ab1"

# CTF events (unchanged across V1/V2 — emitted by CONDITIONAL_TOKENS).
TOPIC_CONDITION_RESOLUTION = "0xb44d84d3289691f71497564b85d4233648d9dbae8cbdbb4329f301c3a0185894"
TOPIC_PAYOUT_REDEMPTION = "0x2682012a4a4f1973119f1c9b90745d1bd91fa2bab387344f044cb3586864d18d"
TOPIC_CONDITION_PREPARATION = "0xab3760c3bd2bb38b5bcf54dc79802ed67338b4cf29f3054ded67ed24661e4177"
# CTF position split / merge events. Emitted by CONDITIONAL_TOKENS when a
# user converts USDC into a YES+NO pair (split) or back (merge). These are
# the "missing leg" of PnL: a market maker mints inventory via splits and
# sells both halves on the orderbook; without indexing splits, the buy
# cost is invisible and apparent PnL is overstated by exactly the split
# amount. Signatures match Gnosis CTF v1 ABI.
TOPIC_POSITION_SPLIT  = "0x2e6bb91f8cbcda0c93623c54d0403a43514fabc40084ec96b6d5379a74786298"
TOPIC_POSITIONS_MERGE = "0x6f13ca62553fcc2bcd2372180a43949c1e4cebba603901ede2f4e14f36b282ca"

# Gamma API (market metadata)
GAMMA_API = "https://gamma-api.polymarket.com"

# Indexing settings
LOG_BATCH_SIZE = int(os.environ.get("PM_LOG_BATCH_SIZE", "1250"))  # blocks per eth_getLogs call — empirically optimal on 2026-04-18 at ~1500 blocks/min; past 1250, QuikNode response time grows superlinearly and throughput drops.
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
