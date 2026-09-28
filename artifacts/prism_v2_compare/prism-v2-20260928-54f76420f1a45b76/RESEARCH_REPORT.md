# Prism / V2 Fixed Historical Comparison

| Strategy | Net Return | Drawdown | Sharpe | Mean Exposure | Closed Trades | Traded Dates |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A0_V2_F0 | 0.00% | 0.00% | null | 0.00% | 0 | 0 |
| B0_PRISM_A_SHARE_V1 | 0.00% | 0.00% | null | 0.00% | 0 | 0 |
| C0_V2_EXPOSURE_CONTROL | 0.00% | 0.00% | null | 0.00% | 0 | 0 |

Verdict: `INSUFFICIENT_TRADING_EVIDENCE`. No universal live-trading superiority is claimed.

## Protocol And Data

SOURCE_COMMIT: `410de2d9b5d3cf380d0d759ea7652bb541956fd6`.
Snapshot SHA256: `2e2598e9e115d3110f3031a710d1997e74ee374c51db73a4cef99939054ff820`; audited cutoff: `20260928`.
Scope: UNIVERSE_HISTORY_LIMITED, HISTORICAL_DATA_REVISIONS_NOT_POINT_IN_TIME_VINTAGES, REUSED_HOLDOUT, HISTORICAL_RISK_WARNING_UNKNOWN, RAW_PRICE_LEDGER_CORPORATE_ACTIONS_INCOMPLETE.
Old portfolio test: `UNKNOWN_FOR_REAL_DATA; LOCATED_DEMO_ARTIFACTS_FALSE`; old summary exposure: `UNKNOWN_FOR_REAL_DATA; ALL_SPLIT_FACTOR_IC_IN_LOCATED_DEMO_EVIDENCE`.
No Jev/LLM/API inference, threshold search, production publication, or strategy activation.

C budget multiplier: `1`; `UNIDENTIFIABLE_ZERO_BASE_EXPOSURE`.
C scales stock/sector/gross/risk caps, never cash or terminal returns; it is not exact risk matching.

## Dates And Boundaries

`split.csv` preserves train252/calibration63/validation63/test126 and the common allowed signals.
Train labels mature strictly before calibration. Validation excludes signals whose five-session expiry crosses test.
Each replay contains its full common settlement tail; the two test blocks share one continuous account.
- test: 20260319 - 20260928; 126 allowed signals.
- validation: 20251210 - 20260318; 57 allowed signals.

## Observed Account And Gate Results

All three test accounts remained in cash: no fills, fully closed trades, fees or modeled slippage.
Zero drawdown and equal NAV do not establish improved forecasting or better risk management.
There is no realized P&L to attribute to market states, entry sectors or exit reasons.
The frozen snapshot has no known historical risk-warning flags. The shared BLOCK_NEW_ENTRIES policy therefore prevents new risk; it was not relaxed to manufacture trades.

Funnel denominators use allowed signal-date roster rows; actual fills/closed trades include the common settlement tail.
| Strategy | Roster Rows | Factor Valid | 65/55 | Not Extended | Return Estimate | Net Edge | Eligibility | Allocated | Orders | Fills | Closed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A0_V2_F0 | 673974 | 646722 | 61091 | 40340 | 40340 | 0 | 0 | 0 | 0 | 0 | 0 |
| B0_PRISM_A_SHARE_V1 | 673974 | 646722 | 61091 | 40340 | 40340 | 0 | 0 | 0 | 0 | 0 | 0 |
| C0_V2_EXPOSURE_CONTROL | 673974 | 646722 | 61091 | 40340 | 40340 | 0 | 0 | 0 | 0 | 0 | 0 |

All-blocker occurrences are non-exclusive and include all signal-date roster rows, not only rejected entry candidates.
| Strategy | Main Blocker | Occurrences |
| --- | --- | ---: |
| A0_V2_F0 | HISTORICAL_RISK_WARNING_UNKNOWN | 673974 |
| A0_V2_F0 | SCORE_ENTRY_CONDITION_NOT_MET | 612883 |
| A0_V2_F0 | NET_EDGE_BELOW_MINIMUM | 88343 |
| B0_PRISM_A_SHARE_V1 | HISTORICAL_RISK_WARNING_UNKNOWN | 673974 |
| B0_PRISM_A_SHARE_V1 | SCORE_ENTRY_CONDITION_NOT_MET | 612883 |
| B0_PRISM_A_SHARE_V1 | MARKET_STATE_BLOCKS_NEW_RISK | 320940 |
| C0_V2_EXPOSURE_CONTROL | HISTORICAL_RISK_WARNING_UNKNOWN | 673974 |
| C0_V2_EXPOSURE_CONTROL | SCORE_ENTRY_CONDITION_NOT_MET | 612883 |
| C0_V2_EXPOSURE_CONTROL | NET_EDGE_BELOW_MINIMUM | 88343 |

Fixed test-signal adjusted-label diagnosis: count=40230, mean=-0.20%, median=-1.12%, p95=18.45%.
These are adjusted five-session signal labels, not account net returns, evidence of B prediction improvement or permission to change the frozen gates.

## Interpretation And Attribution

A keeps original F0/score/expiry behavior. B adds only market-state quantity scaling, adaptive close-stop management and actual-flat cooldown.
All groups share the same original fifteen factors, three horizon scores, train return bins, legal-unit rules, fills, fees and NAV accounting.
Shared repairs are recorded in `shared_fixes.md`; their effect is not a Prism benefit or an exact native-A reproduction.
`regime_summary.csv` attributes daily account returns by closing market state; non-contiguous state groups are not separate account backtests.
`sector_summary.csv` uses actual lot entry sectors; `exit_summary.csv` uses fully closed economic lots, including bonus descendants and confirmed dividends.
`cost_summary.csv` reports actual costs from independent base/stress replays; no terminal fee subtraction is used.
`year_quarter_summary.csv` and `test_subperiods.csv` are descriptive attribution only, not interval selection.
`gate_funnel.csv` distinguishes cumulative gates, first/all blockers and actual orders/fills/closed lots.
`candidate_label_diagnostic.csv` is fixed five-session adjusted-price signal diagnosis, never account net return or improved B directional prediction.
Zero orders imply null unfilled-order ratio; zero volatility/trades imply null Sharpe/win rate. Profit factor is null without closed-loss denominator.
Turnover is both-side fill notional / mean common-date NAV; daily turnover divides all common sessions.

## Evidence

```json
{
  "verdict": "INSUFFICIENT_TRADING_EVIDENCE",
  "numerical_return_leader": null,
  "b_minus_a_net_return": 0.0,
  "paired_ci_supports_leader": false,
  "cost_stress_preserves_lead": false,
  "test_halves_consistent": false,
  "beats_exposure_control": false,
  "scope_limited": true,
  "scope_flags": [
    "HISTORICAL_DATA_REVISIONS_NOT_POINT_IN_TIME_VINTAGES",
    "HISTORICAL_RISK_WARNING_UNKNOWN",
    "RAW_PRICE_LEDGER_CORPORATE_ACTIONS_INCOMPLETE",
    "REUSED_HOLDOUT",
    "UNIVERSE_HISTORY_LIMITED"
  ],
  "accounting_valid": true,
  "sample_sufficient": false,
  "evidence_supports_historical_leader": false,
  "generalization": "FIXED_HISTORICAL_INTERVAL_ONLY_NOT_UNIVERSAL_LIVE_SUPERIORITY",
  "prediction_claim": "IDENTICAL_RAW_F0_SCORES; POLICY_NOT_NEW_DIRECTIONAL_FACTORS"
}
```

`paired_bootstrap.json` uses the same test-session moving blocks for A/B, not independently sampled stock trades.
Confidence intervals describe fixed-history stability, not future returns. Available-universe, action and holdout limitations remain binding.
All detailed cells and three independent figures are listed with bytes/SHA256 in `artifact_manifest.json`.
Raw vendor market databases and shared feature caches remain local; only derived research outputs are eligible for delivery.
