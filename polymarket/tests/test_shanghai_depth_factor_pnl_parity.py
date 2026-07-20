from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "polymarket"
    / "research"
    / "2026-07-20-shanghai-depth-factor-pnl-parity"
    / "depth_pnl_parity.py"
)


def test_synthetic_executable_label_matches_nautilus_pnl_and_boundaries(tmp_path: Path) -> None:
    completed = subprocess.run(  # noqa: S603 - fixed local interpreter and repository script
        [sys.executable, str(SCRIPT), "--synthetic-only", "--output-dir", str(tmp_path)],
        cwd=REPO_ROOT.parent,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    rows = json.loads((tmp_path / "depth_pnl_parity.json").read_text(encoding="utf-8"))["synthetic"]
    assert [row["case"] for row in rows] == [
        "normal_round_trip",
        "missing_entry_ask_at_0999",
        "missing_exit_bid_then_recovers",
    ]
    assert all(row["status"] == "pass" for row in rows)
    normal, no_ask, delayed_exit = rows
    assert normal["counts"]["completed_round_trips"] == 1
    assert normal["gross_pnl"]["direct"] == pytest.approx(-0.10)
    assert normal["gross_pnl"]["native"] == pytest.approx(-0.10)
    assert no_ask["counts"]["fills"] == 0
    assert no_ask["counts"]["direct_missing_entry_ask_callbacks"] >= 1
    assert delayed_exit["counts"]["completed_round_trips"] == 1
    assert delayed_exit["counts"]["direct_exit_due_missing_bid_callbacks"] >= 1
    assert delayed_exit["counts"]["native_exit_due_missing_bid_callbacks"] >= 1
