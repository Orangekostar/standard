# Three-Stock Rank Rotation

This is an isolated research policy, not a production strategy change. It does not overwrite the archived prism comparison or modify the running paper worker.

## Policy

- Use fixed `F0_BALANCED` formula `forecast_class3` and `score3`, not JEV. Native UP is score >= 60, DOWN is score <= 40, and FLAT is between them.
- Keep at most three actual securities, including pending corporate share entitlements.
- Buy UP candidates in descending current score order. Equal scores use code order for new entries; existing positions win ties.
- When full, strictly higher-scoring UP candidates replace the weakest holding with an available current score. Entry-day scores are never used for rotation.
- DOWN exits take priority. FLAT remains held unless a higher-scoring UP candidate replaces it. Missing predictions are not treated as DOWN or as zero scores.
- Generate signals after the close; execute at the following opening quote. All sales precede purchases. A failed replacement sale cannot fund its paired purchase or free a slot.
- Target one third of the previous close NAV per new entry, including buy fees, limited by actual available cash. Do not rebalance retained positions daily.
- Pending exits retry at later eligible opens. Failed buys are ranked again at the next close, without same-open fallback.
- There is no fixed three-day holding expiry, forced final liquidation, forecast return-bin gate, 65/55 entry gate, breadth cap, sector cap, ATR risk budget or overextension filter.
- Retain T+1, suspensions, observed opening price limits, dated security quantities, tick rounding, commissions and one-time slippage.

## Data Boundaries

Prepare a separate read-only SQLite backup of the old frozen snapshot. Recompute market/sector factors with the filtered SSE/SZSE mainboard roster. Exclude STAR 688/689, ChiNext 300/301, BSE and frozen-snapshot ST names from both candidates and factor context.

The separately approved research policy allows unknown historical ST status. Frozen names are not historical ST classifications and can introduce selection bias. Production and the archived comparison still block unknown historical ST.

Reuse the previous validation/test calendar windows but allow close signals through each window's penultimate session, because there is no fixed-expiry settlement tail. Each window and cost case has an independent account. F0 weights are fixed: no training on outcomes, return-bin fitting, threshold optimization or fresh untouched-holdout claim.

Raw-price accounting applies corporate actions actually present in the snapshot. If those records are incomplete, reported results are research ledger returns, not certified dividend-complete total returns. Unsold final holdings remain marked to the last close; no final sale fees are charged for positions that were not sold.

## Commands

Run from the `fix/mainboard-only-universe` worktree:

```bash
/home/ww/vv/quant/.venv/bin/python -m unittest discover -s tests/v2 -p test_rank_rotation.py -q

/home/ww/vv/quant/.venv/bin/python -m scripts.rank_rotation_backtest all \
  --source-experiment /home/ww/vv/quant/.worktrees/prism-v2-backtest/cache/experiments/prism_v2/frozen-20260928-v1 \
  --experiment-root cache/experiments/rank_rotation/frozen-mainboard-3d-20260929-v1 \
  --output-root artifacts/rank_rotation/rank-rotation-3d-20260929-v1
```

Completed outputs are reusable only when the dataset, configuration, implementation and artifact hashes match. Incomplete or mismatched output directories are preserved and rejected; use a new output path rather than overwrite them.

Outputs include `summary.csv`, `RESEARCH_REPORT.md`, an implementation/data-bound `frozen_protocol.json`, `run_manifest.json`, and per-window/per-cost NAV, decisions, orders, fills, execution attempts, closed trades, daily holdings, remaining positions and independent `account.db` ledgers.

Feature chunks use up to four independent processes in this research runner. The shared cache builder defaults to serial execution; serial and parallel feature/label contents are regression-tested for equality.
