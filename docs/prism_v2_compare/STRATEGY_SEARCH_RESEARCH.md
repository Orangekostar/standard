# Bounded Win-Rate Strategy Search

This is a separately approved research experiment. It does not change production, replace the archived rank-rotation results, invoke JEV, or claim a globally optimal or future winning strategy.

## Frozen Search

The grid has exactly 36 rules: three entry families (trend pullback, confirmed short-term rebound, low-frequency F0 UP), two entry strengths, three planned maximum holding periods (3/5/10 sessions), and two closing take-profit thresholds (2%/4%). The closing stop-loss threshold is 3%. Native F0 three-session forecasts are diagnostic inputs, not calibrated win probabilities.

Three consecutive 63-session discovery windows precede the old test calendar. Each window/cost uses its own CNY 1 million account. Win rate pools positive, fully closed economic trades after costs. Discovery return is the mean of the three equal-capital account returns, not a continuous compounded account return; drawdown is the worst single-window drawdown.

A rule qualifies only if both standard and doubled-slippage cases have at least 30 closed trades, at least 20 traded dates across discovery windows, positive mean net ledger return, worst-window drawdown no more than 15%, complete valuations and no remaining positions after the common 11-session entry-stop tail. All remaining marked PnL is still reported.

Rank qualified rules by standard-cost win rate, then mean net return, lower drawdown and stable candidate ID. Freeze the choice and discovery evidence hashes before loading test prices. Recheck only that candidate under both costs. A failed test does not trigger reselection. If no rule qualifies, recheck one predeclared raw-win-rate leader for diagnosis and retain a no-winner verdict regardless of its test result.

## Execution

- Maximum three actual securities, including pending corporate share entitlements. No daily higher-score replacement.
- Entries target previous-close NAV/3 including buy fees, bounded by actual cash and 1% of signal-day ADV20. Minimum ADV20 is CNY 50 million; unknown/stressed market states block new entries.
- Entry signals, stops, targets and expiry are evaluated after close and executed at a later eligible open. Intraday highs/lows never become assumed fills. Next-open gaps can overshoot stop thresholds.
- Maximum holding is a planned exit instruction, not a guarantee when a security is suspended or limit-down. Failed exits cannot free slots or provide money. Failed buys get no same-open fallback.
- Reuse existing dated security rules, tick/lot rounding, T+1, opening limits, actual sale cash, fee rounding and single-counted slippage.
- Rebound is a separate entry hypothesis and does not require F0 UP or exit merely because F0 remains DOWN. LOW_FREQ_UP retains DOWN as an additional exit.
- Comparison-price entry basis is the actual fill price times its dated adjustment factor; stop/target comparison uses the subsequent contemporaneous comparison close.

Exact family thresholds and ranking are recorded in the generated report and frozen implementation/configuration hashes. The old rank replay defaults are preserved; its private budget, entry-reason and metric extension points allow ledger reuse rather than a second execution engine.

## Data Limits

The mainboard/name-non-ST roster and frozen features are reused without modifying them. Unknown historical ST is allowed only in the already approved research scope. Frozen names are not historical ST labels and can create selection bias. Corporate-action/dividend records are incomplete, and historical vendor revisions are not point-in-time data vintages.

The test calendar has already been inspected. A discovery/test pass here remains finite-candidate research on a reused holdout, not a fresh untouched out-of-sample result, multiple-testing-adjusted significance claim, certified executable dividend-complete return, or production activation authorization.

## Reproduce

Run from `/home/ww/vv/quant/.worktrees/mainboard-only-universe`:

```bash
/home/ww/vv/quant/.venv/bin/python -m unittest discover -s tests/v2 -p test_strategy_search.py -q

/home/ww/vv/quant/.venv/bin/python -m scripts.strategy_search_backtest \
  --experiment-root cache/experiments/rank_rotation/frozen-mainboard-3d-20260929-v1 \
  --output-root artifacts/strategy_search/strategy-search-36-20260929-v1
```

Outputs include `frozen_protocol.json`, `leaderboard.csv`, `selection_cells.json`, `selection_freeze.json`, `RESEARCH_REPORT.md`, a complete hashed `run_manifest.json`, and each cell's independent account, daily NAV, actual fills, orders, decisions, execution attempts, closed trades and remaining positions. Exact matching runs validate hashes and reuse completed results. Incomplete or mismatched results are preserved and rejected, never silently overwritten.
