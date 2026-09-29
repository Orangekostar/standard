# UP与市场宽度优化

固定四个候选：UP+B50、ANY+B60、UP+B60、UP+B65。B表示市场与板块宽度均须达到的百分比；ANY不要求forecast为up。固定对照为上一轮ANY+B50、5日持仓、RANGE新买额度减半的联合过滤策略。

入场仍需加强趋势回踩、板块20日动量为正；止盈4%、止损3%，收盘触发下一可成交开盘执行。费用、T+1、涨跌停、现金约束、最多3只和期末11日停止新买均沿用旧引擎。不改变生产交易，也不调用JEV。

先跑六个历史63日初筛窗口，每种成本均需至少5/6盈利、平均收益正、最差窗口≥−3%、最大回撤≤10%、至少60笔平仓和40个成交日，全部平仓。候选按较弱成本的季度收益25%分位、均值、回撤排序；须严格优于已合格的固定对照才可升级。初筛后冻结唯一选择，再加载2026-03-19—06-25和2026-06-26—09-28两个66日窗口。仅复核冻结对象与对照，禁止后段重选。初筛无升级时，仅诊断预定排序首位，不得改判。

后段每窗口每种成本须净盈利、最大回撤≤10%、至少10笔平仓和10个成交日、零未平仓；两窗口较弱成本平均收益还须高于对照，最差回撤不高于对照。各窗口独立100万元，不能当成连续复利收益。总计60个初筛账户、8个后段账户。历史已被研究，非新的独立样本外验证；历史ST、公司行动与数据修订偏差仍在。

从当前工作树运行：

```bash
/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_entry_filter_research -q
/home/ww/vv/quant/.venv/bin/python -m scripts.entry_filter_backtest \
  --experiment-root cache/experiments/rank_rotation/frozen-mainboard-3d-20260929-v1 \
  --output-root artifacts/entry_filter_research/up-breadth-20260929-v1
```

`core/pipeline/entry_filter_research.py`实现额外信号门槛和归档编排，执行、计费、原环境条件仍复用既有模块。`scripts/entry_filter_backtest.py`为入口；测试覆盖前日信号、双宽度门槛、原策略一致性、不合格不升级、禁止后段重选和归档防篡改。
