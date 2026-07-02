"""Contract tests for the Polymarket v1 Nautilus-native runner."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest


pytest.importorskip("nautilus_trader.core.data", reason="Nautilus compiled runtime is not built")

from polymarket.backtest_v1 import run_from_config  # noqa: E402


def write_ndjson(path: Path) -> None:
    rows = [
        {
            "local_msg_index": 1,
            "recv_wall_time_utc": "2026-06-26T02:25:28.635Z",
            "raw_json": {
                "event_type": "book",
                "market": "condition",
                "asset_id": "yes",
                "timestamp": "2026-06-26T02:25:28.600Z",
                "bids": [["0.40", "100"]],
                "asks": [["0.60", "100"]],
            },
        },
        {
            "local_msg_index": 2,
            "recv_wall_time_utc": "2026-06-26T02:25:29.000Z",
            "raw_json": {
                "event_type": "price_change",
                "market": "condition",
                "timestamp": "2026-06-26T02:25:28.900Z",
                "price_changes": [
                    {
                        "asset_id": "yes",
                        "side": "BUY",
                        "price": "0.41",
                        "size": "50",
                    },
                    {
                        "asset_id": "yes",
                        "side": "SELL",
                        "price": "0.60",
                        "size": "90",
                    },
                ],
            },
        },
        {
            "local_msg_index": 3,
            "recv_wall_time_utc": "2026-06-26T02:25:30.000Z",
            "raw_json": {
                "event_type": "last_trade_price",
                "market": "condition",
                "asset_id": "yes",
                "timestamp": "2026-06-26T02:25:29.900Z",
                "side": "BUY",
                "price": "0.60",
                "size": "10",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_runner_uses_nautilus_backtest_engine_and_native_reports(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "live.ndjson"
    write_ndjson(ndjson_path)
    config_path = tmp_path / "experiment.yml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: native_runner_smoke
            adapter:
              name: live_ws_v1
              input:
                ndjson_path: {ndjson_path.as_posix()}
            selection:
              asset_id: "yes"
            strategy:
              enabled: false
            runtime:
              run_id: native-runner-smoke
            report:
              output_dir: ./runs
            """,
        ).lstrip(),
        encoding="utf-8",
    )

    summary = run_from_config(config_path)
    run_dir = Path(summary["run_dir"])
    resolved = json.loads(Path(summary["outputs"]["resolved_config"]).read_text(encoding="utf-8"))
    run_summary = json.loads(Path(summary["outputs"]["summary"]).read_text(encoding="utf-8"))

    assert summary["engine"] == "nautilus_trader.backtest.engine.BacktestEngine"
    assert run_summary["engine"] == "nautilus_trader.backtest.engine.BacktestEngine"
    assert resolved["engine"] == "nautilus_trader.backtest.engine.BacktestEngine"
    assert summary["order_book_deltas_count"] == 2
    assert summary["trade_ticks_count"] == 1
    assert (run_dir / "account_report.txt").exists()
    assert (run_dir / "fills_report.txt").exists()
    assert (run_dir / "positions_report.txt").exists()


def test_report_output_dir_must_stay_inside_experiment_runs(tmp_path: Path) -> None:
    ndjson_path = tmp_path / "live.ndjson"
    write_ndjson(ndjson_path)
    config_path = tmp_path / "bad-output.yml"
    config_path.write_text(
        textwrap.dedent(
            f"""
            experiment:
              name: bad_output
            adapter:
              name: live_ws_v1
              input:
                ndjson_path: {ndjson_path.as_posix()}
            selection:
              asset_id: "yes"
            strategy:
              enabled: false
            runtime:
              run_id: bad-output
            report:
              output_dir: ../outside-runs
            """,
        ).lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="experiment-local runs"):
        run_from_config(config_path)
