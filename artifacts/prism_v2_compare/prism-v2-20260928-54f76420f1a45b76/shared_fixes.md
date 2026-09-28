# Source Audit

## Fixed Identity And Actual Function Mapping

V2 base: `0809f0ef38e2e776cc80b231d3e188edcddf3173`, remote
`git@github.com:Orangekostar/standard.git`, original branch `codex/technical-v2`.
Read-only remote verification on20260928 returned that same SHA. Experiment
worktree is independent and uses branch `research/prism-v2-backtest`.

Prism source: [irfndi/prism-liquidity-agent at d15c63e](https://github.com/irfndi/prism-liquidity-agent/tree/d15c63ef7a5d426ad71f4bbf4dae05c94ba0bc02).
Only the required strategy-service, churn-guard, jev-service, engine/program and
license files were downloaded. No Prism software was installed or executed.

| Actual function | Comparison use |
|---|---|
| technical_v2.apply_adjustment_factors / compute_technical_v2 | Original F01-F15/Q01-Q06; align missing sessions before calling |
| sector_v2.build_context | Original lagged membership, equal-weight market/industry context |
| formula_v2.score_stock / fit_return_bins / estimate_formula_return | Shared F0 h1/3/5; one purged train-only h5 model |
| intent_v2.derive_research_intent | Actual quantity, sellable quantity and original overextended flag |
| technical_v2.build_labels / build_fixed_split / purge_cross_boundary | Original next-open h5 label and fixed chronological boundaries |
| evaluation_v2._simulate_formula_portfolio | Native identity only; cost0.00212, universal100 lot, actual holdings, score/expiry exits, no implemented price stop |
| evaluation_v2._factor_ic | Does not exclude test; old summary exposure cannot be assumed absent |
| V2Store / PaperPortfolio / execute_open_orders | Shared real account ledger, no replacement account system |
| metrics_v2.calculate_portfolio_metrics | Shared NAV-return conventions; separate candidate and actual-order metrics |
| scripts/v2.py production entrypoints | Not called by this experiment; production locks/publication untouched |

## Prism Interpretation

[strategy-service.ts](https://github.com/irfndi/prism-liquidity-agent/blob/d15c63ef7a5d426ad71f4bbf4dae05c94ba0bc02/engine/strategy-service.ts)
implements bounded volatility-scaled DLMM range width and drift/volatility shape
selection. It also has online outcome-driven weighting and recovery heuristics;
those are intentionally not migrated. The supplied A-share formulas, not DLMM
bin arithmetic or LP income, define PRISM_A_SHARE_V1.

[churn-guard.ts](https://github.com/irfndi/prism-liquidity-agent/blob/d15c63ef7a5d426ad71f4bbf4dae05c94ba0bc02/engine/churn-guard.ts)
limits new entries rather than required exits. A-share adaptation uses actual
full liquidation and two exchange sessions, not UTC-day entry counts.

[jev-service.ts](https://github.com/irfndi/prism-liquidity-agent/blob/d15c63ef7a5d426ad71f4bbf4dae05c94ba0bc02/engine/jev-service.ts)
provides advisory judgments with deterministic fallback. Actual
[engine/program.ts](https://github.com/irfndi/prism-liquidity-agent/blob/d15c63ef7a5d426ad71f4bbf4dae05c94ba0bc02/engine/program.ts)
also has an optional paper-only stress size-halving path; "shadow only" is not
a complete description of all consumers. Neither path is an A-share trained
probability model. This comparison never imports/calls Jev and preserves existing
Jev code. MIT license is retained in `PRISM_LICENSE`; Python A-share rules are
independently implemented from the user-supplied mathematical specification.

| Downloaded source | SHA256 |
|---|---|
| strategy-service.ts | c5ba91c06316c1556321e67828b69dcae86f1aa89252254afa5c0ba288c8d7f0 |
| churn-guard.ts | ecfbb5e31323603e09180781f9697453b5bba7e16daecb062f8f727206004bab |
| jev-service.ts | 75af2085ff9ceb35abaa619ebd6940db7f863c02b520c1b4ed2538f8a64b4e0e |
| engine/program.ts | 06fca87446b57b7d10c768be917ab32519545a9b457bed5cab52e6089f35a6e1 |
| LICENSE | 5905fc7adbcb4fff0fbb274370905c2b84c6d72da533da8c691ad675281779a6 |

## Historical Rule Evidence

Historical quantity/tick rules are independent from fixed research fee assumptions.
The [March2023 registration-reform manual, printed p119](https://edu.sse.com.cn/zcz/document/c/10758658/files/546820356f9d4af48184976b367356b4.pdf)
was downloaded and extracted with pdftotext: editorial date March2023; main
boards100 increments, STAR minimum200, ChiNext100 increments, applicable maximums.
[SSE's 2019 STAR explanation](https://edu.sse.com.cn/tib/qa/c/4866268.shtml)
confirms STAR increments of one and residual liquidation below200.
[SZSE's July2023 explanation](https://investor.szse.cn/institute/rules/t20230706_601604.html)
confirms main/ChiNext100 increments and tick. The experiment data starts after
the conservative verified applicability dates in configuration.

From20260706 use the actual new editions, not those editions retrospectively:
[SSE2026 notice/rules](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml),
[SZSE2026 notice](https://investor.szse.cn/lawrules/rule/trade/t20260424_620190.html),
[SZSE2026 PDF p12,3.3.8-3.3.11](https://docs.static.szse.cn/www/lawrules/rule/trade/current/W020260424690713155663.pdf).
Those notices state20260706 effectiveness; the relevant quantity rules remain
as configured. Unknown board/date combinations are unavailable for all groups.
Daily source price-limit and suspension metadata, not historical names, determine
open feasibility. This remains OPEN_AUCTION_EXECUTION_APPROXIMATION: no queue or
order-book reconstruction is claimed.

## Native Baseline And Data Limits

A_NATIVE_REFERENCE is not a winner candidate. Located old evaluations are
`artifacts/technical_v2/demo-20260922-480fdd78cb43` and
`evidence/technical_v2/be7fa3895f3013156aa0b9d5ea65f0706d0c252f/evaluation`.
The latter manifest explicitly says data_source_mode=demo, six codes,615 mature
dates and504 split assignments; its run/selection records say final_test_opened=false
and NO_ELIGIBLE_CANDIDATE. It is not the current real source snapshot. No
split_assignments table exists in the real source schema. Old factor-IC
implementation accepts all splits, so historical
summary exposure is UNKNOWN/REUSED_HOLDOUT absent stronger evidence. Separate
old portfolio execution and summary exposure fields are recorded in the dataset
audit; old selection/final_test_opened artifacts are not modified.

Initial read-only inspection: source path resolved through production settings is
`/home/ww/vv/quant/.worktrees/technical-v2/cache/v2/market.db`; 801 price sessions,
audited cutoff20260928, 4,314,526 real daily rows, reconstructed SW_L1 history.
Instrument versions are current listed snapshots (20260925-28), with no complete
historical delisted universe. Corporate_actions has zero rows and historical
is_risk_warning is entirely NULL. Snapshot preparation must independently
revalidate and record actual frozen counts. Consequences: UNIVERSE_HISTORY_LIMITED,
HISTORICAL_RISK_WARNING_UNKNOWN, RAW_PRICE_LEDGER_CORPORATE_ACTIONS_INCOMPLETE;
no survivor-free universe or unqualified account net-PnL superiority claim.

The native sync audit uses the SSE calendar key for its combined analysis
universe, not a separate per-SZSE audit (`scripts/v2.py::_sync`). The frozen
latest audit is20260928:5221 expected codes,5209 observed plus12 known suspended,
coverage1.0 and all required endpoint statuses OK. All801 audit records use that
native SSE key; both exchange open-session calendars independently match. This
does not claim two independently populated exchange audit series.

## Shared Replay Corrections Found Before Comparison Test

Historical account initialization needs a logical initial-capital event before
the first historical fill. The original default uses wall-clock creation time,
which sorts after fills from earlier years. Optional `initial_event_at` preserves
the production default while allowing all experiment accounts to reconcile.

An already-confirmed cash dividend must settle even when its eligible shares
were sold before payment. The shared corporate-action guard now retains that
obligation. This does not invent missing corporate-action rows in the real DB.

Independent validated execution-open and valuation-close fields are retained
before full OHLCV feature validation. Thus same-day close/high/volume validity
cannot decide an earlier opening fill. Quantity flooring remains Decimal through
the legal-unit boundary; a value just below200 cannot round up through float.
The provider's canonical security type is lowercase `stock`, not `STOCK`;
normalization is shared and current ST names remain excluded.

Allocation reserves the finalized order's ceiling-price notional plus exact
buy fees, after B's multiplier and quantity-specific edge check. All groups use
the same conservative capacity reservation and never spend expected exit cash.
This differs from the unmodified native reference's reference-price reservation
and is not a Prism benefit. The status reader's native last-non-null-column
contract is unchanged; pandas `GroupBy.last` replaces slow Python aggregation.

Corporate-share entitlements now use record-date quantity history, including
parents sold before processing or listing. Confirmed unlisted shares are valued
from their ex-date and retained as receivables; listing transfers their value to
individual zero-cost child lots without a NAV jump. Children inherit their
parent's expiry and comparison-price stop/high references. Fully closed economic
trades aggregate parent/bonus descendants and confirmed dividends, rather than
counting each bonus lot as a separate win. Pending rights block new risk and delay
actual-flat cooldown. These are symmetric shared corrections, not B benefits.
They do not repair absent historical corporate-action rows in the real snapshot.

The config loader rejects unsupported changes to the fixed source, real-data
and comparison-price modes, A/B's F0/h5/five-session baseline, B's close-time
tighten-only stop, C's four-cap validation-only scaling, shared execution/fee
modes, exact tail/subperiods, twelve-cell bootstrap and forbidden delivery modes.
Otherwise a declared option could silently disagree with actual execution.
New rejection cases were reproduced before adding validation; numeric consumers
and provenance-only fields are mapped in `PARAMETER_AUDIT.md`.

Confirmed unlisted shares consume stock, sector, gross and name capacity from
their ex-date, including when the parent lot is already closed. The minimal
fixture previously exceeded a5% gross cap (8.9848624%) and now stays within it
while allowing another code to use only remaining capacity. This correction is
shared by A/B/C, not an extra B risk rule.

Report generation verifies root/copy data, audit, parameters, split and protocol
bindings before writing outputs. Insufficient fixed history produces an explicit
ineligible protocol, unavailable C exposure and null-valued NOT_RUN artifacts;
it never opens test or shortens504/120. Funnel denominators exclude settlement
tail roster rows while preserving real tail fills and closed trades. Derived
result packaging uses a manifest whitelist, at most40MiB parts and verified
part/archive/restored-file hashes; raw market databases and credentials are
excluded. These paths have directed fixture coverage, not main trading evidence.

Development verification:54 targeted tests passed before full-market preparation.
The first real50x160 offline smoke completed in37.169s under cProfile with peak
process RSS1385656320 bytes and0 API calls. Its preliminary failed attempt was an
adapter case mismatch, not absent market data. The profile identified15.732s in
status aggregation; later equivalent optimization is covered independently.
Main comparison test has not been opened. These are not TEST_EXPOSED_BUGFIXes.

## Completed Preparation And Current Verification

Frozen snapshot SHA256:
`2e2598e9e115d3110f3031a710d1997e74ee374c51db73a4cef99939054ff820`.
The completed preparation contains5349 codes,801 actual exchange sessions and775
mature h5 signal dates; no fixed split was shortened. Full54-chunk features took
1811.501s with peak process RSS3484811264 bytes and0 API calls. The refreshed
50x160 offline smoke took9.124s; neither smoke is a main historical comparison.
Train fitting used1245043 rows across246 purged signal dates, with latest label
end20250903 strictly before calibration20250904. Validation begins20251210 and
has57 allowed signals after its boundary exclusion. Test has126 signals from
20260319 through20260917 and a common settlement tail through20260928.

The stage CLI and report exporter have passed twelve-cell fixture, immutable
protocol/C, successful-cell reuse, tamper detection, no-network, exact summary
column and nonblank figure/hash checks. The latest related V2 regression passed236
tests in113.988s on the implementation committed as7a39ed2. Matplotlib/NumPy
deprecation warnings and Streamlit's bare-mode
context warning were nonfatal third-party warnings, not test failures.
At this pre-comparison source audit, actual main validation/freeze/test/report,
complete requirement review and result delivery remain pending. This file is
copied immutably to the run's `shared_fixes.md`; subsequent actual completion
evidence belongs in `HANDOFF.md` and `COMPLETION_AUDIT.md`. Uploading source alone
does not complete the experiment.
