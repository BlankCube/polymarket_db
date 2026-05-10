"""On-chain event indexer for Polymarket trades, resolutions, and redemptions.

Uses eth_getLogs via the Polygon full node to fetch events in batches,
decode them, and insert into PostgreSQL.

This module is a library of decoders and per-event-type processors; the
active entry point is `unified_indexer.run()`, which drives a single scan
over block ranges and calls the processors here.
"""

import json
from datetime import datetime, timezone

import psycopg2.extras
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from config import POLYGON_RPC

w3 = Web3(Web3.HTTPProvider(POLYGON_RPC, request_kwargs={"timeout": 60}))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)


# ============ ABI fragments for decoding ============

ORDER_FILLED_ABI = {
    "name": "OrderFilled",
    "type": "event",
    "inputs": [
        {"name": "orderHash", "type": "bytes32", "indexed": True},
        {"name": "maker", "type": "address", "indexed": True},
        {"name": "taker", "type": "address", "indexed": True},
        {"name": "makerAssetId", "type": "uint256", "indexed": False},
        {"name": "takerAssetId", "type": "uint256", "indexed": False},
        {"name": "makerAmountFilled", "type": "uint256", "indexed": False},
        {"name": "takerAmountFilled", "type": "uint256", "indexed": False},
        {"name": "fee", "type": "uint256", "indexed": False},
    ]
}

ORDERS_MATCHED_ABI = {
    "name": "OrdersMatched",
    "type": "event",
    "inputs": [
        {"name": "takerOrderHash", "type": "bytes32", "indexed": True},
        {"name": "takerOrderMaker", "type": "address", "indexed": True},
        {"name": "makerAssetId", "type": "uint256", "indexed": False},
        {"name": "takerAssetId", "type": "uint256", "indexed": False},
        {"name": "makerAmountFilled", "type": "uint256", "indexed": False},
        {"name": "takerAmountFilled", "type": "uint256", "indexed": False},
    ]
}

CONDITION_RESOLUTION_ABI = {
    "name": "ConditionResolution",
    "type": "event",
    "inputs": [
        {"name": "conditionId", "type": "bytes32", "indexed": True},
        {"name": "oracle", "type": "address", "indexed": True},
        {"name": "questionId", "type": "bytes32", "indexed": True},
        {"name": "outcomeSlotCount", "type": "uint256", "indexed": False},
        {"name": "payoutNumerators", "type": "uint256[]", "indexed": False},
    ]
}

PAYOUT_REDEMPTION_ABI = {
    "name": "PayoutRedemption",
    "type": "event",
    "inputs": [
        {"name": "redeemer", "type": "address", "indexed": True},
        {"name": "collateralToken", "type": "address", "indexed": True},
        {"name": "parentCollectionId", "type": "bytes32", "indexed": True},
        {"name": "conditionId", "type": "bytes32", "indexed": False},
        {"name": "indexSets", "type": "uint256[]", "indexed": False},
        {"name": "payout", "type": "uint256", "indexed": False},
    ]
}


# ============ Per-batch cache ============

class BatchCache:
    """Per-batch token_id -> condition_id cache.

    Previously this class also cached block timestamps behind an
    eth_getBlock RPC call — redundant, because Polygon's eth_getLogs
    already returns `blockTimestamp` on every log (see log_timestamp()
    below). Removing that call cut batch wall-time by 25-50s on a
    500-block batch.

    Instantiated fresh per batch so memory stays bounded.
    """

    def __init__(self, conn):
        self._conn = conn
        self._token_cond: dict[str, str | None] = {}

    def condition_id_for_token(self, token_id_str: str) -> str | None:
        if token_id_str in self._token_cond:
            return self._token_cond[token_id_str]
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT condition_id FROM token_market_map WHERE token_id = %s",
                (token_id_str,),
            )
            row = cur.fetchone()
        result = row[0] if row else None
        self._token_cond[token_id_str] = result
        return result


def log_timestamp(log) -> datetime:
    """Extract block timestamp from a log object.

    Polygon's eth_getLogs returns `blockTimestamp` (hex unix seconds) inline
    on every log, so we never need a separate eth_getBlock round-trip. Falls
    back to an RPC fetch only for the edge case of logs that somehow lack
    the field — shouldn't happen on current Polygon full nodes.
    """
    ts_hex = log.get("blockTimestamp")
    if ts_hex is not None:
        if isinstance(ts_hex, str):
            return datetime.fromtimestamp(int(ts_hex, 16), tz=timezone.utc)
        return datetime.fromtimestamp(int(ts_hex), tz=timezone.utc)
    block = w3.eth.get_block(log["blockNumber"])
    return datetime.fromtimestamp(block["timestamp"], tz=timezone.utc)


# ============ Low-level ABI decoders ============

def decode_indexed_bytes32(topic_hex):
    """Decode an indexed bytes32 from a log topic."""
    return topic_hex.hex() if isinstance(topic_hex, bytes) else topic_hex


def decode_indexed_address(topic_hex):
    """Decode an indexed address from a log topic (last 20 bytes of 32-byte topic)."""
    if isinstance(topic_hex, bytes):
        return Web3.to_checksum_address(topic_hex[-20:])
    return Web3.to_checksum_address("0x" + topic_hex[-40:])


def decode_uint256(data, offset=0):
    """Decode a uint256 from ABI-encoded data at given 32-byte slot."""
    start = offset * 32
    return int.from_bytes(data[start:start + 32], "big")


def decode_uint256_array(data, offset_slot):
    """Decode a dynamic uint256[] from ABI-encoded data.

    The slot at `offset_slot` holds a byte offset into `data` where the array
    length precedes the elements.
    """
    ptr = decode_uint256(data, offset_slot)
    length = int.from_bytes(data[ptr:ptr + 32], "big")
    return [
        int.from_bytes(data[ptr + 32 + i * 32: ptr + 32 + (i + 1) * 32], "big")
        for i in range(length)
    ]


# ============ Trade-field derivation ============

def derive_trade_fields(maker_asset_id, taker_asset_id, maker_amount, taker_amount, cache: BatchCache):
    """Derive token_id, side, price, usdc_amount, token_amount from raw fill data."""
    maker_is_usdc = (maker_asset_id == 0)
    taker_is_usdc = (taker_asset_id == 0)

    if maker_is_usdc and not taker_is_usdc:
        # Maker pays USDC, receives tokens -> maker is BUYING
        token_id = str(taker_asset_id)
        side = "BUY"
        usdc_amount = maker_amount
        token_amount = taker_amount
    elif taker_is_usdc and not maker_is_usdc:
        # Maker pays tokens, receives USDC -> maker is SELLING
        token_id = str(maker_asset_id)
        side = "SELL"
        usdc_amount = taker_amount
        token_amount = maker_amount
    else:
        # Both non-USDC (rare: token-for-token swap in neg risk) or both USDC.
        token_id = str(maker_asset_id) if maker_asset_id != 0 else str(taker_asset_id)
        side = "UNKNOWN"
        usdc_amount = 0
        token_amount = max(maker_amount, taker_amount)

    # Both amounts are in 6-decimal (USDC) scale; tokens are 1:1 with USDC face value.
    price = None
    if token_amount > 0 and usdc_amount > 0:
        price = round(usdc_amount / token_amount, 6)

    condition_id = (
        cache.condition_id_for_token(token_id) if token_id != "0" else None
    )
    return token_id, condition_id, side, price, usdc_amount, token_amount


# ============ Log fetching ============

def fetch_logs(address, topics, from_block, to_block):
    """Fetch logs for given address and topics."""
    params = {
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
        "address": Web3.to_checksum_address(address),
        "topics": topics,
    }
    return w3.eth.get_logs(params)


def get_current_block():
    return w3.eth.block_number


# ============ Per-event processors ============
#
# Each processor reads a list of raw w3 log dicts, decodes them, and inserts
# into the corresponding table. They take an explicit ``conn`` and ``cache``
# so the outer driver controls transactions + cache lifetime.


def process_order_filled_logs(logs, exchange_name, conn, cache: BatchCache):
    """Parse OrderFilled logs and insert into order_fills. Returns row count."""
    if not logs:
        return 0

    rows = []
    for log in logs:
        bn = log["blockNumber"]
        ts = log_timestamp(log)

        tx_hash = log["transactionHash"]
        tx_hash = tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash
        log_idx = log["logIndex"]

        order_hash = decode_indexed_bytes32(log["topics"][1])
        maker = decode_indexed_address(log["topics"][2])
        taker = decode_indexed_address(log["topics"][3])

        data = log["data"] if isinstance(log["data"], bytes) else bytes(log["data"])
        maker_asset_id = decode_uint256(data, 0)
        taker_asset_id = decode_uint256(data, 1)
        maker_amount_filled = decode_uint256(data, 2)
        taker_amount_filled = decode_uint256(data, 3)
        fee = decode_uint256(data, 4)

        token_id, condition_id, side, price, usdc_amount, token_amount = derive_trade_fields(
            maker_asset_id, taker_asset_id, maker_amount_filled, taker_amount_filled, cache,
        )

        rows.append((
            tx_hash, log_idx, bn, ts, exchange_name,
            order_hash if order_hash.startswith("0x") else "0x" + order_hash,
            maker.lower(), taker.lower(),
            str(maker_asset_id), str(taker_asset_id),
            maker_amount_filled, taker_amount_filled, fee,
            token_id, condition_id, side, price, usdc_amount, token_amount,
        ))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO order_fills
            (tx_hash, log_index, block_number, block_timestamp, exchange,
             order_hash, maker, taker, maker_asset_id, taker_asset_id,
             maker_amount_filled, taker_amount_filled, fee,
             token_id, condition_id, side, price, usdc_amount, token_amount)
            VALUES %s
            ON CONFLICT (tx_hash, log_index) DO NOTHING
        """, rows, page_size=1000)
    return len(rows)


def process_orders_matched_logs(logs, exchange_name, conn, cache: BatchCache):
    """Parse OrdersMatched logs and insert into order_matches."""
    if not logs:
        return 0

    rows = []
    for log in logs:
        bn = log["blockNumber"]
        ts = log_timestamp(log)

        tx_hash = log["transactionHash"]
        tx_hash = tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash
        log_idx = log["logIndex"]

        taker_order_hash = decode_indexed_bytes32(log["topics"][1])
        taker_order_maker = decode_indexed_address(log["topics"][2])

        data = log["data"] if isinstance(log["data"], bytes) else bytes(log["data"])
        maker_asset_id = decode_uint256(data, 0)
        taker_asset_id = decode_uint256(data, 1)
        maker_amount_filled = decode_uint256(data, 2)
        taker_amount_filled = decode_uint256(data, 3)

        token_id, condition_id, _, price, usdc_amount, token_amount = derive_trade_fields(
            maker_asset_id, taker_asset_id, maker_amount_filled, taker_amount_filled, cache,
        )

        rows.append((
            tx_hash, log_idx, bn, ts, exchange_name,
            taker_order_hash if taker_order_hash.startswith("0x") else "0x" + taker_order_hash,
            taker_order_maker.lower(),
            str(maker_asset_id), str(taker_asset_id),
            maker_amount_filled, taker_amount_filled,
            token_id, condition_id, price, usdc_amount, token_amount,
        ))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO order_matches
            (tx_hash, log_index, block_number, block_timestamp, exchange,
             taker_order_hash, taker_order_maker,
             maker_asset_id, taker_asset_id,
             maker_amount_filled, taker_amount_filled,
             token_id, condition_id, price, usdc_amount, token_amount)
            VALUES %s
            ON CONFLICT (tx_hash, log_index) DO NOTHING
        """, rows, page_size=1000)
    return len(rows)


# ============ V2 decoders (post-2026-04-28 cutover, block 86,126,998) ============
#
# V2 OrderFilled and V2 OrdersMatched have a different on-wire layout than V1.
# Instead of inferring (token_id, side) from the (makerAssetId, takerAssetId)
# pair where one element is 0-for-collateral, V2 ships `side` (uint8 enum) and
# `tokenId` (uint256) as direct event fields. V2 also adds two trailing
# bytes32 fields — `builder` (origin tag) and `metadata` (free-form
# attribution hash) — which we persist into the new `order_fills.builder` and
# `order_fills.metadata` columns added by 2026_05_08_v2_indexer.sql.
#
# To keep the existing schema and downstream queries working unchanged, the
# V2 decoder synthesises a V1-shape (maker_asset_id, taker_asset_id) pair
# from the V2 (side, tokenId): one slot is "0" (collateral leg), the other
# carries tokenId, picked by side. Result: a V2 row looks structurally
# identical to a V1 row in every column rollups / AI prompts read, plus the
# extra V2-only fields. `exchange_version=2` lets analytics scope explicitly
# when needed.
#
# Storage scaling note: V2 amounts arrive in pUSD (PolymarketUSD wrapper)
# but pUSD is a 1:1 6-decimal wrapper around USDC, so /1e6 in the existing
# downstream queries is correct as-is. Verified on-chain 2026-05-08.

# V2 Side enum (Solidity ordering: 0 = BUY, 1 = SELL).
V2_SIDE_BUY = 0
V2_SIDE_SELL = 1


def _v2_synthesize_legacy_asset_pair(side: int, token_id: int) -> tuple[int, int]:
    """Map V2 (side, tokenId) → V1 (makerAssetId, takerAssetId) shape.

    V1 convention: the "collateral leg" of the trade is encoded as 0 in one
    of the two asset_id slots; the other slot carries the CTF token id. The
    side is recovered downstream by checking which slot is 0:
      - maker BUY  → maker pays USDC (slot 0 = 0), receives tokens (slot 1 = tokenId)
      - maker SELL → maker pays tokens (slot 0 = tokenId), receives USDC (slot 1 = 0)
    """
    if side == V2_SIDE_BUY:
        return 0, token_id
    if side == V2_SIDE_SELL:
        return token_id, 0
    # Unknown side enum (shouldn't happen — Solidity constrains uint8 + 2-value
    # enum at the source). Fall through to a safe pair so the row still inserts;
    # derive_trade_fields will tag side="UNKNOWN" downstream.
    return token_id, token_id


def process_order_filled_v2_logs(logs, exchange_name, conn, cache: BatchCache):
    """Parse V2 OrderFilled logs and insert into order_fills.

    V2 ABI:
      event OrderFilled(
        bytes32 indexed orderHash,
        address indexed maker,
        address indexed taker,
        uint8   side,                  // 0=BUY, 1=SELL (maker's perspective)
        uint256 tokenId,               // CTF outcome token id
        uint256 makerAmountFilled,
        uint256 takerAmountFilled,
        uint256 fee,
        bytes32 builder,
        bytes32 metadata
      )

    Topics:    [0]=signature, [1]=orderHash, [2]=maker, [3]=taker
    Data slots: 0=side, 1=tokenId, 2=makerAmt, 3=takerAmt, 4=fee, 5=builder, 6=metadata
    Total data: 0xe0 (224 bytes).
    """
    if not logs:
        return 0

    rows = []
    for log in logs:
        bn = log["blockNumber"]
        ts = log_timestamp(log)

        tx_hash = log["transactionHash"]
        tx_hash = tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash
        log_idx = log["logIndex"]

        order_hash = decode_indexed_bytes32(log["topics"][1])
        maker = decode_indexed_address(log["topics"][2])
        taker = decode_indexed_address(log["topics"][3])

        data = log["data"] if isinstance(log["data"], bytes) else bytes(log["data"])
        side_uint = decode_uint256(data, 0)
        token_id_uint = decode_uint256(data, 1)
        maker_amount_filled = decode_uint256(data, 2)
        taker_amount_filled = decode_uint256(data, 3)
        fee = decode_uint256(data, 4)
        # bytes32 reads as 32-byte raw slot — preserve as 0x-prefixed hex.
        builder = "0x" + data[5 * 32:6 * 32].hex()
        metadata = "0x" + data[6 * 32:7 * 32].hex()

        # Synthesise V1-shape asset pair so downstream queries keep working.
        maker_asset_id, taker_asset_id = _v2_synthesize_legacy_asset_pair(side_uint, token_id_uint)
        token_id, condition_id, side, price, usdc_amount, token_amount = derive_trade_fields(
            maker_asset_id, taker_asset_id, maker_amount_filled, taker_amount_filled, cache,
        )

        rows.append((
            tx_hash, log_idx, bn, ts, exchange_name,
            order_hash if order_hash.startswith("0x") else "0x" + order_hash,
            maker.lower(), taker.lower(),
            str(maker_asset_id), str(taker_asset_id),
            maker_amount_filled, taker_amount_filled, fee,
            token_id, condition_id, side, price, usdc_amount, token_amount,
            builder, metadata, 2,
        ))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO order_fills
            (tx_hash, log_index, block_number, block_timestamp, exchange,
             order_hash, maker, taker, maker_asset_id, taker_asset_id,
             maker_amount_filled, taker_amount_filled, fee,
             token_id, condition_id, side, price, usdc_amount, token_amount,
             builder, metadata, exchange_version)
            VALUES %s
            ON CONFLICT (tx_hash, log_index) DO NOTHING
        """, rows, page_size=1000)
    return len(rows)


def process_orders_matched_v2_logs(logs, exchange_name, conn, cache: BatchCache):
    """Parse V2 OrdersMatched logs and insert into order_matches.

    V2 ABI:
      event OrdersMatched(
        bytes32 indexed takerOrderHash,
        address indexed takerOrderMaker,
        uint8   side,
        uint256 tokenId,
        uint256 makerAmountFilled,
        uint256 takerAmountFilled
      )

    Topics:    [0]=signature, [1]=takerOrderHash, [2]=takerOrderMaker
    Data slots: 0=side, 1=tokenId, 2=makerAmt, 3=takerAmt
    Total data: 0x80 (128 bytes).

    Note: V2 dropped `maker_order_maker` (the matched maker side) from the
    event — that information is now redundant because per-fill OrderFilled
    events already carry each maker. The row inserts with NULL there.
    """
    if not logs:
        return 0

    rows = []
    for log in logs:
        bn = log["blockNumber"]
        ts = log_timestamp(log)

        tx_hash = log["transactionHash"]
        tx_hash = tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash
        log_idx = log["logIndex"]

        taker_order_hash = decode_indexed_bytes32(log["topics"][1])
        taker_order_maker = decode_indexed_address(log["topics"][2])

        data = log["data"] if isinstance(log["data"], bytes) else bytes(log["data"])
        side_uint = decode_uint256(data, 0)
        token_id_uint = decode_uint256(data, 1)
        maker_amount_filled = decode_uint256(data, 2)
        taker_amount_filled = decode_uint256(data, 3)

        maker_asset_id, taker_asset_id = _v2_synthesize_legacy_asset_pair(side_uint, token_id_uint)
        token_id, condition_id, side_str, price, usdc_amount, token_amount = derive_trade_fields(
            maker_asset_id, taker_asset_id, maker_amount_filled, taker_amount_filled, cache,
        )

        rows.append((
            tx_hash, log_idx, bn, ts, exchange_name,
            taker_order_hash if taker_order_hash.startswith("0x") else "0x" + taker_order_hash,
            taker_order_maker.lower(),
            str(maker_asset_id), str(taker_asset_id),
            maker_amount_filled, taker_amount_filled,
            token_id, condition_id, price, usdc_amount, token_amount,
            side_str, 2,
        ))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO order_matches
            (tx_hash, log_index, block_number, block_timestamp, exchange,
             taker_order_hash, taker_order_maker,
             maker_asset_id, taker_asset_id,
             maker_amount_filled, taker_amount_filled,
             token_id, condition_id, price, usdc_amount, token_amount,
             side, exchange_version)
            VALUES %s
            ON CONFLICT (tx_hash, log_index) DO NOTHING
        """, rows, page_size=1000)
    return len(rows)


def process_resolution_logs(logs, conn, cache: BatchCache):
    """Parse ConditionResolution logs and insert into resolutions + update markets.

    Markets.resolved / resolution_payout / resolved_at are updated in a single
    bulk statement derived from a VALUES table, not one UPDATE per row.
    """
    if not logs:
        return 0

    insert_rows = []
    market_updates = []  # (condition_id, payout_json, resolved_at)

    for log in logs:
        bn = log["blockNumber"]
        ts = log_timestamp(log)

        tx_hash = log["transactionHash"]
        tx_hash = tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash
        log_idx = log["logIndex"]

        condition_id = decode_indexed_bytes32(log["topics"][1])
        oracle = decode_indexed_address(log["topics"][2])
        question_id = decode_indexed_bytes32(log["topics"][3])

        data = log["data"] if isinstance(log["data"], bytes) else bytes(log["data"])
        outcome_slot_count = decode_uint256(data, 0)
        payout_numerators = decode_uint256_array(data, 1)

        cid = condition_id if condition_id.startswith("0x") else "0x" + condition_id
        qid = question_id if question_id.startswith("0x") else "0x" + question_id
        payout_json = json.dumps(payout_numerators)

        insert_rows.append((
            tx_hash, log_idx, bn, ts,
            cid, oracle.lower(), qid,
            outcome_slot_count, payout_json,
        ))
        market_updates.append((cid, payout_json, ts))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO resolutions
            (tx_hash, log_index, block_number, block_timestamp,
             condition_id, oracle, question_id, outcome_slot_count, payout_numerators)
            VALUES %s
            ON CONFLICT (tx_hash, log_index) DO NOTHING
        """, insert_rows, page_size=1000)

        # Bulk UPDATE markets via a VALUES table JOIN — one round-trip
        # instead of one per resolution.
        psycopg2.extras.execute_values(cur, """
            UPDATE markets AS m SET
                resolved = TRUE,
                resolution_payout = v.payout::jsonb,
                resolved_at = v.resolved_at::timestamptz,
                updated_at = NOW()
            FROM (VALUES %s) AS v(condition_id, payout, resolved_at)
            WHERE m.condition_id = v.condition_id
        """, market_updates, page_size=1000)

    return len(insert_rows)


def _process_split_or_merge_logs(logs, conn, table_name: str):
    """Shared decoder for PositionSplit / PositionsMerge (identical layout).

    ABI: ``event PositionSplit/PositionsMerge(address indexed stakeholder,
    address collateralToken, bytes32 indexed parentCollectionId,
    bytes32 indexed conditionId, uint256[] partition, uint256 amount)``.

    Indexed (in topics):
      topics[1] = stakeholder (address)
      topics[2] = parentCollectionId (bytes32)
      topics[3] = conditionId (bytes32)

    Non-indexed data area:
      slot 0 = collateralToken (address, right-padded in 32 bytes)
      slot 1 = partition[] offset
      slot 2 = amount (uint256)
      then [partition.length, partition[0], partition[1], ...]
    """
    if not logs:
        return 0

    rows = []
    for log in logs:
        bn = log["blockNumber"]
        ts = log_timestamp(log)

        tx_hash = log["transactionHash"]
        tx_hash = tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash
        log_idx = log["logIndex"]

        stakeholder = decode_indexed_address(log["topics"][1])
        parent_coll = decode_indexed_bytes32(log["topics"][2])
        condition_id = decode_indexed_bytes32(log["topics"][3])

        data = log["data"] if isinstance(log["data"], bytes) else bytes(log["data"])
        # Address sits in the last 20 bytes of the 32-byte slot.
        collateral_token = "0x" + data[12:32].hex()
        partition = decode_uint256_array(data, 1)
        amount = decode_uint256(data, 2)

        cid = condition_id if condition_id.startswith("0x") else "0x" + condition_id
        pcoll = parent_coll if parent_coll.startswith("0x") else "0x" + parent_coll

        rows.append((
            tx_hash, log_idx, bn, ts,
            stakeholder.lower(), collateral_token.lower(),
            pcoll, cid,
            json.dumps(partition), amount,
        ))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, f"""
            INSERT INTO {table_name}
            (tx_hash, log_index, block_number, block_timestamp,
             stakeholder, collateral_token, parent_collection_id, condition_id,
             partition, amount)
            VALUES %s
            ON CONFLICT (tx_hash, log_index) DO NOTHING
        """, rows, page_size=1000)
    return len(rows)


def process_position_split_logs(logs, conn, cache: BatchCache):
    """Insert PositionSplit logs into ``position_splits``."""
    return _process_split_or_merge_logs(logs, conn, "position_splits")


def process_positions_merge_logs(logs, conn, cache: BatchCache):
    """Insert PositionsMerge logs into ``position_merges``."""
    return _process_split_or_merge_logs(logs, conn, "position_merges")


def process_redemption_logs(logs, conn, cache: BatchCache):
    """Parse PayoutRedemption logs and insert into redemptions.

    ABI layout of the non-indexed data area:
      slot 0: conditionId  (bytes32, static)
      slot 1: indexSets    (uint256[] offset — dynamic)
      slot 2: payout       (uint256, static)
    """
    if not logs:
        return 0

    rows = []
    for log in logs:
        bn = log["blockNumber"]
        ts = log_timestamp(log)

        tx_hash = log["transactionHash"]
        tx_hash = tx_hash.hex() if isinstance(tx_hash, bytes) else tx_hash
        log_idx = log["logIndex"]

        redeemer = decode_indexed_address(log["topics"][1])
        collateral_token = decode_indexed_address(log["topics"][2])
        # topics[3] is parentCollectionId (indexed bytes32) — unused.

        data = log["data"] if isinstance(log["data"], bytes) else bytes(log["data"])
        condition_id = "0x" + data[0:32].hex()
        index_sets = decode_uint256_array(data, 1)
        payout = decode_uint256(data, 2)

        rows.append((
            tx_hash, log_idx, bn, ts,
            redeemer.lower(), collateral_token.lower(),
            condition_id,
            json.dumps(index_sets), payout,
        ))

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, """
            INSERT INTO redemptions
            (tx_hash, log_index, block_number, block_timestamp,
             redeemer, collateral_token, condition_id, index_sets, payout)
            VALUES %s
            ON CONFLICT (tx_hash, log_index) DO NOTHING
        """, rows, page_size=1000)

    return len(rows)
