# 量价阶段与环境联动研究

研究对象是可观测的量价形态，不把任何阶段命名当作机构建仓、洗盘或拉升的事实。数据没有账户级持仓或资金流，也没有上证指数/深成指日线，背景采用原冻结主板股票池和行业合成指数。

固定四种入场：放量突破；近10日出现缩量回撤后的突破；要求回撤前40日已有横盘量价形态；再要求回撤时市场或板块5日收益为负、突破时两者5日收益均为正。另保留上轮5日趋势回踩联合过滤作为固定对照。完整条件和参数在输出的frozen_protocol.json中，交易不会使用未来阶段或未来收益。

阶段统计另外比较不加成交额约束的价格回撤/突破，避免把市场共振和价格形态定义本身误当作机构行为证据。前瞻5日毛收益是诊断指标，不是可执行组合净收益。阶段与环境占比为描述性相关，不能推断操盘意图。

账户沿用独立100万元、最多3只、原分数排序、震荡新买预算减半、5日持有、4%止盈/3%止损收盘触发、下一可成交开盘执行、T+1、涨跌停、停牌、费用最低额和滑点。每窗口最后11个交易日停止新买。实际买入仍禁止STRESS/UNKNOWN状态；描述性阶段统计包含这些时期，以便观察回撤。量能使用成交额衡量，量价压力是OHLC构造的代理，不能解释为真实机构净流入。

共80个账户：5种规则×6个早期窗口×2种成本，随后冻结选择，再对5种预定规则×2个后段窗口×2种成本进行固定诊断。后段任何其他规则表现再好也不能重新当选；仅早期合格且优于原策略的冻结者有资格接受升级复核。样本不足、季度亏损过大、回撤超限或未平仓均不得宣称稳定策略。

文件：

- `core/backtest/price_volume_research.py`：阶段特征、顺序约束、入场与账本适配。
- `core/pipeline/price_volume_research.py`：分段统计、冻结、80个账户、报告与哈希归档。
- `scripts/price_volume_backtest.py`：命令入口。
- `tests/v2/test_price_volume_research.py`：因果特征、过去高点/成交额基准、阶段顺序与支撑、次日成交、冻结与防篡改测试。

```bash
/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_price_volume_research -q
/home/ww/vv/quant/.venv/bin/python -m scripts.price_volume_backtest \
  --experiment-root cache/experiments/rank_rotation/frozen-mainboard-3d-20260929-v1 \
  --output-root artifacts/price_volume_research/phases-20260929-v1
```

历史已反复用于研究，后段不是全新的独立样本外验证。历史ST、公司行动和成分资料不完整；通过历史筛选不等于证明机构行为或未来盈利。没有生产启用和JEV调用。
