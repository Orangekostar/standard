# Technical V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the complete Standard V2 technical-only formula and Jev research system, including whole-market status coverage, paper execution, runtime entry points, evidence, and release verification.

**Architecture:** Build a separate V2 vertical slice beside the legacy MVP. Mode-isolated providers feed a versioned SQLite store; a causal feature/context layer feeds independent formula and Jev routes; a fixed evaluation pipeline and idempotent paper ledger write immutable run artifacts consumed by CLI, worker, and read-only Streamlit UI.

**Tech Stack:** Python 3.13, pandas, NumPy, SQLite, Tushare, Streamlit, unittest, standard-library HTTP/JSON plus injectable transport.

**Spec:** `docs/superpowers/specs/2026-09-23-technical-v2-design.md`

## Global Constraints

- Preserve the legacy app, database, cache, holdings, and public interfaces unless a task explicitly marks a compatibility edit.
- `DATA_MODE=real` is default and can never call synthetic/mock generators.
- Use only price/volume/amount, market/sector technical state, calendar, eligibility, adjustment, trading-status, and corporate-action metadata as V2 inputs.
- Freeze F01-F15, Q01-Q06, F0/F1/F2, J0/J1, horizons 1/3/5, fixed-v1 split, fee assumptions, and budget limits exactly as supplied.
- Use `Asia/Shanghai` for business dates and schedules.
- Formula scores and Jev probabilities remain distinct fields and concepts.
- Only h=5 creates intent, quantity, orders, or account actions; h=1/3 are diagnostic.
- Real external failures remain typed statuses; never generate substitute success data.
- Do not connect to a live broker or modify user holding/account files.
- Run tests with `/home/ww/vv/quant/.venv/bin/python -m unittest`.

---

### Task 1: Freeze baseline, prompt, configuration, and common contracts

**Files:**
- Create: `docs/v2/EXECUTION_PROMPT.md`
- Create: `docs/v2/REPO_AUDIT.md`
- Create: `docs/v2/BASELINE_MANIFEST.json`
- Create: `configs/technical_v2.json`
- Create: `core/technical_v2/__init__.py`
- Create: `core/technical_v2/contracts.py`
- Create: `core/technical_v2/config.py`
- Modify: `config.py`
- Modify: `.env.example`
- Test: `tests/v2/test_config_contracts.py`

**Interfaces:**
- Produces: `TechnicalV2Config.load(path)`, `RunStatus`, `canonical_json(value)`, `sha256_json(value)`, `json_safe(value)`, and mode-specific paths on `settings`.
- Invariant: checked-in config values match `standard_v2_plan_config.json`; non-finite JSON values raise a contract error.

- [ ] **Step 1: Write failing configuration/serialization tests**

```python
def test_config_keeps_frozen_factor_and_budget_values(self):
    cfg = TechnicalV2Config.load(PROJECT_ROOT / "configs/technical_v2.json")
    self.assertEqual(cfg.factors.factor_count, 15)
    self.assertEqual(cfg.jev.model, "jev-1.13.0")
    self.assertEqual(cfg.jev.development_budget_usd, 15)

def test_canonical_json_rejects_nan(self):
    with self.assertRaises(ContractError):
        canonical_json({"probability": float("nan")})
```

- [ ] **Step 2: Run RED check**

Run: `/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_config_contracts -v`

Expected: import failure because `core.technical_v2` does not exist.

- [ ] **Step 3: Implement validated config/contracts and archive exact prompt/config**

`RunStatus` contains `status`, `code`, `message`, and JSON-safe `details`; config loading validates horizon, factor/risk counts, normalized weights, positive limits, and mode paths.

- [ ] **Step 4: Run GREEN checks and baseline audit commands**

Run the Task 1 test plus `git status`, HEAD/remote inspection, SQLite aggregate counts, and secret-name-only environment inspection. Record actual results in the baseline files.

### Task 2: Add V2 store, source modes, calendar, and dated universe

**Files:**
- Create: `core/data/v2_store.py`
- Create: `core/data/v2_provider.py`
- Create: `core/data/v2_universe.py`
- Modify: `core/data/data_manager.py`
- Modify: `core/data/symbols.py`
- Test: `tests/v2/test_data_boundary.py`
- Test: `tests/v2/test_v2_store.py`

**Interfaces:**
- Produces: `V2Store(path).migrate()`, transactional `upsert_*`/`read_*` methods, `ProviderResult[T]`, `RealV2Provider`, `DemoV2Provider`, `classify_instrument(row)`, `build_analysis_universe(...)`, and `resolve_sessions(...)`.
- Consumes: mode paths and statuses from Task 1.
- Invariant: V2 tables never write to legacy `daily_bars` or `stock_basic`; real provider failure has no fabricated rows.

- [ ] **Step 1: Write failing C01-C03 storage/data tests**

```python
def test_real_provider_without_token_does_not_return_mock_rows(self):
    result = RealV2Provider(token="", transport=None).daily("20260922")
    self.assertEqual(result.status, "UNAVAILABLE_CREDENTIALS")
    self.assertTrue(result.frame.empty)

def test_tushare_daily_units_are_normalized_once(self):
    normalized = normalize_daily_frame(pd.DataFrame([{"vol": 12, "amount": 3.5}]))
    self.assertEqual(normalized.loc[0, "volume_shares"], 1200)
    self.assertEqual(normalized.loc[0, "amount_cny"], 3500)

def test_analysis_universe_keeps_star_and_chinext_but_excludes_bj(self):
    self.assertEqual(set(build_analysis_universe(self.instruments)["ts_code"]),
                     {"600000.SH", "688001.SH", "300001.SZ"})
```

- [ ] **Step 2: Run RED check**

Run: `/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_data_boundary tests.v2.test_v2_store -v`

Expected: V2 store/provider imports fail.

- [ ] **Step 3: Implement schema migration and source adapters**

Create explicit migrations for the prompt's `schema_migrations`, instruments,
calendar, raw bars, adjustments, statuses, memberships, corporate actions,
runs, features, predictions, registry, and paper tables. Use unique keys and
`BEGIN IMMEDIATE` transactions; latest pointers update after validation only.

- [ ] **Step 4: Run GREEN checks**

Run Task 2 tests and `tests.test_symbols`; inspect generated test DB schema and duplicate-key behavior.

### Task 3: Implement causal sector context and all frozen technical features

**Files:**
- Create: `core/analysis/__init__.py`
- Create: `core/analysis/sector_v2.py`
- Create: `core/factors/technical_v2.py`
- Modify: `core/factors/registry.py`
- Modify: `core/factors/technical.py`
- Modify: `core/factors/volume_price.py`
- Modify: `core/strategies/factor_selection.py`
- Test: `tests/v2/test_technical_features.py`
- Test: `tests/v2/test_sector_context.py`
- Test: `tests/v2/test_legacy_causality.py`

**Interfaces:**
- Produces: `build_context(panel, membership, as_of)`, `compute_technical_v2(panel, context)`, `TECHNICAL_V2_REGISTRY`, factor metadata, and reason-coded nulls.
- Consumes: normalized adjusted/raw bars and dated universe from Task 2.
- Invariant: output has exactly F01-F15 and Q01-Q06; computations read no future rows.

- [ ] **Step 1: Write failing C04-C05 hand-calculation and causality tests**

```python
def test_future_append_does_not_change_historical_features(self):
    before = compute_technical_v2(self.panel.iloc[:-1], self.context).query("date == '2026-09-21'")
    after = compute_technical_v2(self.panel, self.context).query("date == '2026-09-21'")
    pd.testing.assert_frame_equal(before.reset_index(drop=True), after.reset_index(drop=True))

def test_flat_bar_has_zero_clv_and_no_infinite_factor(self):
    result = compute_technical_v2(self.flat_panel, self.flat_context).iloc[-1]
    self.assertEqual(result["F08"], 0.0)
    self.assertFalse(np.isinf(result.filter(regex="^F|^Q").astype(float)).any())
```

- [ ] **Step 2: Run RED check**

Run the three Task 3 test modules; expected failure is missing V2 factor/context modules.

- [ ] **Step 3: Implement context-first vectorized formulas and legacy fixes**

Compute market/sector series once, join them by entity/date, enforce observed-bar
and coverage thresholds, then compute the frozen formulas. Replace callable
legacy full-series ranks with causal expanding/rolling behavior and deduplicate
intraday bars by timestamp before aggregation.

- [ ] **Step 4: Run GREEN and performance fixture checks**

Run Task 3 tests and record one 200 x 300 synthetic feature timing/memory result without treating returns as evidence.

### Task 4: Implement formula scoring, sector scoring, return bins, and intent

**Files:**
- Create: `core/strategies/formula_v2.py`
- Create: `core/strategies/intent_v2.py`
- Test: `tests/v2/test_formula_v2.py`
- Test: `tests/v2/test_intent_v2.py`

**Interfaces:**
- Produces: `score_stock(features, horizon, config_id)`, `score_sector(context, horizon)`, `fit_return_bins(training_rows)`, `estimate_formula_return(...)`, and `derive_research_intent(rows, holding_state)`.
- Invariant: insufficient features yield no score; formula probability fields remain null; only h=5 yields non-diagnostic intent.

- [ ] **Step 1: Write failing formula/intent tests**

```python
def test_all_positive_factors_score_one_hundred(self):
    row = score_stock({factor: 1.0 for factor in FACTOR_IDS}, 5, "F0_BALANCED")
    self.assertEqual(row.formula_score, 100.0)
    self.assertIsNone(row.p_raw_up)

def test_horizon_three_is_diagnostic_only(self):
    intent = derive_research_intent(self.valid_formula_rows, None, horizon=3)
    self.assertEqual(intent.research_intent, "DIAGNOSTIC_ONLY")
    self.assertIsNone(intent.order_quantity)
```

- [ ] **Step 2: Run RED check**

Run Task 4 test modules; expect missing strategy modules.

- [ ] **Step 3: Implement frozen candidates, bins, trend, and intent rules**

Use group availability rules, score cutoffs, fixed bin edges/support, net-edge downgrade, and separate held/unheld action matrices exactly from the execution prompt.

- [ ] **Step 4: Run GREEN checks**

Run Task 4 tests and inspect all horizon/config weight sums programmatically.

### Task 5: Implement native Jev client, question schema, caching, and pools

**Files:**
- Create: `core/models/__init__.py`
- Create: `core/models/jev_client.py`
- Create: `core/strategies/jev_v2.py`
- Create: `configs/jev_questions_v1.json`
- Test: `tests/v2/test_jev_client.py`
- Test: `tests/v2/test_jev_strategy.py`

**Interfaces:**
- Produces: `JevClient.predict(state, questions, budget)`, `build_jev_state(...)`, `build_questions(entity_type)`, `validate_answers(response)`, and `pool_probabilities(answers, pool_id, horizon)`.
- Consumes: Task 1 config and Task 3 features; transport is injected as a callable for tests.
- Invariant: 15 native choice questions, exact up/flat/down probabilities, no identifier/name/date/future label/private holdings in state.

- [ ] **Step 1: Write failing C07 request/response/cache/budget tests**

```python
def test_missing_key_returns_unavailable_without_transport_call(self):
    transport = CountingTransport()
    result = JevClient(api_key="", transport=transport).predict(self.state, self.questions)
    self.assertEqual(result.status, "UNAVAILABLE_CREDENTIALS")
    self.assertEqual(transport.calls, 0)

def test_probability_sum_outside_tolerance_is_invalid(self):
    parsed = validate_answers(self.response_with_probabilities(0.8, 0.3, 0.1))
    self.assertEqual(parsed.status, "INVALID_RESPONSE")
```

- [ ] **Step 2: Run RED check**

Run both Task 5 modules; expect missing client/strategy imports.

- [ ] **Step 3: Implement request contracts and deterministic cache policy**

Hash requested/returned model, state, instructions, and schema. Apply 30-second
timeout metadata, at most two retry decisions, 401/403/422 terminal behavior,
429/5xx backoff, 240 rpm limiter, conservative budget reservation, and stable
entity ordering. Keep confidence/choice/usage as diagnostics only.

- [ ] **Step 4: Run GREEN checks**

Run Task 5 tests with only fixture transports; do not call paid APIs during unit tests.

### Task 6: Add labels, calibration, fixed splits, cohorts, and selection

**Files:**
- Create: `core/models/calibration_v2.py`
- Create: `core/pipeline/technical_v2.py`
- Test: `tests/v2/test_labels_splits.py`
- Test: `tests/v2/test_calibration_selection.py`

**Interfaces:**
- Produces: `build_labels(prices, calendar, horizons)`, `build_fixed_split(dates)`, `purge_cross_boundary(rows, next_start)`, `select_cohort(...)`, `fit_temperature(...)`, `select_formula_candidate(...)`, and `select_jev_pool(...)`.
- Invariant: final test data cannot enter fit/selection; Jev calibration is split by entity type/horizon and uses date-equal metrics.

- [ ] **Step 1: Write failing C06/C08 tests**

```python
def test_h1_uses_next_open_and_open_after_one_held_session(self):
    label = build_labels(self.prices, self.calendar, horizons=[1]).iloc[0]
    self.assertEqual(label.entry_date, self.sessions[1])
    self.assertEqual(label.exit_date, self.sessions[2])

def test_purge_requires_label_end_before_next_block(self):
    kept = purge_cross_boundary(self.rows, next_start="2026-08-03")
    self.assertTrue((kept["label_end_date"] < "2026-08-03").all())
```

- [ ] **Step 2: Run RED check**

Run both Task 6 modules; expect missing label/calibration functions.

- [ ] **Step 3: Implement the fixed-v1 protocol and deterministic tie-breaks**

Implement 252/63/63/126, 120-session warmup, label-end purge, seed/hash cohort,
temperature grid, support checks, date-equal log loss/Brier, F0/F1/F2 selection,
J0/J1 two-stage selection, and immutable `selection.json` hash.

- [ ] **Step 4: Run GREEN checks**

Run Task 6 tests, including a guard object that raises if final-test rows are accessed during selection.

### Task 7: Implement idempotent paper ledger and portfolio metrics

**Files:**
- Create: `core/backtest/portfolio_v2.py`
- Create: `core/backtest/execution_v2.py`
- Create: `core/backtest/metrics_v2.py`
- Modify: `core/backtest/engine.py`
- Test: `tests/v2/test_execution_v2.py`
- Test: `tests/v2/test_corporate_actions.py`
- Test: `tests/v2/test_metrics_v2.py`

**Interfaces:**
- Produces: `allocate_orders(...)`, `execute_open_orders(...)`, `apply_corporate_actions(...)`, `mark_portfolio(...)`, and `calculate_portfolio_metrics(...)`.
- Consumes: h=5 intents, raw prices, dated rules, fees, and isolated V2 paper tables.
- Invariant: integer cents/Decimal cash, integer shares, T+1 lots, no duplicate fill, unresolved actions/valuation persist.

- [ ] **Step 1: Write failing C09-C10 ledger tests**

```python
def test_same_order_run_twice_creates_one_fill(self):
    execute_open_orders(self.store, [self.buy_order], self.open_market)
    execute_open_orders(self.store, [self.buy_order], self.open_market)
    self.assertEqual(self.store.count_fills(self.buy_order.order_id), 1)

def test_same_day_buy_is_not_sellable(self):
    lot = self.portfolio.buy(self.session, quantity=100)
    self.assertEqual(lot.sellable_quantity(self.session), 0)
```

- [ ] **Step 2: Run RED check**

Run all Task 7 modules; expect missing V2 backtest modules.

- [ ] **Step 3: Implement allocation, fills, fees, actions, and metrics**

Apply market-breadth caps, 10/25/60% caps, risk/ADV sizing, board lot metadata,
next-open ceiling/limit rules, sell-before-buy, slippage only in fill price,
minimum commission only to commission, dividend receivables, share actions, and
unresolved NAV. Mark the old engine and reports legacy without changing results.

- [ ] **Step 4: Run GREEN checks**

Run Task 7 tests and reconcile cash/share arithmetic for each fixture manually in assertions.

### Task 8: Build immutable artifacts, whole-market DAG, and V2 worker profile

**Files:**
- Modify: `core/pipeline/technical_v2.py`
- Create: `core/background/technical_v2_tasks.py`
- Modify: `core/background/precompute_worker.py`
- Modify: `core/background/snapshot_store.py`
- Modify: `core/background/task_rules.py`
- Test: `tests/v2/test_pipeline_publication.py`
- Test: `tests/v2/test_worker_profile.py`

**Interfaces:**
- Produces: `TechnicalV2Pipeline.run(...)`, immutable run/publication manifests, `read_latest_manifest(...)`, and worker `--profile technical_v2`.
- Invariant: dependency keys match run/as-of/data hash; full-universe left join emits six stock rows per code; publication history is append-only.

- [ ] **Step 1: Write failing C11 publication/DAG tests**

```python
def test_full_coverage_includes_failed_entity_rows(self):
    rows = build_prediction_contract(self.universe, self.partial_predictions)
    self.assertEqual(len(rows), len(self.universe) * 2 * 3)
    self.assertEqual(rows.query("entity_id == 'missing'")["prediction_status"].nunique(), 1)

def test_previous_jev_cannot_be_republished_under_new_as_of(self):
    with self.assertRaises(ArtifactMismatch):
        publish(self.new_formula, self.old_jev, as_of="2026-09-22")
```

- [ ] **Step 2: Run RED check**

Run Task 8 tests; expect absent publication/profile interfaces.

- [ ] **Step 3: Implement typed DAG and append-only artifacts**

Implement `data_v2_sync -> features_v2 -> formula_v2/jev_v2 -> paper_v2/evaluate_matured_v2 -> publish_technical_v2`, atomic manifests, publication IDs, progress based on entities, and partial formula publication.

- [ ] **Step 4: Run GREEN checks**

Run Task 8 tests plus existing background/task-rule regression modules.

### Task 9: Add required CLI, demo workflow, and deployment profile

**Files:**
- Create: `scripts/v2.py`
- Create: `deploy/systemd/quant-technical-v2.service`
- Create: `deploy/systemd/install_quant_technical_v2_service.sh`
- Create: `requirements-dev.txt`
- Create: `requirements-lock.txt`
- Modify: `requirements.txt`
- Test: `tests/v2/test_cli_v2.py`

**Interfaces:**
- Produces: required `doctor`, `sync`, `analyze`, `fit-evaluate`, `paper`, `evaluate-matured`, `export`, `demo`, and `audit-release` subcommands.
- Invariant: one JSON object on stdout; exit 0/2/3 contract; demo never calls a paid/network transport.

- [ ] **Step 1: Write failing C12 CLI tests**

```python
def test_demo_is_deterministic_and_network_free(self):
    first = run_cli("demo", "--seed", "20260923")
    second = run_cli("demo", "--seed", "20260923")
    self.assertEqual(first.json["artifact_hash"], second.json["artifact_hash"])
    self.assertEqual(first.json["data_source_mode"], "demo")

def test_real_doctor_missing_jev_key_returns_prerequisite_exit(self):
    result = run_cli("doctor", "--mode", "real", env={"TYPESAFE_API_KEY": ""})
    self.assertEqual(result.returncode, 2)
```

- [ ] **Step 2: Run RED check**

Run Task 9 tests; expect missing CLI module/subcommands.

- [ ] **Step 3: Implement commands, stale gate, lock, and systemd template**

Resolve `latest` against a validated exchange calendar and report
`expected_as_of`; stale results cannot create orders. Lock actual installed
versions and ensure service paths use `%h`/environment configuration rather
than another user's absolute directory.

- [ ] **Step 4: Run GREEN checks and CLI smoke**

Run Task 9 tests, `python -m scripts.v2 demo --seed 20260923`, and `doctor --mode real`; preserve truthful prerequisite statuses.

### Task 10: Add read-only V2 Streamlit application

**Files:**
- Create: `app_v2.py`
- Create: `ui/__init__.py`
- Create: `ui/technical_v2.py`
- Modify: `README.md`
- Test: `tests/v2/test_ui_contract.py`

**Interfaces:**
- Produces: `render_technical_v2(manifest, enqueue_refresh)` and `format_ratio(value)`.
- Consumes: immutable publication artifacts and request writer only.
- Invariant: importing/rerunning UI cannot invoke provider sync, model fit, batch Jev, paper fills, or run publication.

- [ ] **Step 1: Write failing UI contract tests**

```python
def test_ratio_formats_once_and_formula_score_is_not_probability(self):
    self.assertEqual(format_ratio(0.012), "1.20%")
    row = build_display_row({"formula_score": 78, "p_cal_up": 0.61})
    self.assertEqual(row["formula_score"], "78.0 分")
    self.assertEqual(row["p_cal_up"], "61.00%")

def test_ui_module_has_no_pipeline_execution_import(self):
    self.assertNotIn("TechnicalV2Pipeline.run", Path("ui/technical_v2.py").read_text())
```

- [ ] **Step 2: Run RED check**

Run Task 10 tests; expect missing UI modules.

- [ ] **Step 3: Implement status-first, filterable, exportable UI**

Render run/data status, sectors, every stock in a sector, A/B comparison,
conditional actions/paper holdings, evaluation, and complete CSV/JSON download.
Use compact tabs/expanders and preserve missing/risk rows.

- [ ] **Step 4: Run GREEN and startup checks**

Run Task 10 tests, `python -m py_compile app_v2.py ui/technical_v2.py`, and a headless Streamlit startup smoke.

### Task 11: Run required checks and produce truthful evaluation artifacts

**Files:**
- Create: `docs/v2/DATA_CONTRACT.md`
- Create: `docs/v2/FACTOR_SPEC.md`
- Create: `docs/v2/MODEL_CARD.md`
- Create: `docs/v2/EVALUATION_REPORT.md`
- Create: `artifacts/technical_v2/<run_id>/` required schemas/results
- Test: `tests/v2/test_release_audit.py`

**Interfaces:**
- Produces: all section 9.5 and section 12 artifacts plus `artifact_manifest.json` hashes.
- Invariant: unavailable experiments have schema-only CSV plus explicit status/reason; synthetic evidence is labeled demo.

- [ ] **Step 1: Write failing release-artifact audit**

```python
def test_required_artifacts_exist_and_match_manifest_hashes(self):
    report = audit_release(self.run_dir)
    self.assertEqual(report.missing, [])
    self.assertEqual(report.hash_mismatches, [])
```

- [ ] **Step 2: Run RED check**

Run release audit before generation; expected failure lists every missing required artifact.

- [ ] **Step 3: Run bounded verification once and generate evidence**

Run the demo, actual local-data audit, formula analysis, Jev smoke only when a
credential and confirmed pricing exist, fixed evaluation only when data meets
the protocol, C01-C12, original regression, and one performance fixture. Write
the exact pass/fail/skip counts and reasons.

- [ ] **Step 4: Run GREEN release audit**

Run `python -m scripts.v2 audit-release --run-id <generated-run-id>` and the Task 11 test.

### Task 12: Final prompt audit, handoff, commits, and remote release attempt

**Files:**
- Create: `docs/v2/HANDOFF.md`
- Create: `docs/v2/PR_BODY.md`
- Create: `docs/v2/RELEASE_NOTES.md`
- Create: shareable release bundle and `SHA256SUMS.txt`
- Create externally or as release asset: `publish_receipt.json`

**Interfaces:**
- Produces: T00-T11 and requirement-by-requirement PASS/FAIL/NOT_RUN evidence, commit A/B, prerelease tag, remote/PR/release status.
- Invariant: no secret/private/raw licensed data is staged; no force push or automatic merge.

- [ ] **Step 1: Audit every prompt requirement against authoritative evidence**

Use the execution prompt sections, T00-T11, C01-C12, required commands/files,
and final status object as a checklist. A passing test counts only when it
covers the named requirement.

- [ ] **Step 2: Commit code/config/tests as commit A and regenerate affected evidence**

Stage explicit paths, run `git diff --cached --check`, scan staged diff for
credential values, commit, and record A in run manifests. If code changes after
evaluation, create A2 and rerun affected evidence.

- [ ] **Step 3: Write complete handoff and commit evidence as commit B**

Include baseline/code commits, branch/tag, data/schema facts, algorithm
versions, all candidates, actual experiment identities/results, commands,
test counts, service/account impact, and recovery workflow. Commit B without
trying to embed B's own hash in itself.

- [ ] **Step 4: Tag, push, verify, and package fallback**

Create `technical-v2-YYYYMMDD-<B7>`, push branch/tag, verify `git ls-remote`,
then create PR/prerelease when tooling and authentication permit. On one failed
targeted recovery, record `PUBLISH_BLOCKED`, retain the bundle/hashes, and do
not claim remote success.

- [ ] **Step 5: Run final completion audit**

Run the complete original+V2 unittest suite once, compile/import checks, CLI
demo/doctor/audit-release, staged/working-tree inspection, artifact hash audit,
and remote SHA checks. Fill the final status object with one actual value per
field and stop only when all software work and locally possible verification
are complete.
