# Mainboard Universe

New analysis, JEV inputs, and BUY/ADD orders use non-risk-warning SSE/SZSE mainboard stocks only. ChiNext (300/301), STAR (688/689), BSE, and ST securities are excluded. Existing positions retain valuation and exit processing.

The comparison default is `configs/prism_v2_compare_mainboard_v2.json`. Use a new experiment root; do not reuse `frozen-20260928-v1`. Universe/configuration changes invalidate old feature and replay bindings. Costs, edge thresholds, factors, and splits are unchanged.

The original `prism_v2_compare_v1.json` and completed experiment are archival evidence. Reproduce them using their original source revision. Today's ST name is not backfilled into historical dates; unknown historical risk-warning status still blocks new entries.
