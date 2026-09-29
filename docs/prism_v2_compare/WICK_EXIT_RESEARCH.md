# 高位插针替换固定止盈

原策略为加强趋势回踩入场、板块20日动量与市场/板块宽度门槛、最多3只，震荡状态新买目标减半。本实验只改止盈，不使用之前未见独立收益的板块缩量禁买。

三个固定对照：FIXED原4%止盈；NO_TP取消固定止盈；WICK取消固定止盈并加高位长上影退出。均保留3%止损、5交易日到期退出、成本与执行限制。

形态要求：当日最高价≥此前20日最高价的98%，且≥昨收的103%；上影线长度≥当日高低振幅的50%，且≥实体的2倍。包含红K、绿K、十字星；要求有效真实OHLC和20个前序交易日高点。并非机构出货的认定。信号日收盘后确认，下一可成交开盘卖出，不使用最高价成交；已有待卖、风险与止损优先。

六个63交易日早期窗口和两个66日后期窗口，每组两成本，共48个独立100万元账户。前置冻结定义、不搜索阈值；保存全部插针事件、逐笔成交与NAV、收益/胜率/回撤及退出原因统计。历史已反复使用，不能当成新样本外。

```bash
cd /home/ww/vv/quant/.worktrees/mainboard-only-universe
/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_wick_exit_research -q
/home/ww/vv/quant/.venv/bin/python -m scripts.wick_exit_backtest \
  --experiment-root cache/experiments/rank_rotation/frozen-mainboard-3d-20260929-v1 \
  --output-root artifacts/wick_exit_research/high-wick-20260929-v1
```

退出适配器在既有提交卖单步骤前修改同一个卖出字典，确保后续新买容量计算和决策日志使用实际退出原因；原入场、成交与预算实现不变。去掉固定止盈时，若同日也到期，恢复到期退出，避免原止盈分支遮蔽到期条件。FIXED仍走原逻辑，用逐笔成交和NAV验证基准一致。
