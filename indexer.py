"""On-chain event indexer for Polymarket trades, resolutions, and redemptions.

Uses eth_getLogs via the Polygon full node to fetch events in batches,
decode them, and insert into PostgreSQL.
"""

import json
import time
import traceback
from datetime import datetime, timezone
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from db import get_conn, get_state, set_state
from config import (
    POLYGON_RPC, CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE, CONDITIONAL_TOKENS,
    TOPIC_ORDER_FILLED, TOPIC_ORDERS_MATCHED,
    TOPIC_CONDITION_RESOLUTION, TOPIC_PAYOUT_REDEMPTION,
    CTF_EXCHANGE_START_BLOCK, NEG_RISK_START_BLOCK,
    CONDITIONAL_TOKENS_START_BLOCK, LOG_BATCH_SIZE
)

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


def get_block_timestamp(block_number, _batch_cache={}):
    """Get block timestamp. Cache is cleared between batches by caller."""
    if block_number in _batch_cache:
        return _batch_cache[block_number]
    block = w3.eth.get_block(block_number)
    ts = datetime.fromtimestamp(block['timestamp'], tz=timezone.utc)
    _batch_cache[block_number] = ts
    return ts


def clear_batch_cache():
    get_block_timestamp.__defaults__[0].clear()


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
    return int.from_bytes(data[start:start+32], 'big')


def decode_uint256_array(data, offset_slot):
    """Decode a dynamic uint256[] from ABI-encoded data."""
    # The offset_slot points to a 32-byte word that contains the byte offset
    # of the array data from the start of the data area
    ptr = decode_uint256(data, offset_slot)
    byte_ptr = ptr  # byte offset
    length = int.from_bytes(data[byte_ptr:byte_ptr+32], 'big')
    arr = []
    for i in range(length):
        start = byte_ptr + 32 + i * 32
        arr.append(int.from_bytes(data[start:start+32], 'big'))
    return arr


def resolve_token_info(token_id_str, conn, _batch_cache={}):
    """Look up condition_id for a token_id. Cache cleared between batches."""
    if token_id_str in _batch_cache:
        return _batch_cache[token_id_str]
    with conn.cursor() as cur:
        cur.execute("SELECT condition_id FROM token_market_map WHERE token_id = %s", (token_id_str,))
        row = cur.fetchone()
    result = row[0] if row else None
    _batch_cache[token_id_str] = result
    return result


def clear_token_cache():
    resolve_token_info.__defaults__[0].clear()


def derive_trade_fields(maker_asset_id, taker_asset_id, maker_amount, taker_amount, conn):
    """Derive token_id, side, price, usdc_amount, token_amount from raw fill data."""
    maker_is_usdc = (maker_asset_id == 0)
    taker_is_usdc = (taker_asset_id == 0)

    if maker_is_usdc and not taker_is_usdc:
        # Maker is paying USDC, receiving tokens -> maker is BUYING
        token_id = str(taker_asset_id)
        side = "BUY"
        usdc_amount = maker_amount
        token_amount = taker_amount
    elif taker_is_usdc and not maker_is_usdc:
        # Maker is paying tokens, receiving USDC -> maker is SELLING
        token_id = str(maker_asset_id)
        side = "SELL"
        usdc_amount = taker_amount
        token_amount = maker_amount
    else:
        # Both non-USDC (rare: token-for-token swap in neg risk) or both USDC
        token_id = str(maker_asset_id) if maker_asset_id != 0 else str(taker_asset_id)
        side = "UNKNOWN"
        usdc_amount = 0
        token_amount = max(maker_amount, taker_amount)

    price = None
    if token_amount > 0 and usdc_amount > 0:
        # Both amounts are in 6-decimal (USDC) scale, but tokens are 1:1 with USDC face value
        # Price = USDC / tokens
        price = round(usdc_amount / token_amount, 6)

    condition_id = resolve_token_info(token_id, conn) if token_id != "0" else None

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


# ============ OrderFilled indexer ============

def process_order_filled_logs(logs, exchange_name, conn):
    """Parse OrderFilled logs and insert into order_fills."""
    if not logs:
        return 0

    rows = []
    block_timestamps = {}

    for log in logs:
        bn = log['blockNumber']
        if bn not in block_timestamps:
            block_timestamps[bn] = get_block_timestamp(bn)

        tx_hash = log['transactionHash'].hex() if isinstance(log['transactionHash'], bytes) else log['transactionHash']
        log_idx = log['logIndex']

        # Decode indexed topics
        order_hash = decode_indexed_bytes32(log['topics'][1])
        maker = decode_indexed_address(log['topics'][2])
        taker = decode_indexed_address(log['topics'][3])

        # Decode non-indexed data
        data = bytes(log['data']) if not isinstance(log['data'], bytes) else log['data']
        maker_asset_id = decode_uint256(data, 0)
        taker_asset_id = decode_uint256(data, 1)
        maker_amount_filled = decode_uint256(data, 2)
        taker_amount_filled = decode_uint256(data, 3)
        fee = decode_uint256(data, 4)

        token_id, condition_id, side, price, usdc_amount, token_amount = derive_trade_fields(
            maker_asset_id, taker_asset_id, maker_amount_filled, taker_amount_filled, conn
        )

        rows.append((
            tx_hash, log_idx, bn, block_timestamps[bn], exchange_name,
            "0x" + order_hash if not order_hash.startswith("0x") else order_hash,
            maker.lower(), taker.lower(),
            str(maker_asset_id), str(taker_asset_id),
            maker_amount_filled, taker_amount_filled, fee,
            token_id, condition_id, side, price, usdc_amount, token_amount
        ))

    if rows:
        import psycopg2.extras
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
        conn.commit()

    return len(rows)


# ============ OrdersMatched indexer ============

def process_orders_matched_logs(logs, exchange_name, conn):
    """Parse OrdersMatched logs and insert into order_matches."""
    if not logs:
        return 0

    rows = []
    block_timestamps = {}

    for log in logs:
        bn = log['blockNumber']
        if bn not in block_timestamps:
            block_timestamps[bn] = get_block_timestamp(bn)

        tx_hash = log['transactionHash'].hex() if isinstance(log['transactionHash'], bytes) else log['transactionHash']
        log_idx = log['logIndex']

        taker_order_hash = decode_indexed_bytes32(log['topics'][1])
        taker_order_maker = decode_indexed_address(log['topics'][2])

        data = bytes(log['data']) if not isinstance(log['data'], bytes) else log['data']
        maker_asset_id = decode_uint256(data, 0)
        taker_asset_id = decode_uint256(data, 1)
        maker_amount_filled = decode_uint256(data, 2)
        taker_amount_filled = decode_uint256(data, 3)

        token_id, condition_id, _, price, usdc_amount, token_amount = derive_trade_fields(
            maker_asset_id, taker_asset_id, maker_amount_filled, taker_amount_filled, conn
        )

        rows.append((
            tx_hash, log_idx, bn, block_timestamps[bn], exchange_name,
            "0x" + taker_order_hash if not taker_order_hash.startswith("0x") else taker_order_hash,
            taker_order_maker.lower(),
            str(maker_asset_id), str(taker_asset_id),
            maker_amount_filled, taker_amount_filled,
            token_id, condition_id, price, usdc_amount, token_amount
        ))

    if rows:
        import psycopg2.extras
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
        conn.commit()

    return len(rows)


# ============ ConditionResolution indexer ============

def process_resolution_logs(logs, conn):
    """Parse ConditionResolution logs and insert into resolutions + update markets."""
    if not logs:
        return 0

    rows = []
    block_timestamps = {}

    for log in logs:
        bn = log['blockNumber']
        if bn not in block_timestamps:
            block_timestamps[bn] = get_block_timestamp(bn)

        tx_hash = log['transactionHash'].hex() if isinstance(log['transactionHash'], bytes) else log['transactionHash']
        log_idx = log['logIndex']

        condition_id = decode_indexed_bytes32(log['topics'][1])
        oracle = decode_indexed_address(log['topics'][2])
        question_id = decode_indexed_bytes32(log['topics'][3])

        data = bytes(log['data']) if not isinstance(log['data'], bytes) else log['data']
        outcome_slot_count = decode_uint256(data, 0)
        payout_numerators = decode_uint256_array(data, 1)

        cid = "0x" + condition_id if not condition_id.startswith("0x") else condition_id
        qid = "0x" + question_id if not question_id.startswith("0x") else question_id
        ts = block_timestamps[bn]

        rows.append((
            tx_hash, log_idx, bn, ts,
            cid, oracle.lower(), qid,
            outcome_slot_count, json.dumps(payout_numerators)
        ))

    if rows:
        try:
            import psycopg2.extras
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO resolutions
                    (tx_hash, log_index, block_number, block_timestamp,
                     condition_id, oracle, question_id, outcome_slot_count, payout_numerators)
                    VALUES %s
                    ON CONFLICT (tx_hash, log_index) DO NOTHING
                """, rows, page_size=1000)
                # Update market resolved status for rows that exist in markets table
                for row in rows:
                    cid = row[4]
                    payout_json = row[8]
                    ts = row[3]
                    cur.execute("""
                        UPDATE markets SET resolved = TRUE, resolution_payout = %s, resolved_at = %s, updated_at = NOW()
                        WHERE condition_id = %s
                    """, (payout_json, ts, cid))
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"  Error inserting resolutions: {e}")
            return 0

    return len(rows)


# ============ PayoutRedemption indexer ============

def process_redemption_logs(logs, conn):
    """Parse PayoutRedemption logs and insert into redemptions."""
    if not logs:
        return 0

    rows = []
    block_timestamps = {}

    for log in logs:
        bn = log['blockNumber']
        if bn not in block_timestamps:
            block_timestamps[bn] = get_block_timestamp(bn)

        tx_hash = log['transactionHash'].hex() if isinstance(log['transactionHash'], bytes) else log['transactionHash']
        log_idx = log['logIndex']

        redeemer = decode_indexed_address(log['topics'][1])
        collateral_token = decode_indexed_address(log['topics'][2])
        # topics[3] is parentCollectionId (indexed bytes32), we skip it

        data = bytes(log['data']) if not isinstance(log['data'], bytes) else log['data']
        condition_id_raw = data[0:32]
        condition_id = "0x" + condition_id_raw.hex()

        # indexSets is dynamic array starting at slot 1
        index_sets = decode_uint256_array(data, 1)
        # payout is after the dynamic array — need to find it
        # Actually the ABI is: conditionId (bytes32), indexSets (uint256[]), payout (uint256)
        # conditionId is slot 0 (32 bytes)
        # indexSets offset is slot 1
        # payout offset is slot 2
        payout = decode_uint256(data, 2)

        # Wait, let me re-check. The non-indexed params are:
        # conditionId (bytes32) - static, slot 0
        # indexSets (uint256[]) - dynamic, slot 1 has offset
        # payout (uint256) - static, slot 2
        # Actually for mixed static+dynamic, the static slot 2 holds the payout directly? No.
        # In ABI encoding, dynamic types get offsets. Let me reconsider.
        # Layout: slot0 = conditionId (static bytes32), slot1 = offset to indexSets, slot2 = payout (static uint256)
        # This is correct because bytes32 and uint256 are static, uint256[] is dynamic.

        ts = block_timestamps[bn]

        rows.append((
            tx_hash, log_idx, bn, ts,
            redeemer.lower(), collateral_token.lower(),
            condition_id,
            json.dumps(index_sets), payout
        ))

    if rows:
        try:
            import psycopg2.extras
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(cur, """
                    INSERT INTO redemptions
                    (tx_hash, log_index, block_number, block_timestamp,
                     redeemer, collateral_token, condition_id, index_sets, payout)
                    VALUES %s
                    ON CONFLICT (tx_hash, log_index) DO NOTHING
                """, rows, page_size=1000)
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"  Error inserting redemptions: {e}")
            return 0

    return len(rows)


# ============ Main indexing loops ============

def index_exchange_events(exchange_address, exchange_name, state_key, start_block):
    """Index OrderFilled and OrdersMatched events for an exchange contract."""
    last_block = int(get_state(state_key, str(start_block)))
    current = get_current_block()
    conn = get_conn()

    total_fills = 0
    total_matches = 0
    from_block = last_block + 1

    print(f"Indexing {exchange_name} events from block {from_block} to {current} ({current - from_block} blocks)")

    try:
        while from_block <= current:
            to_block = min(from_block + LOG_BATCH_SIZE - 1, current)

            try:
                # Fetch OrderFilled logs
                fill_logs = fetch_logs(
                    exchange_address,
                    [TOPIC_ORDER_FILLED],
                    from_block, to_block
                )
                n_fills = process_order_filled_logs(fill_logs, exchange_name, conn)
                total_fills += n_fills

                # Fetch OrdersMatched logs
                match_logs = fetch_logs(
                    exchange_address,
                    [TOPIC_ORDERS_MATCHED],
                    from_block, to_block
                )
                n_matches = process_orders_matched_logs(match_logs, exchange_name, conn)
                total_matches += n_matches

            except Exception as e:
                print(f"  Error at blocks {from_block}-{to_block}: {e}")
                traceback.print_exc()
                time.sleep(2)
                # Try smaller batch
                if LOG_BATCH_SIZE > 100:
                    to_block = min(from_block + 100, current)
                    try:
                        fill_logs = fetch_logs(exchange_address, [TOPIC_ORDER_FILLED], from_block, to_block)
                        process_order_filled_logs(fill_logs, exchange_name, conn)
                        match_logs = fetch_logs(exchange_address, [TOPIC_ORDERS_MATCHED], from_block, to_block)
                        process_orders_matched_logs(match_logs, exchange_name, conn)
                    except Exception as e2:
                        print(f"  Still failing with smaller batch: {e2}")
                        from_block = to_block + 1
                        continue

            set_state(state_key, str(to_block))

            progress = (to_block - last_block) / max(current - last_block, 1) * 100
            if total_fills % 5000 < 500 or (to_block - from_block) == LOG_BATCH_SIZE - 1:
                print(f"  [{exchange_name}] Block {to_block}/{current} ({progress:.1f}%) | fills={total_fills} matches={total_matches}")

            from_block = to_block + 1
            time.sleep(0.05)  # gentle rate limit

    finally:
        conn.close()

    print(f"  [{exchange_name}] Done. {total_fills} fills, {total_matches} matches indexed.")
    return total_fills, total_matches


def index_conditional_token_events(start_block=None):
    """Index ConditionResolution and PayoutRedemption from Conditional Tokens contract."""
    if start_block is None:
        start_block = CONDITIONAL_TOKENS_START_BLOCK

    last_block_res = int(get_state("ct_resolution_last_block", str(start_block)))
    last_block_redeem = int(get_state("ct_redemption_last_block", str(start_block)))
    last_block = min(last_block_res, last_block_redeem)
    current = get_current_block()
    conn = get_conn()

    total_resolutions = 0
    total_redemptions = 0
    from_block = last_block + 1

    print(f"Indexing CT events from block {from_block} to {current} ({current - from_block} blocks)")

    try:
        while from_block <= current:
            to_block = min(from_block + LOG_BATCH_SIZE - 1, current)

            try:
                res_logs = fetch_logs(
                    CONDITIONAL_TOKENS,
                    [TOPIC_CONDITION_RESOLUTION],
                    from_block, to_block
                )
                n_res = process_resolution_logs(res_logs, conn)
                total_resolutions += n_res

                redeem_logs = fetch_logs(
                    CONDITIONAL_TOKENS,
                    [TOPIC_PAYOUT_REDEMPTION],
                    from_block, to_block
                )
                n_redeem = process_redemption_logs(redeem_logs, conn)
                total_redemptions += n_redeem

            except Exception as e:
                print(f"  Error at blocks {from_block}-{to_block}: {e}")
                time.sleep(2)
                from_block = to_block + 1
                continue

            set_state("ct_resolution_last_block", str(to_block))
            set_state("ct_redemption_last_block", str(to_block))

            progress = (to_block - last_block) / max(current - last_block, 1) * 100
            if total_resolutions % 100 < 10 or total_redemptions % 500 < 50:
                print(f"  [CT] Block {to_block}/{current} ({progress:.1f}%) | resolutions={total_resolutions} redemptions={total_redemptions}")

            from_block = to_block + 1
            time.sleep(0.05)

    finally:
        conn.close()

    print(f"  [CT] Done. {total_resolutions} resolutions, {total_redemptions} redemptions indexed.")
    return total_resolutions, total_redemptions
