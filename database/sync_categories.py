#!/usr/bin/env python3
"""Sync market categories from Gamma API events endpoint."""

import time
import requests
import psycopg2
from db import get_conn

GAMMA_API = "https://gamma-api.polymarket.com"


def sync_categories():
    conn = get_conn()
    offset = 0
    total_updated = 0
    total_events = 0

    print("Syncing categories from Gamma events API...")

    while True:
        try:
            resp = requests.get(
                f"{GAMMA_API}/events",
                params={"limit": 100, "offset": offset},
                timeout=30
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            print(f"  Error at offset {offset}: {e}")
            time.sleep(3)
            continue

        if not events:
            break

        with conn.cursor() as cur:
            for event in events:
                event_id = event.get("id")
                category = event.get("category")
                event_title = event.get("title")
                markets = event.get("markets", [])

                if not markets:
                    # Try to get markets from the event's slug
                    continue

                for m in markets:
                    cond_id = m.get("conditionId") or m.get("condition_id")
                    if not cond_id:
                        continue
                    cur.execute("""
                        UPDATE markets
                        SET category = %s, event_title = %s, event_id = %s
                        WHERE condition_id = %s AND (category IS NULL OR category != %s)
                    """, (category, event_title, event_id, cond_id, category))
                    if cur.rowcount > 0:
                        total_updated += cur.rowcount

                total_events += 1

        conn.commit()
        print(f"  Events: {total_events}, markets updated: {total_updated} (offset={offset})")
        offset += 100

        if len(events) < 100:
            break

        time.sleep(0.3)

    conn.close()
    print(f"Done. {total_events} events processed, {total_updated} markets tagged.")


if __name__ == "__main__":
    sync_categories()
