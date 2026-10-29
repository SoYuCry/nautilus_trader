"""Synthetic-only native wiring tests; never inspect historical or uploaded data."""

import io
import json
import tempfile
import unittest
from pathlib import Path

import collector_native as c

from nautilus_trader.model import BookAction
from nautilus_trader.model import InstrumentClose
from nautilus_trader.model import OrderBookDeltas
from nautilus_trader.model import RecordFlag
from nautilus_trader.model import TradeTick
from nautilus_trader.persistence import ParquetDataCatalog
from polymarket.event_catalog.native import flatten_order_book_batches
from polymarket.event_catalog.native import restore_order_book_batches


class NativeWiringTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).parent / "tmp"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "market.jsonl"
        self.metadata = {
            "conditions": [
                {
                    "condition_id": "condition",
                    "scheduled_expiration": "2026-10-24T00:00:00+08:00",
                    "metadata_evidence": {"path": "synthetic.http.json", "key": "markets[0]"},
                    "tokens": [
                        {"token_id": "123", "outcome": "Yes"},
                        {"token_id": "456", "outcome": "No"},
                    ],
                }
            ]
        }

    def book(self, token="123", bids=None, asks=None, tick="0.001", timestamp="1792713600000"):
        return {
            "event_type": "book",
            "asset_id": token,
            "market": "condition",
            "timestamp": timestamp,
            "bids": bids or [],
            "asks": asks or [],
            "tick_size": tick,
        }

    def change(self, token="123", price="0.200", size="2.000001", bid=None, ask=None):
        return {
            "asset_id": token,
            "side": "BUY",
            "price": price,
            "size": size,
            "best_bid": bid,
            "best_ask": ask,
        }

    def pc(self, changes, timestamp="1792713600001"):
        return {
            "event_type": "price_change",
            "market": "condition",
            "timestamp": timestamp,
            "price_changes": changes,
        }

    def write(self, frames, receipts=None):
        receipts = receipts or ["2026-10-23T00:00:00.123456789+08:00"] * len(frames)
        self.raw.write_text(
            "".join(
                json.dumps(
                    {
                        "capture_seq": i * 2 + 10,
                        "timestamp_received": receipt,
                        "received_monotonic_ns": 100 + i,
                        "frame": frame,
                    }
                )
                + "\n"
                for i, (frame, receipt) in enumerate(zip(frames, receipts, strict=True))
            )
        )

    def adapt(self):
        scanned = c.scan(self.raw, self.metadata)
        instruments = c.instruments_from_scan(scanned)
        trace, audit = io.StringIO(), {}
        data = list(c.native_stream(self.raw, instruments, audit, trace))
        return (
            scanned,
            instruments,
            data,
            audit,
            [json.loads(line) for line in trace.getvalue().splitlines()],
        )

    def test_nanosecond_clock_and_decimal_exactness_native_readback(self):
        self.write(
            [self.book(bids=[{"price": "0.1234567890123456", "size": "2.1234567890123456"}])]
        )
        scanned, instruments, data, audit, trace = self.adapt()
        self.assertEqual(c.iso_ns("1970-01-01T00:00:00.123456789Z"), 123456789)
        self.assertEqual(data[0].ts_init % 1_000_000_000, 123456789)
        delta = data[0].deltas[1]
        self.assertEqual(str(delta.order.price), "0.1234567890123456")
        self.assertEqual(str(delta.order.size), "2.1234567890123456")
        (self.root / "catalog").mkdir()
        catalog = ParquetDataCatalog(str(self.root / "catalog"))
        catalog.write_instruments(instruments)
        catalog.write_order_book_deltas(flatten_order_book_batches(data))
        batches = restore_order_book_batches(
            catalog.query_order_book_deltas([str(instruments[0].id)])
        )
        self.assertEqual(str(batches[0].deltas[1].order.size), "2.1234567890123456")
        self.assertEqual(len(catalog.instruments()), 2)
        self.assertIsNone(instruments[0].info["fee_schedule"])
        self.assertEqual(instruments[0].info["fee_status"], "unknown")

    def test_original_message_token_atomicity_no_received_coalescing(self):
        self.write(
            [
                [
                    self.book(),
                    self.pc([self.change(), self.change("456"), self.change(price="0.100")]),
                ],
                self.book(),
            ]
        )
        _, _, data, audit, trace = self.adapt()
        self.assertEqual(len(data), 4)
        self.assertEqual([len(item.deltas) for item in data], [1, 2, 1, 1])
        self.assertEqual(trace[1]["change_indices"], [0, 2])
        self.assertEqual(trace[2]["change_indices"], [1])
        self.assertEqual(audit["adjusted_record_count"], 3)
        for batch in data:
            self.assertEqual(sum(bool(d.flags & int(RecordFlag.F_LAST)) for d in batch.deltas), 1)
            self.assertTrue(batch.deltas[-1].flags & int(RecordFlag.F_LAST))
            self.assertEqual(len({d.ts_init for d in batch.deltas}), 1)

    def test_late_source_timestamp_never_moves_record_before_receipt(self):
        self.write(
            [self.book(timestamp="1792713601000"), self.book(timestamp="1792713590000")],
            ["2026-10-23T00:00:02Z", "2026-10-23T00:00:03Z"],
        )
        _, _, data, _, _ = self.adapt()
        self.assertGreater(data[0].ts_event, data[1].ts_event)
        self.assertLess(data[0].ts_init, data[1].ts_init)
        self.assertEqual(data[1].ts_init, c.iso_ns("2026-10-23T00:00:03Z"))

    def test_wall_clock_regression_preserves_capture_availability_order(self):
        self.write([self.book(), self.book()], ["2026-10-23T00:00:02Z", "2026-10-23T00:00:01Z"])
        _, _, data, audit, trace = self.adapt()
        self.assertEqual(data[1].ts_init, data[0].ts_init + 1)
        self.assertEqual(audit["receive_wall_clock_regressions"], 1)
        self.assertEqual(trace[1]["adjustment_ns"], 1_000_000_001)

    def test_single_sided_multi_level_and_empty_snapshot_preserved(self):
        self.write(
            [
                self.book(bids=[["0.2", "2"], ["0.1", "3"]]),
                self.book(asks=[["0.8", "4"], ["0.9", "5"]]),
                self.book(),
            ]
        )
        _, _, data, _, _ = self.adapt()
        self.assertEqual([len(b.deltas) for b in data], [3, 3, 1])
        self.assertTrue(all(b.deltas[0].action == BookAction.CLEAR for b in data))
        self.assertFalse(any(isinstance(item, InstrumentClose) for item in data))

    def test_exact_seller_pruning_and_missing_bbo_policy(self):
        self.write(
            [
                self.book(
                    bids=[["0.4", "1"], ["0.3", "2"], ["0.2", "3"]],
                    asks=[["0.6", "4"], ["0.7", "5"]],
                ),
                self.pc([self.change(price="0.3", bid="0.3", ask="0.6")]),
                self.pc([self.change(price="0.4", bid=None, ask="0.6")]),
            ]
        )
        _, _, data, audit, trace = self.adapt()
        self.assertEqual(c.RULE, "carried_bbo_delete_only/v1")
        self.assertEqual(audit["pruning"]["123"]["deleted_levels"], 1)
        self.assertEqual(len(data[1].deltas), 2)
        self.assertEqual(data[1].deltas[-1].action, BookAction.DELETE)
        self.assertEqual(str(data[1].deltas[-1].order.price), "0.400")
        self.assertEqual(
            len(data[2].deltas), 1
        )  # incomplete quote does not delete the observed level

    def test_preinitialization_updates_retained_and_unknown_initial_tick_null(self):
        self.write([self.pc([self.change()]), self.book(tick="0.001")])
        scanned, instruments, data, audit, trace = self.adapt()
        self.assertEqual(len(data), 2)
        self.assertEqual(audit["counts"]["batches_before_initialization"], 1)
        self.assertIsNone(instruments[0].info["effective_initial_tick_size"])
        self.assertEqual(scanned["tokens"]["123"]["first_book"]["tick_size"], "0.001")
        self.assertFalse(trace[0]["initialized"])

    def test_book_tick_unknown_and_later_tick_notifications_trace_only(self):
        tick = {
            "event_type": "tick_size_change",
            "market": "condition",
            "asset_id": "123",
            "timestamp": "1792713600001",
            "old_tick_size": "0.01",
            "new_tick_size": "0.001",
        }
        self.write([self.book(tick=None), tick, self.book(tick="0.001")])
        scanned, instruments, data, _, trace = self.adapt()
        self.assertIsNone(instruments[0].info["effective_initial_tick_size"])
        self.assertEqual(trace[1]["status"], "recorded_tick_notification")
        self.assertEqual(len(data), 2)

    def test_actual_websocket_resolution_only_not_scheduled_expiry(self):
        self.write(
            [
                self.book(),
                {
                    "event_type": "market_resolved",
                    "market": "condition",
                    "winning_asset_id": "123",
                    "timestamp": "1792713601000",
                },
            ]
        )
        _, _, data, audit, trace = self.adapt()
        closes = [item for item in data if isinstance(item, InstrumentClose)]
        self.assertEqual(len(closes), 2)
        self.assertEqual([str(item.close_price) for item in closes], ["1.000", "0"])
        self.assertEqual(audit["tokens_without_actual_close"], [])

    def test_resolution_duplicate_requires_consistent_winner(self):
        resolved = {
            "event_type": "market_resolved",
            "market": "condition",
            "winning_asset_id": "123",
            "timestamp": "1792713601000",
        }
        self.write([self.book(), resolved, resolved])
        _, _, data, _, trace = self.adapt()
        self.assertEqual(sum(isinstance(item, InstrumentClose) for item in data), 2)
        self.assertEqual(
            sum(row.get("status") == "duplicate_actual_close_evidence" for row in trace), 2
        )
        self.write([self.book(), resolved, {**resolved, "winning_asset_id": "456"}])
        with self.assertRaisesRegex(ValueError, "Conflicting actual winners"):
            self.adapt()

    def test_trades_and_unknown_tokens_never_silently_filtered(self):
        trade = {
            "event_type": "last_trade_price",
            "asset_id": "123",
            "market": "condition",
            "timestamp": "1792713600001",
            "price": "0.123",
            "size": "3.1234567",
            "side": "SELL",
        }
        self.write([trade])
        _, _, data, _, _ = self.adapt()
        self.assertIsInstance(data[0], TradeTick)
        self.assertEqual(str(data[0].size), "3.1234567")
        self.write([self.book(token="999")])
        with self.assertRaisesRegex(ValueError, "Unmapped raw token"):
            self.adapt()

    def test_missing_expiry_blocks_without_fake_future_or_cutoff(self):
        self.write([self.book()])
        self.metadata["conditions"][0]["scheduled_expiration"] = None
        with self.assertRaisesRegex(ValueError, "no recorded scheduled expiration"):
            self.adapt()

    def test_no_float_or_rounding_bridge(self):
        with self.assertRaises(TypeError):
            c.decimal(0.1)
        self.write([self.book(bids=[["0.12345678901234567", "1"]])])
        with self.assertRaisesRegex(ValueError, "exceeds native precision"):
            self.adapt()


if __name__ == "__main__":
    unittest.main()
