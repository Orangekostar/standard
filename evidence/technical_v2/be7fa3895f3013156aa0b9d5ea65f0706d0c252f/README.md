# Technical V2 Runtime Evidence

- Code commit: `be7fa3895f3013156aa0b9d5ea65f0706d0c252f`
- Generated: `2026-09-23T08:40:08Z`
- Mode: `DEMO_ONLY`
- Analysis run: `run-17790695de0951d52c46`
- Evaluation run: `evaluation-20260923-429241fabcfef0c0`

This directory records a clean, deterministic runtime verification. It contains
derived evaluation tables, audit summaries, and UI screenshots. It does not
contain a market database, raw licensed data, credentials, Jev request state,
user holdings, or live orders.

The 640-session fixed protocol found 615 mature signal dates and created all 14
required result files. The split passed. Formula candidates produced no closed
validation trades and therefore none passed the frozen support gates. Jev
historical cache was unavailable by design. Selection remained
`NO_ELIGIBLE_CANDIDATE` and the final test stayed unopened.

The seven-node background worker DAG completed from a clean 90-session demo
root. Jev reported the expected typed `PARTIAL` runtime state while the worker
node remained successful; publication completed with one consistent binding.
