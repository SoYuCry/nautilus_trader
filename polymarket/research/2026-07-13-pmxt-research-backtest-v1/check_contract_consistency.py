"""
Reproducible PMXT research replay-contract consistency check.

Runs WITHOUT the compiled Nautilus runtime.  It proves, on the real PMXT event
data referenced by experiment.yml, that:

1. the runner mode gate rejects this config unless replay.mode=pmxt_research;
2. the adapter replay order is deterministic across two independent loads;
3. the shared replay clock (timestamp, fallback timestamp_received) is
   non-decreasing and sequence is strictly increasing;
4. the factor baseline panel replays the identical step order and clock the
   backtest bridge will consume.

Usage (repository root):

    python polymarket/research/2026-07-13-pmxt-research-backtest-v1/check_contract_consistency.py \
        --config polymarket/research/2026-07-13-pmxt-research-backtest-v1/experiment.yml
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from polymarket.adapters.pmxt_event_v1 import PMXTEventV1Adapter  # noqa: E402
from polymarket.data_health import analyze_dataset_health  # noqa: E402
from polymarket.replay_contract import PMXT_RESEARCH_MODE  # noqa: E402
from polymarket.replay_contract import PMXT_RESEARCH_ORDERING_KEY  # noqa: E402
from polymarket.replay_contract import blocking_health_issues  # noqa: E402
from polymarket.replay_contract import enforce_replay_mode_gate  # noqa: E402
from polymarket.replay_contract import replay_timestamp  # noqa: E402
from polymarket.replay_contract import verify_pmxt_replay_clock_order  # noqa: E402


BASELINE_MODULE_PATH = REPO_ROOT / "polymarket/research/2026-07-08-pmxt-l2-factor-baseline/factor_research.py"


def load_yaml(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}


def load_baseline_module():
    spec = importlib.util.spec_from_file_location("pmxt_l2_factor_baseline_check", BASELINE_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--event-dir",
        type=Path,
        default=None,
        help="Override adapter.input.event_dir (the PMXT data lives outside this repository)",
    )
    args = parser.parse_args()
    config = load_yaml(args.config.resolve())
    if args.event_dir is not None:
        config["adapter"]["input"]["event_dir"] = str(args.event_dir)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results: dict[str, object] = {"generated_at": datetime.now(UTC).isoformat(), "run_id": run_id}
    event_dir = Path(str(config["adapter"]["input"]["event_dir"]))
    results["event_dir"] = str(event_dir)
    results["input_hashes"] = {
        name: {"size_bytes": (event_dir / name).stat().st_size, "sha256": sha256_file(event_dir / name)}
        for name in ("orderbook.parquet", "event_index.json", "gamma_event.raw.json", "manifest.json")
        if (event_dir / name).exists()
    }

    # 1. Mode gate: strict default rejects, explicit research mode passes.
    stripped = {key: value for key, value in config.items() if key != "replay"}
    try:
        enforce_replay_mode_gate(stripped)
        raise AssertionError("strict_capture default unexpectedly accepted PMXT adapter")
    except ValueError:
        results["strict_default_rejects_pmxt"] = True
    assert enforce_replay_mode_gate(config) == PMXT_RESEARCH_MODE
    results["explicit_research_mode_accepted"] = True

    # 2. Deterministic ordering across two independent loads.
    adapter_config = config["adapter"]
    dataset_a = PMXTEventV1Adapter(repo_root=REPO_ROOT).load(adapter_config)
    dataset_b = PMXTEventV1Adapter(repo_root=REPO_ROOT).load(adapter_config)
    key_a = [(step.sequence, step.timestamp, step.timestamp_received) for step in dataset_a.steps]
    key_b = [(step.sequence, step.timestamp, step.timestamp_received) for step in dataset_b.steps]
    assert key_a == key_b, "adapter replay order is not deterministic across loads"
    results["deterministic_across_loads"] = True
    results["steps"] = len(dataset_a.steps)
    results["ordering_key"] = PMXT_RESEARCH_ORDERING_KEY
    results["source_quality"] = dict(dataset_a.metadata.source_quality)

    # 3. Contract clock check + mode-aware health blocking.
    results["replay_clock_check"] = verify_pmxt_replay_clock_order(dataset_a)
    health = analyze_dataset_health(dataset_a)
    blocking = blocking_health_issues(health, mode=PMXT_RESEARCH_MODE)
    assert not blocking, f"pmxt_research blocking health codes: {sorted({i.code for i in blocking})}"
    results["health_receive_time_inversions_diagnostic"] = health.summary.receive_time_inversion_count
    results["health_blocking_issue_count_pmxt_mode"] = 0

    # 4. Factor baseline replays the same step order and clock.
    baseline = load_baseline_module()
    panel = baseline.build_factor_panel(dataset_a, config.get("adapter", {}))
    assert list(panel["sequence"]) == [step.sequence for step in dataset_a.steps]
    panel_clocks = [ts.to_pydatetime() for ts in panel["replay_timestamp"]]
    assert panel_clocks == [replay_timestamp(step) for step in dataset_a.steps]
    results["factor_panel_order_matches_steps"] = True
    results["factor_panel_clock_matches_contract"] = True

    text = json.dumps(results, indent=2, default=str) + "\n"
    runs_dir = args.config.resolve().parent / "consistency_runs"
    runs_dir.mkdir(exist_ok=True)
    (runs_dir / f"{run_id}.json").write_text(text, encoding="utf-8")
    # Latest-result convenience copy; per-run history lives in consistency_runs/.
    (args.config.resolve().parent / "contract_consistency_result.json").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
