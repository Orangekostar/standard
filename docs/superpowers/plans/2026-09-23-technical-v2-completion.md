# Technical V2 Completion Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every remaining Technical V2 placeholder with a durable, testable runtime that synchronizes verified data, produces independent stock and sector predictions, runs the frozen evaluation protocol, manages two paper accounts, evaluates matured forecasts, and executes the same DAG from the background worker.

**Architecture:** Keep the existing V2 vertical slice and frozen configuration. Add durable store APIs and shared application services underneath the CLI and worker, so both entry points invoke identical logic. Publish only validated immutable artifacts; the UI remains read-only and consumes those artifacts.

**Tech Stack:** Python 3.13, pandas, NumPy, SQLite, Streamlit, unittest.

**Specification:** `docs/v2/EXECUTION_PROMPT.md` and byte-identical `configs/technical_v2.json`.

## Global Constraints

- Preserve legacy data, holdings, APIs, and UI behavior outside Technical V2.
- Keep real/demo stores and artifacts physically separate; real mode never fabricates rows.
- Formula and Jev remain independent methods. A missing Jev credential yields a typed partial status, not formula failure or synthetic Jev output.
- Stock and sector predictions are distinct entities. Only stock h=5 predictions may create paper orders.
- Every fit uses only data available before its evaluation interval. Final test is evaluated once after selection is frozen.
- Every paper mutation is idempotent and auditable in integer cents/shares.
- CLI, worker, and UI consume the same persisted run identity: `run_id`, `as_of_trade_date`, and `data_hash`.
- Follow test-driven development: add a failing test, observe RED, implement the minimum complete behavior, then observe GREEN.

---

### Task 1: Complete the V2 durable store contract

**Files:**
- Modify: `core/data/v2_store.py`
- Modify: `tests/v2/test_v2_store.py`

**Required changes:**
- Add schema migration 2 without rewriting migration 1.
- Add durable `label_rows`, `evaluation_rows`, and `paper_valuations` tables with unique keys that make repeated runs idempotent.
- Add transactional APIs for analysis runs, feature rows, immutable first-recorded prediction rows, model registry, labels, evaluations, valuations, and paper-account state.
- Reject a second prediction write when the same key has different payload content; allow exact replay.

- [x] Write tests for migration upgrade, exact replay, conflicting prediction rejection, and label/evaluation/valuation upserts.
- [x] Run RED: `/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_v2_store -v`
- [x] Implement migration and APIs.
- [x] Run GREEN with the same command and inspect `PRAGMA integrity_check` plus duplicate-key behavior.

### Task 2: Make synchronization incremental and industry history explicit

**Files:**
- Modify: `core/data/v2_provider.py`
- Modify: `core/data/v2_store.py`
- Modify: `core/data/v2_universe.py`
- Modify: `scripts/v2.py`
- Modify: `tests/v2/test_data_boundary.py`
- Modify: `tests/v2/test_cli_v2.py`

**Required changes:**
- Add the official SW2021 L1 classification endpoint and dated membership ingestion; retain current-snapshot fallback with an explicit `history_mode` and typed reason.
- Fetch daily, adjustment, limit, suspension, and corporate-action data only for missing or explicitly refreshed sessions.
- Persist a per-session synchronization audit containing expected eligible instruments, observed rows, known non-trading rows, coverage, and completion status.
- Resolve `latest` only to the newest session whose audit is complete; a partial date cannot become the default `as_of`.

- [x] Write tests for endpoint arguments, missing-session selection, partial-session rejection, and snapshot fallback metadata.
- [x] Run RED: `/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_data_boundary tests.v2.test_cli_v2 -v`
- [x] Implement provider/store/sync behavior.
- [x] Run GREEN and a repeated demo sync proving zero unnecessary session fetches.

### Task 3: Produce and persist complete stock and sector analysis

**Files:**
- Modify: `core/pipeline/technical_v2.py`
- Modify: `scripts/v2.py`
- Modify: `tests/v2/test_technical_pipeline.py`
- Modify: `tests/v2/test_cli_v2.py`

**Required changes:**
- Build independent sector feature rows and formula/Jev predictions for every eligible sector, alongside stock rows.
- Emit the complete normalized prediction contract required by section 7.1, including identifiers, cutoff, data/evidence status, factor/risk payloads, method-specific fields, intent, and execution-reference fields.
- Guarantee exactly two method rows per eligible entity/horizon when a method is available; otherwise emit an explicit unavailable row instead of omitting coverage.
- Persist `analysis_runs`, long-form factor rows, immutable prediction rows, and model-registry references before publication.
- Validate whole-universe stock coverage and sector coverage before updating `LATEST`.

- [x] Write tests for stock/sector row counts, required fields, typed unavailable coverage, and database persistence.
- [x] Run RED: `/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_pipeline_publication tests.v2.test_cli_v2 -v`
- [x] Implement the shared analysis service and CLI integration.
- [x] Run GREEN and compare published rows with persisted rows by primary key and hash.

### Task 4: Implement the frozen fit/evaluate protocol

**Files:**
- Create: `core/pipeline/evaluation_v2.py`
- Modify: `scripts/v2.py`
- Create: `tests/v2/test_evaluation_runtime.py`
- Modify: `tests/v2/test_cli_v2.py`

**Required changes:**
- Materialize forward labels for h=1/3/5 using executable future-session prices and actual `label_end` dates.
- Freeze `dataset_manifest.json` before candidate evaluation.
- Enforce 504 mature evaluation dates split 252/63/63/126, 120-session warmup, and label-end purging.
- Evaluate legacy diagnostic, M0, F0/F1/F2, J0/J1, and prior candidates; Jev cohorts remain capped at 32 stocks and 32 sectors and may use only immutable cached predictions.
- Fit calibration on the calibration split, select once using the frozen rules, register selected models, then evaluate final test exactly once.
- Generate every result artifact required by section 9 from computed rows; shortages and unavailable external methods produce precise status artifacts, never invented metrics.

- [x] Add deterministic fixture tests for split boundaries, purging, selection, single final-test evaluation, and required result files.
- [x] Run RED: `/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_evaluation_runtime tests.v2.test_cli_v2 -v`
- [x] Implement the evaluation service and replace `READY_FOR_FIXED_PROTOCOL`.
- [x] Run GREEN, then independently recompute sample metrics from emitted prediction/label pairs.

### Task 5: Implement paper ordering, fills, valuation, and matured evaluation

**Files:**
- Create: `core/pipeline/paper_runtime_v2.py`
- Modify: `scripts/v2.py`
- Create: `tests/v2/test_paper_runtime.py`
- Modify: `tests/v2/test_cli_v2.py`

**Required changes:**
- Create separate formula and Jev accounts with CNY 1,000,000 initial cash.
- Read only immutable selected h=5 stock predictions from the bound run, apply eligibility and portfolio constraints, and persist reason-coded orders.
- Fill eligible orders only at the next session open with configured fees/slippage; preserve pending/rejected/cancelled states explicitly.
- Apply corporate actions idempotently and write daily account/position valuations with raw-price lineage and stale/suspension reasons.
- Join matured immutable predictions to newly available labels, insert each evaluation once, compute forward metrics, and publish sample-size threshold status.

- [x] Add tests for account separation, next-open timing, limit/suspension rejection, cash/lot invariants, corporate-action replay, and one-time matured scoring.
- [x] Run RED: `/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_paper_runtime tests.v2.test_cli_v2 -v`
- [x] Implement the shared paper/evaluation service and replace both command placeholders.
- [x] Run GREEN and reconcile ledger cash plus holdings to valuation totals.

### Task 6: Wire the background DAG to real services

**Files:**
- Modify: `core/background/technical_v2_tasks.py`
- Modify: `core/background/precompute_worker.py`
- Modify: `tests/v2/test_technical_v2_tasks.py`
- Modify: `tests/test_precompute_worker.py`

**Required changes:**
- Provide production handlers for all seven V2 DAG nodes.
- Let the root sync node create the run binding; require every dependent node to validate the same binding and dependency artifact hashes.
- Report real entity counts and typed partial/unavailable statuses.
- Publish only when mandatory dependencies are valid; an unavailable optional Jev branch must remain visible without blocking formula publication.

- [x] Write a full `--once` DAG integration test with demo data plus tests for binding/hash mismatch and optional Jev unavailability.
- [x] Run RED: `/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_worker_profile -v`
- [x] Implement handler registration and shared-service calls.
- [x] Run GREEN and inspect the completed task graph/artifact chain.

### Task 7: Render genuine stock/industry state in the read-only UI

**Files:**
- Modify: `ui/technical_v2.py`
- Modify: `tests/v2/test_ui_technical_v2.py`

**Required changes:**
- Read actual sector prediction entities instead of deriving industry state by grouping stock h=5 rows.
- Display formula scores and Jev probabilities as distinct quantities, with explicit unavailable states and run/data lineage.
- Keep the UI read-only; no data refresh, model call, order, or ledger mutation may occur in a page render.

- [x] Add tests for sector entity rendering, unavailable-state display, and zero side effects.
- [x] Run RED: `/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_ui_contract -v`
- [x] Implement UI consumption changes.
- [x] Run GREEN and validate desktop/mobile screenshots plus app health.

### Task 8: Rebuild evidence and release from actual runtime behavior

**Files:**
- Modify: `docs/v2/PROMPT_COMPLIANCE_AUDIT.md`
- Modify: `docs/v2/IMPLEMENTATION_EVIDENCE.md`
- Modify: `docs/v2/FINAL_VERIFICATION.md`
- Modify: `scripts/audit_v2_prompt.py`
- Create: new immutable evidence directory under `evidence/technical_v2/`
- Create: new distribution ZIP and receipt under `dist/`

**Required changes:**
- Extend the prompt audit to reject placeholder success paths and verify command-generated artifacts, persisted rows, sector coverage, paper ledgers, and worker handlers.
- Run the full suite, lint, compile, prompt audit, CLI smoke flow, worker smoke flow, database integrity checks, artifact hash checks, UI health, and screenshot review.
- Commit code first, generate immutable evidence from that commit, commit evidence second, and create a new annotated tag. Do not move or overwrite the previous tag.
- Push the new branch commit and tag only after local and remote hashes agree.

- [x] Run `/home/ww/vv/quant/.venv/bin/python -m unittest discover -s tests -v`.
- [x] Run scoped Ruff checks and `/home/ww/vv/quant/.venv/bin/python -m compileall -q core scripts ui tests` (the project venv does not package Ruff).
- [x] Run the exact prompt audit and all CLI/worker/UI smoke checks from a clean temporary demo root.
- [ ] Verify `git diff --check`, repository status, evidence hashes, distribution hashes, and remote branch/tag hashes.
