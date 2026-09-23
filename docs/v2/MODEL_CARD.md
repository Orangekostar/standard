# Technical V2 Model Card

## Intended use

Technical V2 is a research and paper-trading system for daily SH/SZ A-share direction at horizons 1, 3, and 5. It is not connected to a broker. It produces full-universe status rows, technical observations, conditional research intent, and independent paper-account plans.

## Route A: deterministic formula

- Version: `technical-v2-fixed-v1`
- Inputs: F01-F15 and Q01-Q06 from price, volume, amount, calendar, adjustment, universe, and sector metadata.
- Candidates: F0/F1/F2 only; h=5 is the selection target and h=1/3 are diagnostic.
- Output: 0-100 score, `up/flat/down`, observed trend, factor/group explanation, missing reasons, and conditional intent.
- Current status: `UNVALIDATED_DEFAULT`. F0 is the reference because the real V2 dataset is unavailable; no profitability claim is made.

## Route B: TypeSafe Jev

- Fixed model request: `jev-1.13.0`
- Native endpoint: `POST https://api.typesafe.ai/v1/systemone`
- Request: one anonymous entity state and 15 structured `choice` questions (five groups x three horizons).
- Pools: J0 equal linear pool and J1 fixed formula-weighted linear pool. No independence multiplication is used.
- Validation: exact up/flat/down keys, finite [0,1] values, sum tolerance 0.001, returned model, and usage. Partial/invalid answers stay unavailable.
- Calibration candidates: identity or one scalar temperature from `{0.5,0.75,1,1.5,2,3,4,5}`, fitted on calibration data only.
- Controls: 30-second timeout, two extra retries, 240 requests/minute, response cache, same-request locking, verified pricing snapshot, USD 15 development and USD 5 daily caps.
- Current status: `UNAVAILABLE_CREDENTIALS`. The checked-in demo uses explicit Dirichlet synthetic responses labeled `SYNTHETIC_DEMO_ONLY`; they are not Jev calls or evidence.

## Target and evaluation

The label is adjusted open at t+1+h divided by adjusted open at t+1 minus one. The stock threshold is `max(0.005,0.25*sigma20*sqrt(h))`; sector threshold floor is 0.002. Boundaries are mutually exclusive with flat inclusive.

The fixed protocol requires 504 mature signal dates: 252 train, 63 calibration, 63 validation, and 126 final test, each with a 120-session warmup and boundary purge by actual label end date. Formula selection uses validation net Sharpe with eligibility/tie rules. Jev selection uses date-equal log loss, then Brier, then J0. The final test is opened only after `selection.json` is frozen.

Current fixed evaluation status is `NOT_RUN_WITH_REASON`; mature real signal dates are zero and `final_test_opened=false`.

## Limitations

- Structured output is not proof of financial predictive validity.
- Anonymous states reduce direct identity leakage but cannot prove absence of pretraining contamination in retrospective replay.
- Current industry fallback is not historical PIT data.
- Daily open simulation is an approximation and cannot prove order-book fills.
- Unresolved corporate actions, missing rule metadata, and missing prices block performance conclusions.
- Raw Jev probabilities remain `JEV_SHADOW_UNVALIDATED` until at least 60 forward signal dates and 1,000 mature records per entity type meet all frozen comparison gates.

No output authorizes live trading.
