# 板块缩量回撤禁买对照

固定条件：SW_L1 板块合成指数收盘价相对3交易日前下跌，最近3日成交额均值不超过此前20日均值的80%。成交额参考期通过 shift(3) 排除最近3日，所有数据在信号日收盘已知。

保留原 ENV_H5_SECTOR20_BREADTH_R50 的入场、排名、仓位、费用、成交限制和退出；只禁止命中条件的板块新开仓。未知数据也禁止新开仓，但单独统计，避免把缺数效果归因于缩量。已有持仓不被此条件强制清仓。连续23日必须状态 OK、价格和成交额正且有限、成分无变动；统一交易日历补齐缺失行。

两方案 × 8个不重叠窗口 × 两种成本，共32个独立100万元账户。六个63日早期窗口、两个66日后期窗口，最后11日不新开仓。条件预先冻结，无阈值搜索或后期选优。输出板块逐日信号、候选过滤计数、逐笔成交/NAV和汇总；校验数据、板块上下文、全部实现及产物哈希，禁止覆盖档案。

```bash
cd /home/ww/vv/quant/.worktrees/mainboard-only-universe
/home/ww/vv/quant/.venv/bin/python -m unittest tests.v2.test_sector_pullback_research -q
/home/ww/vv/quant/.venv/bin/python -m scripts.sector_pullback_backtest \
  --experiment-root cache/experiments/rank_rotation/frozen-mainboard-3d-20260929-v1 \
  --output-root artifacts/sector_pullback_research/avoid-20260929-v1
```

研究复用了既有历史，不提供稳定盈利或机构操盘证据；历史ST、成分、公司行动覆盖仍有限。板块合成序列并非官方指数。本模块不改生产、不调用JEV。

首次运行后追加16账户“仅数据质量过滤”拆分对照，保持同一known定义而不排除缩量回撤；用于避免将数据质量过滤的效果归因于缩量。执行方式：

```bash
PYTHONPATH=. /home/ww/vv/quant/.venv/bin/python artifacts/sector_pullback_research/quality_control.py
```

最终解释见`artifacts/sector_pullback_research/COMPARISON.md`。两个实验各自冻结并校验产物；拆分脚本的源码哈希另行记录。
