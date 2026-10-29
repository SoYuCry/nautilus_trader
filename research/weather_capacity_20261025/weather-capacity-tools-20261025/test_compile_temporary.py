"""Synthetic atomic compiler/readback and frozen-input gate tests."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import compile_temporary as compiler
import validate_samples
from inspect_transfer import ROOT
from inspect_transfer import file_identity


class CompileTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "market.jsonl"
        book = {
            "event_type": "book",
            "market": "condition",
            "asset_id": "123",
            "timestamp": "1792713600000",
            "tick_size": "0.001",
            "bids": [["0.3", "1.1234567"], ["0.2", "2"]],
            "asks": [],
        }
        trade = {
            "event_type": "last_trade_price",
            "market": "condition",
            "asset_id": "123",
            "timestamp": "1792713590000",
            "price": "0.3",
            "size": "2.1234567",
            "side": "BUY",
        }
        self.raw.write_text(
            json.dumps(
                {
                    "capture_seq": 1,
                    "received_monotonic_ns": 100,
                    "timestamp_received": "2026-10-23T00:00:00.123456789+08:00",
                    "frame": [book, trade],
                }
            )
            + "\n"
        )
        self.http = self.root / "saved-http.json"
        self.http.write_text('{"synthetic":true}\n')
        self.contract = {
            "schema": "temporary_weather_frozen_input/v1",
            "synthetic_test": True,
            "transfer_status": "completed_and_frozen",
            "completion_confirmation": "synthetic fixture only",
            "event_slug": "synthetic-event",
            "raw_path": str(self.raw),
            "source_files": [
                {"path": str(path), "identity": file_identity(path)}
                for path in (self.raw, self.http)
            ],
            "analysis_window": {
                "timezone": "Asia/Shanghai",
                "start": "2026-10-23T00:00:00+08:00",
                "end": "2026-10-23T00:00:00.123456789+08:00",
            },
            "metadata": {
                "conditions": [
                    {
                        "condition_id": "condition",
                        "scheduled_expiration": "2026-10-24T00:00:00+08:00",
                        "metadata_evidence": {"path": str(self.http), "key": "synthetic"},
                        "tokens": [
                            {"token_id": "123", "outcome": "Yes"},
                            {"token_id": "456", "outcome": "No"},
                        ],
                    }
                ]
            },
        }
        self.contract_path = self.root / "input.json"
        self.output = self.root / "catalog"

    def compile(self):
        self.contract_path.write_text(json.dumps(self.contract))
        return compiler.compile_event(self.contract_path, self.output)

    def test_atomic_native_compile_readback_full_window_and_partial_manifest(self):
        self.compile()
        complete = compiler.read_json(self.output / "COMPLETE.json")
        manifest = compiler.read_json(self.output / "manifest.json")
        validation = compiler.read_json(self.output / "validation.json")
        self.assertEqual(complete["status"], "passed")
        self.assertEqual(manifest["counts"], {"OrderBookDeltas": 1, "TradeTick": 1})
        self.assertEqual(
            manifest["replay"]["window_policy"], "full_captured_event_window_no_analysis_crop"
        )
        self.assertEqual(manifest["capacity_stream_status"], "partial")
        self.assertEqual(validation["instrument_count"], 2)
        self.assertEqual(validation["book_samples"][0]["bid_levels"], 2)
        self.assertEqual(validation["book_samples"][0]["ask_levels"], 0)
        self.assertEqual(
            manifest["partial_metadata"]["tokens_without_actual_close"], ["123", "456"]
        )
        self.assertTrue((self.output / "source_mapping.jsonl").is_file())
        self.assertTrue(manifest["depth_policy"]["sha256"])
        native_sample = validate_samples.validate(self.output)
        self.assertEqual(native_sample["status"], "passed")
        self.assertEqual(native_sample["native_l2_snapshot_samples"][0]["bid_levels"], 2)
        self.assertEqual(native_sample["native_l2_snapshot_samples"][0]["ask_levels"], 0)
        with self.assertRaises(FileExistsError):
            self.compile()

    def test_instrument_readback_rejects_semantic_drift_with_same_ids(self):
        self.compile()
        catalog = compiler.ParquetDataCatalog(str(self.output))
        originals = catalog.instruments()
        for field in (
            "raw_symbol",
            "outcome",
            "price_precision",
            "size_precision",
            "expiration_ns",
        ):
            with self.subTest(field=field):
                values = copy.deepcopy(originals[0].to_dict())
                values[field] = "incorrect"
                altered = SimpleNamespace(id=originals[0].id, to_dict=lambda: values)
                with self.assertRaisesRegex(ValueError, "instrument semantic fields differ"):
                    compiler.readback(self.output, [altered, *originals[1:]], {})
        for field in (
            "condition_id",
            "token_id",
            "effective_initial_tick_size",
            "expiration_semantics",
            "fee_status",
        ):
            with self.subTest(info_field=field):
                values = copy.deepcopy(originals[0].to_dict())
                values["info"][field] = "incorrect"
                altered = SimpleNamespace(id=originals[0].id, to_dict=lambda: values)
                with self.assertRaisesRegex(ValueError, "instrument semantic fields differ"):
                    compiler.readback(self.output, [altered, *originals[1:]], {})

    def test_actual_saved_http_resolution_available_at_receipt_not_close_time(self):
        self.contract["http_resolutions"] = [
            {
                "condition_id": "condition",
                "winning_asset_id": "123",
                "actual_close_time": "2026-10-22T16:05:00+00:00",
                "evidence": {
                    "path": str(self.http),
                    "request_log_path": str(self.http),
                    "received_at": "2026-10-23T01:00:00+00:00",
                    "request_log_line": 1,
                },
            }
        ]
        self.compile()
        manifest = compiler.read_json(self.output / "manifest.json")
        self.assertEqual(manifest["counts"]["InstrumentClose"], 2)
        self.assertEqual(manifest["partial_metadata"]["tokens_without_actual_close"], [])
        catalog = compiler.ParquetDataCatalog(str(self.output))
        closes = catalog.query(compiler.NautilusDataType.InstrumentClose)
        self.assertEqual(
            {v.ts_event for v in closes}, {compiler.c.iso_ns("2026-10-22T16:05:00+00:00")}
        )
        self.assertEqual(
            min(v.ts_init for v in closes), compiler.c.iso_ns("2026-10-23T01:00:00+00:00")
        )
        traces = [
            json.loads(line)
            for line in (self.output / "source_mapping.jsonl").read_text().splitlines()
        ]
        self.assertEqual(sum(v.get("origin") == "saved_http" for v in traces), 2)

    def test_changed_frozen_source_blocks_before_publication(self):
        with self.raw.open("a") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "Frozen source changed"):
            self.compile()
        self.assertFalse(self.output.exists())

    def test_no_completed_handoff_no_compile(self):
        self.contract["transfer_status"] = "quiet_directory"
        with self.assertRaisesRegex(ValueError, "completed/frozen"):
            self.compile()
        self.assertFalse(self.output.exists())

    def test_post_conversion_source_mutation_refuses_publication(self):
        original = compiler.readback

        def mutate(*args):
            result = original(*args)
            with self.http.open("a") as stream:
                stream.write("\n")
            return result

        with patch.object(compiler, "readback", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "Frozen source changed"):
                self.compile()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
