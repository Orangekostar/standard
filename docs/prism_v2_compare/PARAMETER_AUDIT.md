# Frozen Parameter Consumer Audit

This records the roles of every configuration section before opening the actual
comparison test. Runtime numbers come from `configs/prism_v2_compare_v1.json`.
Native F0/intent/bin constants are constrained to the original implementation,
not reimplemented as different tunable parameters. No parameter search is run.

| Configuration paths | Consumer / constrained meaning |
| --- | --- |
| schema_version, experiment_id, origin | Serialized configuration/binding/provenance; origin is validated by load_config |
| source.repository, branch, commit, prism_repository, prism_commit, output_branch | Source audit/baseline identity and delivery provenance; fixed source SHAs validated |
| network.paid_calls, jev_in_primary_comparison, auto_full_market_resync | Forbidden by load_config; comparison routes contain no market resync or inference calls |
| network.keep_existing_jev_code | Validated true; existing Jev modules are preserved |
| data.mode, snapshot, universe, as_of | Validated real/read-only online-backup/available-universe/audited-cutoff contract |
| data.history_sessions_reference | Provenance-only approximate800 reference; never chooses cutoff or fabricates history |
| data.warmup_sessions | Original WARMUP_SESSIONS validation and fixed-split warmup; smoke start |
| data.missing_day_features, feature_cache_format, shared_comparison_price | Validated NaN/parquet/raw-times-contemporaneous-factor modes; align_panel and cache reader |
| data.corporate_actions_required_for_unqualified_net_pnl_claim | Validated true; audit/action scope flags remain binding on the verdict |
| data.min_context_member_coverage, min_sector_members | _cache_features forwards to the original shared build_context |
| data.risk_warning_unknown | _Replay._eligibility shared historical-risk gate; frozen value BLOCK_NEW_ENTRIES |
| data.feature_chunk_stocks | _cache_features bounded stock partition size |
| split.counts | Exact original build_fixed_split counts252/63/63/126 validated |
| split.test_reporting_blocks | test_subperiods; exact63/63 validated; second block includes the same account's settlement tail |
| split.purge | Validated strict label-end boundary; fit_common_bins/_training_rows and fixed_split_manifest |
| split.test_account_continues_across_reporting_blocks, account_reset_between_validation_and_test | Validated modes; one account per whole cell, distinct accounts per split |
| split.final_tail_sessions | replay windows and replay_frames; exact6 validated |
| split.respect_prior_test_exposure, rewrite_old_final_test_flags | Validated modes; separate old execution/summary audit, no old artifact writes |
| split.open_comparison_test_once_after_freeze | Validated mode; test requires hash-bound protocol and reuses completed cell hashes |
| strategies.A0.formula, primary_horizon, use_common_train_return_bins | F0/h5/shared-train contract validated; score_features, fit_common_bins and replay_cell |
| strategies.A0.price_triggered_exit, score_actions, max_planned_holding_sessions | Validated no-price-stop/native-intent/five-session modes; _decide and actual-fill expiry |
| strategies.B0.formula, primary_horizon, use_common_train_return_bins, max_planned_holding_sessions | Same validated F0/h5/train/five-session contract as A |
| strategies.B0.enable_market_state, enable_adaptive_stop, enable_cooldown | Explicit _Replay feature flags; all-disabled equivalence is tested |
| strategies.B0.market_state.efficiency_sessions, breadth_delta_sessions, volatility_sessions, prior_vol_median_sessions | market_regimes rolling/lagged window calculations with full minimum windows |
| strategies.B0.market_state.stress_breadth_below, stress_breadth_delta_at_most, stress_vol_ratio_at_least | market_regimes stress comparisons after context validity |
| strategies.B0.market_state.trend_er_at_least, trend_log_return20_above, trend_breadth_delta_at_least | market_regimes trend comparisons |
| strategies.B0.market_state.risk_multiplier.* | market_regimes map and finalized _allocate quantity multiplier |
| strategies.B0.market_state.zero_er_denominator, incomplete_reference_window | ER zero rule consumed; UNKNOWN fallback mode validated |
| strategies.B0.adaptive_stop.base_atr_multiple, vol_ratio_clip, distance_fraction_clip, missing_reference_k | adaptive_distance computes bounded distance and fallback reason |
| strategies.B0.adaptive_stop.prior_vol_median_sessions | compute_shared_features lagged Q02 rolling median; no today in reference |
| strategies.B0.adaptive_stop.update_at, execution, tighten_only, trailing_formula, per_lot | Validated close/next-open/tighten-only/per-lot contract; update_stop and _decide |
| strategies.B0.cooldown_full_sessions, reentry_condition | entry_allowed uses cooldown count after actual economic full flat; reference relation validated |
| strategies.B0.online_threshold_evolution | Forbidden by load_config; no evolution/search path |
| strategies.C0.policy, lambda_source, scaled_limits, do_not_scale_returns_after_backtest, refit_lambda_in_test | Validated A/four-cap/validation-only/no-terminal-scaling modes; _Replay scale and freeze |
| strategies.C0.lambda_clip, zero_denominator_lambda | exposure_control validation ratio, zero-exposure status and clipping |
| shared_portfolio.initial_capital_cny, max_names | Historical logical initial-capital ledger and finalized name constraint |
| shared_portfolio.max_stock_weight, max_sector_weight, market_breadth_breakpoints, market_gross_caps | _allocate capacity constraints, including unlisted confirmed share exposure |
| shared_portfolio.stock_risk_budget, max_adv_participation | _allocate risk-distance and ADV quantity bounds |
| shared_portfolio.net_edge_min_exclusive | Validated native exclusive0.001; reference and quantity-specific edge gates |
| shared_portfolio.one_day_new_buy_validity, sell_before_buy, do_not_spend_expected_exit_proceeds | Validated modes; shared execution expiry/priority and close-time cash reservation |
| shared_portfolio.block_new_entry_on_pending_exit, block_same_session_sell_and_rebuy, security_rules | Validated modes; pending-exit merge, shared executor and dated security_rule adapter |
| shared_portfolio.fixed_atr_multiple, minimum_risk_distance_fraction | A risk-distance _distance, not a new A price-exit rule |
| shared_portfolio.buy_ceiling_max_fraction, buy_ceiling_atr_fraction, reduce_quantity_fraction, tick_size_cny | _allocate ceiling, _decide reduction and dated security_rule tick |
| costs.commission_each_side, minimum_commission_cny, sell_addon_rate, other_rate_each_side, slippage_cases.* | FeeSchedule, exact fee/affordability calculation and actual open execution |
| costs.reference_round_trip_cost_* | reference_cost consistency validated; shared pre-quantity expected edge |
| costs.basis, slippage_accounting, quantity_specific_fee_check | Validated research-assumption/fill-once/exact-fee modes |
| evaluation.splits, cost_cases, main_replay_cells, threshold_search_trials | Validated fixed matrix/twelve/zero-search; stage scheduling |
| evaluation.min_closed_trades_for_winner, min_traded_dates_for_winner | Per-cell evidence status and validity-first comparison_verdict |
| evaluation.drawdown_tolerance_absolute, return_tie_tolerance | comparison_verdict risk/return/tie and independent evidence comparisons |
| evaluation.bootstrap_seed, repetitions, block_sessions, method | paired_bootstrap RNG/common-date block sampling; method validated |
| evaluation.annualization_sessions, risk_free_rate | calculate_portfolio_metrics annualization; zero risk-free contract validated |
| evaluation.always_export_zero_trade_funnel | Validated true; per-cell funnel, report gate summaries and explicit zero-trade explanations |
| resources.smoke_stocks, smoke_sessions | _smoke offline subset before shared full-market preparation |
| resources.parallel_replays, max_cpu_threads | configure_resources bounds; _cells max two; numeric-library/Arrow pools fixed conservatively to one each |
| resources.available_ram_fraction | _memory_budget/_check_memory; actual peak/budget recorded in feature_manifest |
| resources.gpu_required, production_worker_changes, production_ui_changes | Validated false; no GPU/Worker/UI or production publication paths |
| resources.new_business_test_families | Verification-scope declaration; ten numbered families mapped by completion audit, not a profit metric |
| delivery.docs_dir, artifact_root | Docs mode validated; _initialise isolated result destination |
| delivery.max_archive_part_mib | package read from bound parameters; part limit at most40MiB and restoration checked |
| delivery.upload_raw_vendor_db, force_push, auto_merge, activate_production_winner | Validated false; manifest whitelist and explicit non-force branch push |
| delivery.verify_remote_head | Validated true; actual ls-remote/publish receipt verification |
| security_rule_versions.* | security_rule effective-date/board selection, verified flag, minimum/increment/max quantities and source/version |
| native_contract.* | Exact original factors/risks/groups/F0 weights/horizons/score/bin/sample/shrinkage/label-delta contract validated against original code; original functions are reused |

Here A0/B0/C0 abbreviate their full strategy IDs only in the table. Parameters
that describe a mode are validated; runtime numeric policy values are read by
their consumers. Approximate historical length and provenance labels are not
secret selectors, alternate costs, live activation switches or tunable policies.
