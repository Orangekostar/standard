# Technical V2 Handoff

- Branch: `codex/technical-v2`
- Final code commit: `be7fa3895f3013156aa0b9d5ea65f0706d0c252f`
- Schema version: 3
- Runtime evidence publication: `run-17790695de0951d52c46`
- Immutable evaluation: `evaluation-20260923-429241fabcfef0c0`
- Evidence: `evidence/technical_v2/be7fa3895f3013156aa0b9d5ea65f0706d0c252f/`

Operational entry points:

```bash
python -m scripts.v2 doctor --mode real
python -m scripts.v2 sync --mode real --history-sessions 800
python -m scripts.v2 analyze --mode real --as-of latest --methods formula,jev
python -m scripts.v2 fit-evaluate --mode real --protocol fixed-v1
python -m scripts.v2 paper --mode real --methods formula,jev
python -m scripts.v2 evaluate-matured --mode real
python -m core.background.precompute_worker --profile technical_v2 --once
streamlit run app_v2.py
```

Real mode requires a migrated V2 database and `TUSHARE_TOKEN`; Jev additionally
requires `TYPESAFE_API_KEY`. Missing prerequisites remain typed and never fall
back to synthetic data. Formula and Jev paper accounts are independent. The UI
reads immutable artifacts only. Live trading is not connected.
