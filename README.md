# A股分析框架（MVP）

该项目按以下分层设计：

1. 数据管理层：统一封装 Tushare Pro 数据获取与缓存
2. 因子层：定义因子接口与因子计算
3. 策略层：组合因子，输出每日持仓信号
4. 回测层：对策略结果做收益评估
5. Streamlit UI：参数配置、策略运行与结果展示

## 目录结构

```text
quant/
├── app.py
├── config.py
├── requirements.txt
├── .env.example
└── core/
    ├── data/
    │   ├── data_manager.py
    │   └── tushare_client.py
    ├── factors/
    │   ├── base.py
    │   ├── momentum.py
    │   └── registry.py
    ├── strategies/
    │   ├── base.py
    │   ├── factor_selection.py
    │   ├── momentum.py
    │   ├── ai_prediction.py
    │   └── short_term.py
    ├── backtest/
    │   ├── engine.py
    │   └── metrics.py
    └── pipeline/
        └── runner.py
```

## 快速开始

1. 安装依赖

```bash
pip install -r requirements.txt
```

2. 设置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入 TUSHARE_TOKEN
```

3. 运行 UI

```bash
streamlit run app.py
```

4. 启动后台预计算 Worker（推荐使用 systemd）

```bash
bash deploy/systemd/install_quant_precompute_service.sh
systemctl --user status quant-precompute.service --no-pager
```

手动运行方式：

```bash
python -m core.background.precompute_worker --poll-seconds 5 --max-concurrency 3
```

后台核心任务按固定档位刷新：`09:00 / 16:00 / 20:00`。缓存清理任务默认每 6 小时执行一次。

## 说明

- 当前为可运行的 MVP 骨架，默认使用本地模拟数据（无 token 也可跑通）
- 配置好 `TUSHARE_TOKEN` 后可直接切换到 Tushare 拉取数据
- `ai_prediction.py` 当前是占位策略，后续可接入模型推理

## 并发与缓存架构（已落地）

- 前端页面默认只读取后台快照，不在页面线程中执行重计算
- 用户点击“生成”类按钮时，仅提交后台刷新请求
- 后台 Worker 按固定间隔重算并覆盖快照，前端自动加载最新快照
- 缓存清理按规则定时执行：
    - `min_*` 分钟缓存按“重点股票/普通股票”分别保留
    - 日线区间缓存按结束日期保留窗口清理
    - `watchlist.json`、`holdings.json`、`holding_users.json`、`stock_basic.csv`、`macro_*` 持久保留

## 盘中增量信号（保留日线因子）

`FactorSelectionStrategy` 新增了盘中增量接口：

- `generate_intraday_signal(daily_df, minute_bar, state=None)`：输入一条分钟数据，返回最新信号与更新后的状态
- `generate_intraday_signals(daily_df, minute_df)`：输入分钟序列，返回每个分钟时点的信号快照

最小示例：

```python
from core.strategies.factor_selection import FactorSelectionStrategy

strategy = FactorSelectionStrategy(threshold=0.1)

# daily_df: 你现有的日线 DataFrame（含 trade_date/open/high/low/close/vol/amount）
state = None
for bar in minute_stream:  # bar 为 dict 或 Series，例如 {"trade_time": "2026-02-27 09:35:00", "close": 10.2, ...}
    latest, merged_df, state = strategy.generate_intraday_signal(
        daily_df=daily_df,
        minute_bar=bar,
        state=state,
    )
    # latest["factor_score"], latest["signal"] 即当前分钟更新后的最新信号
```

说明：

- 原有 `generate_signals(daily_df)` 不变，回测逻辑无需修改
- 盘中模式是把分钟数据增量聚合为“当日日线临时 Bar”再复用现有日线因子计算
