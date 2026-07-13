# PMXT Wave -1 Materialization Benchmark (G004)

G004 is a research-only, outcome-blind systems benchmark. Its ordering scope is
**O1 selected-token replay only**. The bounded real G002 O3 artifact is referenced
as external evidence; G004 does not rerun, benchmark, or claim O3 parity.

M1 performs bounded PyArrow row-group/batch source streaming, filters one selected
market/token, attaches the true physical ordinal from each global batch offset, and
then holds only those selected rows in an O1 sort buffer. M1 and M2 retain no full
primitive list: both use rolling digests/counts plus first/middle/last checkpoints,
and M2 additionally creates sparse causal state anchors during that one-pass reduction.
Only M3 retains and writes the full selected-token primitive trace, never orders, fills, strategy
state, outcomes, or PnL. M2/M3 caches are disposable and commit-manifest validated.

The independent PyArrow reducer decodes committed artifacts: M1 is checked against
rolling O1 evidence, every M2 anchor is checked for sequence/primitive/state/prefix
digest, and M3 is checked against the exact full primitive trace. Any mode failure
prevents report generation.

Run the fresh bounded representative benchmark:

```powershell
python polymarket/research/2026-07-14-pmxt-wave-minus1-materialization-benchmark/benchmark_materialization.py --include-complete-date-probe
```

The Jun4 surface records all 49 frozen task IDs as `manifest_feasibility_only` and
does not attempt full-date M3. The decision remains deferred because both the real
ambiguity role and full-date benchmark remain deferred.

## Artifact map

`outputs/benchmark_report.json` joins these checklist surfaces:

- `benchmark_manifest.json`
- `representative_sample_g004.json`
- `m1_m2_m3_results.json`
- `parity_oracles.json`
- `cache_resume_tests.json`
- `outcome_blind_invariance_g004.json`
- `complete_date_feasibility.json`
- `materialization_decision.json`

Generated paths are relative to this benchmark directory. `application_cold_os_cache`
is unknown. Windows process I/O deltas are logical transfer bytes, not physical disk
bytes. Source file size is reported separately as `source_file_bytes_upper_bound`.
RSS is sampled per call from Windows `GetProcessMemoryInfo` working set; it is not
Python allocator memory. `worker_count=1`. Hard breaks require intersecting missing
or corrupt manifest hours; natural update staleness alone is kept distinct.

All outputs retain `performance_claims_allowed=false`, `not_for_pnl=true`, and panel
status `not_confirmatory`.
