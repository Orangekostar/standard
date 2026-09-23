# Technical V2 A股研究系统

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

3. 运行 Technical V2 UI

```bash
streamlit run app_v2.py
```

原型入口仍保留为 `streamlit run app.py`，其结果标记为 legacy，不作为 V2 研究证据。

4. 启动 Technical V2 后台 Worker

```bash
bash deploy/systemd/install_quant_technical_v2_service.sh
systemctl --user enable --now quant-technical-v2.service
```

手动运行方式：

```bash
python -m core.background.precompute_worker --profile technical_v2 --poll-seconds 5 --max-concurrency 3
```

Technical V2 按 `Asia/Shanghai` 的 `09:00 / 16:10 / 20:10` 调度。安装脚本只写入并校验用户服务，不会启动或重启服务。

5. 检查并运行统一入口

```bash
python -m scripts.v2 doctor --mode real
python -m scripts.v2 sync --mode real --history-sessions 800
python -m scripts.v2 analyze --mode real --as-of latest --methods formula,jev
```

## 说明

- V2 默认 `DATA_MODE=real`，缺少 `TUSHARE_TOKEN` 或 `TYPESAFE_API_KEY` 时返回明确前置条件状态，不自动生成模拟行情或 Jev 概率。
- 无网络演示使用 `python -m scripts.v2 demo --seed 20260923`；演示结果不属于研究证据。
- V2 不连接真实券商，`live_trading=NOT_CONNECTED`。旧页面的基本面和 AI 占位策略不进入 V2。

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
