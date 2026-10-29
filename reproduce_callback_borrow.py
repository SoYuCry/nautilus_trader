"""Minimal native callback reentry probe; no clients, network, credentials, or orders sent."""

from datetime import timedelta
import json

import nautilus_trader
from nautilus_trader.common import Environment, LoggerConfig, LogLevel
from nautilus_trader.live import LiveNode
from nautilus_trader.model import (
    InstrumentId,
    OrderInitialized,
    OrderSide,
    Price,
    Quantity,
    TraderId,
)
from nautilus_trader.trading import Strategy


class Probe(Strategy):
    def __init__(self):
        super().__init__()
        self.seen = False
        self.cache_access = False
        self.failure = None

    def on_start(self):
        self.clock.set_timer("submit", timedelta(milliseconds=50), callback=self.send)

    def send(self, _event):
        self.clock.cancel_timer("submit")
        try:
            self.submit_order(
                self.order_factory.limit(
                    InstrumentId.from_str("EXAMPLE.SIM"),
                    OrderSide.BUY,
                    Quantity.from_str("1"),
                    Price.from_str("0.5"),
                )
            )
        finally:
            self.handle.stop()

    def on_order_event(self, event):
        if isinstance(event, OrderInitialized):
            self.seen = True
            try:
                _ = self.cache
                self.cache_access = True
            except Exception as e:
                self.failure = f"{type(e).__name__}: {e}"
                raise


if __name__ == "__main__":
    node = (
        LiveNode.builder(
            "CALLBACK-PROBE", TraderId("CALLBACK-001"), Environment.SANDBOX
        )
        .with_logging(LoggerConfig(stdout_level=LogLevel.WARNING))
        .with_reconciliation(False)
        .with_timeout_connection(0)
        .with_timeout_portfolio(0)
        .with_delay_post_stop_secs(0)
        .build()
    )
    probe = Probe()
    probe.handle = node.handle()
    node.add_strategy(probe)
    try:
        node.run()
    finally:
        node.dispose()
    print(
        json.dumps(
            {
                "version": nautilus_trader.__version__,
                "initialized_seen": probe.seen,
                "cache_access_ok": probe.cache_access,
                "failure": probe.failure,
                "execution_clients": 0,
            }
        )
    )
    if not probe.seen:
        raise RuntimeError("Probe did not reach initialization callback")
