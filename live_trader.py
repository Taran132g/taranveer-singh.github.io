"""Bridge paper trading alerts to Schwab paperMoney/live orders.

This module tails the ``alerts`` table written by ``grok.py`` and turns the
flip-only logic from ``paper_trader.py`` into real Schwab order requests. By
default it targets the paperMoney environment: simply point
``SCHWAB_ACCOUNT_ID`` at your paper account hash and the script will place
market orders through the official REST API.
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, Optional

from dotenv import load_dotenv
from grok import _normalize_and_validate_callback
from schwab.auth import easy_client
from schwab.orders import equities as equity_orders

load_dotenv()

LOGGER = logging.getLogger("live_trader")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class SchwabOrderExecutor:
    """Thin wrapper around ``schwab-py`` order placement."""

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

    def submit_market(self, *, symbol: str, qty: int, side: str) -> Dict[str, Optional[str]]:
        builders = {
            "BUY": equity_orders.equity_buy_market,
            "SELL": equity_orders.equity_sell_market,
            "SHORT": equity_orders.equity_sell_short_market,
            "COVER": equity_orders.equity_buy_to_cover_market,
        }
        try:
            builder = builders[side.upper()](symbol, qty)
        except KeyError as exc:
            raise ValueError(f"Unsupported side '{side}'") from exc
        return self._send(builder, symbol=symbol, side=side.upper(), qty=qty)


class LiveTrader:
    """Flip-only alert monitor that places Schwab orders."""

    def __init__(self, *, dry_run: bool = False) -> None:
        self.db_path = Path(os.getenv("DB_PATH", "penny_basing.db"))
        self.position_size = int(os.getenv("LIVE_POSITION_SIZE", os.getenv("POSITION_SIZE", "5000")))
        self.short_size = int(os.getenv("LIVE_SHORT_SIZE", os.getenv("SHORT_SIZE", str(self.position_size))))
        self.poll_interval = float(os.getenv("LIVE_POLL_INTERVAL", "1"))
        self.state_path = Path(os.getenv("LIVE_STATE_FILE", "live_trader_state.json"))
        self.executor = SchwabOrderExecutor(dry_run=dry_run)
        self.dry_run = self.executor.dry_run
        self.positions: Dict[str, int] = {}
        self.last_alert_id = 0

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
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db_schema(self) -> None:
        with self._open_conn() as conn:
            cur = conn.cursor()
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
        with self._open_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(rowid) FROM alerts")
            row = cur.fetchone()
            return int(row[0]) if row and row[0] else 0

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

    def _submit_order(self, *, alert_id: int, symbol: str, direction: str, side: str, qty: int, price: float) -> bool:
        result = self.executor.submit_market(symbol=symbol, qty=qty, side=side)
        success = result.get("error") is None and (
            result.get("dry_run")
            or (
                result.get("status_code") not in {None, ""}
                and str(result.get("status_code")).startswith("2")
            )
        )
        if success and not result.get("dry_run"):
            delta = qty if side in {"BUY", "COVER"} else -qty
            self._apply_position_delta(symbol, delta)
            self._save_state()
        self._record_order(
            alert_id=alert_id,
            symbol=symbol,
            direction=direction,
            side=side,
            qty=qty,
            price=price,
            result=result,
        )
        return success

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

    def run(self) -> None:
        LOGGER.info("Monitoring alerts from %s (poll %.1fs)", self.db_path, self.poll_interval)
        while True:
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
                alert_id = int(row["rowid"])
                symbol = row["symbol"]
                direction = row["direction"]
                price = float(row["price"])
                self.last_alert_id = alert_id
                self._handle_alert(alert_id, symbol, direction, price)

            if rows and not self.dry_run:
                self._save_state()

            time.sleep(self.poll_interval)


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
