# Technical V2 Design

## Scope

Implement the supplied Standard V2 execution prompt incrementally inside the
existing Python, pandas, SQLite, Streamlit, and background-worker project. The
new product provides technical-only whole-market analysis, deterministic
formula forecasts, TypeSafe Jev probability forecasts, fixed-protocol
evaluation, and isolated paper accounts. It does not connect to a broker,
modify user holdings, or treat synthetic/demo data as real evidence.

The frozen source of truth is
`docs/v2/EXECUTION_PROMPT.md`; `configs/technical_v2.json` is its machine-readable
parameter copy. Any unavailable external prerequisite becomes a typed status,
not a generated substitute.

## Architecture

### Configuration and contracts

`core/technical_v2/contracts.py` owns enums, prediction columns, typed status
objects, UTC/Asia-Shanghai timestamps, canonical JSON serialization, and hash
helpers. `core/technical_v2/config.py` validates the checked-in JSON config and
exposes immutable settings. Internal ratios stay decimal; UI/export formatting
is the only conversion to percentages.

### Data boundary

`core/data/v2_store.py` owns versioned SQLite migrations and transactional V2
reads/writes. It never mutates legacy tables. `core/data/v2_provider.py` owns
real/demo/test source selection, Tushare unit conversion, pagination/truncation
checks, and typed missing statuses. `core/data/v2_universe.py` builds the dated
SH/SZ A-share analysis universe separately from paper-account eligibility.

Real mode never calls a mock generator. Demo/test data live under their own
roots and databases. Every external row carries source, retrieval time, source
version, and completeness. Daily values use raw RMB/share, shares, and RMB;
adjusted prices are derived for technical calculations only.

### Features and context

`core/analysis/sector_v2.py` computes dated market/sector indices, breadth,
amount, membership coverage, and context statuses once per date. It enforces
90% context coverage and five-member sector minimums.

`core/factors/technical_v2.py` computes exactly F01-F15 and Q01-Q06 from a
causally sorted panel. Results are long-form rows with factor values and
reason codes; invalid denominators remain null. Appending future rows or
reordering securities cannot change historical results.

### Forecast routes

`core/strategies/formula_v2.py` groups valid factors, scores F0/F1/F2 at
horizons 1/3/5, scores sectors, derives observed trend, and fits the fixed
score-bin return estimator using training data only. Formula scores never
populate probability fields.

`core/models/jev_client.py` builds and validates the native TypeSafe request,
injects an HTTP transport, applies cache/budget/rate-limit policy, and returns
typed response statuses. `core/strategies/jev_v2.py` anonymizes whitelist-only
stock/sector state, generates 15 choice questions, validates class
probabilities, and builds J0/J1 linear pools. No credential yields
`UNAVAILABLE_CREDENTIALS`; invalid or partial answers never fall back to
formula values.

`core/models/calibration_v2.py` handles temperature calibration and probability
metrics. `core/pipeline/technical_v2.py` constructs labels, purged time splits,
fixed cohorts, candidate comparisons, selection artifacts, whole-universe
status rows, and the end-to-end DAG.

### Intent and paper execution

`core/backtest/portfolio_v2.py` owns account, lot, order, fill, fee, cash, and
corporate-action records. `core/backtest/execution_v2.py` derives research
intent and paper actions, allocates quantity under risk/capacity/sector/gross
caps, and performs idempotent next-open fills with T+1 and limit metadata.
`core/backtest/metrics_v2.py` calculates daily-ledger metrics while preserving
unresolved valuation states.

Formula and Jev use separate paper accounts. User holding files are read-only
and never interpreted as the research account's CNY 1,000,000 capital.

### Runtime and presentation

V2 runs are immutable directories under `artifacts/technical_v2/<run_id>`.
Each manifest binds `run_id`, as-of, information cutoff, config hash, code hash,
and data hash. A latest pointer advances only after validation. Formula output
may publish while Jev remains pending/unavailable; old Jev output is never
relabeled with a new as-of date.

`scripts/v2.py` exposes the required commands and JSON/exit-code contract.
`precompute_worker --profile technical_v2` schedules only the V2 DAG at 09:00,
16:10, and 20:10 Asia/Shanghai. `app_v2.py` and `ui/technical_v2.py` only read
artifacts or enqueue refresh requests; they do not fetch, train, or call Jev.

## Data Flow

1. Resolve the expected and latest complete exchange sessions.
2. Sync versioned metadata/raw data into the mode-specific V2 store.
3. Freeze a dataset manifest and derive adjusted technical prices.
4. Compute market/sector context, then stock and sector features.
5. Independently publish formula rows and Jev rows for all entities/horizons.
6. Build full-coverage left-joined output, retaining failed/missing rows.
7. Generate h=5 intents and idempotent paper orders; h=1/3 stay diagnostic.
8. Evaluate mature labels and frozen candidates without reopening test data.
9. Write immutable artifacts, verify hashes, then update the latest pointer.

## Failure Semantics

Commands return exit 0 for a completed command, 2 for missing external
prerequisites or budget, and 3 for computation/contract failures. A command may
be `PARTIAL` only when it lists failed entities and retains full status-row
coverage. Stale data can be displayed historically but cannot create new paper
orders.

Credentials, raw licensed market data, full Jev states, account files, and
`.env` are never published. Demo outputs are permanently labeled synthetic and
cannot populate real-mode tables, model selection, or evaluation claims.

## Verification

The implementation supplies C01-C12 tests, keeps the original regression suite
green, performs one 200-stock x 300-session synthetic feature benchmark, and
runs the available real-data/Jev checks exactly once. External omissions are
recorded in `EVALUATION_REPORT.md` and the artifact manifest. Final review maps
every prompt requirement and T00-T11 deliverable to current files, commands,
tests, artifacts, and remote evidence.

## Release

Development occurs on `codex/technical-v2`. Code/config/tests are commit A;
evidence and handoff are commit B. The prerelease tag points to B. Push, PR,
release, and remote SHA checks are attempted only with available authorization;
failure is recorded as `PUBLISH_BLOCKED` with a shareable bundle and hashes.
