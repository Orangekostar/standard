# Prism V2 Comparison Implementation Plan

> For agentic workers: Use superpowers:executing-plans inline. The primary agent
> owns semantics, implementation, debugging and final review; only explicit
> deterministic mechanical leaves may be delegated.

**Goal:** Deliver every code, actual historical replay, comparison, audit and
GitHub artifact specified in the supplied ZIP without production changes.

**Architecture:** Shared cached V2 data and scores feed one shared account replay
with an A/B/C policy layer. Separate account databases and immutable hash-bound
stage manifests enforce isolation, temporal boundaries and resume semantics.

**Tech Stack:** Existing Python venv, unittest, pandas/numpy, SQLite and pyarrow;
isolated plotting dependencies only. No GPU, Jev or market-data network calls.

**Spec:** `docs/prism_v2_compare/DESIGN.md` and the complete supplied instructions.

## Global Constraints

V2 base0809f0e; Prism d15c63e; experiment branch research/prism-v2-backtest.
Train252/cal63/validation63/test126, warmup120, expiry5, settlement tail6.
Fixed twelve cells, no threshold search; seed20260928, block10, bootstrap1000.
All specification parameters must be consumed from the frozen JSON configuration;
native F0/intent constants are validated against the original implementation.
No production writes, full resync, paid calls, forced pushes or auto-merge.

## Task 0: Freeze Sources And Specification

Files: this design/plan, `SOURCE_AUDIT.md`, original instructions, source license.

- [x] Verify base SHA, isolated branch/worktree and production data path.
- [x] Read the required V2 and fixed Prism modules and license.
- [x] Extract the complete supplied instruction bundle without modifications.
- [x] Finish the source/function/dated-rule and old-holdout audit.
- [x] Commit the design and instruction evidence with an explicit whitelist (237ccba).

## Task 1: Shared Execution Correctness

Files: `core/backtest/execution_v2.py`, `portfolio_v2.py`,
`tests/v2/test_prism_shared_execution.py`.
Interfaces: existing PaperOrder/SecurityRule get optional defaults; no account
schema replacement. Sell lot caps persist in paper_order_details payload_json.

- [x] Add real-account tests before implementation. Hand-checked expiry test:
  requested20260924, attempted20260925 => zero fills, unchanged100000000 cents.
  Same-code SELL+BUY => sell attempted, buy expired; same event retry => one fill.
- [x] Run `/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_prism_shared_execution`;
  confirm missing behavior fails rather than fixture errors.
- [x] Implement legal quantity flooring, dated one-day validity, lot-directed
  sells, limit-safe slipped prices and confirmed receivables in the shared engine.
- [x] Run targeted existing execution/corporate-action/metrics tests; commit (d04bf5f).

## Task 2: Frozen Config And Pure B Policy

Files: `configs/prism_v2_compare_v1.json`, `core/pipeline/prism_compare_config.py`,
`core/strategies/prism_a_share.py`, `tests/v2/test_prism_policy.py`.
Interfaces: `load_config(path)->dict`, `market_regimes(frame,config)->DataFrame`,
`adaptive_distance(q01,sigma,reference,config)->(distance,reasons)`,
`update_stop(old,high,close,distance)->(stop,high,crossed)`,
`entry_allowed(entry_index,last_flat_exit_index,cooldown)->bool`.

- [x] Tests first: monotone prices ER1/TREND; flat ER0; missing reference UNKNOWN;
  q01=.04,vol ratio100 => d=.12; q01=.001 => .03; missing q01 => unavailable;
  oldstop95,high110,close100,d=.12 => stop98, never95-12; closeNaN => no cross.
- [x] Run failing tests, implement original fixed math, then run passing tests.
- [x] Configuration validates forbidden network/search settings and exact split;
  all runtime parameters have a consumer or explicit provenance-only role,
  recorded in `PARAMETER_AUDIT.md`; unsupported modes have rejection tests.

## Task 3: Consistent Snapshot And Shared Data Cache

Files: `core/pipeline/prism_compare_data.py`, `tests/v2/test_prism_data.py`.
Interfaces: `prepare(source_db,experiment_root,config)->manifest`,
`align_panel(raw,adjustments,sessions,roster)->DataFrame`,
`load_daily_cache(experiment_root)->iterator[(date,frame)]`.

- [x] Fixture tests first: source hash/content unchanged after backup; missing
  middle session => NaN return/volume not a multi-day return; future append cannot
  change past factor or regime; test labels cannot change train bins or C.
- [x] Implement source RO backup, streamed SHA and experiment lock/resume contract.
- [x] Audit real flags, calendars, universe/sector validity, actions, old exposure.
- [x] Align sessions, compute original factors/scores in chunks, h5 labels,
  fixed split and boundary purge. Persist common train model and bounded caches.
- [x] Run the real 50x160 smoke before full-market features; record RAM/time.
  First completed real smoke: 50 stocks/160 sessions,37.169s with cProfile,
  peak process RSS1385656320 bytes. Initial native `stock` case mismatch was
  corrected before this completed run; no market/API resync. The subsequent
  equivalent status-aggregation optimization is checked by a non-null merge test.
  Refreshed smoke9.124s; completed full preparation5349 codes/801 sessions/775
  mature signal dates,54 feature chunks,1811.501s, peak process RSS3484811264 bytes.
  Train-only model1245043 rows/246 purged dates, latest label end20250903.

## Task 4: Shared Stateful Replay

Files: `core/backtest/prism_compare_engine.py`, `tests/v2/test_prism_replay.py`.
Interfaces: `replay_cell(experiment_root,config,strategy,split,cost,lambda_c,out)->dict`.

- [x] Fixture tests first: B extras disabled equals A orders/NAV; signal-close to
  next-open execution; new lot T+1; stop fill only subsequent sellable open;
  two lots with different expiry sell only the expired lot; failed exit does not
  arm cooldown; full actual flat does. Unknown prices never erase a held asset.
- [x] Implement finalized allocation/ranking, pending exit priority/merge, original
  intent with actual holding quantities, lot metadata and common account valuation.
- [x] Export all eight per-cell artifacts, actual rejection funnel and reconciliation.
- [x] Cost test: 100shares@10 fee501cents; slipped price10.01 exactly once; stress
  changes quantity/fill paths instead of subtracting a terminal adjustment.
  Quantity-specific stress rejection,expiry/stop/reduction deduplication and
  partial-reduction cooldown tests pass. Record-date corporate-share attribution,
  unlisted entitlement valuation, inherited expiry, economic-trade grouping and
  hash-bound `replay_cell` are fixture-tested. Primary semantic review and the
  actual12-cell historical comparison are complete; zero trades are not a win.

## Task 5: Stage CLI And Research Evidence

Files: `core/pipeline/prism_comparison.py`, `scripts/compare_prism_v2.py`,
`core/backtest/prism_compare_metrics.py`, `tests/v2/test_prism_comparison.py`.
Interfaces: `validate(root,config)`, `freeze(root,config)->path`,
`test(root,frozen_manifest)`, `report(root)`, CLI subcommands and strict `--all`.

- [x] Tests first: test before freeze rejects; manifest mismatch rejects; successful
  hash-bound replay reused; lambda zero denominator1/unidentifiable, lambda >1 capped1;
  paired common-date bootstrap differs from independent-date sampling; no API access.
- [x] Implement first A/B validation base, C freeze, remaining ten cells and two-worker
  bounded replay scheduling. Test halves continue a single account.
- [x] Export exact summary fields, validity-first verdict, CI and all evidence fields,
  year/quarter/regime/sector/exit attribution, zero-trade adjusted-label diagnosis.
- [x] Generate all required reports/figures/manifests and verify their actual hashes.
  Actual12 cells,161 manifest-record file hashes,6258330 decisions and12 ledgers
  checked. Three1632x960 images pixel-checked and personally viewed. Plotting
  dependencies remain isolated. Insufficient-history/null NOT_RUN fixtures pass.

## Task 6: Real Run, Review And GitHub Delivery

Files: all named `artifacts/prism_v2_compare/<run_id>/` outputs, `HANDOFF.md`,
completion audit and local publish receipt; `core/pipeline/prism_compare_delivery.py`,
`scripts/package_prism_v2.py`, `tests/v2/test_prism_delivery.py`.

- [x] Before the final SOURCE_COMMIT, close the unsupported-option and report
  input-binding checks. Insufficient history must freeze an explicitly ineligible
  protocol, never open test, and export null-valued NOT_RUN artifacts without
  changing504/120 or inventing C exposure.
- [x] Add manifest-whitelisted derived-result packaging and restoration. Limit
  every part to the configured maximum40MiB, verify part/archive/restored-file
  hashes, refuse raw/credential/unlisted inputs, and preserve incomplete attempts.

- [x] Commit SOURCE_COMMIT before test; execute prepare/validate/freeze/test/report
  with the explicit interpreter and frozen paths. Keep zero/negative results.
  Source410de2d; freeze11:55:56.462186UTC before test11:55:56.503642UTC;
  full pipeline722s/peak1845977088 bytes/API0. All12 cells completed; actual
  resume reused12/12. Zero-trade INSUFFICIENT_TRADING_EVIDENCE retained.
- [x] Run one related V2 regression after implementation, document actual failures
  and fixes. If a test-exposed bug is found, record TEST_EXPOSED_BUGFIX and invalidate
  all affected symmetric cells, without tuning the policy.
  Current source verification:236 V2 tests passed in113.988s, including the new
  stage/report/package fixtures. Unsupported-option, audit-binding, insufficient
  history, unlisted-share capacity and tail-denominator regressions were checked
  before the formal comparison. Formal runtime/ledger/report review is complete;
  implementation hash remains unchanged and there is no TEST_EXPOSED_BUGFIX.
- [x] Primary review every ZIP section, numbered test family, parameter, invariant,
  named artifact, command, figure and delivery requirement against current evidence.
  COMPLETION_AUDIT records sections0-15, all10 families and direct actual evidence;
  archive restore verified162 files/7parts; publication verified separately below.
- [x] Upload explicit code/result whitelist and derived <=40MiB packages; try Release
  and PR with actual available authorization; record precise failure/local-only scope.
  Actual PR/Release POSTs each401; Git fallback pushed be0f851, including all7parts,
  core reports/CSV/JSON/PNG, logs and model evidence. No required detail omitted.
- [x] Verify remote branch HEAD and local publish receipt file sizes/SHA.
  Fresh ls-remote/fetch both be0f851;83 changed files bound to Git blobs and
  local size/SHA256. GIT_DELIVERY_EVIDENCE retains the initial result receipt;
  the external publish_receipt is refreshed for the final completion-doc commit.
  Final reply presents actual A/B/C table, insufficient verdict/data limits,
  236 passed tests, source/delivery identity and actual branch/report links.
