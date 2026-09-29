# 沪深主板高胜率策略实验室 v1

执行核验：42项定向/相关回归通过；30股离线smoke完成36账户。正式252账户全部完成，且252账户再次通过哈希复用检查；独立审计5,040次成交、2,520个完整episode全部通过。

市场/行业上下文为冻结成员合成序列，不是上证指数、深成指或官方行业指数。历史ST标记未知占比100%，沿用明确批准的研究口径；缺真实bar约3.734%、行业未知约1.686%、共同排名特征不完整约14.361%，未填造缺失行情。

后段最高观测胜率为S06_STRICT的66.67%，只有3笔交易，Wilson下界20.77%；它不满足样本、盈亏比等门槛。BASE_DEF为本段base收益最高的固定旧对照（6.38%收益、2.28%回撤），stress为0.36%收益、6.50%回撤；这不是后段重新选出的可上线赢家。

结论：NO_HIGH_WIN_CANDIDATE
早期预先冻结唯一主候选：无。
资料限制：EVIDENCE_LIMITED；历史多轮使用（REUSED_HISTORY/REUSED_HOLDOUT），不是全新样本外。
没有达标者时保留原策略配置；这不表示原策略已被证明稳定盈利。本实验没有生产切换。

## 同口径与指标
新候选16组，另有BASE_ENV和BASE_DEF；252独立账户均完成，两个成本分别检查。
每账户初始100万元，最多3只，固定4%收盘止盈、3%收盘止损、实际入场日起第5交易日开盘到期；不可成交则延后。
全部排除已知风险证券、房地产、原始价低于5元及行业未知证券。历史ST、成分和公司行动不完整，不能宣称历史严格剔除ST或完整分红总收益。
新策略按0.5行业内RS60分位+0.3行业20日分位+0.2标准化CLV排序；无旧综合分门槛。BASE_ENV保持原SCORE排序；BASE_DEF保持原DEFENSIVE排序、波动预算与板块分散。
共同修正：评估全部信号日候选后计划；退出优先级为待退出/风险、止损、到期、止盈。对照同步使用修正；无触发差异样例的成交和NAV与原生实现一致。
佣金双边0.0003、每边最低5元；卖出附加0.0005、每边其他费用0.00001。base/stress每边滑点0.001/0.002，只在真实成交价计一次；不同成本可能改变后续交易路径。
交易胜率按完整经济episode扣费净盈亏；零收益不算赢、未成交不算交易。盈利日占比与交易胜率分别报告；本研究没有收益分类模型准确率。
PF无已观测亏损时保存null并标注不可有限估计；零交易胜率null。盈亏比资格按平均episode净回报，另保存资金损益口径。

## 早期六独立窗口
净收益列为六窗口算术平均，不是连续复利；回撤列为六窗口最差回撤。资格必须两种成本同时满足原65%胜率等门槛。

| 策略 | 成本 | 平均净收益 | 汇总胜率 | 平仓数 | Wilson下界 | PF | 最差回撤 | 两成本晋级 |
|---|---|---:|---:|---:|---:|---:|---:|---|
| BASE_DEF | base | 2.67% | 51.39% | 144 | 43.30% | 1.43 | 4.29% | False |
| BASE_DEF | stress | 1.37% | 48.98% | 147 | 41.03% | 1.19 | 4.78% | False |
| BASE_ENV | base | 5.20% | 46.91% | 162 | 39.39% | 1.46 | 7.99% | False |
| BASE_ENV | stress | 2.28% | 44.72% | 161 | 37.25% | 1.19 | 8.60% | False |
| S01_CONFIRMED | base | 2.58% | 50.38% | 133 | 41.99% | 1.31 | 8.12% | False |
| S01_CONFIRMED | stress | 1.53% | 49.62% | 133 | 41.26% | 1.17 | 8.97% | False |
| S01_STRICT | base | 0.13% | 52.63% | 19 | 31.71% | 1.11 | 4.23% | False |
| S01_STRICT | stress | -0.00% | 52.63% | 19 | 31.71% | 1.00 | 4.60% | False |
| S02_CONFIRMED | base | -0.31% | 40.74% | 162 | 33.47% | 0.97 | 9.41% | False |
| S02_CONFIRMED | stress | -0.50% | 40.61% | 165 | 33.41% | 0.96 | 9.89% | False |
| S02_STRICT | base | -0.07% | 45.45% | 11 | 21.27% | 0.83 | 1.40% | False |
| S02_STRICT | stress | -0.13% | 45.45% | 11 | 21.27% | 0.75 | 1.43% | False |
| S03_CONFIRMED | base | -0.24% | 48.57% | 35 | 32.99% | 0.84 | 3.21% | False |
| S03_CONFIRMED | stress | -0.49% | 45.71% | 35 | 30.47% | 0.70 | 3.64% | False |
| S03_STRICT | base | -0.09% | 0.00% | 1 | 0.00% | 0.00 | 0.58% | False |
| S03_STRICT | stress | -0.09% | 0.00% | 1 | 0.00% | 0.00 | 0.60% | False |
| S04_CONFIRMED | base | 5.68% | 55.12% | 127 | 46.44% | 1.87 | 5.89% | False |
| S04_CONFIRMED | stress | 4.24% | 52.34% | 128 | 43.75% | 1.58 | 6.24% | False |
| S04_STRICT | base | 0.50% | 45.45% | 11 | 21.27% | 1.58 | 3.33% | False |
| S04_STRICT | stress | 0.51% | 45.45% | 11 | 21.27% | 1.56 | 3.41% | False |
| S05_CONFIRMED | base | -1.66% | 40.54% | 37 | 26.35% | 0.45 | 4.70% | False |
| S05_CONFIRMED | stress | -1.95% | 40.54% | 37 | 26.35% | 0.39 | 5.36% | False |
| S05_STRICT | base | 0.00% | N/A | 0 | N/A | N/A | -0.00% | False |
| S05_STRICT | stress | 0.00% | N/A | 0 | N/A | N/A | -0.00% | False |
| S06_CONFIRMED | base | 1.00% | 52.78% | 36 | 37.01% | 1.46 | 3.86% | False |
| S06_CONFIRMED | stress | 0.76% | 52.78% | 36 | 37.01% | 1.33 | 4.08% | False |
| S06_STRICT | base | 0.72% | 66.67% | 9 | 35.42% | 2.53 | 2.04% | False |
| S06_STRICT | stress | 0.85% | 66.67% | 9 | 35.42% | 3.92 | 0.95% | False |
| S07_CONFIRMED | base | 0.49% | 41.18% | 17 | 21.61% | 1.39 | 3.26% | False |
| S07_CONFIRMED | stress | 0.36% | 41.18% | 17 | 21.61% | 1.27 | 3.55% | False |
| S07_STRICT | base | -0.36% | 20.00% | 5 | 3.62% | 0.07 | 2.71% | False |
| S07_STRICT | stress | -0.40% | 20.00% | 5 | 3.62% | 0.04 | 2.88% | False |
| S08_CONFIRMED | base | 1.18% | 42.99% | 107 | 34.01% | 1.16 | 9.47% | False |
| S08_CONFIRMED | stress | 0.03% | 41.90% | 105 | 32.92% | 1.00 | 9.99% | False |
| S08_STRICT | base | 0.04% | 100.00% | 1 | 20.65% | 无已观测亏损 | 0.22% | False |
| S08_STRICT | stress | 0.04% | 100.00% | 1 | 20.65% | 无已观测亏损 | 0.24% | False |

## 后段连续132交易日
第66日不重置账户、不停止新买；两个66日分段见review_segments.csv，跨段交易按平仓段列完整episode，NAV收益按实际分段。

| 策略 | 成本 | 累计净收益 | 胜率 | 平仓数 | Wilson下界 | PF | 最大回撤 |
|---|---|---:|---:|---:|---:|---:|---:|
| BASE_DEF | base | 6.38% | 51.52% | 33 | 35.22% | 1.89 | 2.28% |
| BASE_DEF | stress | 0.36% | 35.29% | 34 | 21.49% | 1.03 | 6.50% |
| BASE_ENV | base | 1.52% | 40.00% | 40 | 26.35% | 1.10 | 5.08% |
| BASE_ENV | stress | -0.78% | 37.50% | 40 | 24.22% | 0.95 | 6.02% |
| S01_CONFIRMED | base | -1.60% | 37.04% | 27 | 21.53% | 0.81 | 3.69% |
| S01_CONFIRMED | stress | -3.72% | 32.14% | 28 | 17.93% | 0.61 | 3.89% |
| S01_STRICT | base | -0.60% | 50.00% | 8 | 21.52% | 0.71 | 1.54% |
| S01_STRICT | stress | -1.89% | 37.50% | 8 | 13.68% | 0.31 | 1.96% |
| S02_CONFIRMED | base | -4.32% | 31.71% | 41 | 19.56% | 0.73 | 7.63% |
| S02_CONFIRMED | stress | -5.43% | 31.71% | 41 | 19.56% | 0.67 | 8.58% |
| S02_STRICT | base | -2.48% | 0.00% | 5 | 0.00% | 0.00 | 2.48% |
| S02_STRICT | stress | -2.68% | 0.00% | 5 | 0.00% | 0.00 | 2.68% |
| S03_CONFIRMED | base | -1.52% | 40.00% | 5 | 11.76% | 0.34 | 2.93% |
| S03_CONFIRMED | stress | -1.70% | 40.00% | 5 | 11.76% | 0.29 | 3.00% |
| S03_STRICT | base | 0.00% | N/A | 0 | N/A | N/A | -0.00% |
| S03_STRICT | stress | 0.00% | N/A | 0 | N/A | N/A | -0.00% |
| S04_CONFIRMED | base | -0.73% | 44.00% | 25 | 26.67% | 0.92 | 4.52% |
| S04_CONFIRMED | stress | -2.08% | 44.00% | 25 | 26.67% | 0.79 | 5.46% |
| S04_STRICT | base | 1.79% | 57.14% | 7 | 25.05% | 2.33 | 1.18% |
| S04_STRICT | stress | 1.25% | 57.14% | 7 | 25.05% | 1.74 | 1.18% |
| S05_CONFIRMED | base | -2.55% | 20.00% | 5 | 3.62% | 0.36 | 3.20% |
| S05_CONFIRMED | stress | -2.80% | 20.00% | 5 | 3.62% | 0.33 | 3.35% |
| S05_STRICT | base | 0.00% | N/A | 0 | N/A | N/A | -0.00% |
| S05_STRICT | stress | 0.00% | N/A | 0 | N/A | N/A | -0.00% |
| S06_CONFIRMED | base | 1.58% | 55.56% | 9 | 26.67% | 1.66 | 1.75% |
| S06_CONFIRMED | stress | 1.26% | 44.44% | 9 | 18.88% | 1.49 | 1.80% |
| S06_STRICT | base | 0.28% | 66.67% | 3 | 20.77% | 1.45 | 1.16% |
| S06_STRICT | stress | 0.16% | 66.67% | 3 | 20.77% | 1.25 | 1.16% |
| S07_CONFIRMED | base | -0.01% | 33.33% | 9 | 12.06% | 1.00 | 3.37% |
| S07_CONFIRMED | stress | -0.43% | 33.33% | 9 | 12.06% | 0.88 | 3.58% |
| S07_STRICT | base | 0.00% | N/A | 0 | N/A | N/A | -0.00% |
| S07_STRICT | stress | 0.00% | N/A | 0 | N/A | N/A | -0.00% |
| S08_CONFIRMED | base | -6.40% | 26.09% | 23 | 12.55% | 0.48 | 8.50% |
| S08_CONFIRMED | stress | -7.31% | 26.09% | 23 | 12.55% | 0.44 | 9.36% |
| S08_STRICT | base | 0.00% | N/A | 0 | N/A | N/A | -0.00% |
| S08_STRICT | stress | 0.00% | N/A | 0 | N/A | N/A | -0.00% |

展示角色（仅base描述，不重新选主候选，也不表示资格合格）：
- 最高观测胜率：S06_STRICT；必须同时查看样本数与Wilson区间。
- 最大净收益：BASE_DEF。
- 最低回撤：S03_STRICT；空仓也可能有最低回撤，不能据此称为最优交易策略。

## 失败与推荐结论
主候选失败原因：NO_HIGH_WIN_CANDIDATE
- BASE_DEF：base:WIN_RATE_BELOW_65_PERCENT；base:WILSON_LOWER_BELOW_GATE；stress:WIN_RATE_BELOW_65_PERCENT；stress:WILSON_LOWER_BELOW_GATE；stress:PROFIT_FACTOR_BELOW_1_30；stress:FEWER_THAN_4_NONNEGATIVE_WINDOWS
- BASE_ENV：base:WIN_RATE_BELOW_65_PERCENT；base:WILSON_LOWER_BELOW_GATE；stress:WIN_RATE_BELOW_65_PERCENT；stress:WILSON_LOWER_BELOW_GATE；stress:PROFIT_FACTOR_BELOW_1_30
- S01_CONFIRMED：base:WIN_RATE_BELOW_65_PERCENT；base:WILSON_LOWER_BELOW_GATE；stress:WIN_RATE_BELOW_65_PERCENT；stress:WILSON_LOWER_BELOW_GATE；stress:PROFIT_FACTOR_BELOW_1_30
- S01_STRICT：base:WIN_RATE_BELOW_65_PERCENT；base:INSUFFICIENT_TRADES；base:WILSON_LOWER_BELOW_GATE；base:PROFIT_FACTOR_BELOW_1_30；stress:WIN_RATE_BELOW_65_PERCENT；stress:INSUFFICIENT_TRADES；stress:WILSON_LOWER_BELOW_GATE；stress:PROFIT_FACTOR_BELOW_1_30；stress:NONPOSITIVE_MEAN_WINDOW_RETURN
- S02_CONFIRMED：base:WIN_RATE_BELOW_65_PERCENT；base:WILSON_LOWER_BELOW_GATE；base:PROFIT_FACTOR_BELOW_1_30；base:NONPOSITIVE_EPISODE_EXPECTANCY；base:NONPOSITIVE_MEAN_WINDOW_RETURN；base:WORST_WINDOW_BELOW_MINUS_5_PERCENT；base:FEWER_THAN_4_NONNEGATIVE_WINDOWS；stress:WIN_RATE_BELOW_65_PERCENT；stress:WILSON_LOWER_BELOW_GATE；stress:PROFIT_FACTOR_BELOW_1_30；stress:NONPOSITIVE_EPISODE_EXPECTANCY；stress:NONPOSITIVE_MEAN_WINDOW_RETURN；stress:WORST_WINDOW_BELOW_MINUS_5_PERCENT；stress:FEWER_THAN_4_NONNEGATIVE_WINDOWS
- S02_STRICT：base:WIN_RATE_BELOW_65_PERCENT；base:INSUFFICIENT_TRADES；base:WILSON_LOWER_BELOW_GATE；base:PROFIT_FACTOR_BELOW_1_30；base:NONPOSITIVE_EPISODE_EXPECTANCY；base:NONPOSITIVE_MEAN_WINDOW_RETURN；stress:WIN_RATE_BELOW_65_PERCENT；stress:INSUFFICIENT_TRADES；stress:WILSON_LOWER_BELOW_GATE；stress:PROFIT_FACTOR_BELOW_1_30；stress:NONPOSITIVE_EPISODE_EXPECTANCY；stress:NONPOSITIVE_MEAN_WINDOW_RETURN；stress:FEWER_THAN_4_NONNEGATIVE_WINDOWS；stress:FEWER_THAN_2_POSITIVE_WINDOWS
- S03_CONFIRMED：base:WIN_RATE_BELOW_65_PERCENT；base:INSUFFICIENT_TRADES；base:WILSON_LOWER_BELOW_GATE；base:PROFIT_FACTOR_BELOW_1_30；base:NONPOSITIVE_EPISODE_EXPECTANCY；base:NONPOSITIVE_MEAN_WINDOW_RETURN；stress:WIN_RATE_BELOW_65_PERCENT；stress:INSUFFICIENT_TRADES；stress:WILSON_LOWER_BELOW_GATE；stress:PROFIT_FACTOR_BELOW_1_30；stress:NONPOSITIVE_EPISODE_EXPECTANCY；stress:NONPOSITIVE_MEAN_WINDOW_RETURN；stress:FEWER_THAN_4_NONNEGATIVE_WINDOWS
- S03_STRICT：base:WIN_RATE_BELOW_65_PERCENT；base:INSUFFICIENT_TRADES；base:WILSON_LOWER_BELOW_GATE；base:PROFIT_FACTOR_BELOW_1_30；base:PAYOFF_RATIO_BELOW_0_80；base:NONPOSITIVE_EPISODE_EXPECTANCY；base:NONPOSITIVE_MEAN_WINDOW_RETURN；base:FEWER_THAN_2_POSITIVE_WINDOWS；stress:WIN_RATE_BELOW_65_PERCENT；stress:INSUFFICIENT_TRADES；stress:WILSON_LOWER_BELOW_GATE；stress:PROFIT_FACTOR_BELOW_1_30；stress:PAYOFF_RATIO_BELOW_0_80；stress:NONPOSITIVE_EPISODE_EXPECTANCY；stress:NONPOSITIVE_MEAN_WINDOW_RETURN；stress:FEWER_THAN_2_POSITIVE_WINDOWS
- S04_CONFIRMED：base:WIN_RATE_BELOW_65_PERCENT；base:WILSON_LOWER_BELOW_GATE；stress:WIN_RATE_BELOW_65_PERCENT；stress:WILSON_LOWER_BELOW_GATE；stress:FEWER_THAN_4_NONNEGATIVE_WINDOWS
- S04_STRICT：base:WIN_RATE_BELOW_65_PERCENT；base:INSUFFICIENT_TRADES；base:WILSON_LOWER_BELOW_GATE；base:FEWER_THAN_4_NONNEGATIVE_WINDOWS；base:FEWER_THAN_2_POSITIVE_WINDOWS；stress:WIN_RATE_BELOW_65_PERCENT；stress:INSUFFICIENT_TRADES；stress:WILSON_LOWER_BELOW_GATE；stress:FEWER_THAN_4_NONNEGATIVE_WINDOWS；stress:FEWER_THAN_2_POSITIVE_WINDOWS
- S05_CONFIRMED：base:WIN_RATE_BELOW_65_PERCENT；base:INSUFFICIENT_TRADES；base:WILSON_LOWER_BELOW_GATE；base:PROFIT_FACTOR_BELOW_1_30；base:NONPOSITIVE_EPISODE_EXPECTANCY；base:NONPOSITIVE_MEAN_WINDOW_RETURN；base:FEWER_THAN_4_NONNEGATIVE_WINDOWS；base:FEWER_THAN_2_POSITIVE_WINDOWS；stress:WIN_RATE_BELOW_65_PERCENT；stress:INSUFFICIENT_TRADES；stress:WILSON_LOWER_BELOW_GATE；stress:PROFIT_FACTOR_BELOW_1_30；stress:NONPOSITIVE_EPISODE_EXPECTANCY；stress:NONPOSITIVE_MEAN_WINDOW_RETURN；stress:WORST_WINDOW_BELOW_MINUS_5_PERCENT；stress:FEWER_THAN_4_NONNEGATIVE_WINDOWS；stress:FEWER_THAN_2_POSITIVE_WINDOWS
- S05_STRICT：base:WIN_RATE_BELOW_65_PERCENT；base:INSUFFICIENT_TRADES；base:WILSON_LOWER_BELOW_GATE；base:PROFIT_FACTOR_UNDEFINED；base:PAYOFF_RATIO_UNDEFINED；base:NONPOSITIVE_EPISODE_EXPECTANCY；base:NONPOSITIVE_MEAN_WINDOW_RETURN；base:FEWER_THAN_2_POSITIVE_WINDOWS；stress:WIN_RATE_BELOW_65_PERCENT；stress:INSUFFICIENT_TRADES；stress:WILSON_LOWER_BELOW_GATE；stress:PROFIT_FACTOR_UNDEFINED；stress:PAYOFF_RATIO_UNDEFINED；stress:NONPOSITIVE_EPISODE_EXPECTANCY；stress:NONPOSITIVE_MEAN_WINDOW_RETURN；stress:FEWER_THAN_2_POSITIVE_WINDOWS
- S06_CONFIRMED：base:WIN_RATE_BELOW_65_PERCENT；base:INSUFFICIENT_TRADES；base:WILSON_LOWER_BELOW_GATE；stress:WIN_RATE_BELOW_65_PERCENT；stress:INSUFFICIENT_TRADES；stress:WILSON_LOWER_BELOW_GATE
- S06_STRICT：base:INSUFFICIENT_TRADES；base:WILSON_LOWER_BELOW_GATE；stress:INSUFFICIENT_TRADES；stress:WILSON_LOWER_BELOW_GATE
- S07_CONFIRMED：base:WIN_RATE_BELOW_65_PERCENT；base:INSUFFICIENT_TRADES；base:WILSON_LOWER_BELOW_GATE；stress:WIN_RATE_BELOW_65_PERCENT；stress:INSUFFICIENT_TRADES；stress:WILSON_LOWER_BELOW_GATE；stress:PROFIT_FACTOR_BELOW_1_30
- S07_STRICT：base:WIN_RATE_BELOW_65_PERCENT；base:INSUFFICIENT_TRADES；base:WILSON_LOWER_BELOW_GATE；base:PROFIT_FACTOR_BELOW_1_30；base:PAYOFF_RATIO_BELOW_0_80；base:NONPOSITIVE_EPISODE_EXPECTANCY；base:NONPOSITIVE_MEAN_WINDOW_RETURN；base:FEWER_THAN_2_POSITIVE_WINDOWS；stress:WIN_RATE_BELOW_65_PERCENT；stress:INSUFFICIENT_TRADES；stress:WILSON_LOWER_BELOW_GATE；stress:PROFIT_FACTOR_BELOW_1_30；stress:PAYOFF_RATIO_BELOW_0_80；stress:NONPOSITIVE_EPISODE_EXPECTANCY；stress:NONPOSITIVE_MEAN_WINDOW_RETURN；stress:FEWER_THAN_2_POSITIVE_WINDOWS
- S08_CONFIRMED：base:WIN_RATE_BELOW_65_PERCENT；base:WILSON_LOWER_BELOW_GATE；base:PROFIT_FACTOR_BELOW_1_30；base:WORST_WINDOW_BELOW_MINUS_5_PERCENT；stress:WIN_RATE_BELOW_65_PERCENT；stress:WILSON_LOWER_BELOW_GATE；stress:PROFIT_FACTOR_BELOW_1_30；stress:WORST_WINDOW_BELOW_MINUS_5_PERCENT；stress:FEWER_THAN_4_NONNEGATIVE_WINDOWS
- S08_STRICT：base:INSUFFICIENT_TRADES；base:WILSON_LOWER_BELOW_GATE；base:FEWER_THAN_2_POSITIVE_WINDOWS；stress:INSUFFICIENT_TRADES；stress:WILSON_LOWER_BELOW_GATE；stress:FEWER_THAN_2_POSITIVE_WINDOWS

后段每策略的诊断门槛失败原因见review_summary.csv；只有预先冻结主候选具有正式比较资格。
uncertainty.json包含早期冻结候选与两对照的1000次10交易日同步块bootstrap（seed=20260929），交易按入场日期成组。Wilson与bootstrap不能修复多次选模和历史复用偏差。

## 完整明细与可追溯性
每账户metrics.json包含年化收益、波动、夏普、盈利日占比、空仓比例/平均仓位、最坏5笔、最大单笔亏损、盈亏比、持有期、费用/滑点、板块集中、失败及未平仓数量。
每账户trades.csv为完整episode；fills.csv.gz、orders.csv.gz、daily_nav.csv.gz、decisions.csv.gz、remaining_positions.csv为原始派生账本导出。
per_sector_summary.csv列行业交易表现；coverage_and_blockers.csv列信号/成交阻碍，feature_coverage.json列数据缺失比例。
原始供应商snapshot与全量特征缓存保留本地；仅上传自主策略/账本派生结果。未调用JEV、LLM、收费推理、新闻或财报服务。

## 交易日窗口
- review: 20260319—20260928，132交易日。
- selection_1: 20240821—20241126，63交易日。
- selection_2: 20241127—20250304，63交易日。
- selection_3: 20250305—20250606，63交易日。
- selection_4: 20250609—20250903，63交易日。
- selection_5: 20250904—20251209，63交易日。
- selection_6: 20251210—20260318，63交易日。

绑定：
- 数据SHA：4396426ff714e24324994f24acb651707d4238c08948388ff6457344201d4375
- 协议SHA：84ea8f416eacb272e094c67c3a84256132dabcf1b7575c569fee2cf62a7617c7
- 实现SHA：d2965109b4d2e39d4678fdbf425ba51a4044145fc232bd595971fead134c15aa
