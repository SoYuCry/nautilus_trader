"""Bounded asynchronous opportunity snapshots, not a cumulative fill simulator."""

from dataclasses import dataclass
from decimal import Decimal as D
from queue import Empty, Full, Queue
import threading

from polymarket._core.audit_log import AuditLog


@dataclass(frozen=True)
class CapacitySnapshot:
    opportunity_id: str
    observed_ns: int
    event_id: str
    condition_id: str
    instrument_id: str
    maximum: str
    weather_revision: int
    book_timestamp: str
    asks: tuple[tuple[str, str], ...]
    fee_rate: str
    tick_size: str
    minimum_edge: str
    safety_margin: str
    depth_limit: int


def measure_capacity(snapshot):
    """Measure visible qualifying asks without execution capital/quantity limits."""
    fee_rate = D(snapshot.fee_rate)
    minimum_edge = D(snapshot.minimum_edge)
    margin = D(snapshot.safety_margin)
    tick = D(snapshot.tick_size)
    quantity = notional = fees = D(0)
    levels = []
    for price, size in snapshot.asks:
        p, q = D(price), D(size)
        if not p.is_finite() or not q.is_finite() or not 0 < p < 1 or q < 0:
            raise ValueError("Invalid capacity book")
        if p % tick:
            raise ValueError("Off-tick capacity book")
        unit_fee = fee_rate * p * (1 - p)
        edge = 1 - p - unit_fee - D("0.00001") - margin
        if edge < minimum_edge:
            continue
        quantity += q
        notional += p * q
        fees += unit_fee * q
        levels.append([price, size])
    return {
        **snapshot.__dict__,
        "kind": "capacity_snapshot",
        "qualifying_asks": levels,
        "quantity": str(quantity),
        "notional": str(notional),
        "estimated_fees": str(fees),
        "estimated_capital": str(notional + fees),
        "conditional_settlement_profit": str(quantity - notional - fees),
        "depth_truncated_possible": len(snapshot.asks) >= snapshot.depth_limit,
        "aggregation": "instantaneous_do_not_sum",
    }


class CapacityRecorder:
    """One producer, one writer; overload drops telemetry, never blocks execution."""

    def __init__(self, path, *, queue_size=1024, max_bytes=256 * 1024 * 1024):
        if queue_size <= 0 or max_bytes <= 0:
            raise ValueError("Positive recorder bounds required")
        self.queue = Queue(maxsize=queue_size)
        self.max_bytes = max_bytes
        self.accepted = self.dropped = self.written = 0
        self.error = None
        self.closed = threading.Event()
        self.log = AuditLog(path, max_bytes=max_bytes, auto_flush=False)
        self.thread = threading.Thread(
            target=self._run, name="weather-capacity", daemon=True
        )
        self.thread.start()

    def offer(self, item):
        if self.closed.is_set() or self.error is not None:
            self.dropped += 1
            return False
        try:
            self.queue.put_nowait(item)
            self.accepted += 1
            return True
        except Full:
            self.dropped += 1
            return False

    def status(self):
        return {
            "accepted": self.accepted,
            "written": self.written,
            "dropped": self.dropped,
            "error": self.error,
            "queue_size": self.queue.qsize(),
            "alarm": bool(self.dropped or self.error),
            "segments": self.log.segment + 1,
        }

    def _run(self):
        try:
            while not self.closed.is_set() or not self.queue.empty():
                try:
                    item = self.queue.get(timeout=0.2)
                except Empty:
                    self.log.flush()
                    continue
                record = (
                    measure_capacity(item)
                    if isinstance(item, CapacitySnapshot)
                    else item
                )
                self.log.append(record["kind"], record)
                self.written += 1
                if self.written % 64 == 0:
                    self.log.flush()
        except Exception as e:
            self.error = type(e).__name__
        finally:
            self.log.close()

    def close(self):
        self.closed.set()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            self.error = "Writer shutdown timed out"
