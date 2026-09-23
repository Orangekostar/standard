# Technical V2 Test Report

- Code commit: `e800038e05b2dab1059c062cc28e9f1c3bf1a518`
- Executed at: `2026-09-23T06:02:21Z`
- Command: `/home/ww/vv/quant/.venv/bin/python -m unittest discover -s tests -q`
- Result: **166 passed, 0 failed, 0 errors** in 15.596 seconds.
- V2-only checkpoint before final audit: 133 passed.
- Post-generation release-artifact audit: 1 passed; the full suite was not repeated.
- Static checks: focused Ruff passed; Python compilation passed; `git diff --check` passed.
- UI: Streamlit AppTest rendered five tabs with no application exception; headless health endpoint returned `ok`.
- Deployment: rendered user unit passed `systemd-analyze --user verify`; installer passed `bash -n`.
- Dependency lock: no difference from the installed production environment; Ruff 0.16.1 is pinned separately for development.

Existing legacy `MarketDB` tests emitted unclosed SQLite `ResourceWarning` messages. They were present at baseline; all V2 store checks close their connections and passed.

## External checks

| Check | Status | Evidence |
|---|---|---|
| Real V2 sync/analysis | NOT_RUN_WITH_REASON | V2 DB and `TUSHARE_TOKEN` absent |
| Jev online smoke | NOT_RUN_WITH_REASON | `TYPESAFE_API_KEY` absent; no paid request made |
| Fixed 504-date evaluation | NOT_RUN_WITH_REASON | no mature real V2 dataset; final test unopened |
| Paper orders | NOT_RUN_WITH_REASON | no migrated V2 DB; zero orders created |
| Demo | PASS_FLOW_ONLY | 4 stocks, 24 contract rows, deterministic seed, zero network calls |

## Performance check

The single bounded synthetic factor/context check used 200 stocks x 300 sessions (60,000 rows): 9.338 seconds in process and 245,812 KiB peak RSS. It is calculation performance evidence only, not return evidence. Host: 64 logical CPUs, Intel Xeon Silver 4314, 251 GiB RAM.
