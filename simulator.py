from __future__ import annotations

import logging
import math
import random
import sqlite3
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from enum import Enum
from typing import Callable, Dict, Optional, Tuple

LOGGER = logging.getLogger("simulator")


class OrderStatus(str, Enum):
    """Lifecycle states for simulated and real orders."""

    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass
class OrderResult:
    """Result of an order placement or status poll."""

    order_id: str
    symbol: str
    side: str
    quantity: float
    order_type: str
    status: OrderStatus
    filled_quantity: float
    avg_fill_price: Optional[float]
    timestamp: float
    rejection_reason: Optional[str] = None


@dataclass
class SimulationConfig:
    """Configuration for simulated market behavior.

    Attributes:
        latency_mean_ms: Mean network/exchange latency in milliseconds.
        latency_std_ms: Standard deviation for latency; values are clamped at 0.
        slippage_min_bps: Minimum expected slippage in basis points (1 bps = 0.01%).
        slippage_max_bps: Maximum expected slippage in basis points.
        limit_fill_base_probability: Base probability a limit order fills near the touch.
        volume_impact_factor: Exponential penalty for large orders relative to volume.
    """

    latency_mean_ms: float = 100.0
    latency_std_ms: float = 30.0
    slippage_min_bps: float = 0.0
    slippage_max_bps: float = 3.0
    limit_fill_base_probability: float = 0.6
    volume_impact_factor: float = 1.0

    def validate(self) -> None:
        if self.latency_mean_ms < 0:
            raise ValueError("latency_mean_ms must be non-negative")
        if self.latency_std_ms < 0:
            raise ValueError("latency_std_ms must be non-negative")
        if self.slippage_min_bps < 0:
            raise ValueError("slippage_min_bps must be non-negative")
        if self.slippage_max_bps < self.slippage_min_bps:
            raise ValueError("slippage_max_bps must be >= slippage_min_bps")
        if not 0 <= self.limit_fill_base_probability <= 1:
            raise ValueError("limit_fill_base_probability must be between 0 and 1")
        if self.volume_impact_factor <= 0:
            raise ValueError("volume_impact_factor must be positive")


class MarketModel:
    """Generate latency, slippage, and limit fill outcomes for simulations."""

    def __init__(self, config: SimulationConfig, *, rng: random.Random | None = None) -> None:
        config.validate()
        self.config = config
        self.rng = rng or random.Random()

    def generate_latency(self) -> float:
        """Sample a latency in seconds from a truncated normal distribution."""

        latency_ms = max(0.0, self.rng.gauss(self.config.latency_mean_ms, self.config.latency_std_ms))
        latency_seconds = latency_ms / 1000.0
        LOGGER.debug("Generated latency %.3f ms (%.4f s)", latency_ms, latency_seconds)
        return latency_seconds

    def calculate_slippage(self, *, expected_price: float, side: str) -> Tuple[float, float]:
        """Calculate a slipped fill price for a market order.

        Returns the fill price and the slippage in basis points.
        """

        if expected_price <= 0:
            raise ValueError("expected_price must be positive")

        normalized_side = side.strip().upper()
        if normalized_side not in {"BUY", "SELL", "SHORT", "COVER"}:
            raise ValueError(f"Unsupported side '{side}'")

        bps = self.rng.uniform(self.config.slippage_min_bps, self.config.slippage_max_bps)
        price_delta = expected_price * (bps / 10_000.0)
        direction = 1 if normalized_side in {"BUY", "COVER"} else -1
        fill_price = max(expected_price + direction * price_delta, 0.0)

        LOGGER.debug(
            "Calculated slippage: side=%s expected=%.4f bps=%.3f fill=%.4f",
            normalized_side,
            expected_price,
            bps,
            fill_price,
        )
        return fill_price, bps

    def limit_fill_probability(
        self,
        *,
        current_price: float,
        limit_price: float,
        order_size: float,
        typical_volume: float,
        elapsed_seconds: float,
        side: str,
    ) -> float:
        """Estimate the probability that a limit order fills under current conditions."""

        if current_price <= 0 or limit_price <= 0:
            raise ValueError("Prices must be positive")
        if order_size <= 0:
            raise ValueError("order_size must be positive")
        if typical_volume <= 0:
            raise ValueError("typical_volume must be positive")
        if elapsed_seconds < 0:
            raise ValueError("elapsed_seconds cannot be negative")

        normalized_side = side.strip().upper()
        if normalized_side not in {"BUY", "SELL", "SHORT", "COVER"}:
            raise ValueError(f"Unsupported side '{side}'")

        direction = 1 if normalized_side in {"BUY", "COVER"} else -1
        price_distance_bps = direction * ((limit_price - current_price) / current_price) * 10_000
        price_factor = 0.5 + math.tanh(price_distance_bps / 15.0) / 2

        volume_ratio = order_size / typical_volume
        volume_penalty = math.exp(-self.config.volume_impact_factor * volume_ratio)

        time_factor = 1 - math.exp(-elapsed_seconds / 5.0)

        probability = self._clamp(
            self.config.limit_fill_base_probability * volume_penalty * 0.6
            + price_factor * 0.3
            + time_factor * 0.1,
            0.0,
            1.0,
        )

        LOGGER.debug(
            "Limit fill probability: side=%s price_bps=%.3f volume_ratio=%.4f time=%.2f prob=%.4f",
            normalized_side,
            price_distance_bps,
            volume_ratio,
            elapsed_seconds,
            probability,
        )
        return probability

    def should_fill_limit_order(
        self,
        *,
        current_price: float,
        limit_price: float,
        order_size: float,
        typical_volume: float,
        elapsed_seconds: float,
        side: str,
    ) -> Tuple[bool, float]:
        """Return whether a limit order fills and the underlying probability."""

        probability = self.limit_fill_probability(
            current_price=current_price,
            limit_price=limit_price,
            order_size=order_size,
            typical_volume=typical_volume,
            elapsed_seconds=elapsed_seconds,
            side=side,
        )
        fill = self.rng.random() < probability
        LOGGER.debug("Limit fill decision: prob=%.4f outcome=%s", probability, fill)
        return fill, probability

    @staticmethod
    def _clamp(value: float, minimum: float, maximum: float) -> float:
        return max(minimum, min(maximum, value))


class OrderExecutor(ABC):
    """Interface for order execution backends (simulated or real)."""

    @abstractmethod
    def place_order(
        self, symbol: str, side: str, quantity: float, order_type: str, limit_price: float | None = None
    ) -> OrderResult:
        """Submit an order and return its initial result."""

    @abstractmethod
    def get_order_status(self, order_id: str) -> OrderResult:
        """Fetch the latest status for an existing order."""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Attempt to cancel an order. Returns True if the cancellation was accepted."""


@dataclass
class _TrackedOrder:
    result: OrderResult
    limit_price: float | None
    created_monotonic: float
    submit_timestamp: float
    latency_seconds: float
    quantity: float
    side: str
    order_type: str
    symbol: str
    pending_probability: float = 0.0

    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.result.filled_quantity)


class SimulatedExecutor(OrderExecutor):
    """Simulated execution backend using a :class:`MarketModel`."""

    def __init__(
        self,
        market_model: MarketModel,
        current_price_func: Callable[[str], float],
        *,
        typical_volume_func: Callable[[str], float] | None = None,
        sleep_func: Callable[[float], None] | None = None,
        metrics: "SimulatorMetrics" | None = None,
        db_path: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.market_model = market_model
        self.current_price_func = current_price_func
        self.typical_volume_func = typical_volume_func or (lambda _symbol: 10_000.0)
        self._sleep = sleep_func or time.sleep
        self._orders: Dict[str, _TrackedOrder] = {}
        self.metrics = metrics
        self._db_path = db_path
        self._db: sqlite3.Connection | None = None
        self._session_id = session_id or str(uuid.uuid4())
        if self._db_path:
            self._init_db()

    def place_order(
        self, symbol: str, side: str, quantity: float, order_type: str, limit_price: float | None = None
    ) -> OrderResult:
        normalized_side = side.strip().upper()
        normalized_order_type = order_type.strip().upper()
        if normalized_side not in {"BUY", "SELL", "SHORT", "COVER"}:
            raise ValueError(f"Unsupported side '{side}'")
        if normalized_order_type not in {"MARKET", "LIMIT"}:
            raise ValueError(f"Unsupported order_type '{order_type}'")
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if normalized_order_type == "LIMIT" and (limit_price is None or limit_price <= 0):
            raise ValueError("limit_price must be provided and positive for limit orders")

        latency = self.market_model.generate_latency()
        self._sleep(latency)
        current_price = self._require_price(symbol)
        timestamp = time.time()
        order_id = str(uuid.uuid4())

        if normalized_order_type == "MARKET":
            fill_price, _ = self.market_model.calculate_slippage(expected_price=current_price, side=normalized_side)
            result = OrderResult(
                order_id=order_id,
                symbol=symbol,
                side=normalized_side,
                quantity=quantity,
                order_type=normalized_order_type,
                status=OrderStatus.FILLED,
                filled_quantity=quantity,
                avg_fill_price=fill_price,
                timestamp=timestamp,
            )
            self._orders[order_id] = _TrackedOrder(
                result=result,
                limit_price=None,
                created_monotonic=time.monotonic(),
                submit_timestamp=timestamp,
                latency_seconds=latency,
                quantity=quantity,
                side=normalized_side,
                order_type=normalized_order_type,
                symbol=symbol,
            )
            self._record_metrics(result, latency_seconds=latency, slippage_bps=self._calculate_slippage_bps(current_price, fill_price))
            self._persist_order(result, limit_price=None, latency_seconds=latency, fill_time=timestamp)
            self._persist_fill(order_id, filled_qty=quantity, fill_price=fill_price, slippage_bps=self._calculate_slippage_bps(current_price, fill_price), fill_time=timestamp)
            return result

        assert limit_price is not None
        fill, probability = self.market_model.should_fill_limit_order(
            current_price=current_price,
            limit_price=limit_price,
            order_size=quantity,
            typical_volume=self._require_volume(symbol),
            elapsed_seconds=latency,
            side=normalized_side,
        )
        filled_quantity, status = self._compute_fill(quantity, fill, probability)
        result = OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=normalized_side,
            quantity=quantity,
            order_type=normalized_order_type,
            status=status,
            filled_quantity=filled_quantity,
            avg_fill_price=limit_price if filled_quantity else None,
            timestamp=timestamp,
        )
        self._orders[order_id] = _TrackedOrder(
            result=result,
            limit_price=limit_price,
            created_monotonic=time.monotonic(),
            submit_timestamp=timestamp,
            latency_seconds=latency,
            quantity=quantity,
            side=normalized_side,
            order_type=normalized_order_type,
            symbol=symbol,
            pending_probability=probability,
        )
        self._record_metrics(result, latency_seconds=latency if filled_quantity else None, slippage_bps=0.0 if filled_quantity else None)
        self._persist_order(result, limit_price=limit_price, latency_seconds=latency if filled_quantity else None, fill_time=timestamp if filled_quantity else None)
        if filled_quantity:
            self._persist_fill(order_id, filled_qty=filled_quantity, fill_price=limit_price, slippage_bps=0.0, fill_time=timestamp)
        return result

    def get_order_status(self, order_id: str) -> OrderResult:
        tracked = self._orders.get(order_id)
        if tracked is None:
            raise ValueError(f"Unknown order_id '{order_id}'")
        if tracked.result.status in {OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.FILLED}:
            return tracked.result

        if tracked.order_type == "LIMIT":
            tracked = self._attempt_limit_fill(tracked)
            self._orders[order_id] = tracked

        return tracked.result

    def cancel_order(self, order_id: str) -> bool:
        tracked = self._orders.get(order_id)
        if tracked is None:
            return False
        if tracked.result.status in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}:
            return True

        cancelled = replace(tracked.result, status=OrderStatus.CANCELLED)
        self._orders[order_id] = replace(tracked, result=cancelled)
        self._record_metrics(cancelled, latency_seconds=None, slippage_bps=None)
        self._update_order_status_db(cancelled, fill_time=None, latency_seconds=None)
        LOGGER.info("Cancelled order %s", order_id)
        return True

    def _attempt_limit_fill(self, tracked: _TrackedOrder) -> _TrackedOrder:
        """Attempt to fill a resting limit order based on the market model."""
        remaining = tracked.remaining_quantity()
        if remaining <= 0:
            return tracked

        elapsed = max(0.0, time.monotonic() - tracked.created_monotonic)
        current_price = self._require_price(tracked.symbol)
        fill, probability = self.market_model.should_fill_limit_order(
            current_price=current_price,
            limit_price=tracked.limit_price or current_price,
            order_size=remaining,
            typical_volume=self._require_volume(tracked.symbol),
            elapsed_seconds=elapsed,
            side=tracked.side,
        )
        if not fill:
            return replace(tracked, pending_probability=probability)

        fill_quantity, status = self._compute_fill(remaining, fill, probability)
        new_filled = tracked.result.filled_quantity + fill_quantity
        new_status = status
        if new_filled >= tracked.quantity:
            new_status = OrderStatus.FILLED
        elif status == OrderStatus.FILLED:
            new_status = OrderStatus.PARTIALLY_FILLED

        prior_total = tracked.result.avg_fill_price * tracked.result.filled_quantity if tracked.result.avg_fill_price else 0.0
        new_total = prior_total + (tracked.limit_price or current_price) * fill_quantity
        avg_price = new_total / new_filled if new_filled else None

        updated_result = replace(
            tracked.result,
            status=new_status,
            filled_quantity=new_filled,
            avg_fill_price=avg_price,
        )
        updated_tracked = replace(tracked, result=updated_result, pending_probability=probability)
        latency_seconds = max(0.0, time.monotonic() - tracked.created_monotonic)
        fill_time = time.time()
        self._record_metrics(updated_result, latency_seconds=latency_seconds, slippage_bps=0.0)
        self._update_order_status_db(updated_result, fill_time=fill_time, latency_seconds=latency_seconds)
        self._persist_fill(tracked.result.order_id, filled_qty=fill_quantity, fill_price=tracked.limit_price or current_price, slippage_bps=0.0, fill_time=fill_time)
        return updated_tracked

    def _compute_fill(self, quantity: float, fill: bool, probability: float) -> Tuple[float, OrderStatus]:
        """Compute filled quantity and status for a limit order attempt."""
        if not fill:
            return 0.0, OrderStatus.PENDING

        fill_quantity = quantity if probability >= 0.95 else max(quantity * probability, 1e-6)
        fill_quantity = min(quantity, fill_quantity)
        status = OrderStatus.FILLED if math.isclose(fill_quantity, quantity, rel_tol=1e-9) else OrderStatus.PARTIALLY_FILLED
        LOGGER.debug(
            "Limit fill computed: qty=%.4f prob=%.4f fill_qty=%.4f status=%s", quantity, probability, fill_quantity, status
        )
        return fill_quantity, status

    def _require_price(self, symbol: str) -> float:
        """Return the latest price from the price callback or raise if invalid."""
        price = self.current_price_func(symbol)
        if price is None or price <= 0:
            raise ValueError(f"Invalid price for symbol '{symbol}': {price}")
        return price

    def _require_volume(self, symbol: str) -> float:
        """Return the typical volume for a symbol or raise if invalid."""
        volume = self.typical_volume_func(symbol)
        if volume is None or volume <= 0:
            raise ValueError(f"Invalid typical volume for symbol '{symbol}': {volume}")
        return volume

    def _record_metrics(self, result: OrderResult, *, latency_seconds: float | None, slippage_bps: float | None) -> None:
        """Forward order outcomes into the optional metrics aggregator and DB."""
        if not self.metrics:
            return
        self.metrics.record_order(
            result,
            latency_seconds=latency_seconds,
            slippage_bps=slippage_bps,
            is_limit=result.order_type == "LIMIT",
        )
        if self._db_path:
            self._persist_metrics_snapshot(self.metrics.get_summary())

    @staticmethod
    def _calculate_slippage_bps(expected_price: float, fill_price: float) -> float:
        """Return slippage expressed in basis points relative to an expected price."""
        if expected_price <= 0:
            return 0.0
        return ((fill_price - expected_price) / expected_price) * 10_000

    def _get_db(self) -> sqlite3.Connection:
        """Lazily return an open SQLite connection for simulator persistence."""
        if not self._db_path:
            raise RuntimeError("Database path not configured")
        if self._db is None:
            self._db = sqlite3.connect(self._db_path)
        return self._db

    def _init_db(self) -> None:
        """Create simulator tables if they do not exist."""
        conn = self._get_db()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sim_orders (
                order_id TEXT PRIMARY KEY,
                symbol TEXT,
                side TEXT,
                quantity REAL,
                order_type TEXT,
                limit_price REAL,
                status TEXT,
                submit_time REAL,
                fill_time REAL,
                latency_ms REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sim_fills (
                fill_id TEXT PRIMARY KEY,
                order_id TEXT,
                filled_qty REAL,
                fill_price REAL,
                slippage_bps REAL,
                fill_time REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sim_metrics (
                session_id TEXT,
                timestamp REAL,
                metric_name TEXT,
                metric_value REAL
            )
            """
        )
        conn.commit()

    def _persist_order(
        self,
        result: OrderResult,
        *,
        limit_price: float | None,
        latency_seconds: float | None,
        fill_time: float | None,
    ) -> None:
        """Persist a single order row when database persistence is enabled."""
        if not self._db_path:
            return
        latency_ms = latency_seconds * 1000.0 if latency_seconds is not None else None
        conn = self._get_db()
        conn.execute(
            """
            INSERT OR REPLACE INTO sim_orders (
                order_id, symbol, side, quantity, order_type, limit_price, status, submit_time, fill_time, latency_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.order_id,
                result.symbol,
                result.side,
                result.quantity,
                result.order_type,
                limit_price,
                result.status.value,
                result.timestamp,
                fill_time,
                latency_ms,
            ),
        )
        conn.commit()

    def _update_order_status_db(
        self, result: OrderResult, *, fill_time: float | None, latency_seconds: float | None
    ) -> None:
        """Update existing order rows as fills or cancels occur."""
        if not self._db_path:
            return
        latency_ms = latency_seconds * 1000.0 if latency_seconds is not None else None
        conn = self._get_db()
        conn.execute(
            """
            UPDATE sim_orders
            SET status = ?, fill_time = COALESCE(?, fill_time), latency_ms = COALESCE(?, latency_ms)
            WHERE order_id = ?
            """,
            (result.status.value, fill_time, latency_ms, result.order_id),
        )
        conn.commit()

    def _persist_fill(
        self, order_id: str, *, filled_qty: float, fill_price: float, slippage_bps: float, fill_time: float
    ) -> None:
        """Insert a fill row associated with an order."""
        if not self._db_path:
            return
        conn = self._get_db()
        conn.execute(
            """
            INSERT INTO sim_fills (fill_id, order_id, filled_qty, fill_price, slippage_bps, fill_time)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), order_id, filled_qty, fill_price, slippage_bps, fill_time),
        )
        conn.commit()

    def _persist_metrics_snapshot(self, summary: Dict[str, float]) -> None:
        """Persist the current metrics snapshot for longitudinal analysis."""
        if not self._db_path:
            return
        conn = self._get_db()
        timestamp = time.time()
        entries = [(self._session_id, timestamp, name, value) for name, value in summary.items()]
        conn.executemany(
            "INSERT INTO sim_metrics (session_id, timestamp, metric_name, metric_value) VALUES (?, ?, ?, ?)", entries
        )
        conn.commit()


class RealSchwabExecutor(OrderExecutor):
    """Adapter around the real Schwab executor for future A/B tests."""

    def __init__(self, underlying: Optional[object] = None) -> None:
        if underlying is None:
            from live_trader import SchwabOrderExecutor  # import here to avoid mandatory dependency at import time

            self.underlying = SchwabOrderExecutor()
        else:
            self.underlying = underlying

    def place_order(
        self, symbol: str, side: str, quantity: float, order_type: str, limit_price: float | None = None
    ) -> OrderResult:
        """Submit an order to the underlying Schwab executor."""
        normalized_order_type = order_type.strip().upper()
        normalized_side = side.strip().upper()
        if normalized_order_type == "MARKET":
            response = self.underlying.submit_market(symbol=symbol, qty=int(quantity), side=normalized_side)
        elif normalized_order_type == "LIMIT":
            if limit_price is None:
                raise ValueError("limit_price required for limit orders")
            response = self.underlying.submit_limit(symbol=symbol, qty=int(quantity), side=normalized_side, limit_price=limit_price)
        else:
            raise ValueError(f"Unsupported order_type '{order_type}'")

        timestamp = time.time()
        order_id = response.get("order_id") or str(uuid.uuid4())
        status = OrderStatus.FILLED if response.get("status", "").upper() == "FILLED" else OrderStatus.PENDING
        filled_quantity = float(response.get("filled_quantity") or 0.0)
        return OrderResult(
            order_id=order_id,
            symbol=symbol,
            side=normalized_side,
            quantity=quantity,
            order_type=normalized_order_type,
            status=status,
            filled_quantity=filled_quantity if status == OrderStatus.FILLED else 0.0,
            avg_fill_price=None,
            timestamp=timestamp,
            rejection_reason=response.get("error"),
        )

    def get_order_status(self, order_id: str) -> OrderResult:
        """Poll the Schwab executor for the latest order status."""
        response = self.underlying.fetch_order_status(order_id)
        status_raw = (response.get("status") or "").upper()
        try:
            status = OrderStatus(status_raw)
        except ValueError:
            status = OrderStatus.PENDING
        timestamp = time.time()
        return OrderResult(
            order_id=order_id,
            symbol=response.get("symbol") or "",
            side=response.get("side") or "",
            quantity=float(response.get("quantity") or 0.0),
            order_type=(response.get("order_type") or "").upper(),
            status=status,
            filled_quantity=float(response.get("filled_quantity") or 0.0),
            avg_fill_price=None,
            timestamp=timestamp,
            rejection_reason=response.get("error"),
        )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an in-flight order via the underlying executor."""
        return bool(self.underlying.cancel_order(order_id))


class SimulatorMetrics:
    """In-memory metrics aggregator for simulator runs."""

    def __init__(self) -> None:
        self.total_orders = 0
        self.filled_orders = 0
        self.rejected_orders = 0
        self.cancelled_orders = 0
        self.limit_orders_placed = 0
        self.limit_orders_filled = 0

        self._latency_count = 0
        self._latency_sum = 0.0
        self._latency_sumsq = 0.0
        self._latency_min = float("inf")
        self._latency_max = float("-inf")

        self._slippage_count = 0
        self._slippage_sum = 0.0
        self._slippage_min = float("inf")
        self._slippage_max = float("-inf")

        self._order_statuses: Dict[str, OrderStatus] = {}
        self._order_is_limit: Dict[str, bool] = {}

    def record_order(
        self,
        order: OrderResult,
        *,
        latency_seconds: float | None,
        slippage_bps: float | None,
        is_limit: bool,
    ) -> None:
        """Record order outcomes and optional latency/slippage samples."""

        is_new = order.order_id not in self._order_statuses
        previous_status = self._order_statuses.get(order.order_id)

        if is_new:
            self.total_orders += 1
            self._order_is_limit[order.order_id] = is_limit
            if is_limit:
                self.limit_orders_placed += 1
        else:
            self._decrement_status(previous_status)

        self._order_statuses[order.order_id] = order.status
        self._increment_status(order.status, order.order_id)

        if latency_seconds is not None:
            latency_ms = latency_seconds * 1000.0
            self._latency_count += 1
            self._latency_sum += latency_ms
            self._latency_sumsq += latency_ms ** 2
            self._latency_min = min(self._latency_min, latency_ms)
            self._latency_max = max(self._latency_max, latency_ms)

        if slippage_bps is not None:
            self._slippage_count += 1
            self._slippage_sum += slippage_bps
            self._slippage_min = min(self._slippage_min, slippage_bps)
            self._slippage_max = max(self._slippage_max, slippage_bps)

        if self.total_orders and self.total_orders % 10 == 0:
            LOGGER.info("Metrics summary after %d orders: %s", self.total_orders, self.get_summary())

    def _increment_status(self, status: OrderStatus, order_id: str) -> None:
        if status == OrderStatus.FILLED:
            self.filled_orders += 1
            if self._order_is_limit.get(order_id):
                self.limit_orders_filled += 1
        elif status == OrderStatus.REJECTED:
            self.rejected_orders += 1
        elif status == OrderStatus.CANCELLED:
            self.cancelled_orders += 1

    def _decrement_status(self, status: OrderStatus | None) -> None:
        if status is None:
            return
        if status == OrderStatus.FILLED:
            self.filled_orders = max(0, self.filled_orders - 1)
            self.limit_orders_filled = max(0, self.limit_orders_filled - 1)
        elif status == OrderStatus.REJECTED:
            self.rejected_orders = max(0, self.rejected_orders - 1)
        elif status == OrderStatus.CANCELLED:
            self.cancelled_orders = max(0, self.cancelled_orders - 1)

    def get_summary(self) -> Dict[str, float]:
        latency_mean = self._latency_sum / self._latency_count if self._latency_count else 0.0
        latency_variance = (
            (self._latency_sumsq / self._latency_count) - latency_mean ** 2 if self._latency_count else 0.0
        )
        latency_std = math.sqrt(latency_variance) if latency_variance > 0 else 0.0

        slippage_mean = self._slippage_sum / self._slippage_count if self._slippage_count else 0.0
        return {
            "total_orders": self.total_orders,
            "filled_orders": self.filled_orders,
            "rejected_orders": self.rejected_orders,
            "cancelled_orders": self.cancelled_orders,
            "limit_orders_placed": self.limit_orders_placed,
            "limit_orders_filled": self.limit_orders_filled,
            "latency_min_ms": 0.0 if self._latency_count == 0 else self._latency_min,
            "latency_max_ms": 0.0 if self._latency_count == 0 else self._latency_max,
            "latency_mean_ms": latency_mean,
            "latency_std_ms": latency_std,
            "slippage_min_bps": 0.0 if self._slippage_count == 0 else self._slippage_min,
            "slippage_max_bps": 0.0 if self._slippage_count == 0 else self._slippage_max,
            "slippage_mean_bps": slippage_mean,
        }

    def reset(self) -> None:
        self.__init__()
