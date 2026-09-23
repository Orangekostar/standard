# standard V2：纯技术面“公式计算 + Jev 概率”双路线
## 给 Codex 的开发、验证、选模与 GitHub 发布指令

版本：1.0 · 编制/资料核验日期：2026-09-23  
目标仓库：`https://github.com/Orangekostar/standard`  
已核对的源代码提交：`32279ec22d447675cbfcc6bdbf9b68f10e37bbf4`  
工作分支：`codex/technical-v2`  
交易业务时区：`Asia/Shanghai`，不得使用服务器默认时区决定 A 股交易日。

> 这是可直接执行的开发任务书，不是已经实现的新系统，也不是已验证盈利的策略。文中“现状”来自上述提交的源代码；因子权重、交易阈值和实验预算是本轮冻结的工程初值，不是文献证明的最优参数。完成代码、完成数据验证、完成模型有效性验证、完成远端发布，必须分别报告。

---

## 0. 任务边界与执行原则

你是本仓库的开发者。请在现有 Python / pandas / SQLite / Streamlit / 后台 Worker 架构上增量开发，不另起一个无关项目。直接实施下述任务；遇到外部数据、权限或密钥缺失时，按指定状态记录，继续完成不受影响的代码、测试、文档和发布，不用重复征求已明确事项的确认。

### 0.1 必须交付的产品

1. **全覆盖分析表**：每个分析日，对沪深 A 股分析名册中的每只股票保留一条记录；有数据的输出走势、因子和两种方法的结果，缺失的输出具体状态。不能只算 Top20 却声称覆盖全市场。
2. **板块 → 个股**：至少实现一级行业下钻。交易板块（主板、创业板、科创板）和行业板块分列；概念板块使用单独命名空间，有真实成分资料才启用。
3. **路线 A：公式法**：固定公式计算技术因子、分组得分、趋势分类、条件性买卖意图和研究组合。
4. **路线 B：Jev 法**：同一数据截面、同一收益目标，以 TypeSafe Jev 的结构化 `choice` 输出计算概率池，再做独立校准与条件性决策。
5. **同口径验证**：信号时点、股票池、成本、T+1、持有期及资金约束一致；保留原型作为标明缺陷的历史参考，不能用旧版不成立的收益充当新版基准。
6. **页面、后台、交接与发布**：能启动，能复算，能解释失败，能在 GitHub 找到代码、配置、结果摘要和交接文档。

本轮实现的是每日分析、交易计划、历史回放和独立纸面账户。**不连接真实券商下单，不擅自操作用户真实持仓。**这不影响输出“买入/增持/持有/减持/卖出/观望”及对应数量条件。现有持仓文件只读接入；实际交易自动化留给有账户接口和明确授权的后续任务。

### 0.2 输入限制

只使用 OHLC、成交量、成交额、由这些数据计算的市场/行业技术状态，以及交易日历、证券资格、复权和公司行动等计算/执行必要元数据。禁止把 PE、PB、股息率、市值、财报、新闻、宏观叙事、“主力净流入”供应商评分、题材文本作为 V2 的预测特征。

Jev 是用户本次明确指定的第二条模型路线，不得扩展为 ChatGPT/DeepSeek 等聊天模型、Agent、多角色辩论、联网工具调用或自动因子挖掘系统。Jev 不是“贝叶斯公式”的别名，也不是预期收益 EV 的缩写。本指令按 TypeSafe 官方 Jev 产品实现；没有证据时不要另造同名算法。[J1–J5]

### 0.3 不允许伪造的完成状态

- 没有 Jev 密钥：实现真实 HTTP 适配器和契约测试，输出 `UNAVAILABLE_CREDENTIALS`，不能用随机数、公式概率或假响应冒充 Jev。
- 有代码但没有足够真实历史：`CODE_COMPLETE / DATA_INSUFFICIENT`，不能编造回测表。
- Jev 历史回放：注明 `RETROSPECTIVE_REPLAY`；即使按日期隔离，也不能证明预训练模型没有接触过历史市场信息。
- 前瞻样本未成熟：`JEV_SHADOW_UNVALIDATED`；系统照常记录预测，但不能提前宣布通过收益验证。
- 推送失败：`PUBLISH_BLOCKED`，保留补丁/压缩包和失败原因；不能写“已上传”。
- 软件发布不要求模型一定盈利；模型未达标也要发布真实结果和可运行的研究版本，而不是无限调参直到出现漂亮数字。

---

## 1. 源代码审计结论：任务必须与这些事实绑定

路径均以已核对提交为基准。源文件永久链接见第 15 节。大型文件核对的是相关代码段与函数，不声称已执行全部分支。生产机器上的 `.env`、真实 SQLite 数据库、缓存和账户状态不在本次核验范围。

| 证据 | 已存在的文件/函数 | 已核实的情况 | 本轮处理 |
|---|---|---|---|
| R01 | `core/data/data_manager.py::get_daily_data` | 拉取为空即调用 `_mock_daily_data`，并写入同类缓存 | real/demo/test 分离；真实路径禁止自动 mock |
| R02 | 同文件 `get_stock_basic`、`_prepare_db_priority_panel` | 当前上市名单；失败有三只股票兜底；非空 DB 即返回；部分入口默认只取前 200/220 只 | 全名册、逐日覆盖率、来源与新鲜度；兜底不进入真实运行 |
| R03 | `MarketDB.write_daily_bars/read_daily_panel/write_stock_basic` | SQLite 可复用；字段缺失补 0；历史行情联接当前行业；基础表覆盖写 | 新建版本化 V2 表和读取入口，旧数据只读，历史行业不能穿越 |
| R04 | `DataManager._recent_trade_days` | `pd.bdate_range` 将工作日等同于交易日 | 换成交易所日历，明确下一交易日 |
| R05 | `core/factors/registry.py` | 默认因子包含估值、股息、小市值 | 建立独立 `technical_v2` 注册表，旧字段禁止进入 V2 |
| R06 | `core/factors/technical.py::Alpha9ReversalFactor` | `ret_n.rank(pct=True)` 对整段时间序列排名 | 移出 V2；保留旧版须标注，并修复可调用路径的未来依赖 |
| R07 | `core/factors/volume_price.py::Alpha6TurnoverCovFactor` | 换手率对整段时间序列排名 | 同上；不要将自定义公式直接冠为论文 Alpha#6/#9 |
| R08 | `technical.py::BreakoutFactor/RSIFactor/VolatilityFactor` | “突破”实际是含当日的区间位置；RSI 为简单均值版本；波动因子本身已取负 | 分开定义突破/位置，修复平盘边界，统一正负方向；不套重复负权 |
| R09 | `FactorSelectionStrategy.generate_signals/_zscore` | 历史均值方差 shift(1) 值得保留；合成结果只是二元持仓信号 | V2 用明确的分组公式、三方向和持仓动作；沿用因果性原则，不照搬缺失补零 |
| R10 | 同文件 `generate_intraday_signal` | 临时日线复用日频因子；输入未统一裁剪；分钟状态直接累加量额 | V2 首版不据此声称全天实时覆盖；修复时间裁剪和重复分钟问题，标注预览 |
| R11 | `core/backtest/engine.py::run` | 前日信号乘 close-to-close 收益；无股票数量/T+1/真实开盘撮合；win_rate 含空仓日 | 旧引擎标 legacy；新建统一纸面撮合与逐笔/逐日指标 |
| R12 | `three_bull_pullback.py` 的特征与汇总 | 已有按日期的市场广度、行业聚合，值得借鉴；次日收益用 next_close/next_open，不能当作新仓当日可实现收益 | 复用聚合思路，重新定义收益标签与持仓账本；诊断指标和可交易收益分开 |
| R13 | `data_manager.py::_local_sector_fund_flow_proxy` | 成交额差被放入“主力净流入净额”列 | V2 排除旧资金流分数；需要该信息时只叫“成交额变化” |
| R14 | `recommend_scientific_candidates`、若干 recommend 方法 | 固定权重排序、TopN 和占位空返回并存；sector_rotation/macro 属性为空 | 不把这些占位字段写成已实现功能；完整逐股表生成后才做筛选显示 |
| R15 | `core/data/symbols.py` | `filter_buyable_mainboard` 排除创业板/科创板等 | 分离 `analysis_universe` 与 `trade_eligibility`，不能用主板过滤定义全市场 |
| R16 | `precompute_worker.py`、`snapshot_store.py` | Worker/原子写快照可复用；任务只因未抛异常就标 ok；依赖检查主要看快照存在；快照按任务覆盖 | V2 按同一 run_id/as_of/data_hash 验证依赖，PARTIAL 不冒充 OK，保留历史结果 |
| R17 | `app.py`、`core/pipeline/runner.py` | Streamlit 单文件较大；单股流水线；现有自选/持仓文件路径；AI 占位入口和基本面菜单 | 增加独立 V2 页面/全市场 runner，尽量不重写 4,000 多行旧页面 |
| R18 | `tests/test_data_manager_runtime.py` 等 | 存在 unittest；部分测试直接调用可能拉取/兜底的推荐接口 | 测试显式注入 fixture；正常单测不访问数据商/Jev |
| R19 | 根目录 `report.md` | 2026-05-18 的三阳回踩报告记载历史结果，但依赖本地 cache 文件；统计包含次日开收盘口径 | 视为历史报告，不作为独立复现实验证据；本轮不背书其收益 |
| R20 | `config.py`、`requirements.txt` | 已有 token、cache、market_db 设置；依赖仅基础数据/展示组件，无 Jev 集成 | 在现有技术栈增加最少依赖，配置独立，锁定实际安装版本 |

**初始判断**：保留数据访问接口形状、SQLite 基础、因子接口、市场/行业聚合思路、后台任务/快照及 Streamlit 壳；重做数据真实性边界、V2 因子合成、概率适配、执行回放和全覆盖结果契约。不以“高 Star 框架”之名整体迁移项目。[R01–R20；P1]

---

## 2. 产品与时间契约：先确定到底预测什么

### 2.1 分析范围

- `analysis_universe`：按日期生效的 SH/SZ A 股证券名册，包括主板、创业板、科创板；排除 B 股、基金、指数、债券和北交所股票。以证券类型和交易所元数据判断，不只看后缀。
- 不把 ST、停牌、历史不足或账户无权限的股票从分析页面悄悄删除。它们保留状态行，买入资格另列。
- 默认行业：有权限时使用申万一级行业，读取 `index_member_all` 的历史纳入/剔除记录，不能默认 `is_new=Y` 就得到历史成分。[D4]
- 无历史行业权限时，使用原型 `stock_basic.industry` 的**当前快照**开展当前分析，`sector_history_mode=CURRENT_SNAPSHOT_ONLY`；历史行业回测不能标为严格时点一致。旧历史在无证据时归 `UNKNOWN`，不能把今日行业标签回填到过去。
- 概念板块数据存在时只作额外视图：保存 `(namespace, sector_id, ts_code, valid_from, valid_to, observed_at)`，一股可属多概念；不能跨概念重复购买同一股票。
- “每个板块”指每个已取得真实成分资料的板块；目录同时列出未取得数据的命名空间，不能编造概念覆盖。

### 2.2 数据截止与运行模式

设交易所有效交易日期序列为 `D[0], D[1], ...`。

| 模式 | 特征可用信息 | 输出说明 |
|---|---|---|
| `EOD_FINAL(t)` | D[t] 完整且通过覆盖检查的行情，及截至决策时可见的元数据 | D[t] 收盘后的观察状态，以及下一交易日开始的预测/操作计划 |
| `PREOPEN(t+1)` | 最后一个完整交易日 D[t] 的冻结特征；当日已公开交易状态只用于执行资格 | 当天可使用的计划，绝不使用 D[t+1] 的最终高低收量 |
| `INTRADAY_PREVIEW` | 已收到的分钟数据；不能用未到达的分钟 | 首版只作为旧页面兼容预览，不参与 V2 日频回测、选模和全覆盖承诺 |

V2 默认在交易日 **16:10** 生成、**20:10** 有限补齐后重新生成；**09:00** 读取上一完整截面形成盘前计划。复用现有 Worker，但新任务使用自己的时区/日历/分钟级计划，不由旧 `(9,16,20)` 整点和服务器日期直接判定。09:00 以后新到达的权限、停复牌和涨跌停元数据可刷新执行条件，不重算使用未来行情的 alpha。

Tushare 日线文档注明每日 15–16 点入库；是否完整仍以实际覆盖检查为准，不因过了 16 点就自动认为完整。[D1]

`generated_at`（程序生成时间）、`as_of_trade_date`（行情截止日）、`information_cutoff`（允许信息截止）、`observed_at`（实际获取时间）必须分别存储。重发旧快照不能刷新其行情日期。

### 2.3 预测区间与标签

固定输出持有期 `h ∈ {1,3,5}`，以 **h=5** 作为选模及研究组合主目标，h=1/3 为诊断，禁止分别调参后择优展示。

- 信号时点：D[t] 收盘后。
- 参考入场：D[t+1] 开盘。
- 参考退出：D[t+1+h] 开盘。
- h=1 也跨越一个交易日；不是买入当日卖出。
- 股票价格方向标签：`R_h = adjusted_open[t+1+h] / adjusted_open[t+1] - 1`。
- 阈值：`delta_h = max(0.005, 0.25 * sigma20_t * sqrt(h))`，sigma20 在信号时已知。
- `UP: R_h > delta_h`；`DOWN: R_h < -delta_h`；`FLAT: -delta_h <= R_h <= delta_h`。
- 这是**复权价格变化目标**，不是账户税后收益；现金分红、股票数量与实际费用由执行账本核算，不把两者混称。
- 缺入场/退出数据的标签为未成熟或不可观测，不补 0，不把它删除后掩盖其占比。标签表和特征表分开保存，特征构建器不能读取任何 `future_*` 或 label 列。
- 行业预测目标不是“平均个股上涨概率”：以 D[t] 冻结成员集合，计算成员股票同一 `R_h` 的等权均值，作为**合成行业方向诊断目标**；`delta_sector_h=max(0.002,0.25*sigma_sector20*sqrt(h))`。参考入场缺失使该成分目标不可观测，行业标签须报告覆盖；只有全体参考成分目标可观测时才进入正式行业概率评分。它不代表可直接下单的行业证券。
- 停牌、涨跌停或订单价格条件可能使策略实际入场/退出不同于上述参考区间。单列“预测诊断”和“真实约束下的纸面收益”，不把参考标签当成保证能成交的收益。

---

## 3. 数据升级：只修业务关键问题，不做大规模平台重构

### 3.1 存储与迁移

保留原库/缓存原样，不原地清空。新增 `MARKET_V2_DB_PATH`，默认 `cache/v2/market.db`，版本化迁移由 `core/data/v2_store.py` 实现。`cache/demo_v2/` 与 real 完全分开。旧 `watchlist.json`、`holdings.json`、`holding_users.json` 不覆盖。

最少需要这些表（可在一个 SQLite 数据库内实现）：

| 表 | 唯一键/内容 |
|---|---|
| `schema_migrations` | 迁移版本、应用时间、迁移摘要 |
| `instrument_versions` | code、类型、交易板块、上市/退市状态、生效区间、获取时间/来源 |
| `calendar` | exchange、date、is_open、previous/next session、source |
| `daily_raw` | code/date/source_version；原始 OHLC、pre_close、vol/amount 原值、来源、获取时间、完整性 |
| `adjustments` | code/date/adj_factor/source_version |
| `trading_status` | code/date、停复牌、风险警示、up_limit/down_limit、是否无价格限制、规则版本 |
| `sector_membership` | namespace/sector_id/code/valid_from/valid_to/observed_at，保留历史而非覆盖 |
| `corporate_actions` | 事件 ID、ex/record/pay/list 日期、现金/送转/拆并股比例、状态、来源；只用于价格与账本 |
| `analysis_runs` | run_id、as_of、mode、code/config/data hash、各层状态、覆盖率 |
| `feature_rows` / `prediction_rows` | run_id/entity_type/entity_id/horizon/method；数值及缺失原因 |
| `model_registry` | method/config_id、训练/校准区间、模型 ID、校准器、适用范围、状态 |
| `paper_*` | account、现金、持仓 lot、委托、成交、费用、估值、原因；路线 A/B 隔离 |

现金账本使用整数分或 Decimal，数量使用整数股；价格按证券 tick 处理，JSON 严格禁用 NaN/Infinity，未知字段写 null 并带原因。使用明确列定义和唯一约束；新增列通过 migration，不依赖 `to_sql(append)` 自行兼容不同列集合。SQLite 写入由单一写入器/事务提交；Jev 并发计算不同时竞争批量写库。每次 run 写完并校验后再更新 latest 指针。

### 3.2 真伪与缺失规则

`DATA_MODE=real` 为默认：获取失败返回有原因的缺失，绝不调用 `_mock_daily_data`。`demo/test` 必须显式选择，输出常驻标识，结果不能写入 real 表。测试 fixture 可合成，但必须注明仅验证计算与流程。

旧缓存无来源证据时标为 `LEGACY_UNVERIFIED`，不可仅因价格看起来合理就认证真实。允许通过本地已有抓取日志/来源元数据恢复证据，或对受影响日期重新拉取核验；不能用抽查一只股票证明整库真实。没有证据的旧缓存可显示历史预览，不参与正式选模。

缺失、停牌和 0 不是一回事。价格缺失不能填 0；成交量/额只有来源确认的真实 0 才保留 0。已确认停牌日可以生成单独的 `valuation_mark`：调整后价格延续、成交量额为 0，标记 `mark_only=true`；这只是估值/连续技术序列，不能写成真实可成交 OHLC。未知缺口不做这种补齐。

### 3.3 来源适配与单位

优先复用 Tushare `daily(trade_date=...)` 批量同步，而不是每次页面刷新逐股抓八百天数据。仅补缺失/发生修订的日期，记录请求范围、行数、重复键和错误。默认目标历史长度为最近 **800 个有效交易日**，不得用 800 个自然日替代。

规范字段：价格 RMB/股，`volume_shares` 为股，`amount_cny` 为元，收益和概率为小数。Tushare 日线 `vol` 单位为手、`amount` 为千元，分别换算 `*100`、`*1000`，原字段及单位保留；其他来源按自己的文档转换，不能统一盲乘。[D1]

数据商单次最大条数不能当全市场数量：daily 文档单次上限 6000，stk_limit 文档上限 5800，index_member_all 文档上限 2000；触顶时必须检测截断。[D1,D3,D4] 适配器按实际接口支持的分页/代码分组获取并验证唯一键增长；重复返回同一页即停止并标 PARTIAL。不能假定每个端点都支持同样的 offset 参数。

同时接入已确认存在的 `trade_cal`、`adj_factor`、`stk_limit`、`index_member_all`；权限不足只影响对应能力，不伪造返回。停复牌/公司行动/证券历史等端点由 Codex 在实际 SDK 和官方文档中核对参数后实现，并将 URL、参数、权限结果写入 `docs/v2/DATA_CONTRACT.md`；不要根据函数名字编造 HTTP API。[D2–D5]

### 3.4 复权、板块和截面覆盖

- 技术计算使用 `P*_s = P_raw_s * adj_factor_s / adj_factor_asof`，仅使用本次 as_of 可知版本，O/H/L/C 一致处理；比例、ATR/价格等应对统一缩放不变。
- 原始价格用于订单、涨跌停和账本。复权系数不是“实际持股数量倍增”的依据。
- 窗口均按统一交易日历。停牌估值标记可以参与连续收益和行业广度，未知缺口导致相应窗口因子不可计算。60 日窗口至少有 50 个真实交易 bar、共 61 个有效价格点；不足显示 `INSUFFICIENT_HISTORY`。
- 行业有效日期与记录获取日期分别保存。历史批量获取的行业资料即使带 in/out_date，也只证明有效期重建，不能自动证明当年系统已收到它；标 `RECONSTRUCTED_PIT`。真实前瞻快照标 `OBSERVED_PIT`。历史回放可按历史有效日期和冻结的数据版本重建，不因为 observed_at 是本次下载时间就清空全部历史；但必须标记 RECONSTRUCTED_PIT，并承认无法据此证明当年真实接收时点。只有当前行业标签的 CURRENT_SNAPSHOT_ONLY 仍不能回填历史。
- 股票输出分母是 analysis_universe，不是成功取数的股票数。市场/行业特征至少 90% 成员有当日有效行情或已知停牌估值才能生成；未解释缺口超过 10% 则对应上下文不可用。
- 行业至少 5 个成员才生成稳定的行业技术上下文。小行业、UNKNOWN 行业允许股票保留自身/市场因子，并记录 `sector_context_missing`，不能用 0 假装行业中性。
- 全市场方向表允许 PARTIAL，但任何未覆盖代码必须有一行状态；“完整行覆盖”和“有效预测覆盖”分别展示。

---

## 4. 冻结的技术因子：15 个方向因子 + 6 个风险指标

本套因子是**有量价机制解释、便于验证的候选设计**。文献支持研究因子/分组/评估工作流，不证明以下参数或 A 股收益。保持公式可解释，不把 15 个相关指标描述为 15 次独立投票。[P1,P2]

### 4.1 数学约定

以下价格为截至 t 的调整后技术价格；`r_t=ln(C_t/C_{t-1})`，`sigma20=std(r[t-19:t],ddof=0)`。均值、标准差、最大最小均不使用 t 之后数据。`s=max(sigma20,0.005)`。

`TR_t=max(H_t-L_t,abs(H_t-C_{t-1}),abs(L_t-C_{t-1}))`；`ATR14=mean(TR[t-13:t])`；`a=max(ATR14/C_t,0.001)`。停牌估值标记的 TR=0，不代表当日能交易。

`M_k=C_t/C_{t-k}-1`；`log_return_k=ln(C[t]/C[t-k])`；`MA_k=mean(C[t-k+1:t])`。

`H20prev=max(H[t-20:t-1])`；`H20=max(H[t-19:t])`，`rolling_low_20=min(L[t-19:t])`。

`CLV_t=(2*C_t-H_t-L_t)/(H_t-L_t)`；真实平盘 H=L 时 CLV=0。`A` 为元，`V` 为股。`A20prev=median(A[t-20:t-1])`。

行业/市场等权收益 `r_simple_G,d` 由 d-1 已生效成员、d 的调整后简单收益聚合；已确认停牌成员为 0，未知成员不被静默删掉，按覆盖规则处理。`I_G` 从 1 累乘 `(1+r_simple_G,d)`。滚动行业/市场波动使用其 log index return。

`B_G,20` 是有效成员中 `C_t>=MA20_t` 的比例，分子/分母一起输出；行业指数不是简单平均成员绝对股价。

所有 F01–F15 已标准化为 [-1,1]；不再全历史 zscore，也不对单股全部时间点 rank。缺失因子保留 null 和原因。

### 4.2 方向因子表

| ID / 名称 | 组 | 精确计算 | 解释/边界 |
|---|---|---|---|
| F01 `mom20_riskadj` | T 趋势 | `tanh(ln(C[t]/C[t-20]) / (s*sqrt(20)))` | 中期方向，按波动缩放 |
| F02 `mom60_riskadj` | T | `tanh(ln(C[t]/C[t-60]) / (s*sqrt(60)))` | 较长方向；必须有 61 个价格点 |
| F03 `ma_structure` | T | `tanh((MA20/MA60-1)/(3*a))` | 均线结构，不重复取负 |
| F04 `relative_sector20` | R 相对强弱 | `tanh((ln(C[t]/C[t-20])-ln(I_sector[t]/I_sector[t-20]))/(s*sqrt(20)))` | 行业未知时 null；所有方括号均表示交易日期索引 |
| F05 `relative_market20` | R | 同 F04，将 sector 换为 market | 避免把全市场普涨全归因于个股 |
| F06 `breakout_distance20` | S 结构 | `tanh((C/H20prev-1)/a)` | 真正相对前 20 日高点，不含当日最高价 |
| F07 `range_position20` | S | `(2*C-H20-rolling_low_20)/(H20-rolling_low_20)`；宽度为 0 时取 0 | 区间位置，不命名为突破 |
| F08 `close_pressure5` | S | `mean(CLV[t-4:t])` | K 线收盘位置，非主力买卖身份 |
| F09 `signed_amount_surprise` | V 量价 | `sign(r_t)*tanh(ln(A_t/A20prev))` | 仅 A_t>0 且 A20prev>0；否则 null/已知停牌不预测 |
| F10 `amount_close_pressure20` | V | `sum(CLV*A,20)/sum(A,20)` | 量额加权收盘压力；分母为 0 则 null |
| F11 `signed_volume_balance5` | V | `sum(sign(r)*V,5)/sum(V,5)` | 成交量方向代理，不叫真实资金流 |
| F12 `sector_relative20` | C 上下文 | `tanh((ln(I_sector[t]/I_sector[t-20])-ln(I_market[t]/I_market[t-20]))/(max(sigma_sector20,0.005)*sqrt(20)))` | 行业相对市场强弱 |
| F13 `sector_breadth20` | C | `2*B_sector,20-1` | 行业内趋势扩散 |
| F14 `market_breadth20` | C | `2*B_market,20-1` | 全市场趋势广度 |
| F15 `market_trend20` | C | `tanh(ln(I_market[t]/I_market[t-20])/(max(sigma_market20,0.005)*sqrt(20)))` | 市场整体方向 |

实现时变量统一命名 `log_return_20`、`rolling_low_20`，禁止两个 `L20` 混用。窗口切片含义为闭区间。任何分母缺失或无意义时返回 null，不靠 `1e-12` 把坏数据变成巨大信号。合法零波动用明确的 s/a 下限。

### 4.3 风险指标，不直接冒充上涨信号

| ID | 计算/用途 |
|---|---|
| Q01 `atr_pct14` | ATR14/C；决定计划风险距离与仓位 |
| Q02 `volatility20` | 未取负的 sigma20；仓位和诊断 |
| Q03 `adv20_cny` | 最近 20 日成交额均值，元；容量与最低流动性 |
| Q04 `illiquidity20` | `mean(abs(exp(r)-1)/A)`，只在 A>0 的真实成交日计算，并报告有效天数 |
| Q05 `drawdown20` | `C/max(C[t-19:t])-1`；价格回撤诊断 |
| Q06 `gap_today` | `O_t/C_{t-1}-1`，调整后一致尺度；隔夜跳空风险 |

另外保存 `overextended = (C/MA20-1)>3*a`、当日涨跌停距离、来源/覆盖状态；它们是规则字段，不偷偷扩大可搜索因子池。

已存在的 RSI、反转、三阳回踩先作为 legacy/诊断，不加入本轮 15 因子权重搜索，防止把趋势与反转未经条件定义就混合。后续研究可以增加，但本轮不能无限扩因子。

---
## 5. 路线 A：确定性公式、行业评分与趋势分类

### 5.1 股票分组公式

各组得分为组内有效因子的等权均值：`g_T=mean(F01..F03)`、`g_R=mean(F04,F05)`、`g_S=mean(F06..F08)`、`g_V=mean(F09..F11)`、`g_C=mean(F12..F15)`。

至少 12/15 因子有效，且五组每组至少一个有效因子，才生成 V2 完整合成分。组内缺失会在解释中列出，组间权重不因缺失动态优化。否则只展示能够计算的原始因子和 `INSUFFICIENT_FEATURES`，不能输出中性 50 分掩盖缺失。

基本配置 F0：

| 持有期 | T | R | S | V | C |
|---|---:|---:|---:|---:|---:|
| h=1 | 0.20 | 0.15 | 0.25 | 0.25 | 0.15 |
| h=3 | 0.25 | 0.20 | 0.20 | 0.20 | 0.15 |
| h=5 | 0.30 | 0.20 | 0.20 | 0.15 | 0.15 |

`strength_A,h = Σ_g w_h,g * g_g`，`score_A,h = clip(50+50*strength_A,h,0,100)`。

分类：score>=60 为 `UP`，score<=40 为 `DOWN`，其他为 `FLAT`。这是公式分类结果，**不是上涨概率**；A 路线的模型概率字段为 null，不能把 78 分显示成 78% 胜率。

只允许三个候选配置：

- `F0_BALANCED`：上表。
- `F1_TREND`：各 h 在 F0 基础上 T 加 0.05、V 减 0.05。
- `F2_STRUCTURE`：各 h 在 F0 基础上 S 加 0.05、T 减 0.05。

三者均正权且和为 1。只选一个整体配置用于全部股票，禁止每只股票/每个行业独立搜索最漂亮权重。无有效选模数据时默认 F0，状态 `UNVALIDATED_DEFAULT`。

### 5.2 行业自己的公式，而不是只列行业内 TopN

对每个可用行业生成五个 [-1,1] 量：

`z1=tanh(ln(I_s[t]/I_s[t-20])/(max(sigma_s20,0.005)*sqrt(20)))`；
`z2=tanh(ln(I_s[t]/I_s[t-60])/(max(sigma_s20,0.005)*sqrt(60)))`；
`z3=F12`；`z4=2*B_s20-1`；
`z5=sign(r_simple_s,t)*tanh(ln(A_s,t/median(A_s[t-20:t-1])))`。

行业成交额 A_s 必须按当日有效成员汇总；调整行业成员导致的机械变化单独标注，不编造成资金流。

| 持有期 | z1 | z2 | z3 | z4 | z5 |
|---|---:|---:|---:|---:|---:|
| h=1 | 0.20 | 0.10 | 0.20 | 0.30 | 0.20 |
| h=3 | 0.25 | 0.15 | 0.20 | 0.25 | 0.15 |
| h=5 | 0.30 | 0.20 | 0.20 | 0.20 | 0.10 |

`sector_score=50+50*Σ w*z`，同样用 60/40 划分方向，权重首版不调优。行业按独立行业行排名，不把行业分数复制到每只股票后再次排名，否则大行业被重复计权。

行业没有股票订单数量；输出 `INCREASE_EXPOSURE / MAINTAIN / REDUCE_EXPOSURE / OBSERVE`，再由组合约束落实到个股。不能将行业直接当成可下单证券。h=5 行业方向 UP/FLAT/DOWN 分别映射 INCREASE_EXPOSURE/MAINTAIN/REDUCE_EXPOSURE；数据或概率不可用映射 OBSERVE。其他 horizon 只作诊断。

### 5.3 公式法的收益估计只作可追溯统计

在训练区间，用固定分箱 `[0,20,35,50,65,80,100]` 计算每个 horizon 的 `R_h` 条件均值。边界左闭右开，最后一个箱包含 100。对股票、行业分别拟合，不能混合两种目标。

估计：`mu_bin=(n*mean_bin+100*mean_global)/(n+100)`。n 是样本数，同时必须记录有效日期数；每箱至少 100 条、20 个日期才作为该箱估计，否则只返回 global 并标 `POOLED_LOW_SUPPORT`。global 至少有 1,000 条、30 个日期；不满足则 `expected_gross_return=null`。

这是固定分箱的历史均值公式，不是训练复杂模型，也不改变 F0/F1/F2 因子定义。它只使用相应训练区间，预测日绝不能拿刚发生的未来收益更新同一预测。

`expected_net_edge = expected_gross_return - estimated_round_trip_cost`，`return_estimate_basis=ADJUSTED_PRICE_PROXY`；这是参考复权价差扣费估计，不等于精确账户 EV。费用按第 8 节和计划数量计算；费用只扣一次。支持不足时仍可输出公式方向和条件性意图，但不把不可估的期望收益写成 0 或正收益。

---

## 6. 路线 B：Jev 多因子概率法

### 6.1 技术选择与真实限制

截至资料核验时，官方列出的固定版本为 `jev-1.13.0`。使用固定 ID，不默认用 `jev-latest`；记录响应返回的实际模型 ID。官方产品不提供本任务可用的用户端微调/LoRA 训练接口，因此“选模”指固定预训练版本下的概率组合方案及后处理校准，不编造 Jev training API。[J1,J2]

官方说明 Jev 不擅长精确算术和日期比较。OHLC 运算、因子、阈值、日期、成本和仓位全部先在 Python 中算好；Jev 只判断已经结构化的技术状态。[J3] 产品的结构化输出能力不等于已证实 A 股预测有效。

原生 HTTP：`POST https://api.typesafe.ai/v1/systemone`，Bearer `TYPESAFE_API_KEY`。使用 `core/models/jev_client.py` 隔离调用，保持可注入 transport，测试不访问网络。采用 HTTPX 等轻量客户端并在锁文件固定实际版本，不引入 JS/Next.js 或 Agent SDK。[J2]

### 6.2 股票 state 与问题

每只股票一个请求，包含五组技术状态和三个预测 horizon；共 **15 个 choice 问题**。问题分别根据 T/R/S/V/C 技术证据判断**同一个 horizon 的 UP/FLAT/DOWN 事件**，不是分别计算五种互不相容的“成功率”。

- state 只发送当前允许的因子值、分组值、风险和缺失标记、horizon 的已计算 delta，以及训练区间得到的先验（若有效）。
- 数值保留足够精度，例如 6 位有效数字，并附由 Python 按固定区间计算的 bucket：`strong_negative(<-0.5)`、`negative([-0.5,-0.2))`、`neutral([-0.2,0.2])`、`positive((0.2,0.5])`、`strong_positive(>0.5)`。
- 不发送真实股票名称、代码、绝对历史日期、原始新闻和已知未来收益。用本地映射的匿名 entity_id。这样只能降低历史识别风险，不能证明消除预训练泄漏。
- 不发送路线 A 的最终分类、综合排名、动作或待预测的真实标签，避免 Jev 只是复述 A。
- question ID 在原生接口中是客户端标识，不能假定模型会读到它。每条 `instructions` 内明确当前 entity、所看组、h、参考开盘区间和三类阈值。
- 一个请求中的各组问题共享 state，不能声称它们在统计上独立。按组限定指令只是组织输入，不构成独立性证明。

下面是请求结构示意，Codex 应把字段值由类型检查后的对象生成，不把占位字符串发给 API：

```json
{
  "model": "jev-1.13.0",
  "state": {
    "entity_type": "stock",
    "entity_id": "anonymous_entity",
    "technical_groups": {},
    "targets": {},
    "missing_fields": []
  },
  "questions": {
    "trend_h5": {
      "type": "choice",
      "instructions": "For the anonymous stock described in state, estimate the 5-session adjusted open-to-open price-direction class, using the trend group as the primary evidence. Entry is the next session open; exit is the open 5 sessions after entry. The threshold is already provided in state.targets.h5.delta. Do not calculate prices, dates, or arithmetic. Choose among the three mutually exclusive classes defined below.",
      "criteria": {
        "up": "The future adjusted price return is greater than the supplied positive threshold.",
        "flat": "The future adjusted price return is between the negative and positive thresholds, inclusive.",
        "down": "The future adjusted price return is less than the supplied negative threshold."
      }
    }
  }
}
```

其他 14 条问题使用相同模板替换组和 horizon，并在 instructions 中写明，不只改 qid。state/指令中没有信息的事实不能要求模型补全。

### 6.3 行业 Jev 概率是独立目标

每个行业另构造 compact state。T 包含 z1/z2；R 包含 z3；S 包含行业指数相对前 20 日最高收盘指数的距离、收盘区间位置；V 包含 z5/成交额比；C 包含行业/市场广度与市场趋势。所有量在代码中计算。

S 的行业指数结构只使用行业收盘指数，**不伪造行业真实 OHLC**；归一化距离以 `max(sigma_sector20,0.005)` 缩放、再 tanh；区间位置用最近 20 日行业收盘指数 min/max，零宽度取 0。

同样每行业 15 个 choice，指令目标改为第 2.3 节的**冻结成员等权平均价格收益**，阈值使用 delta_sector。不能平均所有股票的 p_up 后声称这是行业上涨概率。

### 6.4 原始概率校验和组合

读取 `answers[qid].probabilities`；必须正好对应 `up/flat/down`，每个值有限且在 [0,1]，概率和在 `1±0.001` 内。容差内的浮点偏差可归一化一次并记录；超出则 `INVALID_RESPONSE`。不得用 score/100、confidence 或强制 softmax 某个未知字段替代 probabilities。

保留模型 `choice`、`confidence`、实际模型名和 usage。confidence 仅作供应商输出诊断，不再次乘进概率，也不显示为股票胜率。[J2,J4]

两个候选概率组合，除此不新增搜索：

- `J0_EQUAL_POOL`：`q_c=(1/5)*Σ_g p_g,c`。
- `J1_WEIGHTED_POOL`：`q_c=Σ_g w_h,g*p_g,c`，w 使用 F0 各 horizon 权重，**不随公式候选胜负变化**。

二者是线性概率池，不是贝叶斯后验；组之间相关，因此禁止 `Π p_g`，禁止凭“独立投票”提高置信度。[J5 为代码聚合思路，具体股票概率池是本方案设计]

某组没有合法响应时，该 entity/horizon 标 `PARTIAL_RESPONSE`，不补均匀分布，不用公式填充，不在正式评估里假装完整 Jev 预测。

### 6.5 校准与期望收益

对 `q` 做温度缩放：`p_c=exp(log(max(q_c,1e-8))/T)/Σ_k exp(log(max(q_k,1e-8))/T)`。

- T 只在校准集拟合，候选固定 `[0.5,0.75,1.0,1.5,2.0,3.0,4.0,5.0]`，最小化按日期等权的 multiclass log loss；并列取最接近 1 的 T。
- 股票和行业、h=1/3/5 分开校准；不逐股拟合；每类实体每 horizon 至少 1,000 条、30 个日期、每个类别至少 30 条；否则不标 calibrated。
- 在验证集比较 identity T=1 与校准结果；校准结果验证 log loss 未改善则保留 identity，记录 `CALIBRATION_NO_GAIN`，不能挑测试集上的 T。[P3]
- 输出 `p_raw_*` 和 `p_cal_*` 两套字段。没有合格校准器时 p_cal 为 null，页面标题为“未验证模型概率”，不能写“实际上涨概率/胜率”。
- `mu_J,h = Σ_c p_c * mean_train(R_h | class=c)`，class 均值只来自同一实体类型/horizon 的训练区间，先对日期内样本取均值，再对日期等权；每类至少 100 条、20 个日期。这里不向无条件全局收益收缩，避免稀有 DOWN 类的均值被错误拉成正数。缺少任一类支持则 mu=null；不得让 Jev 生成预期价格或收益金额。
- `net_edge_J=mu_J-estimated_round_trip_cost`。h=5 用于主操作；其他 horizon 不构成额外独立证据。

### 6.6 调用、缓存与预算

配置读取 `TYPESAFE_API_KEY`，不打印/提交。固定模型 ID 使用一次小请求核验；`GET /v1/models` 可能只列别名，不能仅因列表不含固定版就误判它不可用，真实请求响应才是必要核验。

每个响应按 `(model_requested,model_returned,state_hash,instructions_hash,schema_version)` 落缓存，并保留实际首次响应。相同请求重跑优先复用；版本变化必须新建记录，不能覆盖原概率。

开发预算上限 **15 USD**，每日运行上限 **5 USD**，单 HTTP 请求超时 30 秒、最多 2 次额外重试。初始并发 8、客户端限速 240 请求/分钟；还要遵守账号更低的实际限额。401/403/422 不反复重试；429/5xx 根据 Retry-After 或 2/5 秒退避，仍失败就记状态。

预算采用运行时核对的供应商价目，记录版本和实际 usage。启动前按 UTF-8 字节数等保守上界预留预算，结算用返回 usage；输入超出当前模型上下文限制则在代码层报错，不任意截去风险或标签定义。核验价目失败且没有经确认的缓存价目时，停止收费请求，不盲估费用。

达到预算的代码仍需保留行，标 `NOT_EVALUATED_BUDGET`；断点继续按稳定 entity_id 顺序完成，不静默改成 TopN。历史 Jev 不做“800 天 × 全市场”暴力回放；使用第 9 节固定分层 cohort。全市场当前分析与历史验证 cohort 是不同覆盖范围，必须明确显示。

---

## 7. 从趋势到买卖：先输出意图，再结合账户与执行约束

### 7.1 统一输出契约

每个 `(run_id,entity_type,entity_id,horizon,method)` 一条记录，唯一键不可重复。输出下列字段；未知填 null，不填 0 冒充结果：

```text
schema_version, run_id, entity_type, entity_id, ts_code_local_only,
exchange, listing_board, sector_namespace, sector_id, sector_name,
as_of_trade_date, information_cutoff, generated_at, earliest_entry_date,
reference_exit_date, horizon, target_definition_version,
data_source_mode, sector_history_mode, feature_coverage,
prediction_status, missing_reason_codes,
method, config_id, model_id, calibration_id, evidence_status,
factor_values, factor_group_scores, risk_metrics, reason_codes,
observed_trend, forecast_class, formula_score,
p_raw_up, p_raw_flat, p_raw_down, p_cal_up, p_cal_flat, p_cal_down,
expected_gross_return, estimated_round_trip_cost, expected_net_edge, return_estimate_basis,
research_intent, account_action, action_blockers,
current_quantity, sellable_quantity, target_quantity, order_quantity,
reference_price, entry_price_ceiling, planned_stop_price,
planned_exit_date, order_status
```

页面默认每只股票一行、展示 h=5，展开查看 h=1/3；规范化预测表每只股票有 2 方法 × 3 horizon=6 条状态/结果行，不能只保留成功的预测。只有 h=5 可产生操作/数量，h=1/3 的 research_intent=DIAGNOSTIC_ONLY、数量/账户动作均 null，避免同一股票重复下三份单。股票不适用的行业字段和行业不适用的数量字段为 null。金额、价格、比例使用明确 schema 类型；内部比例都是小数，展示百分比只在 UI/导出时转换一次。方向类在文档用 UP/FLAT/DOWN 描述，序列化统一为 up/flat/down/unknown；观察趋势和动作按本文大写枚举保存。另加 account_type=paper/readonly_user，禁止把研究账户 100 万元显示成用户资金。由字符串模板和 reason_codes 解释，不调用文本大模型。

`observed_trend` 只描述截至 as_of 的技术状态：T 组均值 >=0.2 为 RISING、<=-0.2 为 FALLING，其余 SIDEWAYS。股票没有完整 T 组则 UNKNOWN；行业观察趋势用 mean(z1,z2) 的同一 0.2/-0.2 阈值。它与 `forecast_class` 分列，不能把今天已经发生的涨跌叫预测准确。

### 7.2 研究意图（股票）

无持仓时：

- A：score5>=65、score3>=55 且不过度偏离，输出 `BUY_WATCH`；score5<=40 输出 `AVOID`；其他 `WAIT`。
- B：用可用概率（先 p_cal，否则 p_raw）计算；p_up5>=0.55、p_down5<=0.25 输出 `BUY_WATCH`；p_down5>=0.55 输出 `AVOID`；其余 WAIT。raw 输出始终带 `UNVALIDATED_PROBABILITY`。
- 若 net_edge 可估且 <=0.001，则 BUY_WATCH 降为 WAIT；无法估计则保留条件性观察，不说有确定净优势。

已有持仓时：

- A：score5>=65 为 ADD_WATCH；45<=score5<65 为 HOLD；40<score5<45 为 REDUCE_WATCH；<=40 为 SELL_WATCH。
- B：p_down5>=0.55 为 SELL_WATCH；否则 p_down5>=0.35 为 REDUCE_WATCH；否则达到上述买入条件为 ADD_WATCH；其余 HOLD。
- 首先执行下一节的风险退出优先级。卖出只涉及现有可卖股份，不生成融券/裸空头。

各方法、各 horizon 全部展示，但主组合只读取 h=5，h=3 仅使用上述冻结过滤，不由 UI 临时切换有利 horizon。

### 7.3 账户动作与默认研究约束

默认纸面账户初始资金 1,000,000 RMB，A/B 各一份；不是用户实际资金。默认最多 10 只，单股 <=10%，单一级行业 <=25%，总股票仓位上限 60%。同一股票跨概念只计一次。保留真实持仓只读视图，不修改原持仓文件。

新买入资格：数据有效、无未知核心状态、非风险警示/退市整理、上市满 120 个交易日、ADV20>=20,000,000 RMB、账户具备所属板块权限、买入/退出规则有来源记录。独立纸面账户默认模拟拥有沪深主板、创业板、科创板权限，标为 RESEARCH_PERMISSION_ASSUMPTION；这不是用户实际权限。真实只读账户权限未配置时仅生成研究意图，数量为 null；不得把“账户无权限”当成“股票趋势下降”。

净优势和支持合格的 BUY_WATCH/ADD_WATCH 才进入常规纸面目标组合。Jev 未完成前瞻验证时可以在**独立 shadow 纸面账户**回放同一规则，但不得替代公式默认发布路线，页面明确标明实验。

总仓位上限按市场广度确定：B_market20<0.35 时不新增仓位；[0.35,0.55) 最大 30%；>=0.55 最大 60%。这是冻结的风险初值，不是已证明的择时规律。现有仓位超上限则按弱信号优先生成减仓计划，而不是假定立即全部卖出。

排序优先 `expected_net_edge` 降序，然后方法强度降序、代码升序。计划止损距离 `d=max(2.5*ATR14_raw,0.03*reference_raw_close)`。参考数量的费用估计先用 `min(0.10,当前总仓位上限/10)*NAV` 对应的名义资金；实际分配后按真实计划数量重算费用和净优势，若不达标取消该候选，不循环重搜参数。分配器先计算新增/目标数量上界，已有不能立刻卖出的份额继续占用所有风险上限。单股最大份额：

`q_cap=min(0.10*NAV/P_ref,0.005*NAV/d,0.01*ADV20/P_ref)`，再受现金、行业、总仓位约束，按证券规则取整。

分配顺序：先为现有应保留持仓预留风险和现金需求，再按上述排序补新股票/增持；不因 TopN 未满而加入不合格股票，不把未用仓位硬分完。不会计算买卖单位的证券保留预测并标 `RULE_METADATA_MISSING`，不得全部套 100 股。

### 7.4 动作优先级与退出

固定优先级：

1. 数据/账户不可靠：暂停新增，不凭空处置；已有风险订单保留并标需核对。
2. 已确认强制交易限制或风险退出条件：生成允许条件下的退出意图。
3. 已到计划退出日、价格触及策略止损、组合风险超限：优先减/卖。
4. 方法给出的减持/卖出。
5. 保留持仓。
6. 允许的新买/增持。

止损只用收盘已知信息生成下一交易日计划：调整后一致口径的收盘价低于初始止损或 `自入场以来最高收盘 - 2.5*当前ATR14`，取二者较高的保护线。所有历史持仓止损线和数量在公司行动后同步调整。

入场交易日 e，计划退出日为交易日历中的 e+h；前一交易日收盘生成退出订单。已有 lot 分别维护入场日/计划退出日；增持不重置老 lot 的退出日。不能卖时留仓并记录原因，不将损失截断在止损价。

`account_action` 取 `BUY/ADD/HOLD/REDUCE/SELL/WAIT/BLOCKED`。一手以下变化归 HOLD/WAIT；卖出意图存在但无可卖数量时 BLOCKED，保留 intent 与冻结原因。没有账户资料时动作字段为条件性展示，不生成实际订单数量。

---

## 8. 统一回测/纸面执行：重点修正确性，不做高频撮合工程

新建 `core/backtest/execution_v2.py`、`portfolio_v2.py`、`metrics_v2.py`，旧 BacktestEngine 留作 legacy，不沿用其净值做新版盈利结论。

### 8.1 冻结的撮合方式

- D[t] 的信号只能生成下一有效交易日的委托。预测阶段不能读取下一开盘价或下一日成交额。
- 委托参考价为 D[t] 原始收盘。买入上限 `P_ref*(1+min(0.02,0.5*ATR_pct14))`，在正式执行资格刷新时结合已公布价格限制/除权参考进行适配；数量基于 t 的参考价和预算，次日价格只能缩减超预算订单，不能利用未来价格增加计划数量。
- 回放在下一日开盘进行一次保守撮合：有有效 raw open，买入价包含正向滑点且不超过买入上限；卖出价包含负向滑点。非限制条件可成交是**日频模拟假设**，不是已观测订单簿成交证明。
- 开盘涨停不假定买单能成交，开盘跌停不假定卖单能成交；官方/数据商明确无价格限制的日期另走规则，缺失价格限制元数据不当作无涨跌停。
- 容量使用 t 日已知 ADV20 限额，不能根据未来一整天成交量来放大开盘委托。这个限制不能证明真实开盘容量，报告必须保留 `OPEN_FILL_APPROXIMATION`。
- 先处理可执行的卖单，再在实际纸面可用资金内处理买单；同日买入 lot 不增加当日 sellable_qty。
- 未成交买单当日失效，下一次信号重新决定；未成交退出单进入待退出状态，下一有效日重新检查。禁止因跌停不能卖而让记录消失。
- 不通过当日 high/low 推断“先止盈再止损”或使用当日最低价买入；本轮不做日线内路径模拟。
- 拒单、部分成交（因容量/现金裁剪）、未成交、持仓冻结均有记录。重复执行同一 run/order id 不重复记账。

### 8.2 费用和公司行动

研究费用初值：佣金每边 0.0003、每笔最低 5 RMB，卖出额附加 0.0005、每边其他费 0.00001，滑点每边 0.001。**这是研究用成本假设，不是对用户券商费率或所有历史日期法定费率的声明。**实际模式必须加载带 effective_from/to 的费用表及来源，历史研究不得以今天费率冒充过去规则。

滑点只在成交价格加减，不能又作为现金费用重复扣；佣金最低收费只作用于佣金，不重复作用于其他费/附加费。净收益估计与执行采用同一配置。

必须支持现金分红、送转/拆并股的最小事件账本：登记资格、除权计价、应收现金与到账现金、股份数量和可卖日期分开。公司行动数据不足时，不用复权系数直接乘真实股票数量；保留未解决事件，相关组合净值标 `NAV_UNRESOLVED_CORPORATE_ACTION`，不能删掉该持仓或利用事后知道的事件预先剔除样本。

退市、长期停牌和缺退出价不得按 0 收益处理；有可靠最终价格/结算事实则记账，否则保留未解决估值状态并影响完整性结论。价格标签诊断可继续报告观测覆盖率，但无法得到完整账本时不宣称净收益验证通过。

### 8.3 指标

逐交易日完整净值序列包括空仓、停牌日；输出净收益、年化收益、年化波动、Sharpe（无风险利率研究默认 0）、最大回撤、成交笔数、已平仓交易胜率、换手、现金占比、未成交比例、持仓行业暴露和未解决估值。

交易胜率只统计定义明确的已平仓 lot/交易单元，日收益为正比例另叫 `positive_day_ratio`。horizon 标签的分类准确率、RankIC 与组合收益单列。

指标不足样本时为 null，不把 0 天或 1 天收益年化成夸张结果。基准为现金和第 9.2 节定义的 M0 简单动量；资金/费用/资格/风险上限同口径，不能拿单股 benchmark_nav 当全市场业绩基准。

---
## 9. 实验、选模与诚实的验收结论

### 9.1 一份固定的数据划分

先写 `dataset_manifest.json`：数据文件/表分区 hash、日历、名册、行业版本、模式、最后完整日期、标签可观测范围。不能一边跑候选一边换股票池。

取最后 **504 个标签已成熟的候选信号日期**，按时间划分：训练 252、校准 63、验证 63、最终测试 126。最后 126 日只在选择结果冻结后读取，分成前后各 63 日作稳定性报告，不用于重新选赢家。各块之前另提供 120 个交易日的特征预热历史，不纳入该块评价。

严格清除跨边界标签：任何拟合所用样本的 `label_end_date` 必须早于下一块首个信号日期。h=5 通常需移除前一块尾部至少 6 个信号日；以真实 label_end_date 判断，而不是只写固定 gap 数字。股票和行业同样执行。

不足 504 个成熟日期时仍完成代码、当前因子分析和演示；可生成明确标记的短历史探索报告，但 `selection_status=INSUFFICIENT_HISTORY`，不能把缩短后的训练/测试伪装成满足此协议。

### 9.2 候选与预算边界

| 候选 | 是否本轮生产路线 | 实验范围 |
|---|---|---|
| legacy 原型 | 否 | 保存旧计算诊断；修正其信号在新引擎中的表现另命名 `LEGACY_SIGNAL_EXEC_V2`，不与旧收益混名 |
| 固定简单动量基线 | 仅对照 | 20 日动量排序；使用同一新引擎、资金、资格、调仓规则 |
| F0 / F1 / F2 | 公式路线候选 | 全部可用真实股票；三次配置评估，不扩大网格 |
| J0 / J1 | Jev 路线候选 | 同一批缓存原始回答，仅更改本地概率池和温度；不为每个候选重新调用 API |
| 训练期类别频率先验 | 仅概率对照 | 判断 Jev 是否比“总按历史比例预测”更好 |

Jev 历史 cohort：在**校准块开始前**，按当时有效行业分层，每行业按 `SHA256(seed=20260923|ts_code)` 升序轮流取一只，直到 32 只或名册耗尽；不要用测试期存活股票来挑 cohort。行业历史回放同理按 sector_id hash 固定取最多 32 个行业。后续退市/停牌的对象保留，新增上市股票不替换失效样本。

分层取样时行业先按稳定 sector_id 升序遍历，每轮各取一只，行业内按上述 hash 排序；重复代码去重，UNKNOWN 作为最后一组。只对该冻结 cohort 的校准/验证/测试日期做历史 Jev 请求；跳过缺失特征但保留状态。路线 A 同时报告全市场结果和**完全相同 cohort、日期、可观测目标**上的匹配结果，不能用 A 的全市场收益与 B 的精选小样本比较。

`LEGACY_SIGNAL_EXEC_V2` 仅使用旧策略的 `momentum_20` 和不含未来数据的区间位置因子、等权、threshold=0.1；不调用整段 rank、基本面或旧 mock 数据。它只是对照，不进入冠军选择。

M0 基线：每个信号日按 20 日调整后 log 收益降序，代码升序破并列，正动量股票才有买入意图；研究组合仍遵守相同资格、行业/总仓位/容量和 T+1，取前 10 个可配置对象，5 日 lot 到期退出。M0 的数量通过第 7.3 节同一风险限额分配，但不要求分箱 expected_net_edge（它是无收益估计的基线），按动量而不是预期收益排序。此差异必须在比较报告中列明，不称为完全相同的信号过滤。

当前全市场 B 分析仍覆盖所有有效股票/行业；cohort 只是减少昂贵历史 API 回放，不是缩减产品范围。非历史 cohort 上应用校准器时标 `CALIBRATION_TRANSFER_UNVALIDATED`，直到有对应前瞻证据。

开发期最多：3 个公式配置、2 个本地 Jev 概率池、每池/实体类型/horizon 的 8 个标量 T 值。只进行一次固定最终测试。不得增加无上限 Optuna/随机搜索、反复改提示词或大规模深度学习训练。

### 9.3 选择规则必须机器可复现

**公式**：先用验证集排除账本未解决、交易数不足 30、有效交易日期不足 20、最大回撤超过 20% 的配置；在剩余者中选扣费后 Sharpe 最大者。Sharpe 差 <=0.05 时先选换手更低，再按 F0、F1、F2 固定顺序。若全不合格，保留 F0 为未验证参考，不宣称有合格冠军。

**Jev**：先检查响应有效率、校准支持、数据/模型版本一致。先在每个概率池内部按第 6.5 节用验证集选择 identity 或校准版本，然后比较两个池的已选版本。主指标为按日期等权 log loss，次指标为 Brier score；log loss 差 <=0.001 选 Brier 较低者，再并列选 J0。记录池内校准选择和池间选择的两层依据。`confidence` 不作为选模目标。

概率指标定义：`logloss=-mean_date(mean_entity(log p_true))`；`Brier=mean_date(mean_entity(sum_c(p_c-y_c)^2))`。报告类别占比、每类召回、10 个固定置信区间的可靠性表、覆盖和按行业/交易板块分组结果；小组样本不足标注，不拼成“全部板块有效”。

收益比较和概率比较分别给结论：概率更好不等于净收益更高。两条路线各有自己的配置选择结果，不强制融合成第三个模型。

候选确定后，写 `selection.json` 并保存 SHA256，再运行最终测试。测试恶化仍原样报告，不返回前面的测试窗口改参数；下一轮改动必须新版本并承认旧测试已看过。[P4]

### 9.4 Jev 真正的前瞻验证

无论历史回放多好，首次交付时通常仍是 `JEV_SHADOW_UNVALIDATED`。记录实时时点产生且不可覆盖的预测，待对应 h 标签成熟再评分。最低观察门槛为 60 个前瞻信号交易日、每实体类型至少 1,000 条成熟记录及足够类别支持；这个数量只是本轮工程门槛，不等于统计显著性保证。

校准器和候选版本在一个前瞻验证周期内冻结；任何更改都开始新的验证段。只在前瞻期同时满足下列条件时允许写 `JEV_FORWARD_VALIDATED_FOR_SHADOW`：

- 无时点/数据真伪/账本关键错误；
- log loss 和 Brier 均优于训练期频率先验，且报告差值及日期分块稳定性；
- 同 cohort、相同资金约束下的扣费收益不低于公式路线，最大回撤不高于公式路线超过 2 个百分点；
- 前后各半期方向一致；不是靠少数极端样本撑起全部增益。

这仍不自动启用真实交易。没有经过完整观察期就保持 shadow，不要求 Codex 在本次开发会话中等待 60 天，也不能提前填写未来验证结果。需要交付的是可自动积累和评估这些记录的代码与当前真实状态。

### 9.5 结果文件

至少输出：`data_audit.json`、`coverage_by_date.csv`、`coverage_by_sector.csv`、`factor_ic.csv`、`factor_missingness.csv`、`candidate_comparison.csv`、`probability_metrics.csv`、`calibration_bins.csv`、`daily_nav.csv`、`trades.csv`、`orders.csv`、`selection.json`、`run_manifest.json`。

不能运行的项目保留对应状态 JSON 及失败原因，CSV 可只有 schema；文件存在不等于实验通过。历史回放、合成测试、前瞻实录用不同目录和明确 mode，不能拼接到一张净值曲线。

---

## 10. 代码任务绑定、依赖顺序和验收产物

以下新路径是**本轮要求新增**，不是声称仓库当前已经有这些文件。尽量每个模块只承担一个业务职责；不为架构美观拆出几十个空接口。

### T00｜冻结基线与读取运行环境

**读取**：第 1 节全部关键文件、现有 `prompt.md/report.md`、部署脚本、tests；查看 `git status` 和当前 HEAD。  
**新增**：`docs/v2/REPO_AUDIT.md`、`docs/v2/BASELINE_MANIFEST.json`。

确认 origin 指向用户仓库。若远端已前进，记录新 SHA 并检查相关文件差异，在当前工作树上接续；不能强制重置到旧 SHA 或覆盖用户未提交内容。基线清单列明“已读取/未读取”“有运行数据/只有源码”。对现有真实 DB 只读检查表结构、日期覆盖、股票数、字段单位及来源，禁止输出 token/私人账户明细。

**验收**：实际路径→函数→缺陷→本轮任务编号的映射；数据量来自真实查询，而非照抄 report.md 的 3908 股票数字。

### T01｜数据真实性、存储和元数据

**修改**：`config.py`、`.env.example`、`core/data/data_manager.py`、`core/data/symbols.py`。  
**新增**：`core/data/v2_store.py`、`v2_provider.py`、`v2_universe.py`。  
**依赖**：T00。

执行第 3 节，新增 real/demo/test 明确分支；旧单股接口保持可用但真实模式不静默 mock。新分析入口一律走 V2 store/provider。建立日历、复权、行业历史、规则及覆盖审计。修正实时日线 fallback 的真实日期标记，不能将旧收盘价当刚生成的新行情。

**验收**：缺 token 真实模式不会产生模拟行情；同日重复同步不重复行；单位有 fixture 对账；所有分析名册代码都有状态；一次数据不足不会造成全库删除或随机兜底。

### T02｜因子与市场/行业上下文

**读取/借鉴**：`core/factors/base.py`、`technical.py`、`volume_price.py`、`three_bull_pullback.py`。  
**新增**：`core/factors/technical_v2.py`、`core/analysis/sector_v2.py`，必要 `__init__.py`。  
**修改**：`core/factors/registry.py`，注册独立版本，旧因子显式 legacy。  
**依赖**：T01。

实现 15 个因子/6 个风险指标，元数据包含公式版本、输入、单位、lookback、空值原因及组名。公式纯函数；先按股票算必要价格序列，再按日期聚合行业/市场，再回联接相对因子。不能在循环每只股票时反复拉取全市场数据。

同时修复仍可调用的 legacy Alpha6/9 的整段时间 rank：按业务语义改为截至当前的滚动/扩展排名并重新命名为自定义 legacy 修正版，保存旧结果不能混作未改版本。旧分钟入口先裁剪到分钟时间以前已完成的日线、按时间键去重、拒绝乱序或按规则重算，不能累加重复成交量。

**验收**：追加未来价格不改变历史特征；股票顺序变化不改变输出；均匀复权缩放不改变无量纲信号；平盘不会产生无穷/伪强买信号；行业未知不生成虚构行业数据。

### T03｜公式路线

**新增**：`core/strategies/formula_v2.py`。  
**依赖**：T02。

实现第 5 节股票与行业分数、h=1/3/5、固定三个候选、解释字段和训练期固定分箱均值。分数/概率类型明确分开。

**验收**：独立手算小 fixture 与输出一致；所有组/每 horizon 权重和为 1；F0 可在不联网、不使用 Jev 的情况下独立运行。

### T04｜Jev 真实接口、缓存与概率路线

**新增**：`core/models/jev_client.py`、`core/strategies/jev_v2.py`、`configs/jev_questions_v1.json`。  
**依赖**：T02；可与 T03 并行。

实现第 6 节 API、state 白名单、15 个问题、股票/行业独立目标、响应检查、两种概率池、缓存、预算、限速、断点和错误状态。Jev 不是交易执行器，不读私有持仓信息，不读新闻，不调用其他模型。

**验收**：无 key 状态准确；有效 schema fixture 能解析；异常概率不能变成有效预测；真实密钥可用时完成一次小规模在线 smoke，记录脱敏模型 ID/usage，不把 mock smoke 写成真实调用成功。

### T05｜标签、校准与固定选模流水线

**新增**：`core/models/calibration_v2.py`；标签、划分和实验协调放 `core/pipeline/technical_v2.py` 的对应函数或同目录小模块。  
**依赖**：T02–T04。

实现第 2.3/9 节；标签构造器和特征构造器的输入接口隔离。选择文件保存候选、区间、支持数、阈值、费用版本、选择规则与胜出/未胜出原因。不要引入神经网络训练框架或另外训练“JeV 替代模型”。

**验收**：校准/选模参数未读取最终测试；cohort 选择不依赖未来存活；历史 Jev 和真实前瞻严格区分；缺历史时仍能交付可运行代码并报告不足。

### T06｜动作与纸面执行

**新增**：`core/backtest/portfolio_v2.py`、`execution_v2.py`、`metrics_v2.py`。  
**修改**：旧引擎/旧报告只增加明确的 legacy/诊断标签，不偷偷改写历史文件。  
**依赖**：T01、T03、T04；验证指标最终接 T05。

实现第 7–8 节；股票/行业不混下单，账户 A/B 隔离，实际持仓只读。至少覆盖现金、股数、T+1、容量、涨跌停、费用、公司行动、未成交和未解决估值。日线假设清晰，无需开发 tick 订单簿。

**验收**：现金和股份可逐笔对账；没有证券级规则则不生成错误数量；同一委托重复处理无第二笔交易；无法卖出时留仓，不强行结算盈利。

### T07｜后台全市场 DAG 与版本化发布

**修改**：`core/background/precompute_worker.py`、`snapshot_store.py`、必要的 `task_rules.py`。  
**新增**：`core/pipeline/technical_v2.py` 完整全市场运行入口。  
**依赖**：T01–T06。

新增 `--profile technical_v2`；此 profile 只调度以下 V2 任务，不启动旧资金流/AI/题材任务：

```text
data_v2_sync
  -> features_v2
     -> formula_v2
     -> jev_v2
  -> paper_v2 / evaluate_matured_v2
  -> publish_technical_v2
```

依赖以同一 `run_id/as_of/data_hash` 的产物验证，不只检查某个旧快照存在。公式完成可先发布 A 结果和 B 的 PENDING/UNAVAILABLE 状态；不能将昨日 B 概率贴上今日日期。B 完成后追加新 publication_id 的快照，数据截面不变；已记录的实际概率不可覆盖。

现有 atomic write 可复用；新增 run 目录和 latest manifest 指针，不把整个全市场历史反复塞进一个 JSON。任务返回 typed status，不能无条件 `write_snapshot(status='ok')`。重新运行同一配置/日期只补缺失计算和响应。规范化结果由全名册 LEFT JOIN 已完成预测构造缺失状态行，第一次真实有效预测追加保存；后续发布新 publication_id，只更新 latest 指针，不回写旧概率。

**验收**：A 不依赖 Jev 网络完成；断网不会刷新旧数据的 as_of；PARTIAL 与 OK 明确；重复触发不会重复收费/记账；进度显示真实完成 entity 数量而非假进度。

### T08｜V2 展示入口，不重写整份旧 app

**新增**：`app_v2.py`、`ui/technical_v2.py`；保留 `app.py` 为 legacy 入口。  
**修改**：README 默认运行命令改为 `streamlit run app_v2.py`，注明原入口保留。  
**依赖**：T07。

页面顺序：运行/数据状态 → 行业概览 → 行业内全部股票 → A/B 并列对比 → 条件性操作/纸面持仓 → 评估与下载。列表分页/按行业过滤只改变展示，不改变全覆盖计算范围。

每行显示真实行情日期、观察趋势、预测 horizon、两路结果、概率验证状态、主要贡献因子、风险、条件性动作和缺失原因。支持导出当前完整快照 CSV/JSON。内部 0.012 显示 1.20%，不能再次乘百；score=78 显示 78 分，不显示 78%。

界面只读快照、提交刷新请求；禁止 UI rerun 触发训练、全市场拉取或 Jev 批量调用。旧 UI 的基本面字段不能混入新页 state。

**验收**：一只股票可在行业页找到；风险/无数据股票仍有状态行；用户看得出 A 分数与 B 概率区别；显示范围及数据时间不误导。

### T09｜统一 CLI、配置与部署说明

**新增**：`scripts/v2.py`、`configs/technical_v2.json`、`requirements-dev.txt`、实际验证的依赖锁文件；新增 V2 服务模板/安装选项，沿用 `deploy/systemd` 目录。  
**依赖**：T01–T08。

必须提供以下语义一致的命令（实现这些子命令，而不是在交接文档写不存在的命令）：

```bash
python -m scripts.v2 doctor --mode real
python -m scripts.v2 sync --mode real --history-sessions 800
python -m scripts.v2 analyze --mode real --as-of latest --methods formula,jev
python -m scripts.v2 fit-evaluate --mode real --protocol fixed-v1
python -m scripts.v2 paper --mode real --methods formula,jev
python -m scripts.v2 evaluate-matured --mode real
python -m scripts.v2 export --run-id <实际运行ID>
python -m scripts.v2 demo --seed 20260923
python -m scripts.v2 audit-release --run-id <实际运行ID>
streamlit run app_v2.py
python -m core.background.precompute_worker --profile technical_v2
```

`latest` 必须解析为最新**已完整验证**交易日，同时计算按日历/模式应有的 expected_as_of。若实际 as_of 早于 expected_as_of，标 STALE，仅供历史展示，不产生新增纸面委托；不把“最新可取得”自动当成“当前足够新”。CLI 返回 JSON 状态，退出码：0=本命令成功，2=缺外部前置条件/预算，3=计算或契约错误；局部失败要列出，不用 0 假装整项完成。demo 不调用收费 API，使用显式演示响应，仅能证明流程。

systemd 模板使用明确 WorkingDirectory、venv Python、配置和必要目录；不硬编码其他项目路径。新服务与旧服务不同时写同一 V2 输出，安装前检查冲突。交付模板和启动说明即可，不擅自重启用户已有生产服务。

### T10｜必要回归、有限性能检查和实验执行

**新增/修改**：`tests/v2/` 与受影响旧测试，确保 unittest 可发现（必要时添加 `__init__.py`）。  
**依赖**：T01–T09。

执行第 11 节，跑本机允许的真实数据实验；外部失败照实记录，完成可运行的其余范围。实验状态和代码测试状态分别填写。不能因 Jev 不盈利无限循环调参。

### T11｜交接、提交、推送与 GitHub 发布

**新增**：第 12–13 节文档及可分享结果。  
**依赖**：T10。

以“远端确实有文件和结果”为完成条件，不只给本地路径。完成主分支外的提交、push、PR（可用时）、预发布及远端校验；不强推，不自动合并 main。

---

## 11. 测试范围：只做保护研究结论与资金账本的检查

不建立复杂渗透测试、红队提示词库、随机 fuzz 平台、反复全量压力测试、追求 100% 覆盖率或无关架构重构。本轮必要测试限定为以下 **12 类**，每类使用小 fixture/参数化例子，可复用旧测试。

| 编号 | 必要检查 | 为什么不能省略 |
|---|---|---|
| C01 | real 模式不自动 mock；demo 与 real 输出隔离 | 防止基于随机行情推荐 |
| C02 | 数据单位、日期、交易日历、时区、重复行、来源 | 防止容量/日期计算错一个数量级 |
| C03 | 全名册行覆盖与状态；行业有效期/UNKNOWN；主板与分析范围分离 | 防止只算局部却声称全覆盖 |
| C04 | 追加未来不改变历史因子；输入顺序不影响；常数价格/分母边界 | 防止未来泄漏、错误归一化 |
| C05 | 15 因子/6 风险/权重手算；score 与 probability 类型；百分比展示 | 防止方向和数值含义错误 |
| C06 | label 入场退出日期、跨块标签清除、测试不可参与拟合 | 防止虚假样本外表现 |
| C07 | Jev 原生 schema、概率合法性、模型版本、无 key、预算、缓存命中 | 防止伪概率和重复费用；只用少量固定 fixture |
| C08 | 温度缩放和线性池、校准样本支持、只用校准/验证选择 | 防止把未校准概率称作胜率 |
| C09 | T+1、现金/股数、价格限制、买入价上限、证券买卖单位、同单去重 | 防止回测执行不成立 |
| C10 | 一次分红/拆股 fixture、未解决公司行动/退市留账、止损不能保证成交 | 防止收益和持仓凭空出现或消失 |
| C11 | 快照 as_of/hash 一致；旧结果不冒充新数据；UI 不直接发起重计算 | 防止运行结果串日/串模型 |
| C12 | CLI demo/真实缺前置状态、一次最小在线 Jev smoke（有 key 才做）、发布清单核验 | 证明入口与交付真实 |

执行节奏：开始跑一次旧回归建立基线；开发中只跑相关测试；最后跑一次总回归，失败只修复相关项目并重跑，不连续重复全套。默认使用现有 unittest，不为了测试框架引入大依赖。

单次性能检查：固定 200 股票 × 300 交易日合成 panel，只测因子/聚合流水线的耗时和峰值内存；再用实际可取得的一天真实数据检查全流程状态。不将合成 panel 收益当证据。记录机器规格和耗时，不预先承诺固定毫秒数；不做持续压测，不测试券商实盘。

Git 只做一次 staged diff 的敏感字段检查，确认未提交 `.env`、token、真实账户文件和无权分享的数据。它是发布卫生检查，不扩展为大型安全审计。

---
## 12. 最终交接文档与公开结果

提交 `docs/v2/HANDOFF.md`，必须包含下列十项，使用真实内容而不是空模板：

1. 基线 SHA、代码提交 SHA、工作分支、release tag、PR/发布页（成功后提供）。
2. 已完成模块与 T00–T11 对照；未完成项的具体错误/权限/数据原因。
3. 实际数据来源、日期、股票/行业覆盖、未验证旧缓存、当前数据库 schema 版本。
4. 15 因子、6 风险指标、股票/行业公式、Jev 问题模板和概率池版本。
5. 样本划分、cohort、全部候选和选择规则，不能只留下胜者。
6. 公式/JeV 各自真实结果；回放/前瞻/合成测试身份；未成熟指标明确 null。
7. 运行命令、环境变量名称、数据库迁移和恢复方法；不要放密钥值。
8. 测试命令、实际通过/失败/跳过数及原因；跳过网络测试不能写“在线通过”。
9. 现有服务/账户是否被修改，纸面账户与真实只读持仓如何区分。
10. 一次标准日运行如何检查数据、概率、订单、覆盖；遇到 key/数据/预算问题恢复哪条命令。

同时提交：

```text
docs/v2/REPO_AUDIT.md
docs/v2/DATA_CONTRACT.md
docs/v2/FACTOR_SPEC.md
docs/v2/MODEL_CARD.md
docs/v2/EVALUATION_REPORT.md
docs/v2/HANDOFF.md
docs/v2/EXECUTION_PROMPT.md       # 本指令的归档副本
artifacts/technical_v2/<run_id>/run_manifest.json
artifacts/technical_v2/<run_id>/selection.json
artifacts/technical_v2/<run_id>/summary_metrics.csv
artifacts/technical_v2/<run_id>/coverage_summary.csv
artifacts/technical_v2/<run_id>/TEST_REPORT.md
artifacts/technical_v2/<run_id>/artifact_manifest.json
```

公开仓库默认上传代码、配置、合成 fixture、脱敏汇总指标、数据重建说明和 hash。真实原始行情全库、完整 Jev state、用户持仓/现金/账号和 `.env` 不上传；有明确再分发许可的数据才可作为 release asset。未共享原始数据时提供来源、查询区间和复算命令，不宣称公开包包含原始全量数据。

数据缺失时 `EVALUATION_REPORT.md` 也必须存在，说明哪些实验未执行、为什么、已经完成了哪些可验证检查。不要用历史 report.md 的数字填补新版空白。

---

## 13. GitHub 同步与发布流程（执行到远端验证）

### 13.1 两段提交避免版本证据循环

- 提交 A：本轮代码、配置、必要测试、算法文档。记录 `code_commit=A`。
- 在 A 的代码上运行实验；产物记录 A、config_hash、data_manifest_hash。发生代码修复则产生新的代码提交 A2，受影响实验重跑并引用 A2。
- 提交 B：真实实验摘要、审核与交接文档。HANDOFF 引用代码提交 A/A2 和 release tag，不尝试在提交 B 的文件里写 B 自己的 SHA。
- tag 指向 B。远端校验后生成 `publish_receipt.json` 作为 release asset，包含 A/A2、B、tag、远端 SHA 和附件 hash。不要为了把自己 hash 写回自己文件无限产生新提交。

### 13.2 命令流程

先确保 origin 是用户仓库，且工作树没有要被覆盖的用户更改。分支已存在时接续该分支；若同名远端发生冲突，新建 `codex/technical-v2-<UTC时间戳>` 并记录，不 force push。

```bash
git status --short
git remote -v
git fetch origin
# 分支创建/切换只在未覆盖用户更改的前提下执行；禁止 reset --hard。
# 提交 A 只 stage 本轮允许的代码、配置、测试与文档路径。
git add <本轮代码和文档的实际文件清单>
git diff --cached --check
git commit -m "feat: add technical-only formula and Jev analysis pipelines"
# 使用该代码提交运行指定实验、生成真实报告，完成必要回归。
git add <本轮可分享结果和交接文件的实际清单>
git diff --cached --check
git commit -m "docs: publish technical v2 evidence and handoff"
git push -u origin HEAD
```

不要直接 `git add .` 把既有缓存、私人文件或无关项目更改带进去。上面尖括号是必须由 `git status` 得到的实际清单，不是要原样执行的命令；由开发者明确列出本轮变更路径。

发布 tag 规则：`technical-v2-YYYYMMDD-<B前7位>`，日期取实际 UTC 发布日。先完成本地 tag，再 push；同名已存在则检查目标 SHA，不能覆盖。

已安装且授权 `gh` 时：

```bash
gh auth status
gh pr create --repo Orangekostar/standard --base main --head <实际分支> \
  --title "Technical V2: formula + Jev probability" \
  --body-file docs/v2/PR_BODY.md
# PR 已存在则读取其 URL，不重复创建。
gh release create <实际tag> <已核验的公开结果压缩包> <SHA256SUMS.txt> \
  --repo Orangekostar/standard --verify-tag --prerelease \
  --title "Technical V2 research release" \
  --notes-file docs/v2/RELEASE_NOTES.md
```

提前生成所引用的 PR_BODY/RELEASE_NOTES 文件。没有 gh 可使用已授权的 GitHub API/连接器做同等操作，不凭空假定工具已安装。git push 成功但 PR/release 不可用时，仓库代码/文档已发布与 release 未完成分别报告，不能把两个状态合并。

### 13.3 必须核验

`git ls-remote origin refs/heads/<实际分支>` 的 SHA 等于本地 B；tag 指向 B；远端能够读取 HANDOFF、核心新模块、配置和结果摘要。release 附件（若已创建）的文件大小/hash 与本地一致。

`publish_receipt.json` 字段至少：repository、branch、code_commit、release_commit、tag、remote_branch_sha、push_status、pr_url、release_url、asset_sha256、verified_at。先确认 remote 再写成功状态。

推送/认证失败时只进行一次有针对性的恢复尝试，保留 `git diff`/提交 bundle 或 patch，以及可分享的交付压缩包。不得反复改代理、遍历密钥、进行无关服务器操作。报告 `PUBLISH_BLOCKED` 和已完成的本地成果。

---

## 14. Codex 最终审核与停止条件

交付前逐项填写 PASS / FAIL / NOT_RUN_WITH_REASON，不能统一填 PASS：

| 审核项 | 检查内容 |
|---|---|
| 目标一致性 | 两条路线都存在；不含聊天 LLM/Agent；纯技术输入白名单生效 |
| 证据一致性 | 现状结论对应实际文件/提交；未运行实验无伪造数字 |
| 数据真实性 | real 不自动 mock；旧来源未明数据未混入正式训练；日期/单位已检查 |
| 覆盖 | 全分析名册状态行齐全；行业与交易权限不混；有效预测覆盖另计 |
| 因果性 | 因子前缀不变；复权版本、行业有效期、收益标签和数据 cutoff 一致 |
| 概率 | API 确为 Jev；原始/校准概率和 score 分开；不做独立性乘法 |
| 交易解释 | 开盘区间/T+1/股数/现金/费用/停牌/公司行动一致；不把诊断收益当可成交收益 |
| 选模 | 候选有上限，验证选模，最终测试冻结；历史 Jev 不冒充真前瞻 |
| 运行 | CLI、V2 页面、Worker profile、快照/重跑/错误状态存在并已验证相应范围 |
| 测试节制 | 只做第 11 节必要检查，没有无关安全/大规模压力测试 |
| 发布 | 代码、交接和结果已提交；远端 SHA 与附件有真实验证或明确失败状态 |

最终状态用对象表达，避免一个“PASS”掩盖所有问题：

```json
{
  "software": "COMPLETE | PARTIAL",
  "data": "VERIFIED | PARTIAL | INSUFFICIENT | UNAVAILABLE",
  "formula": "VALIDATED_RESEARCH | UNVALIDATED_DEFAULT | NOT_READY",
  "jev": "JEV_FORWARD_VALIDATED_FOR_SHADOW | JEV_SHADOW_UNVALIDATED | UNAVAILABLE_CREDENTIALS | NOT_READY",
  "evaluation": "COMPLETE | PARTIAL | NOT_RUN_WITH_REASON",
  "github": "PUSH_VERIFIED | PARTIAL_RELEASE | PUBLISH_BLOCKED",
  "live_trading": "NOT_CONNECTED"
}
```

上例的竖线表示允许值，实际输出必须选单个值，不把选项列表当真实状态。工程没有完成的功能不能靠 NOT_RUN 掩盖；外部条件不足则精确说明，不为提高完成率造数据。

停止条件：T00–T11 的代码工作和本机可执行验证完成，当前数据/模型/发布状态如实记录；已按权限推送或提供真实推送失败证据。不要无限增加因子、反复看最终测试、等待未来市场结果或扩展到券商实盘。

---

## 15. 资料与证据索引

### 15.1 仓库永久来源

基线根地址：
`https://github.com/Orangekostar/standard/tree/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4`

以下文件与第 1 节 R 编号共同构成可复核依据；大型文件的函数名是主要定位方式。Codex 开工后补充自己当前提交对应行号，不编造旧行号。

| 来源 | 永久链接 | 对应证据 |
|---|---|---|
| 数据和 DB | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/core/data/data_manager.py | R01–04、R13–14 |
| 证券范围 | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/core/data/symbols.py | R15 |
| 因子注册 | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/core/factors/registry.py | R05 |
| 技术因子 | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/core/factors/technical.py | R06、R08 |
| 量价因子 | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/core/factors/volume_price.py | R07 |
| 因子策略 | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/core/strategies/factor_selection.py | R09–10 |
| 旧引擎 | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/core/backtest/engine.py | R11 |
| 三阳回踩 | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/core/strategies/three_bull_pullback.py | R12 |
| Worker | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/core/background/precompute_worker.py | R16 |
| 快照 | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/core/background/snapshot_store.py | R16 |
| UI | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/app.py | R17 |
| 单股 runner | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/core/pipeline/runner.py | R17 |
| 运行测试 | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/tests/test_data_manager_runtime.py | R18 |
| 旧报告 | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/report.md | R19 |
| 配置 | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/config.py | R20 |
| 依赖 | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/requirements.txt | R20 |
| 原型说明 | https://github.com/Orangekostar/standard/blob/32279ec22d447675cbfcc6bdbf9b68f10e37bbf4/README.md | 原运行入口与架构 |

### 15.2 Jev 官方资料

- **J1 — 模型、版本与定制方式**：https://docs.typesafe.ai/models 。核验固定版本、输入格式/上限、无用户微调、别名会变化。价格/限速在实际开发时再核对，不能长期写死。
- **J2 — HTTP API**：https://docs.typesafe.ai/api 。核验 endpoint、choice 请求/响应、qid 不送入底层模型、usage、错误码。
- **J3 — Jev 1.13 局限**：https://docs.typesafe.ai/model-jaggedness/jev-1.13 。说明算术、日期、长状态等限制；因此数学逻辑留在代码。此页面不是金融有效性研究。
- **J4 — confidence 定义**：https://docs.typesafe.ai/confidence 。confidence 来自输出分布，不是另一份独立预测证据。
- **J5 — 分解与代码聚合**：https://docs.typesafe.ai/patterns/composite-scoring 。借鉴原子判断后代码聚合；本方案的股票概率池不是官方已验证的 A 股产品。

### 15.3 数据与规则资料

- **D1 — Tushare daily**：https://tushare.pro/document/2?doc_id=27 。未复权、停牌无行情、15–16 点更新、量额单位与单次上限。
- **D2 — Tushare adj_factor**：https://tushare.pro/document/2?doc_id=28 。复权字段、接口与权限。
- **D3 — Tushare stk_limit**：https://tushare.pro/document/2?doc_id=183 。证券每日涨跌停价，不能只用固定 9.5% 阈值。
- **D4 — Tushare index_member_all**：https://tushare.pro/document/2?doc_id=335 。in_date/out_date/is_new，历史成员不能只取最新。
- **D5 — Tushare trade_cal**：https://tushare.pro/document/2?doc_id=26 。交易日历而不是 bdate_range。
- **D6 — 上交所 2026 年规则发布页**：https://www.sse.com.cn/lawandrules/sselawsrules2025/trade/universal/c/c_20260424_10816492.shtml 。发布页明确 2026-07-06 生效，部分条款另有暂缓实施说明；执行时同时核对附件与暂缓条文。深市和不同板块必须分别核对实际有效规则，不用一份沪市公告推导所有证券规则。

### 15.4 论文/研究工作流依据

- **P1 — Qlib: An AI-oriented Quantitative Investment Platform**，Yang 等，2020：https://arxiv.org/abs/2009.11189 。参考数据→因子/模型→组合→评估分层；不要求把本仓库迁移到 Qlib，也不背书默认成交/费用配置。
- **P2 — 101 Formulaic Alphas**，Kakushadze，2016：https://arxiv.org/abs/1601.00991 。说明公式化量价信号是一条可研究路线；不能把本仓库自定义 Alpha#6/#9 名称当成已复现该论文，更不据此宣布本方案有效。
- **P3 — On Calibration of Modern Neural Networks**，Guo 等，ICML 2017：https://proceedings.mlr.press/v70/guo17a.html 。参考后处理温度校准；原论文实验不是 Jev A 股，校准增益必须本地验证。
- **P4 — The Probability of Backtest Overfitting**，Bailey 等，期刊版 2017：https://scholarworks.wmich.edu/math_pubs/42/ 。支持认真处理多次回测筛选；本轮控制候选/保护测试集不等于已经完成论文的 PBO/CSCV 估计，不伪造 PBO 数值。

---

## 16. 默认参数的机器可读副本

同包 `standard_v2_plan_config.json` 对应本指令的固定数值，Codex 将其转换/复制为仓库 `configs/technical_v2.json`，允许补充数据库路径和实际数据商配置，但不能静默改变因子权重、目标定义和实验切分。任何参数改动必须更新 config_id/hash 并说明理由。

仅拿到此 MD 时，以上各节已经列出全部策略/预算/实验初值；配置副本只是减少转录错误，不是未提供的外部前置文件。

**交付摘要请只报告事实**：完成了什么，哪些数据/模型验证真实通过，哪些仍是 shadow 或被外部条件阻塞，远端分支/提交/tag/交接文档在哪里。不要用“已全面超越”“稳定盈利”“概率绝对准确”替代测量结果。
