# Technical V2 Evaluation Report

The deterministic `DEMO_ONLY` run used 641 stored sessions (640 initial plus one
incremental session), six synthetic stocks, and one synthetic sector. It
materialized 13,461 forward-label rows and found 615 mature signal dates. The
fixed 252/63/63/126 protocol and label-end purge passed.

Formula candidates F0/F1/F2 created no validation trades, so each failed the
frozen minimum closed-trade and trade-date gates. F0 remains an explicitly
unvalidated reference; it is not reported as selected evidence. Historical Jev
predictions were unavailable, so J0/J1 remained
`UNAVAILABLE_CACHED_HISTORY`. The final test was not opened.

M0 and the legacy execution diagnostic each closed 62 synthetic validation
trades. Their results are diagnostics only and are not used to claim real
performance. The exact computed rows are in `candidate_comparison.csv` in the
immutable evidence package.

Current forward paper evidence is also insufficient: the current publication
has not matured, no executable formula edge was available, and demo Jev is
network-disabled. The two accounts therefore contain decisions and valuations
but no orders or fills. No return, IC, probability, or trading result in this
report is presented as real-market evidence.
