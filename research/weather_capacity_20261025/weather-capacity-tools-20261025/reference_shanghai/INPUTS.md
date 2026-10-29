# Recorded Shanghai capacity inputs

Input preparation only: no capacity/profit model, statistics run, Catalog compile,
service/Redis/credential access, or primary exposure. This stage adds only
`prepare_capacity_inputs.py`, `test_prepare_capacity_inputs.py` and this document;
no commit/push is performed by the worker.

## Ready output

Root:
`/data/liuchang/nautilus_catalog_organization/polymarket/research/2026-10-23-weather-history-capacity/tmp/capacity-inputs/`

- `index.json`: eight Event input paths, counts, fee scenarios, 48 frozen weather-log
  read bounds and saved config identities.
- `{event_slug}.json`: condition/config evidence and recorded weather.
- `weather_metar_frozen.py`: byte copy of primary checkout's
  `polymarket/strategies/weather_metar.py`, including the rounded AWC rule.
  SHA256: `d6781b6ef6587f086a1acb3fb33d6dad301bd6aa56b6c2ce891966de43b21924`.
  Preparation does not import/execute it. Consumer should call `normalize_metar`,
  never the copied module's network-fetch function.

## Actual extracted counts

| October day | metar_poll | metar_error | fee_rate, all 11 conditions | metar_rule |
| --- | ---: | ---: | --- | --- |
| 16 | 1,352 | 22 | 0.05 | decoded-temperature |
| 17 | 0 | 0 | 0.05 | decoded-temperature |
| 18 | 0 | 0 | 0.05 | decoded-temperature |
| 19 | 840 | 4 | 0.05 | decoded-temperature |
| 20 | 1,390 | 14 | 0.05 | decoded-temperature |
| 21 | 1,392 | 13 | 0.05 | decoded-temperature |
| 22 | 1,344 | 26 | 0, explicitly saved | decoded-temperature |
| 23 | 359 | 9 | 0, explicitly saved | awc-routine-rounded-celsius-v1 |
| **Total** | **6,677** | **88** | | |

17/18 have `weather: []`, `weather_missing: true`, and an explicit gap. No historical
AWC request, interpolation, carry-forward from another day, or fabricated poll was used.
All 88 conditions have an explicitly recorded `minimum_quantity: "5"` from saved
Gamma `orderMinSize`, with exact response path/JSON pointer and request provenance.

## Event schema: shanghai_capacity_event_inputs/v1

- `event_slug`, `date`, `station: "ZSPD"`, `timezone: "Asia/Shanghai"`, `catalog_path`.
- `analysis_window`: passed through unchanged from accepted Catalog index.
- `conditions[]`: `condition_id`, `no_token_id`, `upper_bound` (Decimal string or null),
  `fee_rate` (Decimal string or null), `minimum_quantity` (Decimal string or null),
  `metar_rule`, and evidence/scenario fields.
- `metar_rule`: Event's saved business setting; omission in historical config means
  the explicit legacy `decoded-temperature` normalizer default. This is not silently
  rewritten to the October 23 rounded rule for other dates.
- `limits`: saved `max_event_cost="100"`, `max_condition_quantity="5"`,
  `max_order_quantity="5"`, `minimum_edge="0"`, `safety_margin="0"`.
  Also `max_book_age_ns="5000000000"`, `weather_poll_ttl_ns="120000000000"`,
  `max_observation_age_seconds="4500"` from the task's explicit current-rule
  requirements. These three are labeled **not historical manifest fields**, with
  current public business-code source/symbol references. No historical effective-time
  configuration is invented. `business_limits` is an identical descriptive alias.
- `business_limit_sources`, `saved_event_configs`: allowlisted Event business data
  and original manifest/active path/JSON pointer. Account/wallet/private-key/root
  runtime fields are not copied, and config references are never followed.
- `weather[]`: `received_ns` (original integer local receive time), `kind`, `rows`,
  `source: {path, line}`, `sources` for duplicates, `record_metadata` for original
  non-row fields, including recorded error details.
- `weather_counts`, `weather_missing`, `gaps`, `excluded_outside_local_receipt_day`,
  `weather_metar_frozen`.

### Fee / bucket semantics

Saved exact-Event business configs take priority, never another day's template.
16–21 explicitly configure 0.05; 22–23 explicitly configure 0. These inputs do not
retroactively modify the accepted Catalogs' unknown effective-fee status.
`fee_effective_verified` remains false: saved strategy policy is not independent
verification of the venue's time-effective schedule. If only saved HTTP schedule
parameters exist, extraction labels a parameter scenario and retains full provenance;
`base_fee` alone is not guessed into a rate. No evidence means null, never default 0.
Conflicting same-Event saved rates fail rather than choosing one silently.

Bucket upper bounds come from saved Gamma `groupItemTitle` using the existing
`2026-10-20-global-weather-capacity/scan.py:bucket_upper` label convention, cross-checked
against saved Event configs. No condition ordinal is used. Top open-ended bucket has
null upper bound. NO token IDs come from the previously accepted token/outcome mapping.

## Weather freezing and same-receipt errors

Input glob is only
`/data/liuchang/poly-weather-runs/multi-event-native/manager/run-*/weather-*.jsonl`,
including `.part-*` segments. All 48 file identities/sizes are fixed before the first
log is read: **567,980,550 initial bytes**, zero excluded partial-line bytes in this run.
Only newline-terminated lines wholly within each initial size are consumed. Appends
are not followed; shrink/replacement fails. Sources are never modified.

Only the exact Shanghai Event and its **local receive date** are selected. Each poll's
entire original `rows` array is retained, including older upstream observations; the
consumer's normalizer filters observation day. Upstream `receiptTime` never substitutes
for original local `received_ns`. Missing local receive time fails.

Real logs can contain a `metar_poll` followed by a normalization `metar_error` with
**the same Event and received_ns**. Both are necessary, not conflicting copies of
one event. They are retained poll-then-error, so the error can revoke the signal.
Same Event+received+kind with identical content deduplicates while retaining source
locations; different content raises. Transport audit/arrival IDs do not change content.
Example: October 16 received_ns=1792080000854349298 is poll/error at lines 117/118 of
`manager/run-1792078213559723608/weather-1792078214270670143.jsonl`.

Weather is sorted by original received_ns, with error after poll for a tied receipt.
It is **not cropped to raw-book cutoff**. In particular October 23 contains later
weather polls: the consumer must honor the separately supplied `analysis_window` and
raw market-data cutoff, not extend the capacity window merely because weather exists.

## Reproduce / tests

The default output already exists and is never overwritten. Reproduction requires a
new task-local output path; it freezes a new set of log lengths, so counts may differ
if the live logs have grown. It never follows live growth during a run.

```bash
cd /data/liuchang/nautilus_catalog_organization
TASK="$PWD/polymarket/research/2026-10-23-weather-history-capacity"
PYTHONDONTWRITEBYTECODE=1 python/.venv/bin/python "$TASK/prepare_capacity_inputs.py" \
  --output "$TASK/tmp/capacity-inputs-NEW"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$TASK" \
  python/.venv/bin/python -m unittest test_prepare_capacity_inputs -v
```

**11 offline input-copy tests pass**, covering frozen partial/growing files,
replacement/truncation, duplicates/conflicts, tied poll/error preservation, no upstream
receipt substitution, date/Event filtering, retained full rows, bucket parsing,
explicit-zero/unknown/fallback fee policy, config allowlisting, frozen module SHA,
missing weather and refusal to overwrite existing output. Ruff formatting/import checks
pass for the two new Python files. No capacity/profit statistics are run here.

Logs: `tmp/prepare-capacity-inputs-20261023.log`,
`tmp/validation-capacity-inputs-20261023.log`.
