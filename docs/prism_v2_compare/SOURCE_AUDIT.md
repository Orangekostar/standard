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
