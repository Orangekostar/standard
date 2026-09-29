# 沪深主板高胜率实验室 v1 交接

固定源码底座：`3df7170a6a492097461ebbd9fafb2f450c0c4f5d`。
开发分支：`research/mainboard-high-win-v1`。未合并main，未修改生产配置、Worker、UI或latest指针。

## 接口和固定协议

- `configs/high_win_suite_v1.json`：16个新规则、两个原生对照、252账户及全部门槛。
- `core/factors/high_win_features.py`：比较价OHLC、真实交易日索引、完整滚动窗口、行业自身历史及全截面排名。
- `core/strategies/high_win_suite.py`：8族×CONFIRMED/STRICT的不可变注册、条件、锚点及原因码。
- `core/backtest/high_win_research.py`：18组共享执行壳，旧对照调用原FactorMarket和FactorReplay预算。
- `core/backtest/high_win_metrics.py`：完整经济episode、Wilson、收益和晋级门槛。
- `core/pipeline/high_win_research.py`：数据/协议绑定、共享特征、spawn隔离账户、哈希复用及失败单元恢复。
- `core/pipeline/high_win_reporting.py`：全部比较表、分段、行业、阻碍及配对日期块bootstrap。
- `scripts/high_win_backtest.py`：prepare/screen/freeze/review/report/all，`--all`等价于all子命令。

```bash
cd /home/ww/vv/quant/.worktrees/mainboard-high-win-v1
/home/ww/vv/quant/.venv/bin/python -m scripts.high_win_backtest --all \
  --experiment-root /home/ww/vv/quant/.worktrees/mainboard-only-universe/cache/experiments/rank_rotation/frozen-mainboard-3d-20260929-v1 \
  --output-root artifacts/high_win_strategy_lab/mainboard-high-win-v1 \
  --config configs/high_win_suite_v1.json --workers 4
```

可单独运行阶段。同一实现/数据/协议下已完成单元按文件哈希复用；受损或失败单元保留在failed_attempts后重跑，其余单元不重开仓。
改变实现哈希或协议须使用新输出目录，不能把不同执行底座结果混排。

## 固定执行和事件语义

全部组先执行昨日计划，再估值，再按当日收盘形成下一日计划。新买只试下一交易日一次；未成交事件不复活。
新策略无forecast、旧综合分或旧TREND_PULLBACK前置门槛。两原生对照保留原入场/排序/预算差异。
共享修正为全部当日候选评估与到期优先于止盈；原生无修正触发样例的成交/NAV完全相同。
每个新策略独立消费首次完整合格事件，包括持仓中、无席位或尾部不能下单的事件，避免在更晚时点重发已知旧锚点。
新窗口独立账户从窗口首日维护事件状态。不存在按未来执行成功与否回填信号。
S07事件键另含行业ID，避免行业变更后不同板块同一天事件互相混淆。

## 输出和文件名适配

正式输出根：`artifacts/high_win_strategy_lab/mainboard-high-win-v1/`。
每策略/窗口/成本下：`metrics.json`、`trades.csv`、`daily_nav.csv.gz`、`fills.csv.gz`、`orders.csv.gz`、`decisions.csv.gz`、`execution_attempts.csv.gz`、`remaining_positions.csv`、`cell_complete.json`。
NAV/orders沿用底座gzip格式，是提示词允许的小范围接口适配；trades是经济episode而非attempt或每次成交。
后段为单个review账户132日；`review_segments.csv`来自同一NAV，66日边界不平仓、不重置。跨段episode按退出段列完整交易，不能把它误当该段NAV收益。

`derived_cache/`、原始供应商snapshot及其他原始库仅本地保存，不上传。
自主规则生成的账户交易、净值、指标、阻碍、报告属于本请求明确要求发布的派生研究结果；不包含账户密钥、真实委托或供应商原始行情库。
发布采用显式文件范围；任何压缩包不得含derived_cache或原始DB，分片不得超过40MiB。

## 验证状态

已完成41项定向测试（特征、8族正反例/严格子集/未来不变、执行、统计、冻结、spawn及恢复）和相关公司行动/费用/T+1回归，另通过1项同步日期块bootstrap测试，合计42项。
命令与原始日志将绑定在正式输出的test_report.json中。30股票、至多160交易日smoke完成36账户，smoke不参与选模。
正式252账户全部完成，无失败；独立复核5,040次成交和2,520个完整episode的费用、现金、净值、原始开盘价与盈亏全部通过。原始运行从协议冻结至报告生成约510.09秒（账户阶段183.32秒+58.66秒，其余为特征准备/报告）。
结论为NO_HIGH_WIN_CANDIDATE，主候选null；后段S06_STRICT观测胜率66.67%但只有3笔，不能升级。BASE_DEF后段base收益6.38%/回撤2.28%，stress收益0.36%/回撤6.50%，也未达到高胜率资格。
完整逐项核验见FINAL_AUDIT.md，远端提交和PR实际API结果见release_receipt.json及pr_api_receipt.json。

SOURCE_AUDIT.md保留开发开始时的底座检查；最终缺陷修复与要求完成情况由FINAL_AUDIT.md逐条给出。
