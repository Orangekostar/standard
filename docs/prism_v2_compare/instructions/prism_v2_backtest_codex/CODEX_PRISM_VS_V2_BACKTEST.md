# Codex 最终执行指令：Prism 思路 A 股适配版 vs Technical V2 原策略

版本：1.0｜编制日期：2026-09-28｜任务类型：开发、真实历史回测、比较、交接与 Git 发布。

## 0. 任务目标和完成定义

你要直接完成开发与实验，不要只交计划：在 `Orangekostar/standard` 内，用同一份沪深 A 股历史数据、同一交易日历、费用和撮合引擎，比较原版 Technical V2 公式策略与本文件明确定义的 Prism 思路 A 股适配策略，并给出“哪一个更好，或者证据为何不足”的真实结论。

本轮主实验完全使用确定性技术规则，不引入新闻、财报、LLM 或 Agent，不调用 Jev 付费接口。现有 Jev 功能保留不删除，但不阻塞本次比较。没有当时实际记录的预测，禁止现在批量调用 Jev 后冒充历史在线决策。

Prism 原项目交易的是 Solana/Meteora DLMM 流动性仓位。这里不是其 A 股官方复现：只迁移“市场状态→波动自适应风险距离→持仓退出→限制重复进场”的思想。策略正式命名 `PRISM_A_SHARE_V1`；不把 LP 手续费、TVL、无常损失、bin 或链上资金直接映射成 A 股收益。不宣称日线能测量订单簿、CVD、真实资金身份或毒性订单流。

最终必须交付：可运行代码、冻结协议、A/B/仓位对照的结果表、净值与成交/拒单明细、结论报告、交接文档，以及 GitHub 分支上的实际文件。无交易、收益为负或无法判胜，也是有效结果；禁止为了产生交易而临时放宽阈值。

## 1. 已核对的源码基线与证据

### 1.1 固定版本

- V2：`Orangekostar/standard`，`codex/technical-v2`，`0809f0ef38e2e776cc80b231d3e188edcddf3173`。
- Prism：`irfndi/prism-liquidity-agent`，`d15c63ef7a5d426ad71f4bbf4dae05c94ba0bc02`。
- 用户此前报告的约800日行情、615个成熟信号日、没有合格候选，只是待核实的既有记录，不得硬编码为本轮数据统计。

先只读检查当前工作区、远端 SHA、`git status`、本地未提交修改。以以上 V2 SHA 建立独立实验 worktree；不要 checkout/reset 正在运行 UI/Worker 的目录。远端若发生变化，记录差异，本轮仍保留上述固定策略基线，不默默更换被比较的策略。确需本地已验证的数据修复时，将最小补丁单独记录，作为两组共享修复。

建议新分支 `research/prism-v2-backtest`。分支或 worktree 已存在时检查其协议与数据哈希，恢复任务；不得 force reset、覆盖其他开发结果或 force push。

### 1.2 开发前必须阅读的文件及实际含义

| 现有文件/函数 | 已核对的事实 | 本轮如何处理 |
|---|---|---|
| `core/factors/technical_v2.py` | F01–F15、Q01–Q06；复权、波动、ATR、行业/市场特征 | 复用原定义，缓存共享特征，不给B单独修因子 |
| `core/analysis/sector_v2.py` | 等权市场/行业、成员关系与广度 | 复用；核查日期有效性，增加因果的状态变化特征 |
| `core/strategies/formula_v2.py` | F0/F1/F2、固定分组权重、收益分箱与收缩估计 | A/B主比较固定F0，训练段拟合同一收益分箱 |
| `core/strategies/intent_v2.py` | 5期主操作、65/55买入观察条件、持仓相关动作 | 主实验按真实持仓调用，不传`None`后期待自动有买单 |
| `core/pipeline/evaluation_v2.py::_simulate_formula_portfolio` | 旧历史回测已传持仓与`overextended`，不同于在线入口；默认预期成本常数0.00212 | 不把在线接线缺口误认为旧回测原因；保留原生口径记录，主比较统一成本修正 |
| 同文件 `_simulate_formula_portfolio` | 按期退出和分数动作；保存计划止损不等于已经执行价格止损 | A按原生历史策略保留，不偷偷增加价格止损；B的价格退出是显式策略差异 |
| 同文件 `run_fixed_evaluation` | 只运行旧候选，选模通过才打开测试；不是通用A/B比较器 | 新建独立比较入口，不重复跑该命令期待出现Prism结果 |
| 同文件 `_factor_ic` | 输入包含所有split，函数本身没有排除test | 查旧因子汇总是否用过测试段；“最终净值未跑”不等于完全未接触测试数据 |
| `core/pipeline/technical_v2.py` | 504信号日分252/63/63/126，120日预热，按`label_end_date`清除跨界样本 | 复用日期口径；冻结后才能看测试表现 |
| `core/backtest/execution_v2.py`、`portfolio_v2.py`、`metrics_v2.py` | 账户、费用、数量限制、订单、净值、指标 | 共用引擎；只做必要的对称正确性修复 |
| `scripts/v2.py` | 线上`analyze`固定F0；有运行锁与生产publication写入 | 本实验不调用线上分析、发布或paper命令，不改生产latest |
| Prism `engine/strategy-service.ts` | 波动自适应区间、趋势/震荡配置、结果反馈 | 只迁移确定性状态与区间管理；本轮不做在线权重进化 |
| Prism `engine/churn-guard.ts` | 限制重复进入，不限制必要退出 | 映射为A股交易日冷却期 |
| Prism `engine/jev-service.ts`、`program.ts` | 专项Jev以shadow为主，另有纸面减仓开关 | 不将其当成已训练的A股概率模型；本轮不启用 |

Prism只下载/阅读上述相关源码和许可证，不需要安装Bun、Solana SDK、向量数据库或执行其启动脚本。若复制代码，保留MIT许可和来源；自行实现A股数学规则也要记录思想来源。

## 2. 实验对象：两个主策略，一个解释性对照

| ID | 定义 | 角色 |
|---|---|---|
| `A0_V2_F0` | 原V2的F0信号、收益门槛、持仓动作、固定5期退出；共享撮合正确性修复 | 主基线 |
| `B0_PRISM_A_SHARE_V1` | 完全相同F0信号和净收益门槛，加本文件第6节的状态/自适应风险距离/价格退出/冷却 | 主挑战者 |
| `C0_V2_EXPOSURE_CONTROL` | A0策略不变，使用验证段确定并冻结的统一风险预算缩放 | 检查B是否只因少买而更稳 |
| `A_NATIVE_REFERENCE` | 旧原生口径或已有原生评估制品 | 仅溯源，不参与主判胜 |

A/B的原始15因子、方向得分、涨平跌分类必须相同。此次回答的是“Prism式持仓管理能否改善原信号的交易表现”，不是“Prism发现了更强的方向因子”。筛选后的样本不同，不得拿其条件胜率当作全市场预测能力提高。

本轮不搜索F1/F2，不调整Jev问题，不做动态因子学习。原三阳回踩策略、旧`BacktestEngine`和旧开盘到当日收盘收益不是本轮主基线。读取原评估结果核对身份，不能拿不同时段的旧摘要数字与新净值直接比较。

## 3. 运行隔离与真实数据冻结

### 3.1 环境

已知服务器工作区：`/home/ww/vv/quant/.worktrees/technical-v2`。
已知已安装依赖的解释器：`/home/ww/vv/quant/.venv/bin/python`。
必须实际验证路径和导入模块；不要用Conda `(base)` 的裸`python`，不要升级已验证环境。

优先读取工作区`.env`所解析的数据路径和显式覆盖值；打印路径，不打印密钥。当前默认源库是工作区内的`cache/v2/market.db`，但不能只凭默认值假定实际位置。

### 3.2 数据副本

使用SQLite `Connection.backup()`或等价SQLite在线备份创建一致性快照。源连接只读，禁止复制正在写入的裸`.db`来代替一致性备份。备份保存在实验目录，禁止改写源库。

本实验使用独立的：

- 市场快照：`cache/experiments/prism_v2/<snapshot_id>/market_snapshot.db`
- 运行锁：`cache/experiments/prism_v2/<snapshot_id>/.compare.lock`
- 每策略/每阶段/每费用情景的模拟账户库；不能共用账户状态。
- 结果：`artifacts/prism_v2_compare/<run_id>/`。

不用生产`artifacts/technical_v2/.command.lock`，不用生产paper账户，不修改生产`latest.json`，不启动或重启Worker/UI，不删除任何生产锁。禁止执行全800日强制同步。

一次流式计算快照SHA256，记录文件大小、备份时间、源数据库路径及数据表版本；不要每个策略把全库转成巨型Python字典重新哈希。源库后续更新不进入本轮实验。失败重启复用已核验的快照和特征。

### 3.3 数据审计与有效范围

先记录：原始行情/复权/交易状态的日期范围、逐日股票数、上市/退市资料、行业历史版本、审计完整性、公司行动数据是否足够、真实/演示数据比例。源库仅允许真实行情参与收益报告。

选择`D`为冻结快照的最新审计完整交易日，不能使用系统日期或单纯`MAX(daily_raw.date)`。沪深日历必须核对，按实际开市session推进；不使用自然日或自行生成工作日日历。

分析名册包含当前库能提供的沪深A股历史记录，逐日按已知上市/退市日期和当时交易状态确定资格；不能用当前“ST”名称或当前行业分类回填全部历史。若历史退市证券、证券状态或当时行业版本不齐：仍做现有数据范围内的成对研究，但输出`UNIVERSE_HISTORY_LIMITED`等具体范围标记，不宣称完整A股、无幸存者偏差。不能仅为B筛出信息更完整的行业。

第三方脏行仅按已定义的输入规范隔离并统计，不用模拟行代替。特征缺失导致股票不可用时两组用同一状态，不直接删除本该在名册中的记录；统计每天的覆盖率与所有拒绝原因。

公司行动必须单独核对：有可靠记录则两组共用现有分红/送转处理。若缺失，允许先跑明确标为`RAW_PRICE_LEDGER_CORPORATE_ACTIONS_INCOMPLETE`的诊断回放，并输出复权价格标签诊断；不能把复权开盘标签直接叫账户净收益，不能悄悄把现金分红换算为实际新增股份。此时主判胜降为口径受限，继续交付代码、数据缺项和可得结果，不假装完整净收益评测通过。

## 4. 日期切分与信息隔离

### 4.1 复用但核实旧切分

优先读取用户已有固定评估的`dataset_manifest.json`和`split_assignments`，核对实际日期、数据快照范围、源码版本。若它与本次冻结数据在对应日期不一致，保留旧记录作为`A_NATIVE_REFERENCE`，主实验按本次快照建立自己的切分，不混用不同数据。

新切分沿用`build_fixed_split`：最后504个5期成熟信号日，依次train252、calibration63、validation63、test126；每段起点之前至少120个交易日作为特征预热。只冻结一次，A/B/C不得独立重选日期。

- train：拟合同一F0、股票、h=5的收益分箱，绝不拿测试段收益拟合。
- calibration：本轮不拟合Jev，也不额外调参数；保留原有时间隔离作用。
- validation：检查两主策略，并计算C组的预算缩放参数；不搜索B规则。
- test：冻结协议和C参数后，一次成对回放A/B/C；两个63日子段仅作归因，账户连续，不在中间重置。

训练/校准/验证样本的`label_end_date`必须严格早于下一段首日。策略只看截至t收盘的数据，训练使用的标签也必须在允许的拟合边界前成熟。

回测交易窗口统一：每段从第一个可用信号日收盘开始，到最后一个共同允许信号日后的第6个交易日收盘结束，包含无交易日并使用相同起止日期。验证段只保留计划5期退出不跨测试起点的信号。测试尾段停止新开仓、保留最多6个session的退出/结算尾部；若受停牌/跌停等约束未能卖出，保留持仓按规则估值并记录，不能凭空清仓。无足够尾部行情的日期不得当成成熟可比信号日。

### 4.2 关于此前“最终测试未开启”

用户本次请求是完成策略比较，允许新实验命名空间在策略冻结后做一次最终历史比较；不授权改写旧`selection.json`/旧`final_test_opened`记录，不自动发布线上赢家。

必须检查并披露：旧`_factor_ic()`可能已经用所有split计算过因子统计。因此分开记录`old_portfolio_test_executed`和`old_test_summary_exposure`。无法证实未接触时用`UNKNOWN/REUSED_HOLDOUT`，不称“从未看过的独立测试”。这不阻止本轮完成成对历史比较，但限制泛化结论。

不得调用旧`run_fixed_evaluation()`并因`NO_QUALIFIED_CANDIDATE`就停止新比较。即使A/B没通过旧上线门槛，也输出一次被冻结策略的研究回放；上线资格仍单独记录。零交易或样本不足不能通过改阈值“修复”。

全期`factor_ic`、测试因子胜率和测试分组收益只能在冻结后生成，不能用于开发B。结果已产出后仅允许修复真实实现错误；必须记`TEST_EXPOSED_BUGFIX`，两组对称重跑受影响部分，不再称首次盲测，不追加策略搜索。

不足504日时：保留已可用的真实诊断与完成代码，不伪造日历或自动缩短切分以产生胜者；报告具体还差多少成熟日期/预热数据。

## 5. A0：原V2策略精确定义

复用当前`score_stock(features,horizon,"F0_BALANCED")`，h=1/3/5。主交易只用5期及其3期确认，不把三个周期当三笔订单。

F0分组：T=(F01,F02,F03)，R=(F04,F05)，S=(F06,F07,F08)，V=(F09,F10,F11)，C=(F12,F13,F14,F15)。各组有效因子均值；总有效至少12/15且各组非空。

```text
Score1 = 50 + 50*(.20*T + .15*R + .25*S + .25*V + .15*C)
Score3 = 50 + 50*(.25*T + .20*R + .20*S + .20*V + .15*C)
Score5 = 50 + 50*(.30*T + .20*R + .20*S + .15*V + .15*C)
```

调用原`derive_research_intent`并输入当时真实模拟持仓和可卖数量；不得把在线`holding_state=None`造成的无动作当成原策略收益。

- 空仓：Score5≥65且Score3≥55且未过度偏离，才形成买入观察；Score5≤40回避，其余等待。
- 已持仓：Score5≥65加仓观察；45≤Score5<65持有；40<Score5<45减仓观察；≤40卖出观察。
- `overextended`使用原定义，必须传入；不修改其含义。
- 训练段拟合并冻结原收益分箱及收缩模型。A/B使用同一模型，净优势必须大于0.001；收益估计缺失时不新增仓位，不强行默认有优势。
- 原始已实现历史策略按每lot实际入场日+5个交易日到期退出；加仓不得重置旧lot的退出日。
- 原生历史回测未执行价格止损，因此A0保留“分数退出＋到期退出”。原2.5×ATR/最低3%距离在A0只是仓位风险预算尺度。B新增的价格退出必须明确归入策略差异，不能偷偷同时加进A0后仍称完全原版。

保留`A_NATIVE_REFERENCE`说明：原费用门槛0.00212、原证券规则、原数据名单和缺失处理。优先核验已有制品，不再无意义重跑旧全量。必须将共用正确性修复后的A0与未修原生口径区分，不声称数值逐项完全相同。

## 6. B0：PRISM_A_SHARE_V1冻结规则

本节是明确提出的A股适配规则与研究初值，不是Prism仓库已有的A股参数。只实现以下三块：市场状态、波动自适应风险距离与价格退出、退出后冷却。其余信号/收益门槛/排名/账户约束与A0相同。

### 6.1 市场状态（只用截至t的数据）

市场指数、市场广度B_t使用V2现有等权市场context。市场对数收益`r_m`：

```text
ER20_t = abs(sum(r_m[t-19:t])) / sum(abs(r_m[t-19:t]))
ΔB5_t = B_t - B_(t-5)
σm20_t = std(r_m[t-19:t], ddof=0)
VRm_t = σm20_t / median(σm20[t-60:t-1])
```

记号区间含两端；`[t-60:t-1]`是前60个session，不含今天。ER分母为0则ER=0；VR参考波动为0、缺失或无足够窗口则状态UNKNOWN。NaN不能偷偷转换成false或0，停牌缺行不能把多日变化当成单日变化。共同特征输入先按code×交易所session对齐：无真实交易日线保留NaN；仅估值用的延续价格不得混进成交量/收益特征。此调整作为共享修复，不只应用于B。

按以下优先级判定：

1. 必要context/窗口缺失：`UNKNOWN`。
2. B_t<0.35，或`ΔB5≤-0.10且VRm≥1.5`：`STRESS`。
3. 市场20期累计对数收益>0、ER20≥0.35、ΔB5≥0：`TREND_EXPANSION`。
4. 其他：`RANGE`。

新增风险预算乘数：STRESS=0，TREND_EXPANSION=1，RANGE=0.5，UNKNOWN=1并记录回退到A规则。UNKNOWN不会解除A已有的市场/数据硬约束。

这个乘数只影响当天BUY/ADD数量；不直接把已有持仓乘0.5，不因Jev不可用减仓，不强制卖掉所有STRESS持仓。原总仓位广度上限0/30%/60%仍两组共用，不被B覆盖放宽。

### 6.2 波动自适应距离及收盘触发退出

股票使用原Q02=σ20、Q01=ATR14/C。参考波动为前60个session的σ20中位数；60个有效数值不足时回退，不临时缩短参考窗口：

```text
k_t = 2.5 * clip(σ20_t / median(σ20[t-60:t-1]), 0.5, 2.0)
d_fraction_t = clip(k_t * Q01_t, 0.03, 0.12)
```

参考波动缺失/非正时，取k=2.5并记录`ADAPTIVE_WIDTH_FALLBACK`；Q01缺失时仍遵循两组共同的不可建仓规则。不是靠未来收益选择k。

B的风险数量上限由原`0.005*NAV/stop_distance`改为`0.005*NAV/(d_fraction_t*参考价)`；所有其他约束取相同最小值。再乘市场状态乘数，向下取合法申报数量，绝不为了满足最小交易单位向上放大。必须在为后续候选预留现金/行业/总仓位之前完成乘数和费用检查；不能先按原数量占用预算、最后才统一把已生成订单缩水。

每个已成交lot保存：入场session、实际成交价、入场时d_fraction、到期日、最高收盘价、当前止损参考线。价格比较在同一复权尺度进行，执行仍使用当日未复权开盘价。分红/拆并时必须由共用公司行动处理同步调整数量、参考线；不能因除权机械触发错误止损。

- 初始线：以实际入场价换算到统一比较尺度后，乘`(1-d_fraction_signal)`。
- 当天收盘更新最高收盘价H_close，候选追踪线=`H_close - d_fraction_t*C_t`（两者同一价格尺度）。
- 新参考线=`max(旧参考线,候选追踪线)`；只能上移，不能因为波动增大放宽旧持仓风险。
- 当C_t≤参考线，生成下一交易日最早执行的SELL；不得按t日止损价或t日最低价假装已成交。
- 没触发价格线时，照常应用A的分数动作与固定5期到期。最早可卖日期、停牌、跌停仍由共用引擎处理。
- 同一code多lot时分别判断触发/到期数量，不能因一个lot到期而误卖未到期lot。整股汇总和零股处理必须一致。

退出优先级：数据/合约要求的共用退出→到期lot→B价格线触发lot→分数SELL/REDUCE→HOLD→ADD。合并同一股同一session的卖单数量，去重；已有待执行退出时不得新加仓。

### 6.3 退出后冷却

B在某股全部实际卖出后记录`last_flat_exit_session`。随后两个完整交易日不新增这只股票。判断条件为：

```text
new_entry_session_index - last_flat_exit_session_index > 2
```

例如周一实际全部卖出，最早周四开盘再次买入（假定中间均开市）。只有真实卖出后触发冷却，不能以生成SELL意图为起点；部分减仓不触发全仓冷却；冷却只限制BUY/ADD，不阻碍退出。

本轮不做恢复概率延期、不做越跌越补、不扩大5期最大计划持有期、不复制Prism的少样本在线阈值进化。

## 7. 共同撮合与账户：不能让B享有更好的成交假设

账户初始100万元；不融资、不卖空。每策略/每split从同样现金起步。最多10只、单股10%、行业25%、市场广度总暴露0/30%/60%、单股风险预算0.5%、日均成交额参与上限1%，沿用V2研究口径。

### 7.1 时序

- t收盘生成信号/订单；最早t+1开盘成交。
- t+1撮合时不读取当日收盘、最高、最低或全天成交量来决定已知的开盘动作。
- 开盘先处理之前已提交的退出；再处理之前已提交的买单。新买入lot当日不可卖。信号侧只能用截至t的资金与信息；不能把“计划卖出”当成已实现现金。
- 买入计划只在指定下一session有效；未成交则失效，下一信号重新判断。退出在不可卖/停牌/规则阻止时保留并重试，不能当作成交。
- 每次真实成交同步更新现金、费用、lot、订单状态。重复执行同一事件不产生第二笔资金变动。
- 一个code同时SELL和BUY时共用引擎统一优先退出并禁止同日新增；这个引擎规范对A/B/C一致，作为共享修复记录。

### 7.2 费用与价格

主费用用统一研究假设：双边佣金各0.0003、每笔最低5元、卖出附加0.0005、双边其他费用各0.00001，单边滑点0.001。附加费是固定研究假设，不宣称覆盖全部历史实际税率。

大额且买卖名义金额近似相同的参考往返成本：`2*.0003+.0005+2*.00001+2*.001=0.00312`；旧入口的0.00212与该组合不一致。主比较统一修正，不只给B使用更准确费用。

精确成交费用按各笔名义金额算，滑点只计入成交价一次。预期净收益门槛先用上述参考成本；生成候选数量后用该数量的买卖假设名义额加最低佣金重新检查净优势。若不足0.001则拒单，不反复搜索“恰好能过”的数量。费用压力情景将单边滑点提高至0.002，完整重走数量、门槛与撮合，不只在最终收益上手工减一笔钱。

原买入价格上限沿用`reference*(1+min(.02,.5*Q01))`按tick处理；滑点后价格不能突破该上限、实际涨跌停价或有效规则。若日线无法证明开盘可成交，使用对两组一致的保守规则，注明`OPEN_AUCTION_EXECUTION_APPROXIMATION`。不要声称日线重建了真实排队。

沪深主板/创业板/科创板规则按证券板块与生效日期取值；原统一`lot_size=100`不能冒充科创板实际申报规则。主比较用独立规则适配器支持最小申报量/递增量/零股清仓；有无法验证的板块则两组共同标记不可交易并保留覆盖记录，不临时只改B。源码或官方规则来源写入报告。

### 7.3 净值与账本

以现金+可估值持仓+明确可确认应收构成净值。停牌估值只能沿用最后真实可得价并标记`MARK_ONLY`，不能当新成交价；确定未可估值的持仓不得填0、删除或无条件把净值前向填充。终止上市后不能无限沿用停牌价假装可退出，应单列未解决资产风险并限制判胜。

校验费用扣除、买卖数量、FIFO/lot归属、分红送转、未成交和冻结资金。原`unfilled_order_ratio`不能混入“预筛拒绝”行计算；实际订单失败率与预筛拒绝率分别报告。部分成交/待退出不当作整笔已平仓。

## 8. C0仓位对照与实验预算

先只跑A0/B0的基准费用validation，统计包含所有交易日的平均实际毛暴露E_A和E_B。

```text
lambda_C = clip(E_B / E_A, 0, 1)   # E_A>0
```

E_A=0时lambda_C=1、控制组标`UNIDENTIFIABLE_ZERO_BASE_EXPOSURE`。E_B>E_A则lambda_C=1并标“无法向上匹配，避免给A增加原上限”；这不是精确风险匹配，只是验证段确定的减仓对照。

C保留A信号/退出，将A的单股、行业、总暴露和单股风险预算上限都乘lambda_C，现金限制不变，重新执行账本；不是事后把净值乘lambda。lambda在读取测试表现之前写入冻结配置。压力费用下不重估lambda。

固定12个主回放单元：A/B/C × validation/test × 基准/高滑点。A/B基准validation是上述控制参数拟合已完成的两单元，直接复用，不重复跑。C基准validation用于报告，不再回调lambda。没有策略阈值网格搜索。

先做50股×160session的离线业务烟测，再全市场计算一次特征。特征分块缓存，策略回放CPU运行；默认2个回放并发、最大8CPU线程，内存上限按机器可用内存的50%控制；不占GPU，不重复请求行情。大表按日期/股票分块，不建立所有组合重复的巨型DataFrame。

已有相同源数据、实现哈希、配置、策略ID、阶段、费用情景的成功单元直接复用。修改共同撮合代码则所有受影响组一起失效重算，不只重跑表现不好的组。

## 9. 结果表、指标与判胜规则

### 9.1 必须输出的指标

`comparison_summary.csv`每行一个策略×split×费用情景：

```text
strategy_id, split, cost_scenario, start_date, end_date,
initial_nav, final_nav, net_return, annualized_return,
max_drawdown, sharpe, daily_turnover, total_turnover,
average_gross_exposure, average_cash_ratio,
filled_buy_count, closed_trade_count, traded_dates,
closed_trade_win_rate, profit_factor,
fees_total, modeled_slippage_total,
rejected_candidate_count, unfilled_order_ratio,
open_position_count, unresolved_asset_count,
data_scope_status, comparison_status
```

收益和暴露都以小数存储，页面/Markdown格式化百分比，不写百分比字符串进数值列。标准夏普用日净收益、252年化、无风险率按0研究假设；零波动/零交易时夏普和胜率为空，不伪造0分或100%胜率。

同一测试账户连续计算的两个63日子段单列汇总。再报告年度/季度、行业、市场状态分组；只用于解释，不据此挑选获胜区间或改变策略。

必要图：全段A/B/C净值、回撤、实际仓位三个独立图。每个图标清相同起止时间、费用、数据范围；无须做漂亮前端或PPT。

### 9.2 配对不确定性

对相同测试session的A/B每日净收益做共同日期块抽样：固定seed=20260928、块长10个session、1000次moving-block bootstrap，每次对A/B使用相同块索引，截断至原长度。报告累计净收益差B−A的95%区间。不能按个股交易行独立抽样冒充独立样本。区间是历史稳定性描述，不是未来收益保证。

### 9.3 判胜（在测试前锁定）

先看数据/会计有效性，再看收益，最后结合风险。定义最大回撤为正的损失幅度。

- 两组无交易、任一关键组少于30笔完整平仓或20个实际交易日：`INSUFFICIENT_TRADING_EVIDENCE`，仍列真实数值。
- 关键净值无法完整核对：`ACCOUNTING_OR_DATA_INCONCLUSIVE`，不伪造胜者。
- B测试净收益>A，且B最大回撤≤A最大回撤+0.02：`B_RETURN_LEADER_WITHIN_RISK_TOLERANCE`。
- 对称条件A>B：`A_RETURN_LEADER_WITHIN_RISK_TOLERANCE`。
- 较高收益伴随超出2个百分点的额外回撤：`RETURN_RISK_TRADEOFF`，不能只挑夏普强行判胜。
- 数值近似相同（净收益差绝对值≤1e-6）：`TIE`。

再增加独立的证据字段，而不是把数值领先等同充分胜出：

- `paired_ci_supports_leader`：收益差区间是否完全支持同一领先者。
- `cost_stress_preserves_lead`：高滑点情景是否仍是同一领先者。
- `test_halves_consistent`：两个连续63日子段是否方向一致。
- `beats_exposure_control`：B是否优于C；若C与B接近或更好，只能说仓位/风险下降可能解释表现。
- `scope_limited`：幸存者/公司行动/旧测试暴露等限制。

只有口径充分、样本足够、数值领先且置信区间与成本压力均支持，才写“本次固定历史区间内证据支持某策略更好”；不能写成普遍实盘结论。只降低仓位导致回撤变小，不属于预测能力提升。

### 9.4 零交易与亏损也要分析

输出统一漏斗：名册→行情有效→因子有效→65/55条件→不过度偏离→可估收益→净优势足够→交易资格→状态/冷却→可分配数量→委托→成交→完整平仓。

每组保留首要阻断原因和全部原因，不能把“预测空动作”“无净收益估计”“市场不适合”混为一个状态。若全被净收益门槛挡住，不自动去掉门槛；额外输出候选的固定5期复权收益标签分布，清楚标成信号诊断而非账户回测。

Profit factor使用完整平仓后的净利润总额/净亏损绝对额；无亏损分母时保存null并附说明，不在JSON中写Infinity。双边总换手=成交名义金额之和/同期平均NAV，日均换手=总换手/共同交易日数，避免与单边或年化换手混淆。

因为A/B原始分数一致，全市场原始RankIC/分类准确率本应一致；B改变的是进入/退出与资金，不得凭选择后的不同样本宣称原始预测更准。

## 10. 最小开发任务与文件落点

先列实际函数映射后实现，不凭名字想当然新增第二套账户系统。

| 任务 | 新增/修改位置 | 交付与完成条件 |
|---|---|---|
| T0 冻结 | `docs/prism_v2_compare/SOURCE_AUDIT.md`、独立worktree | SHA、旧基线身份、共享修复、数据/旧test使用范围清楚 |
| T1 数据 | `core/pipeline/prism_compare_data.py` | 一致性备份、逐日资格、共享特征、labels、固定split，源库未被改写 |
| T2 策略 | `core/strategies/prism_a_share.py` | 第6节纯函数、状态、风险距离、冷却；关闭B附加模块时与A同输入同输出 |
| T3 共用回放 | `core/backtest/prism_compare_engine.py` | 复用`V2Store/PaperPortfolio/execute_open_orders`，支持策略回调、独立账户和正确订单时序 |
| T4 比较 | `core/pipeline/prism_comparison.py`、`scripts/compare_prism_v2.py` | 12个单元、缓存复用、冻结C、配对指标、报告 |
| T5 交接发布 | `docs/prism_v2_compare/HANDOFF.md`与结果目录 | 报告、源码、配置、结果摘要、日志/哈希、远端推送回执 |

`configs/prism_v2_compare_v1.json`须包含本文件所有实际使用参数，不能只在JSON写参数却在实现里另硬编码；单元测试检查读取结果。配置中保留`origin=RESEARCH_ADAPTATION_NOT_PRISM_OFFICIAL_A_SHARE_STRATEGY`。

新增入口最低支持：

```bash
python -m scripts.compare_prism_v2 prepare --source-db PATH --experiment-root PATH
python -m scripts.compare_prism_v2 validate --experiment-root PATH --config PATH
python -m scripts.compare_prism_v2 freeze --experiment-root PATH --config PATH
python -m scripts.compare_prism_v2 test --experiment-root PATH --frozen-manifest PATH
python -m scripts.compare_prism_v2 report --experiment-root PATH
```

另做`--all`顺序入口供一次执行，内部严格先prepare→validate→freeze→test→report；不能因旧选模没有合格模型而跳过所有比较。`test`已有完成哈希则复用，不重复查看/调参；无冻结清单拒绝运行。

这些是Codex要实现的新命令，不是宣称仓库现在已经存在。

## 11. 测试与预算：只测会改变结论的路径

不做渗透测试、模糊测试、长期压力测试、重复全套UI测试或盲目扩大样本搜索。优先沿用`tests/v2`的实际测试框架；没有pytest不为测试一个函数全环境升级。

至少覆盖以下10类定向检查，每类用少量最小fixture：

1. 追加未来行情不会改变过去因子/市场状态；变更test收益不会改变训练分箱或冻结C。
2. B附加功能全部关闭时，对共享A输入得到同动作、同订单和净值。
3. t收盘信号不在t开盘/收盘成交；新买入当日不卖；收盘触发止损只在后续可卖开盘尝试。
4. 费用单位、最低佣金、0.00312参考往返成本和滑点单次计入；压力情景重新撮合。
5. 多lot到期/价格退出/分数减仓去重，未成交不扣现金，重试不重复成交。
6. 高波动风险距离有界，参考线只能收紧；除权/缺失不会变成虚假突破或退出。
7. 两日冷却以实际全部成交后起算；不能阻断卖出。
8. 缺失行业/价格/真实数据标记一致，回放不删除亏损或不可估值持仓。
9. 三组账户隔离、指标同日期、预筛拒绝不混入订单失败率，现金与资产账本对平。
10. 没有Jev缓存/密钥不调用API不造概率；输出不修改生产publication，发布清单哈希真实。

先烟测一次，修具体失败再运行受影响测试。完成后跑一次相关V2回归即可，不把187项通过数量作为盈利证据。性能测量复用正式运行时间/峰值内存，不另做压力工程。

## 12. 必须交付的文件

```text
artifacts/prism_v2_compare/<run_id>/
  protocol_frozen.json
  dataset_manifest.json
  data_audit.json
  split.csv
  parameters.json
  shared_fixes.md
  baseline_identity.json
  comparison_summary.csv
  paired_bootstrap.json
  verdict.json
  regime_summary.csv
  sector_summary.csv
  gate_funnel.csv
  test_subperiods.csv
  RESEARCH_REPORT.md
  TEST_REPORT.md
  artifact_manifest.json
  figures/{equity,drawdown,exposure}.png
  <strategy>/<split>/<cost>/
    daily_nav.csv.gz
    decisions.csv.gz
    orders.csv.gz
    fills.csv.gz
    closed_trades.csv.gz
    remaining_positions.csv
    metrics.json
    runtime.json
```

报告首页先给A/B/C对照表和一句判定，再解释主要收益/亏损来自哪些状态、行业、退出原因及成本。标清相对原生A的共用引擎修复，禁止把修复收益归给Prism。没有结果的文件给明确status/reason，不填虚构数值。

交接文档必须说明：两种策略具体差异、数据有效范围、训练/验证/测试日期、测试暴露情况、全部实际命令、环境、耗时/内存、失败与恢复点、下一次如何复现、最终是否建议继续研究。不要自动切换生产策略或启动实盘。

## 13. GitHub提交和发布回执

开发完成后按文件白名单提交源码、测试、冻结配置、报告、核心CSV/JSON和小型图。不要`git add .`把.env、凭证、2.7GB行情库或全部缓存上传。授权允许上传代码与研究结果，不代表允许转分发供应商原始行情。

大明细压缩后按大小分包（建议每件≤40MiB，保留哈希和分片复原说明）。优先GitHub Release附件；无gh/API权限则核心结果与压缩小包通过Git上传分支，过大且无授权上传通道的资产记录`LOCAL_ONLY`及准确本地路径，不能标全量上传成功。

执行并核验：

```bash
git push origin HEAD:refs/heads/research/prism-v2-backtest
git ls-remote origin refs/heads/research/prism-v2-backtest
```

不自动合并、不force push、不覆盖原`codex/technical-v2`。能创建PR则创建，不能则记录具体权限失败而非虚构链接。

`SOURCE_COMMIT`是跑实验的代码提交；`DELIVERY_COMMIT`是随后含结果/交接的提交，允许不同并分别记录。不要为把最终提交号写进它自身追求不可能的自引用哈希。最终本地`publish_receipt.json`记录远端HEAD、实际上传的文件、大小与SHA256；交接说明源码提交与交付提交的绑定方式。

最终回复用户：真实A/B/C核心表、判胜和局限、主要改动、实际测试结果、远端分支/提交/PR/Release状态、结果路径。不得仅说“实现完成”而不展示比较。

## 14. 信息不足/异常时的处理

- 没有本地数据：检查已知路径、环境覆盖和主worktree；真实查找失败后报告具体缺项，仍提交可运行比较器和已有证据，禁止模拟收益。
- 大量历史数据已完整：不重新下载800天。需要补少量已识别缺项时仅在隔离副本处理，不扩大为数据工程重建。
- 原库被Worker使用：使用一致性备份；不删锁、不kill未知进程、不停止其他任务。
- Prism某段说明与源码冲突：按固定SHA的实际实现解释，但本文件第6节是冻结的A股适配定义，不随README营销描述更改。
- A/B都没有交易或都亏损：完成报告、漏斗与发布，这是结果，不是允许挑数据重做的理由。
- 共同基础缺失导致无法形成可靠净收益：明确范围受限或不能判胜，交付可得回放和缺项，不无限等接口或凭空宣布谁更好。

## 15. 可复查来源

以下固定SHA源码是事实依据；A股适配参数、实验矩阵和判胜容忍带是本次研究设计。

```text
https://github.com/Orangekostar/standard/blob/0809f0ef38e2e776cc80b231d3e188edcddf3173/core/pipeline/evaluation_v2.py
https://github.com/Orangekostar/standard/blob/0809f0ef38e2e776cc80b231d3e188edcddf3173/core/pipeline/technical_v2.py
https://github.com/Orangekostar/standard/blob/0809f0ef38e2e776cc80b231d3e188edcddf3173/core/strategies/formula_v2.py
https://github.com/Orangekostar/standard/blob/0809f0ef38e2e776cc80b231d3e188edcddf3173/core/strategies/intent_v2.py
https://github.com/Orangekostar/standard/blob/0809f0ef38e2e776cc80b231d3e188edcddf3173/core/backtest/execution_v2.py
https://github.com/Orangekostar/standard/blob/0809f0ef38e2e776cc80b231d3e188edcddf3173/core/backtest/metrics_v2.py
https://github.com/irfndi/prism-liquidity-agent/blob/d15c63ef7a5d426ad71f4bbf4dae05c94ba0bc02/engine/strategy-service.ts
https://github.com/irfndi/prism-liquidity-agent/blob/d15c63ef7a5d426ad71f4bbf4dae05c94ba0bc02/engine/churn-guard.ts
https://github.com/irfndi/prism-liquidity-agent/blob/d15c63ef7a5d426ad71f4bbf4dae05c94ba0bc02/engine/jev-service.ts
https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup
https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml
```

交易规则按历史生效版本逐项核验，不能把2026版直接应用到更早历史。规则依据用于撮合正确性，不是用于预测股票基本面。
