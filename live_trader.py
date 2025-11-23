"""Bridge paper trading alerts to Schwab paperMoney/live orders.

This module tails the ``alerts`` table written by ``grok.py`` and turns the
flip-only logic from ``paper_trader.py`` into real Schwab order requests. By
default it targets the paperMoney environment: simply point
``SCHWAB_ACCOUNT_ID`` at your paper account hash and the script will place
market orders through the official REST API.

When ``grok.py`` runs inline with :class:`LiveTrader`, alerts never need to hit
SQLite; they can be delivered directly via the in-process callback. The polling
loop in this file remains for the cases where you run ``live_trader.py`` as a
standalone service (e.g., on a different host or to backfill older alerts).
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from schwab.auth import easy_client
from schwab.orders import equities as equity_orders

load_dotenv()

LOGGER = logging.getLogger("live_trader")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# Config helpers: these keep setup failures obvious so you don't place trades
# without the right Schwab credentials.
def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# Schwab's OAuth callback must be a full URL; this keeps mistakes user-friendly
# by explaining what went wrong instead of failing silently.
def _normalize_and_validate_callback(url: str) -> str:
    if not url:
        raise ValueError("SCHWAB_REDIRECT_URI is empty")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            f"Invalid SCHWAB_REDIRECT_URI '{url}'. Expected full URL like 'https://127.0.0.1:8182/'."
        )
    return url if url.endswith("/") else url + "/"


# Tiny parser for yes/no env vars (e.g., LIVE_DRY_RUN=1 turns off real orders)
def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class SchwabOrderExecutor:
    """Thin wrapper around ``schwab-py`` order placement.

    Separating this layer keeps trading decisions (LiveTrader) and API calls
    (SchwabOrderExecutor) loosely coupled, which makes dry-run testing and
    error handling easier to follow.
    """

    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run or _bool_env("LIVE_DRY_RUN", False)
        self.account_id = _require_env("SCHWAB_ACCOUNT_ID")
        api_key = _require_env("SCHWAB_CLIENT_ID")
        app_secret = _require_env("SCHWAB_APP_SECRET")
        redirect_uri = _normalize_and_validate_callback(_require_env("SCHWAB_REDIRECT_URI"))
        token_path = Path(os.getenv("SCHWAB_TOKEN_PATH", "./schwab_tokens.json"))

        LOGGER.info("Initializing Schwab client (dry_run=%s)", self.dry_run)
        try:
            self.client = easy_client(
                api_key=api_key,
                app_secret=app_secret,
                callback_url=redirect_uri,
                token_path=token_path,
            )
        except Exception as exc:  # pragma: no cover - network interaction
            raise RuntimeError(f"Failed to create Schwab client: {exc}") from exc

    def _send(self, builder, *, symbol: str, side: str, qty: int) -> Dict[str, Optional[str]]:
        payload = builder.build() if hasattr(builder, "build") else builder
        result = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "order_id": None,
            "location": None,
            "status_code": None,
            "error": None,
            "dry_run": self.dry_run,
        }

        if self.dry_run:
            LOGGER.info("[DRY-RUN] %s %s %s", side, qty, symbol)
            return result

        try:
            response = self.client.place_order(self.account_id, payload)
        except Exception as exc:  # pragma: no cover - network interaction
            LOGGER.error("Order failed: %s", exc)
            result["error"] = str(exc)
            return result

        result["status_code"] = str(response.status_code)
        location = response.headers.get("Location") if response else None
        if location:
            result["location"] = location
            if "/orders/" in location:
                result["order_id"] = location.rstrip("/").split("/")[-1]

        if not response or not (200 <= response.status_code < 300):
            LOGGER.error(
                "Order rejected (status=%s) for %s %s %s", response.status_code if response else "?", side, qty, symbol
            )
            result["error"] = response.text if response else "Unknown order error"
        else:
            LOGGER.info("Order accepted (id=%s) for %s %s %s", result["order_id"], side, qty, symbol)

        return result

    def fetch_order_status(self, order_id: str) -> Dict[str, Optional[str]]:
        result: Dict[str, Optional[str]] = {"order_id": order_id, "status": None, "error": None}

        if self.dry_run:
            result["status"] = "FILLED"
            result["dry_run"] = True
            return result

        fetch_order = getattr(self.client, "get_order", None)
        if fetch_order is None:
            result["error"] = "Schwab client does not expose get_order"
            return result

        try:
            response = fetch_order(self.account_id, order_id)
        except Exception as exc:  # pragma: no cover - network interaction
            LOGGER.error("Failed to fetch order %s: %s", order_id, exc)
            result["error"] = str(exc)
            return result

        result["status_code"] = str(getattr(response, "status_code", None))
        try:
            payload = response.json() if hasattr(response, "json") else None
        except Exception:
            payload = None

        if isinstance(payload, dict):
            status = payload.get("status") or payload.get("orderStatus") or payload.get("order_status")
            filled_qty = payload.get("filledQuantity") or payload.get("filled_quantity")
            result["status"] = status
            result["filled_quantity"] = filled_qty
            result["raw"] = payload
        else:
            result["raw"] = str(payload)

        return result

    def submit_market(self, *, symbol: str, qty: int, side: str) -> Dict[str, Optional[str]]:
        builders = {
            "BUY": equity_orders.equity_buy_market,
            "SELL": equity_orders.equity_sell_market,
            "SHORT": equity_orders.equity_sell_short_market,
            "COVER": equity_orders.equity_buy_to_cover_market,
        }
        try:
            builder_factory = builders[side.upper()]
        except KeyError as exc:
            raise ValueError(f"Unsupported side '{side}'") from exc

        try:
            builder = builder_factory(symbol, qty)
        except TypeError:
            builder = builder_factory(symbol=symbol, quantity=qty)
        return self._send(builder, symbol=symbol, side=side.upper(), qty=qty)

    def submit_limit(self, *, symbol: str, qty: int, side: str, limit_price: float) -> Dict[str, Optional[str]]:
        builders = {
            "BUY": equity_orders.equity_buy_limit,
            "SELL": equity_orders.equity_sell_limit,
            "SHORT": equity_orders.equity_sell_short_limit,
            "COVER": equity_orders.equity_buy_to_cover_limit,
        }
        try:
            builder_factory = builders[side.upper()]
        except KeyError as exc:
            raise ValueError(f"Unsupported side '{side}'") from exc

        try:
            builder = builder_factory(symbol, limit_price, qty)
        except TypeError:
            builder = builder_factory(symbol=symbol, price=limit_price, quantity=qty)
        return self._send(builder, symbol=symbol, side=side.upper(), qty=qty)

    def cancel_all_orders(self) -> bool:
        """Attempt to cancel all open orders on the account."""

        if self.dry_run:
            LOGGER.info("[DRY-RUN] Skip cancel_all_orders")
            return True

        cancel_all = getattr(self.client, "cancel_all_orders", None)
        if cancel_all is None:
            LOGGER.warning("Schwab client does not expose cancel_all_orders; skipping")
            return False

        try:
            cancel_all(self.account_id)
            LOGGER.info("Cancel-all request sent")
            return True
        except Exception as exc:  # pragma: no cover - network interaction
            LOGGER.error("Failed to cancel all orders: %s", exc)
            return False


class LiveTrader:
    """Flip-only alert monitor that places Schwab orders.

    Think of this as a traffic cop: it listens for new alerts, checks whether
    you're already long/short, and then decides whether to flip, close, or
    stay flat. The class also guards against runaway trading via rate limits
    and a kill-switch file.
    """

    def __init__(self, *, dry_run: bool = False, executor: Optional[SchwabOrderExecutor] = None) -> None:
        self.db_path = Path(os.getenv("DB_PATH", "penny_basing.db"))
        self.position_size = int(os.getenv("LIVE_POSITION_SIZE", os.getenv("POSITION_SIZE", "5000")))
        self.short_size = int(os.getenv("LIVE_SHORT_SIZE", os.getenv("SHORT_SIZE", str(self.position_size))))
        self.poll_interval = float(os.getenv("LIVE_POLL_INTERVAL", "1"))
        self.state_path = Path(os.getenv("LIVE_STATE_FILE", "live_trader_state.json"))
        self.executor = executor if executor is not None else SchwabOrderExecutor(dry_run=dry_run)
        self.dry_run = getattr(self.executor, "dry_run", dry_run)
        self.prefer_limit_orders = _bool_env("LIVE_PREFER_LIMIT_ORDERS", True)
        self.limit_slippage_bps = float(os.getenv("LIVE_LIMIT_SLIPPAGE_BPS", "10.0"))
        self.kill_switch_path = Path(os.getenv("LIVE_KILL_SWITCH_FILE", "kill_switch.flag"))
        self.max_trades_per_hour = int(os.getenv("LIVE_MAX_TRADES_PER_HOUR", "60"))
        self.limit_fill_timeout = float(os.getenv("LIVE_LIMIT_FILL_TIMEOUT", "60"))
        self.limit_fill_poll_interval = float(os.getenv("LIVE_LIMIT_FILL_POLL_INTERVAL", "2"))
        self.positions: Dict[str, int] = {}
        self.last_alert_id = 0
        self.trade_timestamps: list[float] = []
        self._lock = threading.Lock()

        self._load_state()
        self._init_db_schema()
        if not self.dry_run:
            atexit.register(self._save_state)

    # ------------------------------------------------------------------
    # State & persistence helpers
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
            self.positions = {k: int(v) for k, v in data.get("positions", {}).items()}
            self.last_alert_id = int(data.get("last_alert_id", 0))
            LOGGER.info("Loaded state: %s positions", len(self.positions))
        except Exception as exc:
            LOGGER.warning("Failed to load state: %s", exc)

    def _save_state(self) -> None:
        if self.dry_run:
            return
        payload = {"positions": self.positions, "last_alert_id": self.last_alert_id}
        try:
            self.state_path.write_text(json.dumps(payload, indent=2))
        except Exception as exc:
            LOGGER.error("Failed to persist state: %s", exc)

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------
    def _open_conn(self) -> sqlite3.Connection:
        # Simple, thread-safe-ish SQLite connection helper so every DB touch
        # uses the same pragmatic settings.
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db_schema(self) -> None:
        with self._open_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    timestamp REAL,
                    symbol TEXT,
                    ratio REAL,
                    total_bids INTEGER,
                    total_asks INTEGER,
                    heavy_venues INTEGER,
                    direction TEXT,
                    price REAL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS live_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_rowid INTEGER,
                    symbol TEXT,
                    direction TEXT,
                    side TEXT,
                    qty INTEGER,
                    price REAL,
                    order_id TEXT,
                    status_code TEXT,
                    location TEXT,
                    error TEXT,
                    raw_response TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

        if self.last_alert_id == 0:
            self.last_alert_id = self._get_last_alert_id_from_db()

    def _get_last_alert_id_from_db(self) -> int:
        try:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT MAX(rowid) FROM alerts")
                row = cur.fetchone()
                return int(row[0]) if row and row[0] else 0
        except sqlite3.Error:
            LOGGER.warning("alerts table missing; starting with last_alert_id=0")
            return 0

    def _latest_price(self, symbol: str) -> Optional[float]:
        with self._open_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT price FROM alerts WHERE symbol=? ORDER BY rowid DESC LIMIT 1",
                (symbol,),
            )
            row = cur.fetchone()
            return float(row[0]) if row else None

    # ------------------------------------------------------------------
    # Position bookkeeping
    # ------------------------------------------------------------------
    def _apply_position_delta(self, symbol: str, delta: int) -> None:
        new_qty = self.positions.get(symbol, 0) + delta
        if new_qty == 0:
            self.positions.pop(symbol, None)
        else:
            self.positions[symbol] = new_qty
        LOGGER.info("Position update %s => %s", symbol, new_qty)

    def _record_fill(self, *, symbol: str, side: str, qty: int) -> None:
        delta = qty if side in {"BUY", "COVER"} else -qty
        self._apply_position_delta(symbol, delta)
        if not self.dry_run:
            self._save_state()
        self.trade_timestamps.append(time.time())
        self._enforce_trade_rate_limit()

    def _aggressive_limit_price(self, *, side: str, reference_price: float) -> float:
        if reference_price <= 0:
            return reference_price

        slip_fraction = max(self.limit_slippage_bps, 0.0) / 10000.0
        if side in {"BUY", "COVER"}:
            adjusted = reference_price * (1 + slip_fraction)
        else:
            adjusted = reference_price * (1 - slip_fraction)

        adjusted = max(round(adjusted, 4), 0.01)

        if adjusted != reference_price:
            LOGGER.info(
                "Limit price adjusted for slippage guard: %s -> %s (side=%s, %sbps)",
                reference_price,
                adjusted,
                side,
                self.limit_slippage_bps,
            )

        return adjusted

    # ------------------------------------------------------------------
    # Order + alert processing
    # ------------------------------------------------------------------
    def _poll_limit_fill(self, *, order_id: str, symbol: str, side: str, qty: int) -> bool:
        start = time.time()
        deadline = start + self.limit_fill_timeout
        last_status: Optional[str] = None

        while time.time() < deadline:
            status = self.executor.fetch_order_status(order_id)
            last_status = (status.get("status") or "").upper()

            if status.get("error"):
                LOGGER.warning(
                    "Unable to poll order %s for %s %s %s: %s",
                    order_id,
                    side,
                    qty,
                    symbol,
                    status.get("error"),
                )
                break

            if last_status in {"FILLED", "FILLED_ALL", "FILLED_ALL_SHARES"}:
                LOGGER.info("Limit order %s filled for %s %s %s", order_id, side, qty, symbol)
                self._record_fill(symbol=symbol, side=side, qty=qty)
                return True

            if last_status in {"CANCELED", "REJECTED", "EXPIRED"}:
                LOGGER.warning("Limit order %s ended with status %s", order_id, last_status)
                return False

            time.sleep(self.limit_fill_poll_interval)

        LOGGER.warning(
            "Timed out waiting for limit order %s to fill (last_status=%s)",
            order_id,
            last_status,
        )
        return False

    def _enforce_trade_rate_limit(self) -> None:
        cutoff = time.time() - 3600
        self.trade_timestamps = [ts for ts in self.trade_timestamps if ts >= cutoff]
        if len(self.trade_timestamps) > self.max_trades_per_hour:
            LOGGER.error(
                "Trade rate exceeded limit (%s in the last hour); engaging kill switch",
                len(self.trade_timestamps),
            )
            self._engage_emergency_shutdown("Trade-per-hour limit exceeded")

    def _engage_emergency_shutdown(self, reason: str) -> None:
        LOGGER.error("EMERGENCY STOP: %s", reason)
        try:
            self.executor.cancel_all_orders()
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Failed to request cancel-all: %s", exc)

        for symbol, qty in list(self.positions.items()):
            if qty == 0:
                continue
            side = "SELL" if qty > 0 else "COVER"
            price = self._latest_price(symbol) or 0.0
            self._submit_order(
                alert_id=-1,
                symbol=symbol,
                direction="kill-switch",
                side=side,
                qty=abs(qty),
                price=price,
                force_market=True,
            )

        self._save_state()
        raise SystemExit(1)

    def _check_kill_switch(self) -> None:
        if self.kill_switch_path.exists():
            LOGGER.error("Kill switch file %s detected", self.kill_switch_path)
            self._engage_emergency_shutdown("Kill switch activated")

    # ------------------------------------------------------------------
    # Order + alert processing
    # ------------------------------------------------------------------
    def _record_order(self, *, alert_id: int, symbol: str, direction: str, side: str, qty: int, price: float, result: dict) -> None:
        serialized = json.dumps(result, default=str)
        with self._open_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO live_orders
                (alert_rowid, symbol, direction, side, qty, price, order_id, status_code, location, error, raw_response)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    symbol,
                    direction,
                    side,
                    qty,
                    price,
                    result.get("order_id"),
                    result.get("status_code"),
                    result.get("location"),
                    result.get("error"),
                    serialized,
                ),
            )
            conn.commit()

    def _submit_order(
        self,
        *,
        alert_id: int,
        symbol: str,
        direction: str,
        side: str,
        qty: int,
        price: float,
        force_market: bool = False,
    ) -> bool:
        # Decide whether to chase the best price (limit) or guaranteed speed
        # (market). When we pick limit, we still backstop with a market order
        # if the limit cannot be submitted. Returns True only when the order is
        # accepted and, for limits, actually filled.
        use_limit = self.prefer_limit_orders and not force_market
        effective_price = (
            self._aggressive_limit_price(side=side, reference_price=price)
            if use_limit
            else price
        )

        result = (
            self.executor.submit_limit(symbol=symbol, qty=qty, side=side, limit_price=effective_price)
            if use_limit
            else None
        )
        if use_limit and result and result.get("error"):
            LOGGER.warning(
                "Limit order for %s %s failed (%s); falling back to market as emergency",
                side,
                symbol,
                result.get("error"),
            )
            use_limit = False
            result = None

        if result is None:
            result = self.executor.submit_market(symbol=symbol, qty=qty, side=side)

        submitted = result.get("error") is None and (
            result.get("dry_run")
            or (
                result.get("status_code") not in {None, ""}
                and str(result.get("status_code")).startswith("2")
            )
        )

        filled = False
        fill_status: Optional[str] = None

        if submitted:
            if result.get("dry_run") or not use_limit:
                self._record_fill(symbol=symbol, side=side, qty=qty)
                filled = True
                fill_status = "FILLED"
            else:
                order_id = result.get("order_id")
                if not order_id:
                    LOGGER.warning("No order_id returned for limit order; cannot poll for fills")
                    fill_status = "FAILED"
                else:
                    LOGGER.info(
                        "Limit order accepted for %s %s %s at %s; awaiting fill",
                        side,
                        qty,
                        symbol,
                        price,
                    )
                    poll_success = self._poll_limit_fill(order_id=order_id, symbol=symbol, side=side, qty=qty)
                    filled = poll_success
                    fill_status = "FILLED" if poll_success else "FAILED"
        else:
            fill_status = "FAILED"

        result["fill_status"] = fill_status

        self._record_order(
            alert_id=alert_id,
            symbol=symbol,
            direction=direction,
            side=side,
            qty=qty,
            price=effective_price,
            result=result,
        )
        return filled

    def _handle_alert(self, alert_id: int, symbol: str, direction: str, price: float) -> None:
        position = self.positions.get(symbol, 0)

        if direction == "ask-heavy":
            if position < 0:
                return
            if position > 0:
                flattened = self._submit_order(
                    alert_id=alert_id,
                    symbol=symbol,
                    direction=direction,
                    side="SELL",
                    qty=position,
                    price=price,
                )
                if not flattened:
                    LOGGER.warning(
                        "Skipping SHORT on %s because closing SELL failed (alert %s)",
                        symbol,
                        alert_id,
                    )
                    return
            self._submit_order(
                alert_id=alert_id,
                symbol=symbol,
                direction=direction,
                side="SHORT",
                qty=self.short_size,
                price=price,
            )
        elif direction == "bid-heavy":
            if position > 0:
                return
            if position < 0:
                flattened = self._submit_order(
                    alert_id=alert_id,
                    symbol=symbol,
                    direction=direction,
                    side="COVER",
                    qty=abs(position),
                    price=price,
                )
                if not flattened:
                    LOGGER.warning(
                        "Skipping BUY on %s because closing COVER failed (alert %s)",
                        symbol,
                        alert_id,
                    )
                    return
            self._submit_order(
                alert_id=alert_id,
                symbol=symbol,
                direction=direction,
                side="BUY",
                qty=self.position_size,
                price=price,
            )

    def process_alert(
        self,
        alert_id: int,
        symbol: str,
        direction: str,
        price: float,
        *,
        persist_state: bool = True,
    ) -> None:
        """Process a single alert, optionally persisting state immediately.

        This entry point lets ``grok.py`` dispatch alerts inline without
        waiting for the polling loop, while keeping the standalone ``run``
        method available for tailing the DB.
        """
        with self._lock:
            self.last_alert_id = max(self.last_alert_id, int(alert_id))
            self._handle_alert(alert_id, symbol, direction, price)
            if persist_state and not self.dry_run:
                self._save_state()

    def run(self) -> None:
        # Keep the hot path responsive: when alerts are flowing we poll on a
        # ~50ms cadence. During lulls we exponentially back off to avoid hot
        # loops, but we watch for DB file writes every 10ms so a new alert can
        # break the longer sleep immediately. This mirrors the low-latency
        # monitoring used in ``paper_trader``.
        min_sleep = 0.05
        mtime_probe = 0.01
        max_sleep = max(self.poll_interval, 2.0)
        idle_sleep = min_sleep
        db_path = self.db_path
        last_db_mtime = db_path.stat().st_mtime if db_path.exists() else 0.0

        LOGGER.info(
            "Monitoring alerts from %s (adaptive poll %.0fms–%.1fs)",
            db_path,
            min_sleep * 1000,
            max_sleep,
        )

        while True:
            self._check_kill_switch()
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT rowid, symbol, direction, price
                    FROM alerts
                    WHERE rowid > ?
                    ORDER BY rowid ASC
                    """,
                    (self.last_alert_id,),
                )
                rows = cur.fetchall()

            for row in rows:
                self.process_alert(
                    int(row["rowid"]),
                    row["symbol"],
                    row["direction"],
                    float(row["price"]),
                    persist_state=False,
                )

            if rows and not self.dry_run:
                self._save_state()

            activity_detected = bool(rows)
            db_mtime_snapshot = db_path.stat().st_mtime if db_path.exists() else last_db_mtime

            if activity_detected:
                idle_sleep = min_sleep
                last_db_mtime = db_mtime_snapshot
                time.sleep(idle_sleep)
                continue

            target_sleep = min(idle_sleep * 2, max_sleep)
            wake_deadline = time.monotonic() + target_sleep
            woke_for_write = False

            while time.monotonic() < wake_deadline:
                time.sleep(mtime_probe)
                current_mtime = db_path.stat().st_mtime if db_path.exists() else 0.0
                if current_mtime != db_mtime_snapshot:
                    woke_for_write = True
                    db_mtime_snapshot = current_mtime
                    break

            last_db_mtime = db_mtime_snapshot
            idle_sleep = min_sleep if woke_for_write else target_sleep

            if woke_for_write:
                continue


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send Schwab paperMoney/live orders based on alerts")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without sending Schwab orders")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    trader = LiveTrader(dry_run=args.dry_run)
    try:
        trader.run()
    except KeyboardInterrupt:
        LOGGER.info("Live trader stopped by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
