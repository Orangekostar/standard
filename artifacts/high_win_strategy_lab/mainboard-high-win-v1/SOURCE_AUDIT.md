# 固定底座审计（开发阶段）

- 需求文档：`/home/ww/vv/quant/docs/CODEX_HIGH_WIN_STRATEGY_LAB.md`，已读取全文0—13节。
- 固定底座：`3df7170a6a492097461ebbd9fafb2f450c0c4f5d`，输出分支 `research/mainboard-high-win-v1`。
- 主仓当前main为`32279ec`，其未跟踪docs保留；原主板worktree及研究产物保留。
- 输入：原主板worktree中的`cache/experiments/rank_rotation/frozen-mainboard-3d-20260929-v1`，只读复用。
- manifest SHA256：`4396426ff714e24324994f24acb651707d4238c08948388ff6457344201d4375`。
- snapshot SHA256：`2e2598e9e115d3110f3031a710d1997e74ee374c51db73a4cef99939054ff820`。
- 已实际通过`_verified_source(root, load_rotation_config())`：manifest/config绑定、roster、公司行动、data_audit、feature_manifest、32个feature块和snapshot完整哈希。
- 另外逐个通过`_verify_cache_file`：3个上下文文件和32个replay块。
- 3153股票、801个交易日。非历史ST真值、名称冻结选择偏差、非历史时点修订版本、公司行动不完整、重复使用历史等全部原风险标志必须进入每账户结果。

## 执行口径检查

`_RotationReplay`具备费用、T+1、实际持仓容量、先卖后买、公司行动、原始价开盘成交及未解资产估值。
`_open`仅读取原始开盘、停牌和涨跌停状态。新研究继续复用该路径。
`_SearchReplay`存在候选前6行截断及退出优先级差异，需对全部18组实施统一修正并记录对照差异。
新规则不能继承`_entry_blockers`中预测状态和score的限制；其余共同证券/交易/日期条件必须保留。

## 特征及信号实现状态

新增特征模块已实现比较OHLC、日历完整窗口、量能分母、行业当日归属连接、截面平均秩以及最低样本数。
4项特征定向测试通过；这不替代原文要求的9类测试。
8族/16注册项信号模块已初步实现，尚未完成族正反例与回放集成，不可宣称策略验收通过。

## 下一步及未完成项

完善每族正反例/严格子集/锚点测试；共享执行适配与回归；episode指标和筛选；协议配置及可恢复CLI；离线smoke；252账户；bootstrap；完整中文报告与推送/PR。
尚未冻结正式协议，尚未运行正式高胜率账户，尚未改动生产或上线。
