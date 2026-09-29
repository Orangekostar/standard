# 高胜率实验室实施与验收台账

需求源：`/home/ww/vv/quant/docs/CODEX_HIGH_WIN_STRATEGY_LAB.md`（全文第0—13节）。
固定底座：`3df7170a6a492097461ebbd9fafb2f450c0c4f5d`。
输出分支：`research/mainboard-high-win-v1`；独立 worktree，保留主仓及原研究目录。

## 固定范围

16个新策略（S01—S08，各CONFIRMED/STRICT）及BASE_ENV、BASE_DEF。
216个早期账户，先冻结最多3名合格候选与唯一主候选；随后36个连续132交易日账户。
不改变65%胜率等资格门槛；无合格策略也是有效结论。禁止生产启用、收费推理及重新拉取行情。

## 实施顺序和验收证据

1. T0：核验冻结文件、所有上下文/特征/回放块及snapshot哈希；保存源码审计、旧结果索引、数据绑定。
2. T1：交易日补索引、严格完整窗口OHLC特征；行业/市场上下文按当日归属连接；全截面排名后统一缓存。
3. T2：不可变16策略注册、独立规则及事件锚点；不经过旧TREND_PULLBACK或forecast筛选。
4. T3：共享账本执行；修复top6截断，明确风险/待卖、止损、到期、止盈顺序；所有组同口径。
5. T4：episode指标、Wilson及双成本资格、早期冻结、可恢复CLI、1/4 spawn独立账户。
6. T5：9类定向测试和相关回归；20—50股票/≤160日smoke；252账户实际完成；1000次10日块配对bootstrap。
7. T6：中文完整表、行业/覆盖/失败及未结资产，所有要求的交付文件、逐项最终审计；普通push并核验SHA，尝试实际创建PR。

## 当前审计发现（尚未修复）

- `_SearchReplay.search_day`只构造前6名候选行，已知禁买可能耗尽候选；应评估全部信号日候选后计划。
- 现有退出分支为止损→止盈→到期；本协议要求止损→到期→止盈，且保存所有触发原因。
- `_RotationReplay._entry_blockers`含prediction_status/score门槛；新规则须仅移除这项而保留证券、日期、停牌、复权等共同限制。
- `_open`只读取开盘价、涨跌停及停牌状态；不读取当日H/L/收盘/成交额，继续保留。
- 冻结feature块已含比较OHLC、原始OHLC和上下文指数，无需从生产DB混入数据。
- context_sectors含covered_count、coverage和当日行业，足以实现行业排名质量门槛；排名不能逐股票块独立计算。

## 数据实际位置

`/home/ww/vv/quant/.worktrees/mainboard-only-universe/cache/experiments/rank_rotation/frozen-mainboard-3d-20260929-v1`

存在rank_dataset_manifest、feature_manifest、roster、corporate_actions及冻结snapshot；存在性不等于哈希验证完成。

## 完成审计

当前仅完成需求读取、隔离worktree和上述源码检查；其余T0—T6均待实际证据。
最终必须逐条核对原提示词，不能以文件存在或测试数量代替252账户、完整指标和GitHub交接。
