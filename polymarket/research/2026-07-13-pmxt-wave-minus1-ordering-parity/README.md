# PMXT wave-minus1 ordering/parity evidence (G002)

Research-only program for machine-readable ordering/parity evidence across:

`PMXTEventV1Adapter -> PolymarketL2DatasetV1/O1 -> convert_dataset_to_nautilus -> Nautilus native objects/O3 -> engine callbacks`.

## Boundaries

- `performance_claims_allowed=false` for every artifact.
- No factor, outcome, strategy, fill, fee, position, cash, PnL, or performance conclusion is computed.
- No generic Nautilus engine architecture is modified.

## Ordering semantics

- **O1**: selected-token deterministic replay order. Key is `(source timestamp, timestamp_received, original row ordinal)`. Fully tied rows use original row ordinal only as a reproducible fallback, not true message order.
- **O2**: event-wide diagnostic/materialization order. This is emitted only for diagnostics and must not be cited as execution truth.
- **O3**: Nautilus native object and strategy callback observable order. The program records stable identities/counts/order signatures and whether callbacks match the native input contract.

## Run

```powershell
python polymarket/research/2026-07-13-pmxt-wave-minus1-ordering-parity/ordering_parity.py
```

Outputs are small JSON files under `outputs/`:

- `synthetic_ordering_parity.json`
- `real_curated_smoke_parity.json`
- `summary.json`

## Isolated validation command for local uncompiled repo

The repository-local `nautilus_trader` package may be uncompiled. To validate O3 engine callbacks without copying `.pyd` files or polluting the repo, run from a non-repo directory and import the installed compiled runtime first:

```powershell
$runDir = Join-Path $env:TEMP 'pmxt_ordering_parity_pytest'
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
@'
import os, sys
from pathlib import Path
run_dir = Path(os.environ['TEMP']) / 'pmxt_ordering_parity_pytest'
os.chdir(run_dir)
sys.path = [p for p in sys.path if p not in ('', str(run_dir), str(Path(r'C:/Projects/nautilus_trader')))]
import nautilus_trader
print('nautilus_trader loaded from', nautilus_trader.__file__)
repo = Path(r'C:/Projects/nautilus_trader')
sys.path.insert(0, str(repo))
import pytest
raise SystemExit(pytest.main([
    str(repo / 'polymarket/tests/test_pmxt_wave_minus1_ordering_parity.py'),
    str(repo / 'polymarket/tests/test_pmxt_event_adapter.py'),
    str(repo / 'polymarket/tests/test_replay_contract.py'),
    '-q',
]))
'@ | python -
```

Expected evidence: `nautilus_trader` prints from `C:\Users\xhth\miniconda3\Lib\site-packages\...`, while `polymarket` and tests are loaded from this repo.
