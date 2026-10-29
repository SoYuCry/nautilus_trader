# Confirmed Shanghai real-input stage

Supersedes the blocked-transfer checkpoint in `RESULTS.md` (which remains a truthful
historical record). On 2026-10-23 the owner relayed the explicit user completion criterion:
**“也就是16-23号，如果有的话就是传完了”**. The eight Shanghai Events are present.
The owner compared the tree at 16:36 with the 16:20 observation: 4,502 files,
3,266,034,841 bytes, added=0, removed=0, changed=0. No additional marker is required.

The worker re-froze actual identities at the start of this stage and matched all 4,502
identities to the Shanghai-only observation. Scope is exclusively
`/share/sharefold/xiaocong/to_liuchang/shanghai-temperature`, Event dates 2026-10-16..23.
No source hashes, mutations, network requests, BTC inspection, seller Catalog or permanent
inventory access is part of this stage.

## Recorded metadata intake

`prepare_frozen.py` binds original condition manifest `outcomes`, `token_ids`, `end_date`
and condition/Event identities, then cross-checks the saved successful Gamma Event and
CLOB market-info mappings. Every declared condition and both YES/NO tokens are retained;
conflicts fail rather than silently filtering a condition. All consumed source files,
including saved HTTP bodies and their request log, retain frozen stat identities.
Provenance carries original paths, JSON pointers/keys, request-log line, request ID,
HTTP status, requested and received time.

Saved fee responses and Gamma/CLOB fee parameters are retained as raw evidence, **not**
translated into verified effective fees; `fee_status=unknown` remains explicit. Initial
native effective tick still comes only from recorded raw book evidence; HTTP tick fields
are never used as an invented initial tick. Scheduled expiry is not a settlement or crop.

Actual HTTP closure requires **all**: `closed=true`, `umaResolutionStatus=resolved`, an
explicit `closedTime`, and a terminal `[0,1]` or `[1,0]` payout paired with the validated
token mapping. Closed-only, price-only, or missing-time observations do not emit closes.
Every subsequent actual winner must agree. The first recorded resolved response supplies
availability; native close `ts_event` is the recorded actual close time, while `ts_init`
is the HTTP receipt (plus any audited serialization). It is never backdated to the close.
Raw capture order is unchanged when merging these recorded HTTP observations by availability.
HTTP resolutions after the final raw message can therefore extend the native close stream;
raw market-data bounds and analysis windows remain separately identified.

## Prepared batch and acceptance sequence

- Eight contracts: 11 conditions / 22 tokens each.
- October 16–19: 11 explicit recorded resolved conditions each (22 native closes per Event).
- October 20–23: no actual resolution evidence; no invented close or winning label.
- October 23 analysis ends at the exact frozen raw receive cutoff, not next midnight or
  future data. All raw history remains in output.
- First compile/read back October 16; only after acceptance compile the other seven.
- Preserve missing midnight/initialization/fee/settlement as explicit partial metadata.
- If frozen sources change, fail that Event under the existing checks; do not ask for a marker.

Task-local artifacts:
`tmp/frozen-shanghai-20261023.json`, `tmp/contracts-shanghai-20261023/`,
`tmp/prepare-real-20261023.log`, `tmp/validation-real-intake-20261023.log`.
Real compile/readback results will be reported separately, not inferred from synthetic tests.
Final primary artifact exposure remains the owner's responsibility.
