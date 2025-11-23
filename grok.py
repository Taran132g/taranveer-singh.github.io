"""
Beginner-friendly overview
-------------------------
This file is the "brain" of the alerting pipeline. It does four things:
1) Reads config from the environment/CLI so you can tune symbols and thresholds.
2) Streams live market data from Schwab, cleaning it up into simple Python
   dicts.
3) Decides when an imbalance is "ask-heavy" or "bid-heavy" and writes that
   alert into a small SQLite database.
4) Immediately hands each alert to the in-process LiveTrader so trades can
   fire without waiting on a slower polling loop.

The comments scattered through the file aim to explain each stage in plain
language rather than trading jargon. Feel free to scroll to the section you
care about; each header notes what it controls.
"""

import os
import sys
import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlparse
from collections import deque, defaultdict
from dataclasses import dataclass
from time import time
from typing import Deque, Dict, List, Optional, TypedDict
import sqlite3
import json
import logging
from dotenv import load_dotenv
from schwab.auth import easy_client
from schwab.client import Client
from schwab.streaming import StreamClient

# Configure Logging
# Keep log lines structured and timestamped so you can follow what happened
# without digging through print statements.
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Small helper: print a JSON line with a consistent shape so humans and tools
# can read it easily. Warnings are elevated when they relate to alerts.
def log_structured(event: str, data: dict):
    level = logging.INFO if event != "ALERT" else logging.WARNING
    logging.log(level, json.dumps({"event": event, **data}))

# Exchange Code Mapping
# Schwab sends short exchange codes; this map turns them into readable names
# before we evaluate order-book imbalances.
EXCHANGE_MAP = {
    "NYSE": "NYSE",
    "MEMX": "MEMX",
    "IEXG": "IEX",
    "NSDQ": "NASDAQ",
    "NASDAQ": "NASDAQ",
    "ARCX": "NYSE_ARCA",
    "EDGX": "CBOE_EDGX",
    "MIAX": "MIAX",
    "BATX": "CBOE_BZX",
    "BATY": "CBOE_BYX",
    "MWSE": "MIAX_SAPPHIRE",
    "EDGA": "CBOE_EDGA",
    "AMEX": "NYSE_AMEX",
    "CINN": "CINCINNATI",
    "BOSX": "BOX",
    "PHLX": "NASDAQ_PHLX"
}

# Helpers
# Environment + parsing utilities so the rest of the file can assume clean
# inputs (no need to remember how each env var is formatted).
def _normalize_and_validate_callback(url: str) -> str:
    if not url:
        raise ValueError("SCHWAB_REDIRECT_URI is empty")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid SCHWAB_REDIRECT_URI '{url}'. Expected full URL like 'https://127.0.0.1:8182/'.")
    return url if url.endswith("/") else url + "/"

def _get_int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return max(default, minimum)
    try:
        val = int(raw)
    except ValueError:
        return max(default, minimum)
    return max(val, minimum)

def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

def _get_float_env(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return max(default, minimum)
    try:
        val = float(raw)
    except ValueError:
        return max(default, minimum)
    return max(val, minimum)

def _parse_symbols_from_env(var_name: str = "SYMBOLS", fallback: str = "F") -> List[str]:
    raw = os.getenv(var_name, fallback)
    parts = [p.strip().upper() for p in raw.replace(" ", ",").split(",")]
    seen, out = set(), []
    for p in parts:
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out

# Book Processing
# Convert the raw level-2 order book into bid/ask lists we can count. This is
# the heart of the imbalance detection logic.
class _Row(TypedDict, total=False):
    EX: str
    SIZE: int
    PRICE: float

class _Book(TypedDict, total=False):
    BIDS: List[_Row]
    ASKS: List[_Row]

def _flatten_l2(it: dict) -> _Book:
    out_bids: List[_Row] = []
    out_asks: List[_Row] = []
    symbol = it.get("key", "UNKNOWN")
    error_reported = {"missing_price": False, "parse_price_error": False, "invalid_price": False}

    if DEBUG_BOOK_RAW and _book_raw_remaining[symbol] > 0:
        _book_raw_remaining[symbol] -= 1
        log_structured("BOOK_RAW", {"symbol": symbol, "payload": it})

    bids_src = it.get("2", []) or it.get("BIDS", []) or []
    asks_src = it.get("3", []) or it.get("ASKS", []) or []

    def parse_entries(entries, is_bid: bool) -> List[_Row]:
        result = []
        if not isinstance(entries, list):
            if not error_reported.get("invalid_level", False):
                log_structured("L2_ERROR", {"symbol": symbol, "error": f"Non-list {'bids' if is_bid else 'asks'}"})
                error_reported["invalid_level"] = True
            return result
        for level in entries:
            if not isinstance(level, dict):
                if not error_reported.get("invalid_level", False):
                    log_structured("L2_ERROR", {"symbol": symbol, "error": f"Invalid {'bid' if is_bid else 'ask'} level"})
                    error_reported["invalid_level"] = True
                continue
            price = level.get("0") or level.get("BID_PRICE" if is_bid else "ASK_PRICE")
            if price is None:
                if not error_reported["missing_price"]:
                    log_structured("L2_ERROR", {"symbol": symbol, "error": "missing_price", "is_bid": is_bid})
                    error_reported["missing_price"] = True
                continue
            try:
                price_f = float(price)
            except (TypeError, ValueError) as err:
                if not error_reported["parse_price_error"]:
                    log_structured("L2_ERROR", {"symbol": symbol, "error": "parse_price_error", "is_bid": is_bid})
                    error_reported["parse_price_error"] = True
                continue
            if price_f <= 0:
                if not error_reported["invalid_price"]:
                    log_structured("L2_ERROR", {"symbol": symbol, "error": "invalid_price", "price": price_f, "is_bid": is_bid})
                    error_reported["invalid_price"] = True
                continue
            orders = level.get("3", []) or level.get("BIDS" if is_bid else "ASKS", [])
            if not orders:
                log_structured("L2_ERROR", {"symbol": symbol, "error": f"no_orders in {'bid' if is_bid else 'ask'}"})
                continue
            for order in orders:
                if not isinstance(order, dict):
                    log_structured("L2_ERROR", {"symbol": symbol, "error": f"invalid_order in {'bid' if is_bid else 'ask'}"})
                    continue
                ex = (order.get("0") or order.get("EXCHANGE") or "").upper()
                ex = "NASDAQ" if ex == "NSDQ" else ex
                if not ex or ex not in EXCHANGE_MAP:
                    log_structured("L2_ERROR", {"symbol": symbol, "error": "invalid_exchange", "exchange": ex})
                    continue
                vol = order.get("1") or order.get("BID_VOLUME" if is_bid else "ASK_VOLUME")
                try:
                    vol_i = int(float(vol))
                except (TypeError, ValueError):
                    log_structured("L2_ERROR", {"symbol": symbol, "error": f"parse_volume_error in {'bid' if is_bid else 'ask'}"})
                    continue
                if vol_i <= 0:
                    log_structured("L2_ERROR", {"symbol": symbol, "error": f"invalid_volume in {'bid' if is_bid else 'ask'}"})
                    continue
                result.append({"EX": ex, "SIZE": vol_i, "PRICE": price_f})
        if DEBUG and result:
            log_structured("L2_DEBUG", {
                "symbol": symbol,
                "is_bid": is_bid,
                "count": len(result),
                "exchanges": sorted({r["EX"] for r in result}),
                "top_price": max((r["PRICE"] for r in result), default=0.0),
                "total_volume": sum(r["SIZE"] for r in result)
            })
        return result

    out_bids.extend(parse_entries(bids_src, True))
    out_asks.extend(parse_entries(asks_src, False))

    top_bid = max((row["PRICE"] for row in out_bids), default=0.0)
    top_ask = min((row["PRICE"] for row in out_asks), default=0.0)
    total_bid_vol = sum(row["SIZE"] for row in out_bids)
    total_ask_vol = sum(row["SIZE"] for row in out_asks)
    log_structured("BOOK_SUMMARY", {
        "symbol": symbol,
        "top_bid": top_bid,
        "top_ask": top_ask,
        "bid_volume": total_bid_vol,
        "ask_volume": total_ask_vol,
        "spread_cents": (top_ask - top_bid) * 100.0 if top_bid and top_ask else 0.0
    })
    if DEBUG:
        log_structured("EXCHANGE_DEBUG", {
            "symbol": symbol,
            "bid_exchanges": sorted({row["EX"] for row in out_bids}),
            "ask_exchanges": sorted({row["EX"] for row in out_asks}),
            "total_exchanges": len(set(row["EX"] for row in out_bids) | set(row["EX"] for row in out_asks))
        })

    return {"BIDS": out_bids, "ASKS": out_asks}

@dataclass(frozen=True)
class BookMetrics:
    symbol: str
    total_bids: int
    total_asks: int
    ask_to_bid_ratio: float
    bid_to_ask_ratio: float
    ask_heavy_venues: int
    bid_heavy_venues: int
    per_venue: Dict[str, tuple[int, int]]
    valid_exchanges: int

def process_book(book: _Book, sym: str) -> BookMetrics:
    total_bids = 0
    total_asks = 0
    venue_cells: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    venue_prices: Dict[str, tuple[List[float], List[float]]] = defaultdict(lambda: ([], []))

    for row in book.get("BIDS", []):
        ex = row.get("EX", "UNK") or "UNK"
        try:
            size = int(row.get("SIZE", 0) or 0)
            price = float(row.get("PRICE", 0) or 0)
        except (TypeError, ValueError):
            continue
        if size > 0 and price > 0:
            venue_cells[ex][0] += size
            venue_prices[ex][0].append(price)

    for row in book.get("ASKS", []):
        ex = row.get("EX", "UNK") or "UNK"
        try:
            size = int(row.get("SIZE", 0) or 0)
            price = float(row.get("PRICE", 0) or 0)
        except (TypeError, ValueError):
            continue
        if size > 0 and price > 0:
            venue_cells[ex][1] += size
            venue_prices[ex][1].append(price)

    valid_venues: Dict[str, tuple[int, int]] = {}
    valid_exchanges = 0
    for ex, (bid_sum, ask_sum) in venue_cells.items():
        bid_prices, ask_prices = venue_prices[ex]
        if DEBUG:
            log_structured("VENUE_DEBUG", {
                "symbol": sym,
                "exchange": EXCHANGE_MAP.get(ex, ex),
                "bid_sum": bid_sum,
                "ask_sum": ask_sum,
                "bid_prices": bid_prices,
                "ask_prices": ask_prices
            })
        if bid_prices and ask_prices:
            spread_cents = (min(ask_prices) - max(bid_prices)) * 100.0
            if DEBUG:
                status = "ask-heavy" if ask_sum > bid_sum else "bid-heavy" if bid_sum > ask_sum else "balanced"
                log_structured("SPREAD_DEBUG", {
                    "symbol": sym,
                    "exchange": EXCHANGE_MAP.get(ex, ex),
                    "spread_cents": spread_cents,
                    "bids": bid_sum,
                    "asks": ask_sum,
                    "status": status,
                    "included": spread_cents <= MAX_RANGE_CENTS
                })
            if spread_cents <= MAX_RANGE_CENTS:
                valid_venues[ex] = (bid_sum, ask_sum)
                valid_exchanges += 1
                total_bids += bid_sum
                total_asks += ask_sum

    ask_heavy = sum(1 for b, a in valid_venues.values() if a > b)
    bid_heavy = sum(1 for b, a in valid_venues.values() if b > a)
    ask_to_bid_ratio = (total_asks / total_bids) if total_bids > 0 else float("inf")
    bid_to_ask_ratio = (total_bids / total_asks) if total_asks > 0 else float("inf")

    return BookMetrics(
        symbol=sym,
        total_bids=total_bids,
        total_asks=total_asks,
        ask_to_bid_ratio=ask_to_bid_ratio,
        bid_to_ask_ratio=bid_to_ask_ratio,
        ask_heavy_venues=ask_heavy,
        bid_heavy_venues=bid_heavy,
        per_venue=valid_venues,
        valid_exchanges=valid_exchanges,
    )

# Trade Data Structures
# Global knobs and rolling state that track how many heavy venues exist and
# when an alert was last triggered. Most users only tweak the constants near
# the top of this section.
SYMBOLS: List[str] = []
WINDOW_SECONDS: int = 60
HEARTBEAT_SEC: int = 5
MIN_ASK_HEAVY: int = 4
MIN_BID_HEAVY: int = 4
MAX_RANGE_CENTS: int = 1
ALERT_THROTTLE_SEC: int = 60
MIN_VOLUME: int = 100000
MIN_IMBALANCE_DURATION_SEC: float = 10.0
DB_PATH: str = "penny_basing.db"
DISABLE_BID_HEAVY: bool = False
PRINTED_NO_INSTR: set = set()
last_l1: Dict[str, dict] = {}
trades: Dict[str, Deque] = {}
last_cum_volume: Dict[str, int] = defaultdict(int)
msg_count: Dict[str, int] = {}
last_alert: Dict[str, float] = {}
last_imbalance: Dict[str, Deque[tuple[float, str, BookMetrics]]] = defaultdict(lambda: deque(maxlen=200))
_last_msg_ts: float = 0.0
PRINT_EVERY: int = 20
DEBUG_BOOK_RAW: bool = False
JSON_BOOK: bool = False
SHOW_BOOK: bool = False
BOOK_INTERVAL_SEC: int = 2
_book_raw_remaining: Dict[str, int] = defaultdict(lambda: 5)
volume_window: Dict[str, Deque[int]] = defaultdict(lambda: deque(maxlen=10))
exchange_cache: Dict[str, Optional[str]] = {}
alert_history: Dict[str, List[dict]] = defaultdict(list)
DEBUG: bool = False
_l1_debug_remaining: Dict[str, int] = defaultdict(lambda: 10)
_chart_debug_remaining: Dict[str, int] = defaultdict(lambda: 10)
_timesale_debug_remaining: Dict[str, int] = defaultdict(lambda: 10)
_last_chart_or_timesale_ts: Dict[str, float] = defaultdict(float)
_last_volume_fallback_ts: Dict[str, float] = defaultdict(float)
# Inline live trader hook
# -----------------------
# When grok writes a new alert to SQLite, it also forwards that alert directly
# to LiveTrader in the same process. This keeps latency as low as possible and
# avoids waiting for a separate polling script.
inline_trader_dispatch = None
inline_only_mode: bool = False
inline_only_next_alert_id: int = 0

@dataclass
class Trade:
    ts: float
    px: float
    sz: int

def _touch():
    global _last_msg_ts
    _last_msg_ts = time()

def _prune(sym: str, now_ts: float):
    q = trades[sym]
    cutoff = now_ts - WINDOW_SECONDS
    while q and q[0].ts < cutoff:
        q.popleft()

def _summarize(sym: str, now: float):
    q = trades[sym]
    if not q:
        log_structured("ROLL", {"symbol": sym, "message": "No prints yet"})
        return 0
    hi = max(t.px for t in q)
    lo = min(t.px for t in q)
    vol = sum(t.sz for t in q)
    volume_window[sym].append(vol)
    smoothed_vol = sum(volume_window[sym]) / len(volume_window[sym]) if volume_window[sym] else vol
    window_duration = max(min(now - q[0].ts, WINDOW_SECONDS), 1.0) if q else WINDOW_SECONDS
    vol_per_min = (smoothed_vol / (window_duration / 60)) if window_duration > 0 else 0
    log_structured("ROLL", {
        "symbol": sym,
        "window_sec": WINDOW_SECONDS,
        "high": hi,
        "low": lo,
        "range_cents": (hi - lo) * 100.0,
        "volume": vol,
        "vol_per_min": vol_per_min,
        "window_duration": window_duration
    })
    if DEBUG:
        log_structured("VOLUME_DEBUG", {
            "symbol": sym,
            "raw_volume": vol,
            "smoothed_volume": smoothed_vol,
            "volume_window": list(volume_window[sym]),
            "vol_per_min": vol_per_min,
            "window_duration": window_duration
        })
    return vol_per_min

# Handlers
# Callback functions triggered by Schwab streaming events. Each one updates the
# rolling state and may emit alerts when conditions are met.
def on_level1(msg: dict):
    for it in msg.get("content", []):
        sym = it.get("key")
        if sym in SYMBOLS:
            if DEBUG and _l1_debug_remaining[sym] > 0:
                _l1_debug_remaining[sym] -= 1
                log_structured("L1_DEBUG", {"symbol": sym, "payload": it})
            price = None
            missing_fields = []
            if "LAST_PRICE" not in it:
                missing_fields.append("LAST_PRICE")
            else:
                price = it.get("LAST_PRICE")
            if not price and "BID_PRICE" not in it:
                missing_fields.append("BID_PRICE")
            else:
                price = price or it.get("BID_PRICE")
            if not price and "ASK_PRICE" not in it:
                missing_fields.append("ASK_PRICE")
            else:
                price = price or it.get("ASK_PRICE")
            if not price and "CLOSE_PRICE" not in it:
                missing_fields.append("CLOSE_PRICE")
            else:
                price = price or it.get("CLOSE_PRICE")
            if not price and sym in last_l1:
                price = last_l1[sym].get("LAST_PRICE")
            if not price:
                log_structured("L1_WARNING", {
                    "symbol": sym,
                    "message": "No valid price",
                    "missing_fields": missing_fields
                })
                continue
            try:
                price = float(price)
                last_l1[sym] = it
            except (TypeError, ValueError):
                log_structured("L1_WARNING", {"symbol": sym, "message": "Invalid price", "value": price})
                continue

def on_chart_equity(msg: dict):
    now = time()
    for it in msg.get("content", []):
        sym = it.get("key")
        if sym not in trades:
            continue
        if DEBUG and _chart_debug_remaining[sym] > 0:
            _chart_debug_remaining[sym] -= 1
            log_structured("CHART_DEBUG", {"symbol": sym, "payload": it})
        _last_chart_or_timesale_ts[sym] = now
        t_ms = it.get("CHART_TIME", it.get("TIME"))
        try:
            ts = (float(t_ms) / 1000.0) if t_ms is not None else now
        except (TypeError, ValueError):
            ts = now
        px_val = (it.get("CLOSE_PRICE") or it.get("LAST_PRICE") or
                  it.get("PRICE") or it.get("OPEN_PRICE") or
                  it.get("HIGH_PRICE") or it.get("LOW_PRICE"))
        try:
            px = float(px_val)
        except (TypeError, ValueError):
            log_structured("CHART_ERROR", {"symbol": sym, "error": "Invalid price", "value": px_val})
            continue
        try:
            cum = int(it.get("VOLUME", 0) or 0)
        except (TypeError, ValueError):
            log_structured("CHART_ERROR", {"symbol": sym, "error": "Invalid volume", "value": it.get("VOLUME")})
            cum = 0
        prev = last_cum_volume[sym]
        delta = cum - prev
        if delta < 0:
            log_structured("CHART_WARNING", {"symbol": sym, "message": "Negative volume delta, resetting", "cum_volume": cum, "prev_volume": prev})
            last_cum_volume[sym] = cum
            trades[sym].clear()
            volume_window[sym].clear()
            continue
        last_cum_volume[sym] = cum
        if delta > 0:
            trades[sym].append(Trade(ts, px, delta))
            _prune(sym, ts)
            _touch()
            msg_count[sym] += 1
            if DEBUG:
                log_structured("CHART_VOLUME_DEBUG", {
                    "symbol": sym,
                    "cum_volume": cum,
                    "delta_volume": delta,
                    "trade_count": len(trades[sym])
                })
            if msg_count[sym] % PRINT_EVERY == 0:
                _summarize(sym, now)

def on_timesale(msg: dict):
    now = time()
    for it in msg.get("content", []):
        sym = it.get("key")
        if sym not in trades:
            continue
        if DEBUG and _timesale_debug_remaining[sym] > 0:
            _timesale_debug_remaining[sym] -= 1
            log_structured("TIMESALE_DEBUG", {"symbol": sym, "payload": it})
        _last_chart_or_timesale_ts[sym] = now
        px_val = it.get("LAST_PRICE", it.get("PRICE"))
        sz_val = it.get("LAST_SIZE", it.get("TRADE_SIZE", 0))
        t_ms = it.get("TRADE_TIME", it.get("TIME"))
        try:
            px = float(px_val)
            sz = int(sz_val or 0)
            ts = (float(t_ms) / 1000.0) if t_ms is not None else now
        except (TypeError, ValueError):
            log_structured("TIMESALE_ERROR", {"symbol": sym, "error": "Invalid price or size", "price": px_val, "size": sz_val})
            continue
        if sz > 0:
            trades[sym].append(Trade(ts, px, sz))
            _prune(sym, ts)
            _touch()
            msg_count[sym] += 1
            if DEBUG:
                log_structured("TIMESALE_VOLUME_DEBUG", {
                    "symbol": sym,
                    "trade_size": sz,
                    "trade_count": len(trades[sym])
                })
            if msg_count[sym] % PRINT_EVERY == 0:
                _summarize(sym, now)

def on_book(msg: dict):
    now = time()
    for it in msg.get("content", []):
        sym = it.get("key")
        if sym not in SYMBOLS:
            continue
        book = _flatten_l2(it)
        metrics = process_book(book, sym)
        _last_msg_ts = now
        price = last_l1.get(sym, {}).get("LAST_PRICE", 0.0)
        bid_price = max((b.get("PRICE", 0) for b in book["BIDS"]), default=0.0)
        ask_price = min((a.get("PRICE", 0) for a in book["ASKS"]), default=0.0)
        if not price and bid_price and ask_price:
            price = (bid_price + ask_price) / 2
            log_structured("PRICE_FALLBACK", {
                "symbol": sym,
                "bid_price": bid_price,
                "ask_price": ask_price,
                "midpoint": price
            })
        elif not price:
            log_structured("PRICE_FALLBACK_ERROR", {
                "symbol": sym,
                "bid_price": bid_price,
                "ask_price": ask_price
            })
        q = trades.get(sym, deque())
        vol_per_min = 0
        if (now - _last_chart_or_timesale_ts[sym]) > 30.0:
            log_structured("NO_DATA_WARNING", {
                "symbol": sym,
                "message": "No CHART_EQUITY or TIMESALE_EQUITY data for 30s"
            })
            if (now - _last_volume_fallback_ts[sym]) >= 10.0:
                est_volume = (metrics.total_bids + metrics.total_asks) // 2
                est_volume_per_min = (est_volume / (WINDOW_SECONDS / 60)) if est_volume > 0 else 0
                trades[sym].append(Trade(now, price or bid_price or ask_price or 0.0, est_volume))
                _last_volume_fallback_ts[sym] = now
                _prune(sym, now)
                log_structured("VOLUME_FALLBACK", {
                    "symbol": sym,
                    "est_volume": est_volume,
                    "vol_per_min": est_volume_per_min
                })
                vol_per_min = _summarize(sym, now)
            else:
                vol_per_min = _summarize(sym, now) if q else 0
        else:
            vol_per_min = _summarize(sym, now) if q else 0
        log_structured("IMBALANCE_DEBUG", {
            "symbol": sym,
            "ask_heavy": metrics.ask_heavy_venues,
            "bid_heavy": metrics.bid_heavy_venues,
            "valid_ex": metrics.valid_exchanges,
            "bids": metrics.total_bids,
            "asks": metrics.total_asks,
            "vol_per_min": vol_per_min,
            "price": price
        })
        direction = None
        if not DISABLE_BID_HEAVY and metrics.bid_heavy_venues >= metrics.ask_heavy_venues + 4:
            direction = "bid-heavy"
        elif metrics.ask_heavy_venues >= metrics.bid_heavy_venues + 4:
            direction = "ask-heavy"
        if direction:
            last_imbalance[sym].append((now, direction, metrics))
            # Check if the imbalance has persisted for at least MIN_IMBALANCE_DURATION_SEC
            imbalance_duration = 0.0
            if last_imbalance[sym]:
                first_ts = None
                for ts, dir, _ in reversed(last_imbalance[sym]):
                    if dir != direction:
                        break
                    first_ts = ts
                if first_ts is not None:
                    imbalance_duration = now - first_ts
            if DEBUG:
                log_structured("DIRECTION_DEBUG", {
                    "symbol": sym,
                    "direction": direction,
                    "bid_heavy_venues": metrics.bid_heavy_venues,
                    "ask_heavy_venues": metrics.ask_heavy_venues,
                    "imbalance_duration": round(imbalance_duration, 2)
                })
            if (imbalance_duration >= MIN_IMBALANCE_DURATION_SEC and
                metrics.valid_exchanges >= max(MIN_ASK_HEAVY, MIN_BID_HEAVY) and 
                vol_per_min >= MIN_VOLUME and
                (sym not in last_alert or (now - last_alert[sym]) >= ALERT_THROTTLE_SEC)):
                ratio = metrics.ask_to_bid_ratio if direction == "ask-heavy" else metrics.bid_to_ask_ratio
                heavy_venues = metrics.ask_heavy_venues if direction == "ask-heavy" else metrics.bid_heavy_venues
                alert = {
                    "timestamp": now,
                    "symbol": sym,
                    "ratio": ratio,
                    "total_bids": metrics.total_bids,
                    "total_asks": metrics.total_asks,
                    "heavy_venues": heavy_venues,
                    "direction": direction,
                    "price": price,
                    "exchanges": [EXCHANGE_MAP.get(ex, ex) for ex in metrics.per_venue.keys()]
                }
                alert_history[sym].append(alert)
                if len(alert_history[sym]) > 10:
                    alert_history[sym].pop(0)
                global inline_only_next_alert_id
                if inline_only_mode:
                    inline_only_next_alert_id += 1
                    next_alert_id = inline_only_next_alert_id
                else:
                    c = conn.cursor()
                    c.execute("SELECT IFNULL(MAX(rowid), 0) FROM alerts")
                    next_alert_id = (c.fetchone() or [0])[0] + 1
                if inline_trader_dispatch:
                    inline_trader_dispatch(next_alert_id, alert)
                if not inline_only_mode:
                    c.execute(
                        "INSERT INTO alerts (rowid, timestamp, symbol, ratio, total_bids, total_asks, heavy_venues, direction, price) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (next_alert_id, alert["timestamp"], alert["symbol"], alert["ratio"], alert["total_bids"],
                         alert["total_asks"], alert["heavy_venues"], alert["direction"], alert["price"])
                    )
                    alert_id = next_alert_id
                    conn.commit()
                last_alert[sym] = now
                log_structured("ALERT", {
                    "symbol": sym,
                    "direction": direction,
                    "ratio": round(ratio, 2),
                    "venues": heavy_venues,
                    "bids": metrics.total_bids,
                    "asks": metrics.total_asks,
                    "price": round(price, 4),
                    "vol_per_min": round(vol_per_min, 2),
                    "imbalance_duration": round(imbalance_duration, 2)
                })

async def resolve_exchange(client: Client, sym: str) -> Optional[str]:
    if sym in exchange_cache:
        return exchange_cache[sym]
    try:
        instruments = await client.get_instruments([sym], projection="fundamental")
        for inst in instruments:
            if inst.get("symbol") == sym:
                exchange = inst.get("primary_exchange")
                if exchange in {"NASDAQ", "NYSE"}:
                    exchange_cache[sym] = exchange
                    if DEBUG_INSTR:
                        log_structured("INSTR_DEBUG", {"symbol": sym, "exchange": exchange})
                    return exchange
                else:
                    exchange_cache[sym] = None
                    if DEBUG_INSTR:
                        log_structured("INSTR_DEBUG", {"symbol": sym, "exchange": exchange, "action": "subscribing to both"})
                    return None
        exchange_cache[sym] = None
        if DEBUG_INSTR:
            log_structured("INSTR_DEBUG", {"symbol": sym, "error": "No instrument found", "action": "subscribing to both"})
        return None
    except Exception as e:
        log_structured("INSTR_ERROR", {"symbol": sym, "error": str(e)})
        exchange_cache[sym] = None
        return None

async def _book_monitor_task():
    while True:
        log_structured("BOOK_MONITOR", {"message": "Monitoring book updates"})
        await asyncio.sleep(BOOK_INTERVAL_SEC)

async def _heartbeat_task():
    start = time()
    while True:
        now = time()
        age = (now - _last_msg_ts) if _last_msg_ts else float("inf")
        if age == float("inf"):
            log_structured("HEARTBEAT", {"status": "alive", "message": "No market data yet"})
        else:
            log_structured("HEARTBEAT", {"status": "alive", "last_data_age": round(age, 2)})
        await asyncio.sleep(HEARTBEAT_SEC)

# Main function
async def main():
    # CLI setup: these flags let you tune alert sensitivity without editing
    # code. Defaults come from environment variables so scripts and terminals
    # behave the same way.
    parser = argparse.ArgumentParser(description="Stream L2 data for penny basing detection.")
    parser.add_argument("--symbols", type=str, help="Comma or space separated symbols (overrides $SYMBOLS).")
    parser.add_argument("--window", type=int, help="Rolling window seconds (overrides $WINDOW_SECONDS).")
    parser.add_argument("--heartbeat", type=int, help="Heartbeat interval seconds (overrides $HEARTBEAT_SEC).")
    parser.add_argument("--min-venues", type=int, help="Min heavy venues for alert (overrides $MIN_ASK_HEAVY/$MIN_BID_HEAVY).")
    parser.add_argument("--max-range", type=int, help="Max exchange bid-ask spread in cents (overrides $MAX_RANGE_CENTS).")
    parser.add_argument("--throttle", type=int, help="Min seconds between alerts per symbol (overrides $ALERT_THROTTLE_SEC).")
    parser.add_argument("--min-volume", type=int, help="Min volume per minute for alert (overrides $MIN_VOLUME).")
    parser.add_argument("--min-imbalance-duration", type=float, help="Min duration in seconds for imbalance to trigger alert (overrides $MIN_IMBALANCE_DURATION_SEC).")
    parser.add_argument("--db-path", type=str, help="SQLite database path (overrides $DB_PATH).")
    parser.add_argument("--show-book", action="store_true", help="Show book updates.")
    parser.add_argument("--book-interval", type=int, help="Book print interval seconds (overrides $BOOK_INTERVAL_SEC).")
    parser.add_argument("--json-book", action="store_true", help="Show book as JSON.")
    parser.add_argument("--debug-instr", action="store_true", help="Debug: print instrument lookups.")
    parser.add_argument("--debug-book-raw", action="store_true", help="Debug: print raw book payloads.")
    parser.add_argument("--book-raw-limit", type=int, help="How many raw L2 payloads to print when --debug-book-raw is set (default 5).")
    parser.add_argument("--disable-bid-heavy", action="store_true", help="Disable bid-heavy (buyer) alerts.")
    parser.add_argument("--debug", action="store_true", help="Enable detailed debug logging.")
    args = parser.parse_args()

    load_dotenv()

    api_key = os.getenv("SCHWAB_CLIENT_ID")
    app_secret = os.getenv("SCHWAB_APP_SECRET")
    redirect_uri = os.getenv("SCHWAB_REDIRECT_URI")
    token_path = os.getenv("SCHWAB_TOKEN_PATH", "./schwab_tokens.json")
    account_id_s = os.getenv("SCHWAB_ACCOUNT_ID")

    if not api_key or not app_secret or not redirect_uri or not account_id_s:
        missing = [k for k, v in {
            "SCHWAB_CLIENT_ID": api_key,
            "SCHWAB_APP_SECRET": app_secret,
            "SCHWAB_REDIRECT_URI": redirect_uri,
            "SCHWAB_ACCOUNT_ID": account_id_s,
        }.items() if not v]
        log_structured("CONFIG_ERROR", {"missing_vars": missing})
        sys.exit(1)

    try:
        redirect_uri = _normalize_and_validate_callback(redirect_uri)
    except ValueError as e:
        log_structured("CONFIG_ERROR", {"error": str(e)})
        sys.exit(2)

    try:
        account_id = int(account_id_s)
    except ValueError:
        log_structured("CONFIG_ERROR", {"error": "SCHWAB_ACCOUNT_ID must be an integer"})
        sys.exit(2)

    global WINDOW_SECONDS, HEARTBEAT_SEC, MIN_ASK_HEAVY, MIN_BID_HEAVY
    global MAX_RANGE_CENTS, ALERT_THROTTLE_SEC, MIN_VOLUME, MIN_IMBALANCE_DURATION_SEC
    global DB_PATH, SYMBOLS, DISABLE_BID_HEAVY, trades, msg_count, _book_raw_remaining
    global DEBUG_BOOK_RAW, JSON_BOOK, SHOW_BOOK, BOOK_INTERVAL_SEC, DEBUG_INSTR, DEBUG

    WINDOW_SECONDS = args.window if args.window is not None else _get_int_env("WINDOW_SECONDS", 60, 30)
    HEARTBEAT_SEC = args.heartbeat if args.heartbeat is not None else _get_int_env("HEARTBEAT_SEC", 5, 1)
    MIN_ASK_HEAVY = args.min_venues if args.min_venues is not None else _get_int_env("MIN_ASK_HEAVY", 4, 1)
    MIN_BID_HEAVY = args.min_venues if args.min_venues is not None else _get_int_env("MIN_BID_HEAVY", 4, 1)
    MAX_RANGE_CENTS = args.max_range if args.max_range is not None else _get_int_env("MAX_RANGE_CENTS", 1, 1)
    ALERT_THROTTLE_SEC = args.throttle if args.throttle is not None else _get_int_env("ALERT_THROTTLE_SEC", 60, 10)
    MIN_VOLUME = args.min_volume if args.min_volume is not None else _get_int_env("MIN_VOLUME", 100000, 1000)
    MIN_IMBALANCE_DURATION_SEC = args.min_imbalance_duration if args.min_imbalance_duration is not None else _get_float_env("MIN_IMBALANCE_DURATION_SEC", 10.0, 0.0)
    DB_PATH = args.db_path if args.db_path is not None else os.getenv("DB_PATH", "penny_basing.db")
    os.environ["DB_PATH"] = str(DB_PATH)
    inline_only_requested = _bool_env("INLINE_DISPATCH_ONLY", False)
    # if args.symbols:
    #     SYMBOLS = [s.strip().upper() for s in args.symbols.replace(" ", ",").split(",") if s.strip()]
    # else:
    SYMBOLS = _parse_symbols_from_env("SYMBOLS", "F")
    DISABLE_BID_HEAVY = bool(args.disable_bid_heavy)
    DEBUG_BOOK_RAW = bool(args.debug_book_raw)
    JSON_BOOK = bool(args.json_book)
    SHOW_BOOK = bool(args.show_book)
    BOOK_INTERVAL_SEC = args.book_interval if args.book_interval is not None else _get_int_env("BOOK_INTERVAL_SEC", 2, 1)
    _book_raw_remaining = defaultdict(lambda: args.book_raw_limit if args.book_raw_limit is not None else _get_int_env("BOOK_RAW_LIMIT", 5, 1))
    DEBUG_INSTR = bool(args.debug_instr) or any(sym in {"CRON", "F"} for sym in SYMBOLS)
    DEBUG = bool(args.debug)

    if not SYMBOLS:
        log_structured("CONFIG_ERROR", {"error": "No symbols provided"})
        sys.exit(2)

    trades = {s: deque() for s in SYMBOLS}
    msg_count = {s: 0 for s in SYMBOLS}

    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    global conn, inline_only_next_alert_id
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("PRAGMA table_info(alerts)")
    columns = [info[1] for info in c.fetchall()]
    required_columns = {"timestamp", "symbol", "ratio", "total_bids", "total_asks", "heavy_venues", "direction", "price"}
    if not all(col in columns for col in required_columns):
        c.execute("DROP TABLE IF EXISTS alerts")
        c.execute('''
            CREATE TABLE alerts (
                timestamp REAL,
                symbol TEXT,
                ratio REAL,
                total_bids INTEGER,
                total_asks INTEGER,
                heavy_venues INTEGER,
                direction TEXT,
                price REAL
            )
        ''')
    inline_only_next_alert_id = (c.execute("SELECT IFNULL(MAX(rowid), 0) FROM alerts").fetchone() or [0])[0]
    conn.commit()

    global inline_trader_dispatch, inline_only_mode
    inline_trader_dispatch = None
    inline_only_mode = False
    try:
        from live_trader import LiveTrader

        inline_trader = LiveTrader(dry_run=_bool_env("INLINE_LIVE_DRY_RUN", False))
        loop = asyncio.get_running_loop()

        def inline_trader_dispatch(alert_id: int, alert: dict) -> None:
            asyncio.create_task(
                loop.run_in_executor(
                    None,
                    inline_trader.process_alert,
                    int(alert_id),
                    alert["symbol"],
                    alert["direction"],
                    float(alert["price"]),
                )
            )

        inline_only_mode = inline_only_requested
        inline_log = {"status": "enabled", "dry_run": inline_trader.dry_run}
        if inline_only_mode:
            inline_log["mode"] = "inline_only"
        log_structured("INLINE_TRADER", inline_log)
    except Exception as exc:
        inline_only_mode = False
        log_structured("INLINE_TRADER_ERROR", {"error": str(exc)})

    if inline_only_requested and not inline_only_mode:
        log_structured(
            "INLINE_TRADER",
            {"status": "inline_only_disabled", "reason": "dispatch unavailable"},
        )

    try:
        client = easy_client(api_key=api_key, app_secret=app_secret,
                            callback_url=redirect_uri, token_path=token_path)
    except Exception as e:
        log_structured("CLIENT_ERROR", {"error": f"Failed to initialize Schwab client: {e}"})
        sys.exit(3)

    stream = StreamClient(client, account_id=account_id)

    stream.add_level_one_equity_handler(on_level1)
    has_ts = hasattr(stream, "add_timesale_equity_handler") and hasattr(stream, "timesale_equity_subs")
    if has_ts:
        stream.add_timesale_equity_handler(on_timesale)
    else:
        stream.add_chart_equity_handler(on_chart_equity)
    stream.add_nasdaq_book_handler(on_book)
    stream.add_nyse_book_handler(on_book)

    async def connect_with_retries(max_attempts: int = 3):
        for attempt in range(1, max_attempts + 1):
            try:
                await stream.login()
                log_structured("SUBS", {"status": "success"})
                return True
            except Exception as e:
                log_structured("SUBS_ERROR", {"attempt": attempt, "error": str(e)})
                if attempt < max_attempts:
                    await asyncio.sleep(5)
                continue
        log_structured("SUBS_ERROR", {"error": "Max retry attempts reached"})
        return False

    if not await connect_with_retries():
        log_structured("SUBS_ERROR", {"error": "Failed to establish stream connection"})
        sys.exit(4)

    try:
        if hasattr(stream, "quality_of_service"):
            await stream.quality_of_service(StreamClient.QOSLevel.EXPRESS)
        elif hasattr(stream, "set_quality_of_service"):
            await stream.set_quality_of_service(StreamClient.QOSLevel.EXPRESS)
        elif hasattr(stream, "set_qos"):
            await stream.set_qos(StreamClient.QOSLevel.EXPRESS)
    except Exception as e:
        log_structured("QOS_ERROR", {"error": f"Failed to set QoS: {e}"})

    await stream.level_one_equity_subs(SYMBOLS)
    if has_ts:
        await stream.timesale_equity_subs(SYMBOLS)
    else:
        await stream.chart_equity_subs(SYMBOLS)

    for sym in SYMBOLS:
        if sym == "CRON":
            log_structured("SUBS", {"symbol": sym, "exchange": "NASDAQ"})
            await stream.nasdaq_book_subs([sym])
            continue
        if sym == "F":
            log_structured("SUBS", {"symbol": sym, "exchange": "NYSE"})
            await stream.nyse_book_subs([sym])
            continue
        ex = await resolve_exchange(client, sym)
        if ex is None:
            if sym not in PRINTED_NO_INSTR:
                log_structured("SUBS_WARNING", {"symbol": sym, "message": "No instrument found, subscribing to both books"})
                PRINTED_NO_INSTR.add(sym)
            await stream.nasdaq_book_subs([sym])
            await stream.nyse_book_subs([sym])
        elif ex == "NASDAQ":
            await stream.nasdaq_book_subs([sym])
        elif ex == "NYSE":
            await stream.nyse_book_subs([sym])
        else:
            if sym not in PRINTED_NO_INSTR:
                log_structured("SUBS_WARNING", {"symbol": sym, "exchange": ex, "message": "Unsupported exchange, subscribing to both"})
                PRINTED_NO_INSTR.add(sym)
            await stream.nasdaq_book_subs([sym])
            await stream.nyse_book_subs([sym])

    log_structured("SUBS", {"message": f"Subscribed to L1, {'timesales' if has_ts else 'chart'}, and L2 for: {', '.join(SYMBOLS)}"})
    log_structured("START", {
        "symbols": SYMBOLS,
        "window": WINDOW_SECONDS,
        "venues": MIN_ASK_HEAVY,
        "spread_cents": MAX_RANGE_CENTS,
        "volume_per_min": MIN_VOLUME,
        "min_imbalance_duration": MIN_IMBALANCE_DURATION_SEC,
        "imbalance_threshold": 4
    })

    hb_task = asyncio.create_task(_heartbeat_task())
    book_task = None
    if SHOW_BOOK:
        book_task = asyncio.create_task(_book_monitor_task())

    try:
        while True:
            try:
                msg = await asyncio.wait_for(stream.handle_message(), timeout=30.0)
                if DEBUG:
                    log_structured("STREAM_DEBUG", {"message": msg})
            except asyncio.TimeoutError:
                log_structured("STREAM_ERROR", {"error": "No messages for 30s"})
                if not await connect_with_retries():
                    log_structured("STREAM_ERROR", {"error": "Reconnection failed"})
                    break
    except KeyboardInterrupt:
        log_structured("STOP", {"message": "User stopped"})
    finally:
        hb_task.cancel()
        if book_task:
            book_task.cancel()
        if conn:
            conn.close()
        try:
            await stream.logout()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log_structured("STOP", {"message": "User stopped"})
    except Exception as e:
        log_structured("FATAL_ERROR", {"error": str(e)})
        sys.exit(5)
