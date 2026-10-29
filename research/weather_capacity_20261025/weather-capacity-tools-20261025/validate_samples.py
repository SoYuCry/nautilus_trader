"""Compare recorded snapshot samples to native readback and native L2 book application."""

import argparse
import json
from pathlib import Path

import collector_native as c
from compile_temporary import verify_sources
from compile_temporary import write_json
from inspect_transfer import ROOT

from nautilus_trader.model import BookType
from nautilus_trader.model import OrderBook
from nautilus_trader.persistence import ParquetDataCatalog
from polymarket.event_catalog.native import restore_order_book_batches


def validate(root: Path):
    if not root.resolve().is_relative_to((ROOT / "tmp").resolve()):
        raise ValueError("Validate only this task's temporary Catalogs")
    manifest = json.loads((root / "manifest.json").read_text())
    verify_sources(manifest)
    raw = next(
        Path(v["path"]) for v in manifest["source_files"] if Path(v["path"]).name == "market.jsonl"
    )
    samples, counts = {}, {}
    for position, message in c.messages(raw):
        if message.get("event_type") != "book":
            continue
        expected = {
            side: {
                c.decimal(p): c.decimal(q)
                for p, q in c.level_pairs(message, side)
                if c.decimal(q) > 0
            }
            for side in ("bids", "asks")
        }
        bids, asks = len(expected["bids"]), len(expected["asks"])
        kind = (
            "empty"
            if not bids and not asks
            else "single_sided"
            if not bids or not asks
            else "two_sided"
        )
        counts[kind] = counts.get(kind, 0) + 1
        if kind not in samples:
            samples[kind] = {
                "position": position,
                "expected": expected,
                "token_id": str(message["asset_id"]),
            }
    wanted = {
        (v["position"]["raw_line"], v["position"]["frame_item_index"], v["token_id"]): v
        for v in samples.values()
    }
    with (root / "source_mapping.jsonl").open() as stream:
        for line in stream:
            trace = json.loads(line)
            sample = wanted.get(
                (trace.get("raw_line"), trace.get("frame_item_index"), trace.get("token_id"))
            )
            if sample is not None:
                sample["ts_init"] = trace["ts_init"]
    catalog = ParquetDataCatalog(str(root))
    instruments = {str(v.raw_symbol): v for v in catalog.instruments()}
    accepted = []
    for kind, sample in samples.items():
        item = instruments[sample["token_id"]]
        batches = restore_order_book_batches(catalog.query_order_book_deltas([str(item.id)]))
        batch = next(v for v in batches if v.ts_init == sample["ts_init"])
        book = OrderBook(item.id, BookType.L2_MBP)
        book.apply_deltas(batch)
        if (
            book.bids_to_dict() != sample["expected"]["bids"]
            or book.asks_to_dict() != sample["expected"]["asks"]
        ):
            raise ValueError(f"Native L2 book differs from recorded snapshot: {kind}")
        accepted.append(
            {
                "kind": kind,
                "instrument_id": str(item.id),
                "raw_line": sample["position"]["raw_line"],
                "frame_item_index": sample["position"]["frame_item_index"],
                "capture_seq": sample["position"]["capture_seq"],
                "timestamp_received": sample["position"]["timestamp_received"],
                "ts_init": batch.ts_init,
                "bid_levels": len(book.bids()),
                "ask_levels": len(book.asks()),
                "exact_prices_and_quantities": "passed",
            }
        )
    verify_sources(manifest)
    result = {
        "status": "passed",
        "event_slug": manifest["event_slug"],
        "recorded_snapshot_counts": counts,
        "native_l2_snapshot_samples": accepted,
        "validator_sha256": c.sha256(Path(__file__)),
        "note": "Missing snapshot categories are not invented. Empty/single-sided synthetic cases also have scoped tests.",
    }
    write_json(root / "native_book_acceptance.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    print(json.dumps(validate(parser.parse_args().catalog), indent=2))
