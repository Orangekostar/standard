# Prism / Technical V2 Fixed Comparison Design

The supplied `instructions/prism_v2_backtest_codex/CODEX_PRISM_VS_V2_BACKTEST.md`
and JSON plan are the authoritative experiment specification. This document
records implementation choices, not a smaller completion definition.

## Isolation And Provenance

Develop on `research/prism-v2-backtest`, based on V2
`0809f0ef38e2e776cc80b231d3e188edcddf3173`. Read the production market database
only through a read-only SQLite connection; freeze with `Connection.backup`.
Use the experiment snapshot, `.compare.lock`, cached parquet features, and one
empty V2Store account database per strategy/split/cost cell. No production
locks, publication files, API calls, or running processes are modified.

Source code used for the experiment is committed before opening the comparison
test. Results are bound to that SOURCE_COMMIT; the later DELIVERY_COMMIT is
verified externally rather than written into its own committed contents.

## Data Contract

The snapshot manifest records streamed SHA256, source path, backup time, size,
schema/source versions, audit-complete cutoff, calendar agreement, and coverage.
All available SSE/SZSE A-share records remain in the paired analysis roster.
Available list/delist dates constrain daily membership; current names are not
historical risk-warning evidence. Unknown metadata remains unknown and blocks
new risk where required. Historical universe gaps are explicitly scope-limited.

Each code is aligned to real exchange sessions before computing original V2
factors. Missing bars stay NaN; valuation carries only last genuinely observed
close with MARK_ONLY, stopping on known delisting. Original factors/context,
score_stock (F0, horizons 1/3/5), build_labels and build_fixed_split are reused.
Features and scores are computed once in bounded code chunks. Comparison prices
use raw price times the contemporaneous adjustment factor, an invariant scale;
execution and accounting use raw prices. Missing corporate-action records mean
RAW_PRICE_LEDGER_CORPORATE_ACTIONS_INCOMPLETE, not an unqualified net-PnL result.

The last 504 h5-mature signal dates use train252/calibration63/validation63/test126
and 120-session warmup. Purge label ends at every boundary. Validation orders
must mature before the test begins. Both test reporting blocks share one account.
Only train fits the common return bins; only validation base exposure fits C.
Old portfolio-test execution and old summary exposure are separate audit fields.

## Strategy And Account Contract

A retains original holdings-aware research intent, score exits and actual-entry
plus five-session expiry. It has no price-triggered stop. B adds only the supplied
market-state quantity multiplier, bounded adaptive distance, tighten-only per-lot
close-triggered exit, and two full-session cooldown after actual full liquidation.
B UNKNOWN's market quantity component uses A behavior without relaxing shared hard gates. Disabled B additions
must produce identical actions, orders and NAV to A. C scales only stock, sector,
gross and risk caps by the frozen validation exposure ratio, never cash or returns.

Reuse PaperPortfolio, V2Store and execute_open_orders. Necessary shared fixes are
optional, backward-compatible extensions: dated minimum/increment/max quantity
rules; explicit sell-lot quantities; next-session buy expiry; same-code exit
priority; slipped fills inside exchange limits; confirmed receivables in NAV.
The comparison allocator uses common gates, finalized quantities and exact fee
checks before reserving cash, gross and sector capacity. Exit proceeds are never
spent at signal time. Orders submitted after close cannot execute that close.

Exit priority is required shared exits, expired lots, B stop-crossed lots, then
score exits/reductions. Merge quantities by lot without double counting, and
deduplicate pending exits by code. Illegal small partial exits remain pending;
never enlarge an exit by selling an untriggered lot. Board rules are dated and
unverified boards are consistently unavailable for all strategies.

## Orchestration And Evidence

prepare -> validate (A/B base) -> freeze C and protocol -> test -> report.
The remaining validation cells use the same frozen protocol. Exactly twelve
main cells; successful cells are reused by snapshot, implementation, config,
policy, split and cost hashes. At most two simultaneous replays and eight CPU
threads; memory is capped at half available RAM. A 50-stock/160-real-session
offline smoke precedes the one full feature preparation.

Every cell exports all named NAV, decision, order, fill, closed-lot, remaining
position, metric and runtime files. Metrics distinguish candidate rejection from
actual unfilled orders. Common-date paired moving-block bootstrap uses length10,
1000 repetitions, seed20260928. Verdict validity precedes return/risk comparisons;
scope limitations and independent evidence fields are retained even with zero
trades. Figures, attribution, source audit, handoff and a requirement-by-requirement
completion audit are required. Git uses an explicit whitelist; no vendor DB or
credentials. Large derived detail assets have <=40MiB parts and restore hashes.

## Current Evidence

Baseline regression: 198 tests passed. Read-only source inspection found 801
daily sessions and audited cutoff20260928, real TUSHARE_DAILY bars, reconstructed
SW_L1 membership, only current listed instrument snapshots, no corporate actions,
and all historical risk-warning flags unknown. These are expected data limitations,
not reasons to invent metadata or weaken gates. Snapshot audit must revalidate
these counts because the live source can continue updating before the backup.
