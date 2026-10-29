"""Capacity arithmetic and first-taker sizing checks, without live execution."""

import io
import json
import shutil
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from capacity_run import ZERO
from capacity_run import bounded_taker
from capacity_run import qualifying_levels
from capacity_run import run_event
from capacity_run import total
from capacity_run import weather_updates


class CapacityTests(unittest.TestCase):
    def test_same_receipt_error_cancels_poll_without_transient_availability(self):
        poll = {"received_ns": 100, "kind": "metar_poll", "rows": []}
        error = {"received_ns": 100, "kind": "metar_error"}
        self.assertEqual(weather_updates([poll, error]), [error])
        self.assertEqual(weather_updates([error, poll]), [error])

    def test_single_sided_full_depth_and_fee_formula(self):
        levels = qualifying_levels({"0.98": "5", "0.999": "100", "1": "9999"}, D("0.05"))
        q, cost, fee, profit = total(levels, D(5))
        self.assertEqual(q, D(105))
        self.assertEqual(cost, D("104.8"))
        self.assertEqual(fee, D("0.009895"))
        self.assertEqual(profit, D("0.190105"))

    def test_dust_does_not_count_as_executable_capacity(self):
        self.assertEqual(total(qualifying_levels({"0.99": "4.99"}, D(0)), D(5)), ZERO)

    def test_no_liquidity_or_price_one_does_not_invent_maker_fill(self):
        self.assertIsNone(bounded_taker([], D(100), D(5), D(5), D(5)))
        self.assertEqual(qualifying_levels({"1": "100"}, D(0)), [])

    def test_bounded_taker_uses_best_ask_not_total_depth(self):
        levels = qualifying_levels({"0.98": "4", "0.99": "100"}, D(0))
        self.assertIsNone(bounded_taker(levels, D(100), D(5), D(5), D(5)))
        levels = qualifying_levels({"0.98": "8", "0.99": "100"}, D(0))
        result = bounded_taker(levels, D(100), D(5), D(5), D(5))
        self.assertEqual(result["quantity"], D(5))
        self.assertEqual(result["cost"], D("4.90"))
        self.assertEqual(result["attempt_reserve"], D("4.90005"))

    def test_edge_and_cash_constraints(self):
        self.assertEqual(qualifying_levels({"0.999": "10"}, D(0), D("0.002")), [])
        levels = qualifying_levels({"0.99": "20"}, D(0))
        self.assertIsNone(bounded_taker(levels, D(4), D(5), D(5), D(5)))

    def test_native_timeline_expires_weather_and_never_resweeps_condition(self):
        import collector_native as c

        from nautilus_trader.persistence import ParquetDataCatalog
        from polymarket.event_catalog.native import flatten_order_book_batches

        task = Path(__file__).parent
        with tempfile.TemporaryDirectory(dir=task / "tmp") as directory:
            root = Path(directory)
            raw = root / "raw.jsonl"
            envelopes = []
            for n, (stamp, price, size) in enumerate(
                [
                    ("2026-10-16T08:00:00.100Z", "0.99", "10"),
                    ("2026-10-16T08:00:10Z", "0.98", "20"),
                    ("2026-10-16T08:03:00Z", "0.01", "9999"),
                ],
                1,
            ):
                envelopes.append(
                    {
                        "capture_seq": n,
                        "timestamp_received": stamp,
                        "frame": {
                            "event_type": "book",
                            "market": "condition",
                            "asset_id": "456",
                            "timestamp": str(c.iso_ns(stamp) // 1_000_000),
                            "tick_size": "0.01",
                            "bids": [],
                            "asks": [{"price": price, "size": size}],
                        },
                    }
                )
            raw.write_text("".join(json.dumps(v) + "\n" for v in envelopes))
            scanned = c.scan(
                raw,
                {
                    "conditions": [
                        {
                            "condition_id": "condition",
                            "scheduled_expiration": "2026-10-16T12:00:00Z",
                            "metadata_evidence": {"test": True},
                            "tokens": [
                                {"token_id": "123", "outcome": "Yes"},
                                {"token_id": "456", "outcome": "No"},
                            ],
                        }
                    ]
                },
            )
            instruments = c.instruments_from_scan(scanned)
            audit = {}
            batches = list(c.native_stream(raw, instruments, audit, io.StringIO()))
            catalog_path = root / "catalog"
            catalog_path.mkdir()
            catalog = ParquetDataCatalog(str(catalog_path))
            catalog.write_instruments(instruments)
            catalog.write_order_book_deltas(flatten_order_book_batches(batches))
            manifest = {
                "analysis_window": {"start": "2026-10-16T08:00:00Z", "end": "2026-10-16T08:04:00Z"},
                "captured_window": {"received_max_ns": c.iso_ns("2026-10-16T08:04:00Z")},
                "audit": {"max_adjustment_ns": 0},
            }
            (catalog_path / "manifest.json").write_text(json.dumps(manifest))
            shutil.copyfile(
                str(Path(__file__).parent / "weather_metar_frozen.py"),
                root / "weather_metar_frozen.py",
            )
            t0 = c.iso_ns("2026-10-16T08:00:00Z")
            cfg = {
                "event_slug": "synthetic", "station": "ZSPD", "timezone": "Asia/Shanghai", "metar_rule": "decoded-temperature",
                "date": "2026-10-16",
                "catalog_path": str(catalog_path),
                "conditions": [
                    {
                        "condition_id": "condition",
                        "no_token_id": "456",
                        "upper_bound": "20",
                        "minimum_quantity": "5",
                        "fee_rate": "0",
                    }
                ],
                "weather": [
                    {
                        "kind": "metar_poll",
                        "received_ns": t0,
                        "rows": [
                            {
                                "icaoId": "ZSPD",
                                "obsTime": t0 // 1_000_000_000,
                                "receiptTime": "2026-10-16T08:00:00Z",
                                "temp": "25",
                            }
                        ],
                    }
                ],
            }
            input_path = root / "input.json"
            input_path.write_text(json.dumps(cfg))
            result = run_event(input_path, root / "result")
            self.assertEqual(D(result["weather_valid_hours"]), D(120) / 3600)
            self.assertEqual(result["peak_simultaneous_capacity"]["shares"], D(20))
            self.assertEqual(result["peak_simultaneous_capacity"]["notional"], D("19.60"))
            self.assertEqual(len(result["first_opportunity_sweep_per_condition"]), 1)
            self.assertEqual(result["first_opportunity_sweep_per_condition"][0]["shares"], D(10))
            self.assertEqual(result["bounded_first_taker_estimate"][0]["quantity"], D(5))
            self.assertIsNone(result["bounded_first_taker_estimate"][0]["settlement_profit"])


if __name__ == "__main__":
    unittest.main()
