# Technical V2 Repository Audit

Audit time: 2026-09-23T02:40:23Z  
Frozen source: `32279ec22d447675cbfcc6bdbf9b68f10e37bbf4`  
Development branch: `codex/technical-v2`

## Baseline result

The repository is a working legacy MVP, not a Technical V2 implementation.
Its 33 existing unit tests pass. The baseline database was opened read-only;
it contains 601,874 daily rows for 5,581 distinct codes from 2026-04-14 through
2026-09-22 and 4,122 current `stock_basic` rows. These figures are direct SQL
aggregates, not copied from `report.md`.

The legacy database is `LEGACY_UNVERIFIED`. Its rows do not bind an immutable
source version, retrieval time, completeness result, or unit declaration. The
`daily_bars` code count also differs materially from `stock_basic`. It can be
displayed as historical legacy data but cannot enter formal V2 selection until
provenance is recovered or the affected partitions are resynchronized.

## Evidence-to-task map

| ID | Current evidence | Defect relevant to V2 | Owner |
|---|---|---|---|
| R01 | `DataManager.get_daily_data` | Empty real fetch calls `_mock_daily_data` and writes the same cache family. | T01 real/demo provider separation. |
| R02 | `get_stock_basic`, DB panel builders | Three-row fallback and caller universe limits can masquerade as coverage. | T01 dated full universe/status rows. |
| R03 | `MarketDB` | Missing numeric columns become zero; current industry is joined to history; tables append/replace without schema versions. | T01 explicit V2 migrations and dated metadata. |
| R04 | `_recent_trade_days`, `_mock_daily_data` | `pandas.bdate_range` is not an exchange calendar. | T01 stored exchange calendar/session resolver. |
| R05 | `build_default_factors` | PE/PB/dividend/market-cap factors are in the default registry. | T02 separate technical-only registry. |
| R06 | `Alpha9ReversalFactor.compute` | Full-series percentile rank changes when future rows are appended. | T02 causal legacy correction; excluded from V2. |
| R07 | `Alpha6TurnoverCovFactor.compute` | Full-series turnover percentile has the same future dependency. | T02 causal legacy correction; excluded from V2. |
| R08 | `BreakoutFactor`, `RSIFactor`, `VolatilityFactor` | Range position is named breakout; flat denominators and factor signs do not match V2 definitions. | T02 independent frozen formulas. |
| R09 | `FactorSelectionStrategy` | Past-only z-score is useful, but missing values become zero and output is a binary position. | T02/T03 reason-coded nulls and three classes. |
| R10 | intraday methods in `factor_selection.py` | Daily input is not cut at minute time and repeated minute bars accumulate volume twice. | T02 compatibility fix; V2 remains daily. |
| R11 | `BacktestEngine.run` | Shifted close returns have no shares, cash, T+1, limit, or next-open ledger. | T06 new paper engine; old engine marked legacy. |
| R12 | `three_bull_pullback.py` | Date aggregation is reusable; reported next-close/open diagnostics are not V2 executable returns. | T02 context aggregation; T06 separate ledger. |
| R13 | `_local_sector_fund_flow_proxy` | Amount change is labeled as main-force net flow. | V2 input whitelist excludes this score. |
| R14 | recommendation methods | Fixed Top-N and empty placeholders do not prove full-market output. | T07 left join universe to six stock status rows. |
| R15 | `filter_buyable_mainboard` | Main-board eligibility excludes ChiNext/STAR and cannot define analysis scope. | T01 separates analysis universe from eligibility. |
| R16 | worker/snapshot store | Task success can mean only no exception; dependencies are snapshot-name based and overwrite prior results. | T07 typed DAG plus immutable run/publication IDs. |
| R17 | `app.py`, legacy runner | Large single-file UI and single-stock runner mix unrelated legacy features. | T07/T08 separate runner and read-only V2 app. |
| R18 | existing unittest suite | Several tests depend on legacy fallback behavior; V2 fixtures must be explicit and offline. | T10 C01-C12 plus old regressions. |
| R19 | `report.md` | Historical local-cache results use a different return contract and are not reproducible V2 evidence. | T10/T11 preserve as legacy only. |
| R20 | config/dependencies | No V2 mode paths, Jev contract, or reproducible lock exists. | T00/T09 V2 config and dependency evidence. |

## Legacy database schema observations

`daily_bars` stores raw-looking OHLC, `vol`, `amount`, turnover/fundamental
fields, `_source`, `pct_chg`, `pre_close`, and `change`; none has a database
constraint or attached unit/source-version contract. The writer fills absent
`turnover_rate`, valuation fields, volume, and amount with `0.0`, then deletes
and appends matching code/date keys. `stock_basic` is replaced wholesale and
contains only the current industry snapshot.

Latest observed legacy date counts were 5,554 codes on 2026-09-22 and 5,553
on 2026-09-21. Current industry has 110 distinct non-empty labels, but no
historical validity interval. These facts are insufficient to certify full
universe coverage or point-in-time industry history.

## Environment and external prerequisites

- Isolated worktree Python: 3.13.13.
- pandas 3.0.3, NumPy 2.5.1, Streamlit 1.58.0, Tushare 1.4.29, AkShare 1.18.64.
- The isolated worktree has no configured Tushare or TypeSafe credential.
- Git remote is `git@github.com:Orangekostar/standard.git`.
- GitHub CLI is not installed.

Therefore Task 1 software/config work is testable, while real V2 sync and Jev
online smoke remain external-prerequisite checks. Missing credentials must
produce `UNAVAILABLE_CREDENTIALS`, not demo results.

## Non-V2 state preserved

The original `app.py`, legacy database/cache, `watchlist.json`, `holdings.json`,
`holding_users.json`, old systemd service, and historical reports are not
modified by this audit. The source zip remains untracked in the original main
worktree and is not part of a release by default.
