# Technical V2 Data Contract

Contract version: `technical-v2-store.v1`
Code commit: `e800038e05b2dab1059c062cc28e9f1c3bf1a518`

## Modes and provenance

`real`, `demo`, and `test` are separate storage modes. `real` is the default and never falls back to generated prices. Demo rows carry `data_source_mode=demo`, use a separate database root, and cannot be cited as market evidence. The legacy database is read-only and classified `LEGACY_UNVERIFIED`; it lacks immutable source version, retrieval time, completeness, and unit provenance and is excluded from formal V2 selection.

Each run binds `generated_at`, `as_of_trade_date`, `information_cutoff`, `data_source_mode`, and content hashes independently. `latest` means the latest complete stored daily partition, while `expected_as_of` comes from the SSE exchange calendar and the 16:10 Asia/Shanghai cutoff. A lagging partition is `STALE` and cannot create paper orders.

## Universe and temporal joins

- The dated analysis universe includes SH/SZ A shares on the main, ChiNext, and STAR boards. B shares, funds, indices, bonds, and BSE securities are excluded.
- Risk warnings, suspensions, insufficient history, and account restrictions remain as status rows. `trade_eligible` is separate from analysis membership.
- SW L1 membership uses `valid_from`/`valid_to`; reconstructed history is `RECONSTRUCTED_PIT`. The `stock_basic.industry` fallback is `CURRENT_SNAPSHOT_ONLY` and is never backfilled before `observed_at`.
- A stock may belong to multiple concept namespaces, but concept membership never replaces the primary industry and never duplicates a portfolio position.
- Market/sector context needs at least 90% member coverage. A sector needs at least five members; otherwise its context is null with a reason.

## Storage

`core/data/v2_store.py` owns migration version 1 and uses explicit columns, unique keys, `BEGIN IMMEDIATE`, and idempotent upserts. The tables are:

| Family | Tables | Binding |
|---|---|---|
| Market metadata | `instrument_versions`, `calendar`, `adjustments`, `trading_status`, `sector_membership`, `corporate_actions` | dated/versioned source rows |
| Market observations | `daily_raw` | code/date/source version plus completeness and mode |
| Research | `analysis_runs`, `feature_rows`, `prediction_rows`, `model_registry` | run/entity/horizon/method |
| Paper ledger | `paper_accounts`, `paper_cash_ledger`, `paper_lots`, `paper_orders`, `paper_fills`, `paper_corporate_action_ledger` | integer cents and shares |

Unknown numeric fields are null, never zero-filled. JSON rejects NaN and Infinity. First-recorded prediction artifacts are immutable; a later Jev completion creates a new publication ID over the same run/data binding.

## Units and price basis

| Field | Canonical unit | Source conversion |
|---|---|---|
| OHLC/pre-close | RMB/share | unchanged |
| `volume_raw` | Tushare lots | retained |
| `volume_shares` | shares | Tushare `vol * 100` |
| `amount_raw` | Tushare thousand RMB | retained |
| `amount_cny` | RMB | Tushare `amount * 1000` |
| returns/probabilities | decimal | UI multiplies by 100 exactly once |

Technical prices use `raw_price_s * adj_factor_s / adj_factor_asof` for O/H/L/C. Raw prices remain the execution basis. A confirmed suspension may create a separately marked valuation row; an unexplained missing price is never forward-filled into a tradable bar.

## Provider endpoints

The adapter calls only documented SDK endpoints and keeps endpoint failures local to their capability:

| Endpoint | Parameters used | Stored result | Official contract |
|---|---|---|---|
| `daily` | `trade_date` | daily OHLC/volume/amount | https://tushare.pro/document/2?doc_id=27 |
| `trade_cal` | `exchange`, `start_date`, `end_date` | SSE/SZSE calendar | https://tushare.pro/document/2?doc_id=26 |
| `adj_factor` | `trade_date` | adjustment factors | https://tushare.pro/document/2?doc_id=28 |
| `stk_limit` | `trade_date` | security-level upper/lower limits | https://tushare.pro/document/2?doc_id=183 |
| `index_member_all` | `l1_code`, `is_new=Y/N` | current and removed SW members | https://tushare.pro/document/2?doc_id=335 |
| `suspend_d` | `trade_date`, `suspend_type=S/R` | daily suspension/resumption rows | https://tushare.pro/document/2?doc_id=214 |
| `dividend` | `ts_code` | cash/share corporate actions | https://tushare.pro/document/2?doc_id=103 |
| `bak_basic` | `trade_date`, selected fields | historical instrument audit | https://tushare.pro/document/1?doc_id=262 |

Limit hits are `PARTIAL`: daily 6000, `stk_limit` 5800, `index_member_all` 2000, `suspend_d` 5000, dividend 2000, and `bak_basic` 7000. The adapter does not assume a shared pagination parameter. Corporate actions are opt-in because the documented API is queried per code.

## Current availability

On 2026-09-23 this worktree had no `cache/v2/market.db`, `TUSHARE_TOKEN`, or `TYPESAFE_API_KEY`. No real provider call was made. The checked-in result package therefore contains schema-only real result tables and a clearly labeled synthetic flow run. Recovery starts with `python -m scripts.v2 doctor --mode real`, then `sync`, then `analyze`.
