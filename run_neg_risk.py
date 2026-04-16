#!/usr/bin/env python3
"""Index Neg Risk CTF Exchange events (standalone, can run parallel with CTF indexer)."""
from indexer import index_exchange_events
from config import NEG_RISK_CTF_EXCHANGE, NEG_RISK_START_BLOCK

index_exchange_events(
    NEG_RISK_CTF_EXCHANGE, "neg_risk",
    "neg_risk_exchange_last_block", NEG_RISK_START_BLOCK
)
