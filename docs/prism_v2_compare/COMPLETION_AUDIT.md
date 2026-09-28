# Original ZIP Completion Audit

Status: COMPLETE_GIT_FALLBACK. The actual comparison, restored delivery and
remote Git publication are verified. PR/Release creation failed401 and the
specified fallback was completed. The primary agent owns the semantic and
evidence review; no final review was delegated.

## Authoritative Scope

Original instructions are preserved byte-for-byte in
`instructions/prism_v2_backtest_codex/`. The454-line main specification and
machine-readable plan, not the implementation checklist, define completion.
The scope includes actual twelve-cell historical replay, every named artifact,
account verification, figures, handoff, restoration and actual GitHub receipts.
Zero trades and scope-limited evidence are valid results, not permission to
change the frozen gates or declare a winner.

SOURCE_COMMIT: `410de2d9b5d3cf380d0d759ea7652bb541956fd6`.
Implementation SHA256: `491dd1a2ed1413274018972631011a7c707f86e665aceb4d31a5cf24a4192b59`.
Frozen snapshot SHA256: `2e2598e9e115d3110f3031a710d1997e74ee374c51db73a4cef99939054ff820`.
Run: `prism-v2-20260928-54f76420f1a45b76`.

## Section Evidence

| ZIP section | Requirement and direct evidence | State |
| --- | --- | --- |
| 0 | Runnable fixed A/B/C comparison;12 real cells, actual reports, verified restoration and actual remote Git delivery | VERIFIED |
| 1 | Fixed V2/Prism source identities, required function mapping and MIT source attribution in SOURCE_AUDIT.md; independent linked worktree verified | VERIFIED |
| 2 | A/B share original F0 factors/scores/classification/train bins; C is A's four-cap budget control; native reference is provenance-only | VERIFIED_SOURCE_AND_FIXTURES |
| 3.1 | Explicit existing venv; no environment upgrade or bare Conda python; plotting dependencies isolated | VERIFIED |
| 3.2 | Read-only sqlite3.Connection.backup, immutable snapshot SHA/size/time/schema; independent lock and accounts; no production path targets | VERIFIED_SOURCE_AND_SNAPSHOT |
| 3.3 | Audited D20260928,801 matching exchange sessions,5349-code roster, real-only OHLCV; unknown historical risk/universe/action limitations explicitly retained | VERIFIED_DATA |
| 4.1 |252/63/63/126,120 warmup, strict boundary purge, train-only bins,57 validation/126 test signals, six-session tails; actual63/132 common NAV dates checked | VERIFIED |
| 4.2 | Old demo identity and all-split IC exposure disclosed separately; no old selection/publication writes; actual freeze11:55:56.462186 precedes test11:55:56.503642UTC | VERIFIED |
| 5 | Native15 factors/F0/h1,3,5 and holdings-aware intent; A no price exit; separate lot expiry; original .00212 not main .00312 | VERIFIED_SOURCE_AND_FIXTURES |
| 6.1 | Causal20/5/60 market windows, UNKNOWN precedence, fixed stress/trend/range quantity-only multipliers | VERIFIED_SOURCE_AND_FIXTURES |
| 6.2 | B bounded adaptive distance, finalized sizing, per-lot tighten-only close stop, next-open execution, raw accounting/common adjustment scale, exit deduplication | VERIFIED_SOURCE_AND_FIXTURES |
| 6.3 | Actual economic-flat cooldown, +2 blocked/+3 allowed; partial/pending exit does not arm it or block required sells | VERIFIED_SOURCE_AND_FIXTURES |
| 7.1 | Shared account/execution engine, close-to-next-open, T+1, legal quantity, one-day buys, exit retry/idempotency, no expected-cash spending | VERIFIED_SOURCE_AND_FIXTURES |
| 7.2 | Exact commissions/minimum/other/sell fees, base/stress .00312/.00512 reference, slip-once fills, quantity-specific edge; dated verified board adapter | VERIFIED_SOURCE_AND_FIXTURES |
| 7.3 | Cash+positions+confirmed rights; MARK_ONLY/unresolved delist preserved;12 real isolated ledgers/NAVs reconcile to100000000 cents cash with no unresolved assets | VERIFIED |
| 8 | A/B base validation only; actual0/0 => C1/unidentifiable; exactly12 cells with2 reused, no stress/test refit/search;50x160 smoke and54-chunk full preparation | VERIFIED |
| 9.1 | Exact28 fields checked; continuous63/63 blocks plus6-session tail, all attribution CSVs; three1632x960 PNGs pixel-checked and personally viewed | VERIFIED |
| 9.2 | Same132 test-session paired blocks10/N1000/seed20260928; actual B-A0,95% CI[0,0]; no independently sampled stock rows | VERIFIED |
| 9.3 | All12 cells valid accounts but zero trades => INSUFFICIENT_TRADING_EVIDENCE; no winner, all independent leader evidence false, scope_limited true | VERIFIED |
| 9.4 | Actual673974 test roster/40340 candidates/net-edge pass0/orders0; independent historical-risk block;40230 adjusted labels explicitly not account returns; undefined metrics null | VERIFIED |
| 10 | Every T0-T5 module and five stage commands plus strict --all are implemented; parameter consumers in PARAMETER_AUDIT.md | VERIFIED_SOURCE_AND_CLI_FIXTURES |
| 11 | All10 directed families below; related regression236 tests/113.988s/exit0 before formal test; tests are not profit evidence | VERIFIED_TESTS |
| 12 | All named root/per-cell files;161 manifest-record hashes checked,162 archive files restored; handoff/commands/logs/train model delivered; large details all ARCHIVED_GIT | VERIFIED |
| 13 |83 actual changed files size/SHA/Git-blob verified against pushed/fetched be0f851;7 <=40MiB parts restored; PR/Release POST401 recorded, Git fallback completed | VERIFIED_GIT_FALLBACK |
| 14 | Insufficient-history NOT_RUN/null path tested; actual data sufficient; unknown risk/actions do not trigger fake fills or threshold relaxation | VERIFIED_SOURCE_AND_FIXTURES |
| 15 | Fixed source/license and dated official rule evidence linked in SOURCE_AUDIT.md and bound parameters; no 2026 rules applied before effectiveness | VERIFIED_SOURCE |

## Ten Directed Test Families

These are minimal business fixtures plus the applicable existing V2 tests. The
formal historical matrix is separate evidence. Exact test methods are retained
in the source; the regression receipt contains the command, code hash and count.

| Family | Direct assertions and tests |
| --- | --- |
| 1 | test_prism_data: future adjustment/feature invariance and test-return-independent training; test_prism_policy: future-append causal state; test_prism_comparison: frozen C remains unchanged |
| 2 | test_prism_replay.test_disabled_b_has_identical_actions_orders_and_nav_to_a |
| 3 | test_prism_replay: close-stop next sellable open, same-day feature validity cannot decide open; test_execution_v2.test_same_day_buy_is_not_sellable_and_next_session_is |
| 4 | test_prism_policy fee reference and test_prism_replay minimum-commission/stress quantity-specific rejection; test_prism_shared_execution slipped-limit checks |
| 5 | test_prism_replay: distinct-lot expiry, expiry/stop/reduce deduplication, partial reduction and pending reason upgrade; shared execution selected-lot/idempotency checks |
| 6 | test_prism_policy: distance bounds/fallback/tightening/NaN; test_prism_replay: ex-dividend scale and one economic trade; test_corporate_actions: entitlement valuation without NAV jump |
| 7 | test_prism_policy +2/+3 boundary; test_prism_replay actual full sale versus pending/partial sale and bonus-right cooldown |
| 8 | test_prism_data shared missing/real-bar/board statuses; test_prism_replay carried mark versus unresolved delist; test_corporate_actions unavailable price retained |
| 9 | test_prism_replay independent account IDs, prefilter versus orders, ledger reconciliation and unlisted-right capacity; test_prism_comparison exact common dates/continuous subperiods |
| 10 | test_prism_comparison validation under NO_NETWORK_ALLOWED, immutable report/protocol/hash checks; test_prism_delivery tamper and raw/credential rejection; production route diff remains empty |

## Actual Runtime And Delivery Evidence

- `VERIFICATION_EVIDENCE.json`:12 cells/accounts,6258330 decisions,161 file
  hashes, identical raw scores/prediction statuses, reconciled common-date cash
  NAVs, empty account vendor tables, three nonblank reviewed1632x960 figures.
- `RESTORATION_EVIDENCE.json`: all162 files restored,7 parts <=40MiB,
  archive280802755 bytes/SHA256
  `f859bd90647573e5d46b27ddf067153617390465e53564cd7baa056e8e132d5f`.
- `COMPARISON_EXECUTION.log`: actual prepare/validate/freeze/test/report complete,
  wall722s, peak1845977088 bytes,195% CPU, exit0/API0. Feature preparation was
  shared and reused, not included in this wall time.
- `RESUME_EXECUTION.log`: actual completed test resume reused12/12 cells;
  no different test, refit or additional threshold-search trial.
- `COMMON_RETURN_BINS.json` SHA256
  `633438051caef3ce3b0054a501222481b2639e2090b0fc34d2bbe6b68f430ee7`
  matches the frozen dataset; `TRAIN_FIT_EVIDENCE.json` records strict purge.
- `GITHUB_API_ATTEMPTS.json`: actual PR/Release POSTs each failed401
  Requires authentication; prescribed Git fallback selected, no invented URLs.
- `GIT_DELIVERY_EVIDENCE.json`: actual non-force push, matching ls-remote/fetch
  commitbe0f851,83 changed-file size/SHA256/Git-object bindings including all
  seven parts. Source410de2d is separate from later result/document delivery.
- `DELIVERY_VERIFICATION.json`: fresh pre-delivery related regression236 tests/
  114.360s/exit0, same implementation; original pre-test artifact receipt retained.
- `HANDOFF.md`: actual A/B/C table, differences, dates/scope/old exposure,
  full commands/environment/runtime, zero-edge/risk diagnosis, failures and
  recovery, package binding and research-only recommendation.

No SOURCE_AUDIT/shared_fixes input changed after validation initialization.
Implementation SHA256 stayed491dd1a2... through test and review. There are no
TEST_EXPOSED_BUGFIXes or asymmetrically recomputed groups. All named T0-T5
locations and five commands/--all have fixture and actual pipeline evidence;
every active parameter has a consumer in PARAMETER_AUDIT.md, and native/source
identity values are explicitly provenance-only where appropriate.

Actual initial delivery commit:
`be0f85117f8daf92438edfd4053045ecc9975499`. Subsequent completion-document
changes do not alter frozen source/data/config/results. The external local
`cache/experiments/prism_v2/frozen-20260928-v1/publish_receipt.json` records the
final delivery commit, matching remote HEAD and actual uploaded files with
bytes/SHA256, without a self-referencing committed hash.

No required derived assets are LOCAL_ONLY: all162 archive files are delivered.
Private raw vendor snapshot, shared feature cache, local restored copy, lock
and credentials remain LOCAL_ONLY by design. The production branch is still
0809f0e, its existing latest.json change is preserved, and no merge, force push,
production publication, paid Jev call, resync or policy activation was performed.
