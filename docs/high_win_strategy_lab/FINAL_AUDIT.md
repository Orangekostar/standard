# 原提示词逐项完成审计

需求原文逐字副本：`REQUIREMENTS.md`。SHA256：`d633d1caa9a1d0e7eb469381eed2850fb7940081575b1d8d76b18d297478480b`，与用户文件一致。
研究输出根下的证据文件在下表均用相对名称；代码位置见HANDOFF。

| 原文条目 | 实现与当前证据 | 判定 |
|---|---|---|
| §0 固定底座/独立分支/不覆盖本地修改 | worktree从3df7170建立，research/mainboard-high-win-v1；原main/主板/生产worktree保留 | 通过 |
| §0/§4 16新候选+2原生对照 | configs/high_win_suite_v1.json、policy_grid、protocol_frozen.json，252矩阵无缺项 | 通过 |
| §0 固定4%/3%/5日、不得为高胜率调参 | 不可变策略/固定配置文件hash，所有账户一致；没有资格后仍完整后段比较 | 通过 |
| §1 复用账本、旧对照原生差异、top6问题 | HighWinReplay复用FactorReplay及Rotation账本；8只前6禁买测试；BASE_ENV同样修正；无bug样例原生成交/NAV一致 | 通过 |
| §2 数据存在性与完整哈希 | data_binding.json、prepared.json；32特征块、32回放块、3上下文、名册、公司行动、snapshot完整哈希核验 | 通过 |
| §2 只读冻结来源、不拉数、不动生产 | 全部输入来自指定冻结目录；新输出隔离，未复制生产活库，无网络取行情代码，production_activation=false | 通过 |
| §2 证券/5元/房地产/历史风险限制 | 共用证券函数、已知风险过滤、信号raw close和实际raw open≥5、行业未知不买；252账户独立审计 | 通过 |
| §2 历史ST/公司行动不得编造 | 历史风险标记100%未知保留ALLOW_RESEARCH_ONLY与FROZEN_NAME_FILTER等原标志；公司行动表为空，未宣称完整分红或历史严格剔除ST | 通过，资料限制明确 |
| §2 比较价与raw分离/交易日补索引 | high_win_features及raw5元/复权测试；缺bar保留NaN，没有前填K线；前日值仅用于持仓估值且标记MARK_ONLY | 通过 |
| §2 旧报告索引 | previous_runs_index.json记录22个既有报告/冻结/manifest文件的实际路径和hash；不与异口径数字排名 | 通过 |
| §3 全部共同公式/排名 | R/ell/MA/sigma(ddof0)/TR/ATR/HH/LL/DD/CLV/LW/UW/VA/VR3/ER、当日行业RS60平均秩和行业收益排名；量能端点与行业变更测试 | 通过 |
| §3 新规则无旧分数/12因子/forecast门槛 | 新规则common_mask独立；prediction缺失、score为NaN、F03负值仍可通过新族正例；原生对照保留旧门槛 | 通过 |
| §4 S01—S08逻辑与最近锚点 | 每族正反例、最近突破非最佳旧突破、缺失整理bar拒绝、完整信息截止字段 | 通过 |
| §5 STRICT子集及事件只消费一次 | 每族STRICT子集验证；独立账户事件键，开盘失败后不重试旧锚点；持仓中不下新买单 | 通过 |
| §6.1—3 t收盘/t+1开盘、原交易规则 | 只执行昨日计划；改变执行日close/high/amount不改变当日open买入；实际涨跌停/费用/交易单位继承 | 通过 |
| §6.4—7 实际入场日起5日、close TP/SL、优先级 | 次日触发及跳空按实际价样例；待卖/风险→止损→到期→止盈，所有同日原因写decisions | 通过 |
| §6.8—9 先卖后买、实际席位、账本与未解亏损 | 跌停不能卖时不释放席位；无仓再入才形成episode；未平亏损进NAV；公司行动权益容量回归 | 通过 |
| §6.10 尾部11日与132日中点 | 最后允许开仓日/下一日禁止测试；第65日买入第70日卖出，跨66边界持有且不重置 | 通过 |
| §6 成本精确计费/滑点一次 | 两成本冻结值；5,040笔成交逐笔独立Decimal计算费用、tick滑点与现金，审计PASS | 通过 |
| §7 episode胜率/完整指标/零交易 | metrics.json、trades.csv、remaining_positions.csv；零收益不赢，零交易null，无亏损PF=null+状态；经济根聚合公司行动权益 | 通过 |
| §7.1 早期双成本硬门槛/前3与主候选 | 独立从原始episode重算216账户的所有门槛，0新候选合格，selection_freeze主候选null | 通过 |
| §7.2—3 后段不重新选择、样本不足不承诺 | review_summary.csv全部诊断原因；S06_STRICT只有3笔即使66.67%也不晋级；不启用生产 | 通过 |
| §8 216早期+36后段/重复历史披露 | first_run_receipts.json=216+36成功、0失败；所有18组后段连续132日；early均值不连乘 | 通过 |
| §8 同步10日块bootstrap1000次 | uncertainty.json，seed20260929；无早期候选时仅两预定对照；同日交易聚类/配对/固定seed测试 | 通过 |
| T0—T2 审计/共享特征/注册 | SOURCE_AUDIT.md是初始底座记录；data_binding及新模块存在，正式一次共享缓存覆盖3153股801日 | 通过 |
| T3—T4 共用回放/指标/CLI | 高胜率backtest/metrics/pipeline/CLI实际运行；prepare/screen/freeze/review/report/all、独立四参数、workers1/4、spawn和进度 | 通过 |
| T5 必要测试/smoke/正式/恢复 | 42项定向/相关回归；30股≤160日smoke36账户；正式252；fixture串并行一致、哈希复用、单单元恢复保留失败字节 | 通过 |
| T6 中文全部表/角色/失败原因 | REPORT.md、selection_summary/review_summary/all_candidates、行业及阻碍、三展示角色；完整交易账本CSV | 通过 |
| §10 九类测试预算 | test_report.json逐类映射；未做无关渗透、模糊或全仓压力测试；必要账本审计单独执行 | 通过 |
| §11 必须文件与接口适配 | 原文指定模块/配置/测试/交接及结果均交付；NAV/orders保留.csv.gz格式，小范围适配已记录 | 通过；发布回执见下 |
| §12—13 结论边界和来源 | 无未来65%承诺；不因无候选停止工程交付；原规格及出处完整保存，阈值标为研究假设 | 通过 |

## 独立核验结果

`independent_account_audit.json`：252账户、5,040 fills、2,520 episodes全部通过；没有用原回放函数重算来冒充独立账本核验。
`independent_selection_audit.json`：216早期账户、18策略全部数值门槛独立重算，主候选为null。
selection_freeze SHA为`c58d3caed06a669de1a92819e7271df64a8f324630997daf7543599849117688`，后段/报告不改变冻结选择。

## 数据解释补充

市场和行业上下文均是冻结成员合成序列，不是上证指数、深成指或官方行业指数。成分历史和数据版本限制见原scope_flags。
unknown比例见feature_coverage.json：历史ST未知1.0；缺真实bar约3.734%；行业未知约1.686%；排名共同特征不完整约14.361%。这些不是被补造的历史事实。
本轮价格形态仅描述路径，不据此推断机构身份、洗盘意图或未来走势。

## Git交接完成证据

普通push、远端SHA一致性、实际PR API调用和许可/隐私发布范围须由`release_receipt.json`及`pr_api_receipt.json`证明。
PR若API拒绝，按原T6保留实际失败回执，不把pull/new入口称为已创建PR。未获得实际发布回执前不宣布整个任务完成。
