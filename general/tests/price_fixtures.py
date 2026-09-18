"""Снимок цены для проверок настоящего PriceProvider."""
import json
import time


def price_snapshot(price, event_ms=None):
    return json.dumps({
        "price": str(price),
        "event_time_ms": int(time.time() * 1000) if event_ms is None else event_ms,
    })
