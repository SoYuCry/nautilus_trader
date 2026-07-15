# PMXT Wave -1 Inventory / Quality Model

Research-only dated program for G001. It inventories the curated 441 highest-temperature PMXT event bundles and emits deterministic machine-readable artifacts without computing factors, labels, benchmarks, strategy decisions, fills, fees, positions, or PnL.

Default real-data smoke:

```powershell
python polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/inventory.py `
  --root C:/Projects/PolyReaper/data/curated/polymarket/events `
  --output-dir polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/outputs
```

Key artifacts:

- `inventory_summary.json` - universe reconciliation and pass/fail checks.
- `event_inventory.parquet` - canonical compact detail, one row per event bundle.
- `token_inventory.parquet` - canonical compact detail, one row per event token.
- `quality_calibration.json` - G003 outcome-blind systems calibration; no performance claims.
- `representative_sample.json` - frozen deterministic outcome-blind sample selector output.
- `outcome_blind_invariance.json` - baseline/poisoned/removed input invariance audit.
- `protocol-candidate-g003.json` - candidate protocol record for G004; no M1/M2/M3 choice.
- `protocol-v1.json` - current-panel cohort and Wave -1 rule record.
- `task_state.json` - per-event isolated task status for resumability/failure diagnosis.

Detailed inventories are generated locally and are covered by the repository's global parquet ignore. The program does not duplicate them as JSON. If parquet support is unavailable, it emits a CSV fallback and a small `.parquet.unavailable.txt` diagnostic instead.

Boundaries:

- `performance_claims_allowed=false` in Wave -1 artifacts.
- Current June 4/5/8/9/10 dates are `primary_development_replication`.
- Current June 6/7/11/12 dates are `degraded_robustness`.
- No current date is confirmatory; future confirmation requires a future unseen sealed cohort.
- Manifest missing/corrupt hours create hard-break provenance only when they intersect each event's half-open `[event_start,event_end)` window. Nonintersecting gaps remain provenance-only.
- G003 calibration uses source integrity, schemas/contracts, timestamps/update types,
  missing/corrupt intersections, bounded O1 ordering-ambiguity counts, label-match feasibility categories,
  and resource measurements only. It does not read factor values, future return
  direction/magnitude, outcomes/settlement, strategy decisions, fills, fees,
  positions, cash, or PnL.
- G003 does not claim a full 747M row scan. Full timestamp/label-match distributions
  are explicitly `deferred`; any timestamp evidence is bounded representative
  timestamp-column-only sampling.
- The real 16-token O1 ambiguity scan exceeded the 180-second cap and is explicitly
  `deferred`. The artifacts reference G002 `real_curated_smoke_parity.json` by path
  and SHA-256 for ordering evidence only; G003 does not claim a real ambiguity-heavy winner.

G003 calibration smoke:

```powershell
python polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/calibrate_quality.py `
  --input-dir polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/outputs `
  --output-dir polymarket/research/2026-07-13-pmxt-wave-minus1-inventory/outputs `
  --timestamp-sample-events 0 `
  --ambiguity-token-budget 16 `
  --defer-ambiguity-scan-reason real_budget_16_scan_deferred_after_exceeding_180_second_runtime_cap
```
