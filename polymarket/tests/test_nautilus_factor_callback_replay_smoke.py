from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    REPO_ROOT
    / "polymarket"
    / "research"
    / "2026-07-17-nautilus-factor-callback-replay-smoke"
    / "factor_callback_smoke.py"
)
def test_synthetic_factor_callback_matches_direct_replay(tmp_path: Path) -> None:
    output = tmp_path / "factor_callback_smoke.json"
    completed = subprocess.run(  # noqa: S603 - fixed local interpreter and repository script
        [sys.executable, str(SCRIPT), "--synthetic-only", "--output-dir", str(tmp_path)],
        cwd=REPO_ROOT.parent,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    result = json.loads(output.read_text(encoding="utf-8"))["synthetic"]
    assert result["status"] == "pass"
    assert result["checks"] == {
        "callback_book_state_matches_direct": True,
        "callback_mutation_semantics_match_direct": True,
        "callback_ts_matches_native_input": True,
        "callback_ts_monotonic": True,
        "no_op_not_ranked_or_signaled": True,
        "ranking_sequences_complete": True,
        "ranking_sequences_preserved": True,
        "signal_trigger_sequences_match": True,
    }
    assert result["counts"]["no_op_rows"] >= 1
    assert result["counts"]["ranking_observations"] >= 2


def test_synthetic_factor_callback_is_deterministic(tmp_path: Path) -> None:
    signatures = []
    for _ in range(2):
        completed = subprocess.run(  # noqa: S603 - fixed local interpreter and repository script
            [sys.executable, str(SCRIPT), "--synthetic-only", "--output-dir", str(tmp_path)],
            cwd=REPO_ROOT.parent,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout
        output = tmp_path / "factor_callback_smoke.json"
        signatures.append(json.loads(output.read_text(encoding="utf-8"))["summary_signature"])
    assert signatures[0] == signatures[1]
