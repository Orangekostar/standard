# Technical V2 Handoff

## 1. Repository and release identity

- Repository: `git@github.com:Orangekostar/standard.git`
- Baseline: `32279ec22d447675cbfcc6bdbf9b68f10e37bbf4`
- Code commit A2: `e800038e05b2dab1059c062cc28e9f1c3bf1a518`
- Branch: `codex/technical-v2`
- Evidence commit B: intentionally not self-recorded; obtain with `git rev-parse HEAD` after the evidence commit.
- Release tag: derived after commit B as `technical-v2-20260923-<B7>`; the exact tag and remote SHA belong in `publish_receipt.json` after remote verification.
- PR/release URLs: unavailable until the authorized remote attempt succeeds.

## 2. T00-T11 delivery

T00 baseline/config, T01 V2 data boundary, T02 factors/context, T03 formula, T04 native Jev, T05 labels/calibration/selection, T06 paper ledger, T07 immutable DAG/publication, T08 read-only UI, T09 CLI/deployment, and T10 bounded verification are code-complete. T11 documentation and public demo evidence are included here. Real sync, online Jev smoke, and fixed evaluation are externally blocked by missing credentials/data, not represented as complete experiments.

The itemized T00-T11, C01-C12, and final stop-condition results are in `COMPLETION_AUDIT.md`.

## 3. Data state

The V2 schema is migration version 1. No real V2 database exists in this worktree. `TUSHARE_TOKEN` and `TYPESAFE_API_KEY` are absent. The legacy database spans 2026-04-14 through 2026-09-22 but is `LEGACY_UNVERIFIED` and excluded. The public demo has four synthetic stocks, no real sectors, and 24 prediction contract rows dated 2026-09-22.

## 4. Algorithms and versions

There are 15 directional factors, 6 risk metrics, three formula candidates F0/F1/F2, independent stock/sector formulas, 15 Jev questions, and J0/J1 linear pools. Jev requests fixed `jev-1.13.0`; pricing snapshot and question schema are versioned. Exact formulas and boundaries are in `FACTOR_SPEC.md` and `MODEL_CARD.md`.

## 5. Fixed experiment protocol

The split is 252 train / 63 calibration / 63 validation / 126 final test over the last 504 mature signal dates, with 120-session warmups and label-end purge. The Jev cohort is frozen before calibration, sector round-robin, maximum 32 stocks and 32 sectors. Candidate limits are three formula configurations, two pools, and eight temperatures per pool/entity/horizon. Current mature dates: 0. F0 remains unvalidated; no test data was opened.

## 6. Actual results

Formula has no real IC, return, Sharpe, drawdown, or win-rate result. Jev has no real response, probability metric, calibration, return, or forward-validation result. The demo is a synthetic flow check only. Unmatured metrics are null. See `EVALUATION_REPORT.md` and the artifact CSVs.

## 7. Operations and recovery

```bash
python -m scripts.v2 doctor --mode real
python -m scripts.v2 sync --mode real --history-sessions 800
python -m scripts.v2 analyze --mode real --as-of latest --methods formula,jev
python -m scripts.v2 fit-evaluate --mode real --protocol fixed-v1
python -m scripts.v2 paper --mode real --methods formula,jev
python -m scripts.v2 evaluate-matured --mode real
streamlit run app_v2.py
python -m core.background.precompute_worker --profile technical_v2
```

Environment variable names are in `.env.example`; never commit their values. Migration is additive: back up `MARKET_V2_DB_PATH`, run `sync`, and restore the prior file if migration/startup validation fails. `latest` is calendar-validated; a `STALE` result cannot generate orders.

## 8. Verification

`/home/ww/vv/quant/.venv/bin/python -m unittest discover -s tests -q` ran 166 tests with zero failures/errors; the generated package then passed one targeted release-audit test. Focused Ruff, compilation, systemd verification, lock comparison, CLI smoke, and Streamlit runtime checks passed. Online Jev and real provider checks were not run because keys are absent. Legacy SQLite resource warnings remain documented.

## 9. Services and accounts

No existing service was installed, restarted, disabled, or modified. The new installer refuses an active legacy precompute conflict and does not start the new unit automatically. No user holding file was written. Formula and Jev paper accounts are separate; actual holdings remain `readonly_user`. `live_trading=NOT_CONNECTED`.

## 10. Standard-day check

Run `doctor`, then `sync`; require current `actual_as_of==expected_as_of`. Run `analyze` and inspect route status, full-universe coverage, raw/calibrated probability status, and missing reasons. Run `paper`; stale data, missing security rules, unavailable expected edge, or account restrictions must leave quantity null/blocked. Use `evaluate-matured` only after labels mature. For key failure, set the named environment variable and retry the failed route; for budget failure, wait for/reset the approved budget period rather than bypassing it.

## Final status before remote attempt

```json
{
  "software": "COMPLETE",
  "data": "UNAVAILABLE",
  "formula": "UNVALIDATED_DEFAULT",
  "jev": "UNAVAILABLE_CREDENTIALS",
  "evaluation": "NOT_RUN_WITH_REASON",
  "github": "PUBLISH_BLOCKED",
  "live_trading": "NOT_CONNECTED"
}
```

`github` remains provisional until the single remote push/PR/release attempt is verified and recorded externally in `publish_receipt.json`.
