# Technical V2 Factor Specification

Formula version: `technical-v2-fixed-v1`
Implementation: `core/factors/technical_v2.py`, `core/analysis/sector_v2.py`
Frozen config hash: `7835afd12b6a5533d415208bb97f803e248d81576d3906eb26e5327de8611bc1`

All features use information available through the row date. Prices are adjusted to the as-of factor; amount is RMB and volume is shares. `sigma20` uses log returns with `ddof=0`, `s=max(sigma20,0.005)`, and `a=max(ATR14/close,0.001)`. Direction factors are bounded to [-1, 1]. Missing inputs produce null plus a reason code.

| ID | Group | Definition |
|---|---|---|
| F01 | T | `tanh(log(C/C[-20]) / (s*sqrt(20)))` |
| F02 | T | `tanh(log(C/C[-60]) / (s*sqrt(60)))`; 61 prices and 50 real bars required |
| F03 | T | `tanh((MA20/MA60-1)/(3*a))` |
| F04 | R | stock 20-day log return minus sector, scaled by stock volatility |
| F05 | R | stock 20-day log return minus market, scaled by stock volatility |
| F06 | S | `tanh((C/H20prev-1)/a)`; previous 20 highs exclude today |
| F07 | S | close position in the current 20-day high/low range; flat range is 0 |
| F08 | S | five-day mean close-location value; flat bars are 0 |
| F09 | V | signed log amount surprise against the previous 20-day median |
| F10 | V | 20-day amount-weighted close-location pressure |
| F11 | V | five-day signed volume balance |
| F12 | C | sector 20-day return relative to market, sector-volatility scaled |
| F13 | C | `2 * sector_breadth20 - 1` |
| F14 | C | `2 * market_breadth20 - 1` |
| F15 | C | market 20-day trend scaled by market volatility |

| ID | Risk metric | Use |
|---|---|---|
| Q01 | ATR14 / close | risk distance and sizing |
| Q02 | 20-day log-return volatility | target threshold and diagnostics |
| Q03 | 20-day average amount in RMB | capacity |
| Q04 | mean absolute simple return / amount on valid trading days | illiquidity |
| Q05 | close / 20-day maximum close - 1 | drawdown |
| Q06 | adjusted open / previous adjusted close - 1 | opening-gap risk |

`overextended` is a rule field, not a candidate factor. Legacy RSI, reversal, Three Bull Pullback, valuation, market cap, news, macro, vendor flow scores, and fundamental fields are excluded.

## Formula route

Each group is the mean of its available members. A score requires at least 12 of 15 factors and at least one factor from every group. Missing groups are never replaced by zero. For each horizon, `score=clip(50+50*sum(weight_g*group_g),0,100)`. Score >=60 is `up`, <=40 is `down`, otherwise `flat`; the score is not a probability.

The only candidates are `F0_BALANCED`, `F1_TREND` (+0.05 T, -0.05 V), and `F2_STRUCTURE` (+0.05 S, -0.05 T). All weights remain positive and sum to one. With insufficient real selection data, F0 remains `UNVALIDATED_DEFAULT`.

## Causality checks

Tests cover hand calculations, future-row append invariance, input-order invariance, uniform price-scale invariance, flat ranges, missing industry context, membership timing, and context coverage. The legacy Alpha6/9 paths now use causal expanding ranks and remain explicitly legacy.
