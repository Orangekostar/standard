# Technical V2 Evaluation Report

Generated: 2026-09-23
Code commit: `e800038e05b2dab1059c062cc28e9f1c3bf1a518`
Artifact run: `demo-20260922-480fdd78cb43`

## Result identity

| Layer | Status | Result |
|---|---|---|
| Software | COMPLETE | 166 unit tests passed; CLI, worker profile, immutable publication, UI, ledger, calibration, and release audit are executable |
| Real V2 data | UNAVAILABLE | migrated V2 database and Tushare credential absent |
| Formula evidence | UNVALIDATED_DEFAULT | F0 retained by frozen insufficient-history rule; no real return metrics |
| Jev evidence | UNAVAILABLE_CREDENTIALS | adapter/contract tests pass; no online request or response |
| Fixed evaluation | NOT_RUN_WITH_REASON | 0 of 504 required mature signal dates; final test unopened |
| Live trading | NOT_CONNECTED | no broker adapter and zero real orders |

The legacy database was queried read-only at baseline but remains `LEGACY_UNVERIFIED`; its 601,874 daily rows were not reused for V2 selection. Historical `report.md` metrics are excluded.

## Demo flow

The deterministic seed `20260923` produced four synthetic stocks and 24 contract rows (2 routes x 3 horizons x 4 stocks), all structurally valid. Formula factors and Jev-like probabilities in this command are generated demo inputs. Network calls and paid calls were both zero. There are no future labels, calibrated probabilities, paper fills, or returns, so the demo has no IC, log loss, Brier, Sharpe, drawdown, win rate, or profitability result.

## Candidates and selection

F0, F1, F2, J0, and J1 are all retained in `candidate_comparison.csv`. None was evaluated on real validation data. `selection.json` is immutable and content-addressed; its formula result is `INSUFFICIENT_HISTORY` with F0 as the unvalidated reference, its Jev result is `NO_ELIGIBLE_POOL`, and `final_test_opened=false`.

## Verification evidence

- Full regression: 166 passed, 0 failed, 0 errors in 15.596 seconds.
- V2 checkpoint: 133 passed.
- Post-generation release audit: 1 targeted test passed.
- UI runtime: five tabs rendered without an application exception; headless health returned `ok`.
- Synthetic performance check: 200 stocks x 300 sessions, 60,000 factor/context rows, 9.338 seconds and 245,812 KiB peak RSS. This is performance evidence only.
- Real CLI checks: `doctor`, `fit-evaluate`, `evaluate-matured`, and `paper` each returned exit 2 with exact missing prerequisites; paper created zero orders.
- Known baseline warning: legacy `MarketDB` tests emit unclosed SQLite `ResourceWarning`; no test failed.

## Required result files

All section 9.5 tables exist. When real inputs are unavailable, CSVs contain their schema plus `NOT_RUN_WITH_REASON` rows rather than invented zeros. `artifact_manifest.json` binds published files by size and SHA256.
