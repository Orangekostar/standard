# Prism / Technical V2 Research Handoff

Status: COMPLETE_GIT_FALLBACK. The full fixed matrix, accounts, figures and
restored package are verified and delivered through the research branch.
PR/Release creation failed401; the specified Git fallback was completed.

| Test/base strategy | Net return | Drawdown | Sharpe | Mean exposure | Closed trades |
| --- | ---: | ---: | ---: | ---: | ---: |
| A0_V2_F0 | 0.00% | 0.00% | null | 0.00% | 0 |
| B0_PRISM_A_SHARE_V1 | 0.00% | 0.00% | null | 0.00% | 0 |
| C0_V2_EXPOSURE_CONTROL | 0.00% | 0.00% | null | 0.00% | 0 |

Verdict: `INSUFFICIENT_TRADING_EVIDENCE`, not TIE or a Prism win. All twelve
validation/test/base/stress cells remain at CNY1000000 cash, with no orders,
fills, fees, slippage, unresolved assets or closed trades. Accounting is valid,
but trading evidence is insufficient and all five data-scope limitations apply.
Sharpe, win rate, profit factor and unfilled-order ratio remain null.

## Source And Strategy Identity

SOURCE_COMMIT: `410de2d9b5d3cf380d0d759ea7652bb541956fd6`.
Implementation SHA256: `491dd1a2ed1413274018972631011a7c707f86e665aceb4d31a5cf24a4192b59`.
Fixed native V2: `0809f0ef38e2e776cc80b231d3e188edcddf3173`.
Fixed Prism inspiration: `d15c63ef7a5d426ad71f4bbf4dae05c94ba0bc02`.
Run: `prism-v2-20260928-54f76420f1a45b76`.

A uses the original fifteen directional factors, F0 scores at1/3/5 sessions,
holdings-aware intent, shared train bins, score exits and actual-entry+5-session
lot expiry. It does not acquire a price-triggered exit. B uses exactly the same
raw signals and adds only fixed market-state new-buy sizing, bounded adaptive
risk distance/tighten-only close-stop exits, and two full sessions after actual
full liquidation. UNKNOWN state does not relax shared eligibility constraints.
C is A with four caps scaled by the frozen validation exposure ratio, not
terminal return rescaling. The old native reference is provenance-only: its
.00212 cost, universal100-unit convention and earlier demo data are not a
different-date numerical competitor. All parameter roles are in
`PARAMETER_AUDIT.md`; common corrections are in immutable `shared_fixes.md`.

This is `RESEARCH_ADAPTATION_NOT_PRISM_OFFICIAL_A_SHARE_STRATEGY`. No Jev, LLM,
online threshold evolution, directional-factor change or policy search is used.
No production winner is activated; UI/Worker, existing Jev routes and production
locks/publication are not changed by the experiment.

## Frozen Data And Dates

Worktree: `/home/ww/vv/quant/.worktrees/prism-v2-backtest`.
Source database explicitly overridden to
`/home/ww/vv/quant/.worktrees/technical-v2/cache/v2/market.db`.
Frozen experiment root: `cache/experiments/prism_v2/frozen-20260928-v1`.
Snapshot: `market_snapshot.db`,4093435904 bytes, read-only permissions0444.
SHA256: `2e2598e9e115d3110f3031a710d1997e74ee374c51db73a4cef99939054ff820`.
Backup UTC:20260928 09:58:24.768448 through09:58:45.866884.
Method: read-only SQLite online backup, not a live bare-db copy.

The snapshot contains4314526 real raw rows,801 matching SSE/SZSE sessions
from20230612 to audited D20260928,5349 roster codes and5222 current instrument
versions. The native combined-universe sync audit uses the SSE calendar key;
latest coverage is1.0 (5221 expected,5209 observed,12 known suspended). It is not
two separate exchange audit series. Reconstructed SW_L1 membership is available,
but complete historical delisted universe, point-in-time data vintages,
historical risk-warning flags and corporate-action records are not.

Scope flags remain `UNIVERSE_HISTORY_LIMITED`,
`HISTORICAL_DATA_REVISIONS_NOT_POINT_IN_TIME_VINTAGES`, `REUSED_HOLDOUT`,
`HISTORICAL_RISK_WARNING_UNKNOWN`, and
`RAW_PRICE_LEDGER_CORPORATE_ACTIONS_INCOMPLETE`. Missing historical risk metadata
uses the frozen shared BLOCK_NEW_ENTRIES rule. It is never inferred from today's
name or relaxed to manufacture trades. Raw-price accounting with absent company
actions cannot justify unqualified net-P&L superiority; adjusted signal labels
are a separate diagnostic, not realized account returns.

| Split | First assigned signal | Assigned dates | Allowed replay signals | Common account dates |
| --- | --- | ---: | ---: | --- |
| Train |20240821 |252 | Train-only strict label purge | No comparison account |
| Calibration |20250904 |63 | Temporal isolation, no refitting | No comparison account |
| Validation |20251210 |63 |57 through20260310 |20251210-20260318,63 sessions |
| Test |20260319 |126 |126 through20260917 |20260319-20260928,132 sessions |

Every split has120 real-session warmup. Training uses1245043 rows/246 purged
signal dates; latest training label end20250903 is before calibration20250904.
Validation excludes six boundary signals so expiry never crosses test. Test
has one continuous account per strategy/cost; two63-signal reporting blocks do
not reset it, and the second includes the six-session settlement tail.

Old portfolio test execution is UNKNOWN_FOR_REAL_DATA (located demo artifacts
say false). Old summary exposure is UNKNOWN_FOR_REAL_DATA, with all-split IC in
located demo evidence. This is a reused historical holdout, not an untouched
independent blind test. Old final_test_opened/selection files are preserved.

Protocol froze at2026-09-28T11:55:56.462186+00:00; new test opened at
2026-09-28T11:55:56.503642+00:00. A/B validation mean exposure was0/0, so
lambda_C=1 and `UNIDENTIFIABLE_ZERO_BASE_EXPOSURE`, exactly the prescribed
fallback. Neither stress nor test refits it. Successful A/B base validation
cells are reused rather than recomputed.

## Environment And Commands

Interpreter `/home/ww/vv/quant/.venv/bin/python`: Python3.13.13,
NumPy2.5.1, pandas3.0.3, pyarrow24.0.0, SQLite3.53.4. No production dependency
upgrade. Plotting libraries are isolated in `cache/experiments/prism_v2/plotting`,
pinned by `configs/prism_v2_plotting_requirements.txt` (Matplotlib3.10.8).
This is an offline CPU CLI, not a new frontend or server.

Actual regression command (236 passed/113.988s/exit0):

```bash
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 TMPDIR=/dev/shm /home/ww/vv/quant/.venv/bin/python -m unittest discover -s tests/v2 -p 'test_*.py'
```

A fresh pre-delivery execution of the same command passed236 tests/114.360s/
exit0 without implementation changes; see `DELIVERY_VERIFICATION.json`.
The original pre-test receipt remains immutable in the artifact manifest.

Actual formal pipeline command, executed from the experiment worktree:

```bash
bash -o pipefail -c 'OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 /usr/bin/time -v /home/ww/vv/quant/.venv/bin/python -u -m scripts.compare_prism_v2 --all --source-db /home/ww/vv/quant/.worktrees/technical-v2/cache/v2/market.db --experiment-root cache/experiments/prism_v2/frozen-20260928-v1 --config configs/prism_v2_compare_v1.json 2>&1 | tee cache/experiments/prism_v2/frozen-20260928-v1/comparison_execution.log'
```

Equivalent supported stage commands for local resume, not extra experiments:

```bash
/home/ww/vv/quant/.venv/bin/python -m scripts.compare_prism_v2 prepare --source-db /home/ww/vv/quant/.worktrees/technical-v2/cache/v2/market.db --experiment-root cache/experiments/prism_v2/frozen-20260928-v1
/home/ww/vv/quant/.venv/bin/python -m scripts.compare_prism_v2 validate --experiment-root cache/experiments/prism_v2/frozen-20260928-v1 --config configs/prism_v2_compare_v1.json
/home/ww/vv/quant/.venv/bin/python -m scripts.compare_prism_v2 freeze --experiment-root cache/experiments/prism_v2/frozen-20260928-v1 --config configs/prism_v2_compare_v1.json
/home/ww/vv/quant/.venv/bin/python -m scripts.compare_prism_v2 test --experiment-root cache/experiments/prism_v2/frozen-20260928-v1 --frozen-manifest artifacts/prism_v2_compare/prism-v2-20260928-54f76420f1a45b76/protocol_frozen.json
/home/ww/vv/quant/.venv/bin/python -m scripts.compare_prism_v2 report --experiment-root cache/experiments/prism_v2/frozen-20260928-v1
```

Reusing the same local namespace verifies snapshot/cache/cell hashes and does
not resync live data or perform new model/policy selection. Preserve the actual
SOURCE_COMMIT and parameter/implementation hashes. A changed implementation
invalidates all affected groups symmetrically; partial attempts are preserved
under failed_attempts, not silently overwritten. Restored derived results can be
byte-verified without the private vendor database; replaying the identical data
requires the retained lawful local snapshot/cache. A new live snapshot belongs
in a new experiment namespace, not this frozen run.

## Preparation, Failures And Recovery

Real offline smoke:50 codes x160 sessions,0 API calls. First completed profiled
run37.169s/peak RSS1385656320 bytes; refreshed run9.124s. The initial failed
smoke was a stock/STOCK adapter case mismatch, corrected before formal test.
Status aggregation's native latest-non-null semantics were preserved while
replacing slow Python aggregation; its directed regression remains passing.

Full-market features:54 chunks of at most100 stocks,1811.501s, peak RSS
3484811264 bytes against frozen available-RAM budget120784975872 bytes.
Features/context are computed once and shared, not separately per strategy.
The formal run reuses that preparation. At most two replay processes and eight
CPU threads; numeric and Arrow pools are conservatively limited to one each.
No GPU, paid inference or market resynchronization occurs.

Before test, directed failures exposed unsupported config modes, report audit
binding, insufficient-history delivery, unlisted-share allocation capacity and
tail-roster funnel denominators. Fixes are symmetric and recorded in the source
audit/tests. These were not TEST_EXPOSED_BUGFIXes. Matplotlib/NumPy deprecation
and Streamlit bare-mode context warnings are nonfatal dependency warnings.
The formal --all pipeline completed in722.00s wall time,1372.57s user time,
38.27s system time,195% CPU, peak process RSS1845977088 bytes, zero swaps and
zero market/inference API calls. Full feature preparation was already complete
and reused, so its1811.501s is not part of this pipeline wall time. Exactly two
successful validation/base cells were reused by test; the other ten were new.
An actual subsequent test command reused all12 completed hash-bound cells.
There were no post-test implementation changes or TEST_EXPOSED_BUGFIXes.

## Gate Diagnosis And Evidence

Each test group starts with673974 allowed-signal roster rows.646722 have valid
factors,61091 pass65/55,40340 also pass the extension/return-estimate gates, but
zero pass the frozen exclusive net-edge threshold after base/stress costs.
Therefore there are no eligible allocations or orders even before considering
the independently blocking unknown historical risk-warning flags. B additionally
blocks new risk on320940 STRESS roster rows. All-blocker occurrences are
nonexclusive all-roster counts, not conditional rejection percentages.

The common train-only score65-80 bin mean is0.0037666740741088057 before costs;
the base reference cost is0.00312 and the exclusive minimum edge is0.001.
The score80-100 bin mean is negative. The immutable shrunken train estimates,
not test labels, drive these gates. `COMMON_RETURN_BINS.json` is the exact
frozen model, SHA256
`633438051caef3ce3b0054a501222481b2639e2090b0fc34d2bbe6b68f430ee7`;
`TRAIN_FIT_EVIDENCE.json` records1245043 train rows and246 purged dates.

The fixed adjusted five-session test-label diagnostic has40230 observations,
mean-0.200691%, median-1.1189%, p95 18.4498%. These are signal diagnostics,
not realized account returns or evidence of improved B prediction. There is
no realized sector, regime or exit P&L to attribute. Paired132-session moving
blocks10/N1000/seed20260928 give B-A0 and95% CI[0,0]; every independent leader
evidence field is false. Stress costs and continuous test halves do not create
trading evidence.

Primary verification checked161 manifest-listed files by size/SHA256,12 isolated
accounts,6258330 decision records, every common-date cash/NAV ledger, and empty
vendor tables in the account databases. A/B/C raw scores and prediction statuses
are byte-identical by split. Three1632x960 PNGs passed nonblank pixel checks and
were visually reviewed for framing, labels and overlap. Direct evidence is in
`VERIFICATION_EVIDENCE.json` and `COMPLETION_AUDIT.md`.

Actual account/artifact verification and packaging commands:

```bash
/home/ww/vv/quant/.venv/bin/python docs/prism_v2_compare/verify_fixed_results.py --artifact-dir artifacts/prism_v2_compare/prism-v2-20260928-54f76420f1a45b76 --evidence docs/prism_v2_compare/VERIFICATION_EVIDENCE.json
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 /home/ww/vv/quant/.venv/bin/python -m scripts.package_prism_v2 pack --artifact-dir artifacts/prism_v2_compare/prism-v2-20260928-54f76420f1a45b76 --output-dir artifacts/prism_v2_compare/prism-v2-20260928-54f76420f1a45b76/packages
/home/ww/vv/quant/.venv/bin/python -m scripts.package_prism_v2 restore --manifest artifacts/prism_v2_compare/prism-v2-20260928-54f76420f1a45b76/packages/delivery_manifest.json --output-dir cache/experiments/prism_v2/restored-delivery-410de2d-v1
```

The verification script asserts this observed all-cash run; it is not a generic
profit validator. Preserve the existing restoration and use a new empty output
directory to repeat restore. Pack verifies and reuses the successful archive.
On a fresh checkout, first restore the seven parts, then point the verifier's
`--artifact-dir` to that restored directory; large cell details are archived,
not separately duplicated in Git. Install plotting dependencies only when
absent, using the existing interpreter with
`-m pip install --target cache/experiments/prism_v2/plotting --no-deps -r configs/prism_v2_plotting_requirements.txt`,
never upgrade the production environment. The identical replay additionally
requires the retained lawful local market snapshot/shared cache.
`COMPARISON_EXECUTION.log` and `RESUME_EXECUTION.log` preserve the actual pipeline
and12-cell resume logs without credentials. The completed regression receipt
and per-cell runtime evidence are in the run's `TEST_REPORT.md`.

## Result Package And Git Binding

Run directory: `artifacts/prism_v2_compare/prism-v2-20260928-54f76420f1a45b76/`.
Artifact manifest SHA256:
`03a55af67acf252df578d3e4a14b8a1dad6fc868839de9c762e1d8d6306dd1a7`.
The manifest-whitelisted archive contains all162 files (including that manifest),
280802755 bytes, SHA256
`f859bd90647573e5d46b27ddf067153617390465e53564cd7baa056e8e132d5f`.
Its seven numbered parts are each at most41943040 bytes/40MiB. Actual restoration
to the path above independently matched every file; see
`RESTORATION_EVIDENCE.json` and `packages/delivery_manifest.json` for sizes/hashes.
No required derived detail is omitted: account databases, decision rows and all
other cell outputs are ARCHIVED_GIT, not separately duplicated as large blobs.

Actual GitHub PR and Release POST attempts on2026-09-28 both returned401
`Requires authentication`. No gh CLI, API token or credential-helper authorization
was available. `GITHUB_API_ATTEMPTS.json` records both actual responses. No PR,
Release or attachment URL is fabricated; the prescribed fallback is explicit
core CSV/JSON/report/PNG files and all seven derived parts through Git.

SOURCE_COMMIT is the code used by the frozen protocol. DELIVERY_COMMIT contains
the later results/packages/handoff. The final commit ID, fresh remote HEAD and
actual Git-tracked file list with sizes/SHA256 are recorded externally in
`cache/experiments/prism_v2/frozen-20260928-v1/publish_receipt.json`, avoiding
an impossible self-referencing committed hash. Initial result delivery
`be0f85117f8daf92438edfd4053045ecc9975499` was pushed non-force and verified by
fresh ls-remote, fetch and83 changed-file Git blob/size/SHA256 bindings.
`GIT_DELIVERY_EVIDENCE.json` retains that actual pre-document-update receipt;
the external final receipt additionally binds the subsequent documentation
commit. These checks bind Git objects to the remote commit, not a claim that
every remote blob was independently downloaded again over HTTPS.

Repository branch:
https://github.com/Orangekostar/standard/tree/research/prism-v2-backtest
Fixed actual report:
https://github.com/Orangekostar/standard/blob/be0f85117f8daf92438edfd4053045ecc9975499/artifacts/prism_v2_compare/prism-v2-20260928-54f76420f1a45b76/RESEARCH_REPORT.md
All seven packages and restoration manifest:
https://github.com/Orangekostar/standard/tree/be0f85117f8daf92438edfd4053045ecc9975499/artifacts/prism_v2_compare/prism-v2-20260928-54f76420f1a45b76/packages

```bash
git push origin HEAD:refs/heads/research/prism-v2-backtest
git ls-remote origin refs/heads/research/prism-v2-backtest
git fetch --no-tags origin refs/heads/research/prism-v2-backtest
```

Raw vendor snapshot, feature cache, local restoration, experiment lock and
credentials stay LOCAL_ONLY under the paths above; they are not licensed for
redistribution by this request. The only account DBs in the archive have empty
vendor tables. The production branch remains0809f0e; its user/runtime-modified
latest.json is preserved and no merge, worker restart or live strategy switch
is performed.

Continue research only if lawful historical risk flags, universe/vintage and
corporate-action evidence improve under a new frozen protocol. This run does
not support a production switch, a profitability claim or looser gates.
