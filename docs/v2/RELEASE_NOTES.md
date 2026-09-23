# Technical V2 Research Prerelease

This prerelease contains the Technical V2 code, frozen configuration, tests, documentation, and a deterministic synthetic flow artifact. It does not contain raw licensed market data, credentials, user accounts, holdings, or full Jev states.

Status at build time:

- Software: COMPLETE, 166 tests passed.
- Real data: UNAVAILABLE in the build worktree.
- Formula: UNVALIDATED_DEFAULT (F0 reference, no real return result).
- Jev: UNAVAILABLE_CREDENTIALS; no online or paid request was made.
- Evaluation: NOT_RUN_WITH_REASON; final test unopened.
- Live trading: NOT_CONNECTED.

Use `python -m scripts.v2 doctor --mode real` before any real run. The demo proves serialization and workflow only and must not be interpreted as investment performance.
