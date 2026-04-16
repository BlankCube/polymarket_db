"""Sync market metadata from Polymarket Gamma API into PostgreSQL."""

import json
import time
import requests
from datetime import datetime, timezone
from db import get_conn, bulk_upsert, get_state, set_state
from config import GAMMA_API, GAMMA_PAGE_SIZE


def fetch_markets_page(offset=0, limit=100):
    """Fetch a page of markets from Gamma API."""
    resp = requests.get(
        f"{GAMMA_API}/markets",
        params={"limit": limit, "offset": offset},
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()


def parse_market(m):
    """Parse a Gamma API market into a DB row dict."""
    def parse_ts(s):
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None

    clob_ids = None
    if m.get("clobTokenIds"):
        try:
            clob_ids = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
        except Exception:
            clob_ids = None

    outcomes = None
    if m.get("outcomes"):
        try:
            outcomes = json.loads(m["outcomes"]) if isinstance(m["outcomes"], str) else m["outcomes"]
        except Exception:
            outcomes = None

    outcome_prices = None
    if m.get("outcomePrices"):
        try:
            outcome_prices = json.loads(m["outcomePrices"]) if isinstance(m["outcomePrices"], str) else m["outcomePrices"]
        except Exception:
            outcome_prices = None

    return {
        "condition_id": m.get("conditionId") or m.get("condition_id"),
        "question_id": m.get("questionID"),
        "question": m.get("question"),
        "description": m.get("description"),
        "slug": m.get("slug"),
        "outcomes": json.dumps(outcomes) if outcomes else None,
        "outcome_prices": json.dumps(outcome_prices) if outcome_prices else None,
        "clob_token_ids": json.dumps(clob_ids) if clob_ids else None,
        "neg_risk": m.get("negRisk", False),
        "neg_risk_market_id": m.get("negRiskMarketID") or m.get("negRiskRequestID"),
        "end_date": parse_ts(m.get("endDate") or m.get("endDateIso")),
        "start_date": parse_ts(m.get("startDate") or m.get("startDateIso")),
        "active": m.get("active"),
        "closed": m.get("closed"),
        "volume": float(m.get("volumeNum") or m.get("volume") or 0),
        "liquidity": float(m.get("liquidityNum") or m.get("liquidity") or 0),
        "resolution_source": m.get("resolutionSource"),
    }


def build_token_map_rows(market_row, raw_market):
    """Build token_market_map rows from a parsed market."""
    rows = []
    clob_ids = None
    outcomes = None
    try:
        if market_row["clob_token_ids"]:
            clob_ids = json.loads(market_row["clob_token_ids"])
        if market_row["outcomes"]:
            outcomes = json.loads(market_row["outcomes"])
    except Exception:
        return rows

    if not clob_ids or not outcomes:
        return rows

    for i, token_id in enumerate(clob_ids):
        label = outcomes[i] if i < len(outcomes) else f"Outcome_{i}"
        rows.append({
            "token_id": str(token_id),
            "condition_id": market_row["condition_id"],
            "outcome_index": i,
            "outcome_label": label,
        })
    return rows


def sync_all_markets():
    """Sync all markets from Gamma API."""
    offset = 0
    total_markets = 0
    total_tokens = 0

    print("Starting market metadata sync from Gamma API...")

    while True:
        try:
            markets = fetch_markets_page(offset=offset, limit=GAMMA_PAGE_SIZE)
        except Exception as e:
            print(f"  Error fetching page at offset {offset}: {e}")
            time.sleep(5)
            continue

        if not markets:
            break

        market_rows = []
        token_rows = []

        for m in markets:
            parsed = parse_market(m)
            if not parsed["condition_id"]:
                continue
            market_rows.append(parsed)
            token_rows.extend(build_token_map_rows(parsed, m))

        if market_rows:
            n = bulk_upsert(
                "markets", market_rows, ["condition_id"],
                update_cols=["question", "description", "slug", "outcomes",
                             "outcome_prices", "clob_token_ids", "neg_risk",
                             "neg_risk_market_id", "end_date", "start_date",
                             "active", "closed", "volume", "liquidity",
                             "resolution_source", "updated_at"]
            )
            total_markets += n

        if token_rows:
            n = bulk_upsert("token_market_map", token_rows, ["token_id"],
                            update_cols=["condition_id", "outcome_index", "outcome_label"])
            total_tokens += n

        print(f"  Synced {total_markets} markets, {total_tokens} token mappings (offset={offset})")
        offset += GAMMA_PAGE_SIZE

        if len(markets) < GAMMA_PAGE_SIZE:
            break

        time.sleep(0.3)  # rate limit

    set_state("gamma_last_sync", datetime.now(timezone.utc).isoformat())
    print(f"Done. Total: {total_markets} markets, {total_tokens} token mappings.")
    return total_markets


if __name__ == "__main__":
    sync_all_markets()
