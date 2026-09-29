# Codex执行指令：沪深主板高胜率策略实验室 v1

## 0. 任务与不可混淆的结论

用户目标：当前技术策略收益不理想；设计一系列不同逻辑的策略，在可空仓的前提下优先筛出高扣费后胜率，并在达标者中比较净收益。用户没有授权真实下单。本任务是开发和运行研究回测，不是提供未来胜率保证。

执行而非复述本文件。以 `Orangekostar/standard` 的 `fix/mainboard-only-universe` 为起点，固定源码 `3df7170a6a492097461ebbd9fafb2f450c0c4f5d`。若本地HEAD不同，保留本地改动并建立独立worktree，不 reset/clean/强制覆盖。输出分支 `research/mainboard-high-win-v1`。用户提供的 `/pull/new/...` 是创建PR入口，不是已经编号的PR。

仅使用价格、成交量/额、波动、历史行业归属及交易规则。禁止Jev/LLM/Agent、新闻、财报因子、付费推理、默认全量拉数。复用冻结真实行情及现有账本。不要复制Prism的链上TVL/无常损失指标。

**16个新候选 = 下文8个策略族 × CONFIRMED/STRICT两档。另有2个固定旧策略对照，总计18个策略。**第一轮固定相同的4%止盈、3%止损、5交易日期限及持仓预算，避免用缩小止盈/扩大止损刷胜率。没有达标者就报告没有达标者，不临场降门槛。

本文件的数字是预先冻结的研究初值，不是文献证明的最优参数。用 `high_win_suite_v1.json` 对照本文；若发生冲突必须先修正协议并冻结新版本，不能在看到结果后择其有利者。

## 1. 源码审计：复用什么，不要重复造轮子

| 已核实位置 | 复用/修改方法 |
|---|---|
| `core/backtest/strategy_search_v2.py` | 已有三族36组合、entry_scores、SearchMarket、收盘触发/次日开盘账本适配。新8族不得被旧TREND_PULLBACK掩码先筛一遍。 |
| `core/backtest/factor_portfolio_research.py` | 两个固定旧对照直接来自FactorPolicy；复用价格、行业禁买与实际开盘复查。不得把新策略独有过滤加到原生对照。 |
| `core/backtest/stability_research_v2.py` | 复用真实交易日窗口，保留旧summarize_stability；新建高胜率筛选器，不能修改旧档案结果。 |
| `core/pipeline/factor_portfolio_research.py` | 复用分块载入、spawn四账户隔离、报告/冻结机制；不要直接拷贝旧资格函数。 |
| `core/pipeline/rank_rotation_research.py` | 复用load_rotation_config及冻结数据来源、合法的研究风险口径。 |
| `core/backtest/rank_rotation_v2.py` / `prism_compare_engine.py` | 优先复用真实成交、现金、T+1、公司行动、估值、未完成退出处理；先读实际实现再绑定方法。 |
| `core/backtest/execution_v2.py` / `portfolio_v2.py` | 保留交易单位、实际费用、价格范围、未成交及仓位记录。 |
| `docs/prism_v2_compare/*RESEARCH.md` | 已有因子、板块、量价、长上影研究；检查本地输出但不要把旧零交易报告冒充最新策略表现。 |

旧代码资格更偏向窗口收益与回撤；本轮是明确改变**选模目标**，不是宣称旧工程未考虑胜率。`choose_candidate()`已有胜率排序，但`qualification()`与`select_frontier()`没有本文件的65%硬门槛。

已知定向检查：`_SearchReplay.search_day()`构造compact时只含前6名。叠加禁买后更后面的合格股票可能不在rows中。先用8候选、前6个被禁止的小样例验证；新接口应先评估当日全部候选的信号/已知禁买再挑计划，不用未来信息补单。若公共错误影响旧对照，也同样修复，保留原生差异记录，不能只让新策略享受更好撮合。

禁止对开发方式进行无关渗透、模糊或大规模压力测试。必要账本/日期正确性检查不能省略。

## 2. 数据范围、运行模式与时间

默认尝试读取：
`cache/experiments/rank_rotation/frozen-mainboard-3d-20260929-v1`

这是用户服务器上已记录的相对路径，不是保证当前容器存在。在本机核实 `rank_dataset_manifest.json`、`feature_manifest.json`、`roster.parquet`、`corporate_actions.parquet`及分块文件哈希。若缺失，检查同仓库已有交接指明的实际目录/参数；不要猜文件、不要重新下载800天。确实缺字节才报告具体缺项，同时完成无需该数据的代码与小样例。

输入只读；输出放在新 `artifacts/high_win_strategy_lab/mainboard-high-win-v1/`。需要数据库副本时使用SQLite在线备份，不复制仍在写入的裸.db；不动生产锁、Worker、UI、生产latest指针，不重启服务器。

保留新分支既有主板范围：MAIN_SH/MAIN_SZ，复用证券代码判断函数；排除已知ST/风险警示、科创板、创业板、北交所、非A股。继续排除房地产 `SW_L1:801180.SI`，信号日**未复权**收盘与实际买入原始开盘均≥5元；行业未知不新买。ADV20≥5000万元。

历史ST或公司行动资料缺失不能编造。本次研究沿用所读取冻结配置明确批准的研究口径（例如ALLOW_RESEARCH_ONLY），保留FROZEN_NAME_FILTER/UNKNOWN等标志和未知占比，不改成历史真值。若配置是严格阻断，就不能擅自放宽。研究口径结果可用于同数据相对比较，不能宣称历史严格不买ST、完整分红总收益或可直接实盘。

价格形态统一使用现有比较价（raw × 同期复权因子，或同一as-of等比例复权）。成交和5元门槛使用raw。复权单位变化、分红、送股必须走既有账本逻辑；无法计价的资产单独列出，不能删掉亏损/缺数据交易。

按交易所日历给每个code补齐**日期索引**，缺失价格保持NaN；不以向前填充伪造可交易K线。每个窗口运算都检查所用输入完整性。上下文合成行业/市场序列不是官方指数，不在报告里叫上证指数/深成指。行业成分日期、数据版本限制延续并披露。

最新提交还未提供足以在本文中核验的本机最新收益明细；执行时将本地已有REPORT/summary路径和哈希写入 `previous_runs_index.json`。新实验中现有策略也要同口径回放，不能与不同股票池、日期或成本的旧数字直接排名。

## 3. 精确共同特征定义

所有t、p、b指有序实际交易日索引；区间两端均包含。滚动窗口默认含当天，`prev`明确排除当天。所有排名仅在同一日期截面计算。

- `C,O,H,L`：比较价；`A`：元计成交额；成交量如使用则为股。
- `R_n(t)=C_t/C_(t−n)−1`；`ell_n=ln(C_t/C_(t−n))`；`r=ell_1`。
- `MA_n`：最近n个实际交易日收盘算术平均。
- `sigma20`：最近20个日对数收益标准差，ddof=0；计算需21个有效价格点。
- `TR=max(H−L,abs(H−Cprev),abs(L−Cprev))`；`ATR_n=mean_n(TR)`；`a=ATR14/C`。
- `HHn=max_n(H)`、`LLn=min_n(L)`；`HHnprev=max(H[t−n:t−1])`，LL同理。
- `DD20=C/max(C[t−19:t])−1`，不是期间最大回撤。
- `CLV=(2C−H−L)/(H−L)`；`LW=(min(O,C)−L)/(H−L)`；`UW=(H−max(O,C))/(H−L)`；H=L时本轮形态判定视为缺失，不伪造强确认。
- `VA(t)=A_t/median(A[t−20:t−1])`。
- `VR3(t)=mean(A[t−2:t])/median(A[t−22:t−3])`。分母是此前20期，明确排除分子3期。
- `ER20=abs(sum(r[t−19:t]))/sum(abs(r[t−19:t]))`；20项完整且分母0时取0，否则缺失保留。
- 行业/市场收益由相同上下文合成指数计算。`B`是C≥MA20的有效成员占比，不是当天红盘占比；`ΔB_n=B_t−B_(t−n)`。
- `RS60=ell60_stock−ell60_sector`。`RS60_WITHIN_SECTOR_PCT`为日期t、所属SW_L1行业内RS60的平均秩百分位（pandas rank(method='average',pct=True)，数值越大越强）；至少10个有效股票，否则缺失。
- `SECTOR_RETURN20_PCT`为日期t有效行业20期收益的平均秩百分位；至少5个可排序行业，单行业有效成员≥5、覆盖率≥0.90。
- `Zrel3=(ell3_stock−ell3_sector)/(max(sigma20_stock,0.005)*sqrt(3))`；不是已估计beta的残差。

新策略公共掩码：真实可用bar、当日名册有效、共同主板/价格/行业范围、非停牌、必需价格/复权有效；market_context_status/sector_context_status均OK；market_state∈{RANGE,TREND_EXPANSION}；B_market≥0.45；0<sigma20≤0.04；a≤0.06；ADV20≥5000万元。

**不要额外要求所有新规则都满足原F0的65/55分、12/15因子或旧收益分箱阈值。**新规则使用自身所需输入和上列共同掩码，否则比较的只是原策略子集。F0分数仍可作为诊断字段，不是所有新规则的前置门槛。

新策略共同排名：
`rank_score=.5*RS60_WITHIN_SECTOR_PCT+.3*SECTOR_RETURN20_PCT+.2*(CLV+1)/2`，降序，code升序打破并列。全部18组用同一原始数据版本；两个原生对照保留其原本排序/仓位差异并明确标注。

## 4. 八个策略族（所有条件AND相连）

### S01 — 强行业强股：缩量回踩后确认

1. 令 p=t−1。t日 MA20>MA60，且 MA20(t)>MA20(t−5)；行业20期收益>0、行业广度≥0.50，个股行业内RS60分位≥0.60。
2. p日 C/MA20∈[0.98,1.01]，DD20∈[−0.10,−0.02]，VR3≤0.80。
3. t日 C_t>H_p、CLV_t≥0.40、R1_t∈[0.003,0.03]。只在确认日t收盘产生计划，不在p日低点回填买入。

与旧研究的区别：在现有趋势回踩上，增加独立的前日缩量状态和次日越过前高确认；不是单纯再提高综合分。

输出ID：`S01_CONFIRMED` 与 `S01_STRICT`。优先阅读顺序：第一组，实际全部执行，不按结果跳过。

### S02 — 突破后回踩：守住原突破位再买

1. 在 b∈[t−8,t−2] 中，找最近一个满足 C_b>HH20prev_b、VA_b≥1.50、0<R1_b≤0.06 的突破日。选定后固定 K=HH20prev_b。
2. 从b+1到t，所有 L≥0.97K；从b+1到t−1所有 C≥0.98K；t日 L_t≤1.02K，表示确实回试突破区域。
3. t日 C_t≥K、C_t>H_(t−1)、CLV≥0.40、0<R1≤0.03，行业20期收益>0、行业广度≥0.50。每个(code,b)最多产生一次可成交入场事件。

与旧研究的区别：现有量价研究主要是“先回撤后突破”；这里是“先突破，后回踩，再确认”，顺序和固定锚点不同。

输出ID：`S02_CONFIRMED` 与 `S02_STRICT`。优先阅读顺序：第一组，实际全部执行，不按结果跳过。

### S03 — 区间假跌破：收回支撑后确认

1. 令 p=t−1，支撑 K=LL20prev_p（必须不含p日）。p日 0.97K≤L_p<0.995K、C_p>K、下影比例LW_p≥0.45，VA_p∈[1.0,2.5]。
2. t日 C_t>H_p、CLV≥0.40、0<R1≤0.03；ER20_t≤0.35、C_t≥0.97MA60_t；行业20期收益≥0。
3. 形态名称只描述价格路径，不推断“洗盘”或资金身份；同一(code,p)最多一个入场事件。

与旧研究的区别：不同于跌幅排序反弹：先破既有区间低点并收回，再等后一日确认，且排除明显长趋势下跌。

输出ID：`S03_CONFIRMED` 与 `S03_STRICT`。优先阅读顺序：第二组，实际全部执行，不按结果跳过。

### S04 — 行业相对超跌：强行业中的短期修复

1. p=t−1。Zrel3_p∈[−2.5,−1.0] 且 R3_p≤−0.02。Zrel3只表示相对行业的标准化落后，不称为OLS残差。
2. t日 MA20≥MA60、C_t≥0.98MA60、ell60_t≥0、行业内RS60分位≥0.50；行业20期收益>0、行业广度≥0.55、sigma20≤0.035。
3. t日 C_t>H_p、CLV≥0.40、0<R1≤0.03 后才发信号。不能拿仅跌得多但未确认的股票代替。

与旧研究的区别：相对于行业判断短期超跌，保留中期结构和确认条件；不再等同“全市场跌得最多就买”。

输出ID：`S04_CONFIRMED` 与 `S04_STRICT`。优先阅读顺序：第一组，实际全部执行，不按结果跳过。

### S05 — 低波动收敛：突破后再守住一天

1. b=t−1。b−1日最近10期高低区间宽度 HH10/LL10−1≤0.08，ATR5/ATR20≤0.75；窗口必须完整。
2. b日 C_b>HH20prev_b，VA_b≥1.50、0<R1_b≤0.06，固定 K=HH20prev_b。
3. t日 C_t≥C_b、L_t≥0.98K、VA_t∈[0.7,1.8]，sigma20≤0.03；行业20期收益>0。确认t日收盘后才计划买，不回填b日。

与旧研究的区别：不是直接追当天突破，而是先观察收敛，再检验突破后的价格接受情况。

输出ID：`S05_CONFIRMED` 与 `S05_STRICT`。优先阅读顺序：第二组，实际全部执行，不按结果跳过。

### S06 — 市场压力释放：相对抗跌股先恢复

1. min(B_market[t−5:t−1])≤0.35，B_market,t≥0.45、B_market,t−B_market,t−1≥0.08，市场当日收益>0。
2. 行业广度3期变化≥0.10、行业20期收益≥−0.02；股票ell5−市场ell5≥0、R5≥−0.08、sigma20≤0.035。
3. t日 C>MA5、C>H_(t−1)、CLV≥0.40。只做可观察的恢复，不能把市场最终见底日期作为输入。

与旧研究的区别：由市场广度恢复事件启动，不要求一直满仓；没有事件就不交易。

输出ID：`S06_CONFIRMED` 与 `S06_STRICT`。优先阅读顺序：第二组，实际全部执行，不按结果跳过。

### S07 — 行业轮动初段：新强行业的首次回踩

1. 在b∈[t−5,t−2]中找最近一次行业20期收益分位 P_sector20,b≥0.75 且 P_sector20,b−5≤0.50；行业20期收益_b>0、行业广度_b≥0.55。
2. t日行业分位仍≥0.75、行业广度≥0.55、个股行业内RS60分位≥0.70。p=t−1日 C/MA10∈[0.98,1.01]、R2_p<0、VR3_p≤0.80。
3. t日 C_t>H_p、CLV≥0.40；每个(code,行业b)只记录首次满足条件的信号。禁用未来行业排名。

与旧研究的区别：把行业由弱转强的时间点固定下来，再等待股价回踩，不是每天重复追行业排名第一。

输出ID：`S07_CONFIRMED` 与 `S07_STRICT`。优先阅读顺序：第二组，实际全部执行，不按结果跳过。

### S08 — 放量启动后：缩量整理的二次突破

1. 在b∈[t−8,t−4]中找最近启动日：R1_b∈[0.025,0.065]、VA_b≥1.80、UW_b≤0.25、CLV_b≥0.60。固定 K=(O_b+C_b)/2。
2. 从b+1到t−1（3至7个完整交易日），所有 C≥K、所有 L≥0.98K、所有 H≤1.03H_b；这段平均成交额≤0.70A_b。
3. t日 C_t>max(H_b,…,H_(t−1))、VA_t≥1.20、0<R1_t≤0.04；行业20期收益>0、行业广度≥0.50。每个(code,b)仅一个事件。

与旧研究的区别：要求放量启动后的真实整理和再次确认；不是把放量本身当成主力买入证据。

输出ID：`S08_CONFIRMED` 与 `S08_STRICT`。优先阅读顺序：第二组，实际全部执行，不按结果跳过。

## 5. 两档确认与事件去重

`CONFIRMED`只使用策略族条件＋共同掩码。

`STRICT`在同一信号日额外要求以下全部成立：行业20期收益分位≥0.75；股票行业内RS60分位≥0.75；市场广度≥0.55且5期广度变化≥0；sigma20≤0.025；C/MA20≤1.05。

STRICT不能改变止盈、止损、期限、费用或资金上限，不强行降低止盈来创造高胜率。严格版可能较少交易、也可能错过启动，不预设一定更好。

S02/S03/S05/S07/S08有固定事件锚点。保存(code,family,anchor_date)；锚点必须由截至t的数据确定。该事件首次完整满足入场规则才产生计划，次日买入失败则记录失败并消费该信号，不无限尝试旧信号；需要新锚点/新事件才能重发。同一股票在持仓中不新增买入；完全平仓前不创建新事件订单。各策略独立维护去重状态。S01/S04/S06无长期锚点，但同一(code,t)只有一个信号。

## 6. 两个旧对照以及公共执行契约

- BASE_ENV：`FactorPolicy(ranking='SCORE',vol_control=False,diversify=False,min_price=5)`，原生ID `FS_SCORE_REGIME_D0_P5`。
- BASE_DEF：`FactorPolicy(ranking='DEFENSIVE',vol_control=True,diversify=True,min_price=5)`，原生ID `FS_DEFENSIVE_VOL15_D1_P5`。

BASE_DEF只是现有12组里的固定防御配置，**不是声称它就是用户当前最高收益/已上线策略**。若最新本地报告给出另一胜者，可在previous_runs_index记录，不能在看后段结果后悄悄替换这两个对照。需要额外对照必须在运行前新冻结并修订总单元数。

新策略用原3股预算：独立账户初始100万元，最多3个实际证券，趋势扩张每次新买上限=前收盘NAV/3（含买入费）；RANGE仅新买预算减半；受实际现金、原ADV的1%上限以及真实交易单位限制。不加杠杆、不摊平、不马丁、不在盈利后自动加仓。没有信号就留现金。基准自带防御仓位仅BASE_DEF保留，不把它混称完全相同风险。

所有18组：
1. t日收盘才能确认信号；最早t+1开盘尝试新买。买入仅该日有效，未成取消，不以未来最低价/最佳价格回填。
2. 只读取当时已知的信号、当前开盘价和当日可用交易状态。**不得用t+1收盘、成交额、最高价判断t+1开盘能否买。**
3. 保留当前原始价≥5、已知风险警示、停牌、涨跌停、价格上限与数量规则。买入上限规则沿用共同冻结底座，不能提高新策略追价限额。
4. 固定持有最大5个交易日：实际买入日e，计划在交易日e+5开盘退出，不能从未成交信号日开始计时。不可卖或无对手条件时保持待退出并在后续可执行开盘处理。
5. 4%止盈/3%止损相对**实际含滑点入场价对应的比较价基准**判断。t日收盘触发后才在t+1或后续合格开盘卖；入场日即可形成次日退出计划，但入场日不能卖出新买股份。
6. 不用日线H/L假定盘中触及就按阈值成交。不存在“买当天最高价已到止盈所以卖了”。实际跳空损失可能超过3%，必须真实计账。
7. 退出优先级：已待执行退出/证券风险事件 > 止损 > 到期 > 止盈；同日多个原因全记录，执行一次。基础新策略没有分批止盈或放宽止损。
8. 先卖后买；只有实际已成交的卖出才释放现金/席位；同交易日不卖完再重买同股。分红股份、待上市权益照旧计入持仓容量。
9. 所有待买、持仓、待卖、费用与权益有独立账本；同账户不得重复执行订单。终局未了结亏损必须进NAV和未解决风险，不能只报已平仓赢家。
10. 每个独立窗口末11个交易日停止新买，退出继续。复用原尾部语义并写边界单元测试；后段连续132日期间，中间66日边界不关闭持仓/不停止新买。

成本读取相同冻结研究配置。预计现有值为双边佣金0.0003（每边最低5元）、卖出附加0.0005、每边其他费用0.00001，滑点base每边0.001、stress每边0.002。忽略最低额的参考往返分别0.00312/0.00512，但实际逐单精确计费。成本是研究假设而非宣称历史每一天的法定券商费率；滑点只计入成交价一次。实际配置不一致要在冻结前明确，不能局部改值。

## 7. 胜率优先但不刷胜率：指标与选择规则

一个交易样本=该code从无仓位到完全平仓的独立episode。部分成交/分批退出不是多笔获胜；没有成交的订单不是失败交易，也不是胜利；零收益不是赢。分红、权益、买卖费和滑点计入episode损益。

win_rate=盈利episode数/全部已完全结束episode数。收益率使用真实NAV；季度平均胜率不得简单平均，应汇总wins与closed分母。必须同时输出交易胜率、收益分类准确率（如有）、盈利日占比，三者不得混用。

必须报告：净收益/净值、年化（实际账户天数）、最大回撤、年化波动和夏普、成交与平仓数、胜率、Wilson区间、平均净盈利/平均净亏损、profit factor、平均每笔净回报、最坏5笔、最大单笔亏损、持有时长、空仓比例/平均仓位、费用/滑点、行业集中、失败订单与未平仓资产。

PF=sum正净损益/abs(sum负净损益)。若无亏损，显示“无已观测亏损，PF无穷/不可有限估计”，JSON不要写非法Infinity；另存loss_count=0，不能伪造PF=100。盈亏比用相同episode回报或资金损益口径，输出两个口径并明确资格默认采用平均episode净回报绝对值比。零交易胜率=null、PF=null、夏普无定义，不输出100%。

### 7.1 早期6窗口的候选晋级门槛（两种成本分别满足）

- 汇总扣费后win_rate≥0.65。
- 至少60个已结束episode、30个不同入场日期、20个不同股票；统计证据不足不要求强行开仓。
- Wilson 95%区间下界≥0.55。z=1.959963984540054；这是相关性与多次选择未校正的描述性门槛，不是未来概率保证。
- PF≥1.30、平均净盈亏比≥0.80、平均episode净回报>0。
- 6窗口平均净收益>0；最坏窗口收益≥−5%；每窗口最大回撤≤12%。
- 至少4/6窗口非负、至少2/6窗口真正正收益。**零交易窗口是允许的现金窗口，不视为亏损，但不算正收益窗口。**不沿用旧“必须5/6严格盈利”，以免违背用户允许空仓的目标。
- 所有窗口净值可核算；末尾未解决资产不能宣称无保留胜者。

在合格的新候选中按“较差成本的平均窗口净收益”降序，再按较差成本Wilson下界降序、最差回撤升序、policy_id排序。冻结最多3个候选，第一名是唯一主候选。不是把胜率从70%抬到71%优先于明显更高的合格收益；先满足高胜率，然后优化收益。

一个也不合格仍然执行后续预定的全部候选诊断，主候选保持null、结论NO_HIGH_WIN_CANDIDATE。不能为凑赢家降低胜率、放宽止损或删掉空仓窗口。

### 7.2 后段连续账户的复核目标

所有18组都报告；正式比较资格只评价预先冻结的主候选，不把后段最大收益者重新选成赢家。每成本win_rate≥0.65、至少30episode/20入场日、Wilson下界≥0.50、PF≥1.30、盈亏比≥0.80、净收益>0且超过BASE_ENV、最大回撤≤12%。另明确与BASE_DEF的收益/回撤取舍，不必强行宣称双维度碾压。

样本不足归为INSUFFICIENT_TRADES，不把一小段70%胜率称为稳健。历史资料有限仍归EVIDENCE_LIMITED；可得“在该研究数据口径下领先”，不能升级为实盘承诺。所有失败原因逐项列出。

### 7.3 对用户“胜率一定要高”的兑现方式

兑现的是**低于预先设置的实测门槛就不推荐上线**，不是事前承诺任何市场都65%以上。比如70%赢、平均赢1%、30%输、平均输4%，简单期望仍为−0.5%/笔；高胜率不能靠留下大亏仓或只平小赢家获得。

## 8. 实验矩阵与历史反复使用

复用冻结manifest的实际交易日，不猜自然日期。由stability_windows定义6×63早期窗口和132日后段。

- 早期：18组×6独立窗口×2成本=216账户。
- 冻结：只读取早期结果，生成 `selection_freeze.json`，写入全部候选定义/数据/代码/已尝试次数。
- 后段：18组×1个连续132交易日账户×2成本=36账户。输出两段66日的分段指标，但**不重置账户**，跨段持仓正常延续；第66日不是结算尾部。
- 合计252个账户回放。没有额外随机种子反复找最优，也没有H×TP×SL连续搜索。

整个历史已用于多轮研究，全部明确REUSED_HOLDOUT/REUSED_HISTORY；冻结只约束本轮流程，不创造新的独立样本外。后段全部18个结果是用户要求的完整比较，不能因为都展示就声称未做多重比较。最终挑出的候选需要后续真实前瞻检验，但不因此拒绝完成本轮历史实验。

旧窗口平均收益不是连续复利。早期只报告“六窗口平均/中位/最差”；后段单个连续账户报告真实累计净值和年化。不得将独立100万账户的季度收益连乘后命名实际全程收益。

不用高开销全量PBO穷举。输出本轮候选数16及旧搜索研究索引；对早期冻结3候选及对照做1000次、10交易日块的配对bootstrap（seed20260929）。胜率重采样按入场日期块将episode成组，收益按同步日期块，不将同日多只股票当完全独立。Wilson与bootstrap都不能修复反复查看历史造成的选择偏差。

## 9. 开发落地任务（按顺序）

### T0 — 准备与冻结来源

读取源代码、现有配置和本地最新结果索引，生成 `SOURCE_AUDIT.md`、`previous_runs_index.json`、`data_binding.json`。核实当前本地没有生产切换。禁止覆盖旧实验目录。市场日期、公司行动、历史ST风险标志真实带入。源ref若晚于固定SHA，建立此SHA的新worktree，记录与当前分支的关系。

### T1 — 一次性扩展共享特征

新增 `core/factors/high_win_features.py`。函数接口建议：
`build_high_win_features(panel, market_context, sector_context, sessions, as_of) -> DataFrame`。
只按必要列读取OHLC/额/原指标/行业上下文，分块一次计算，共享给252账户。输入不足时返回有原因码的空值，不调用网络补数据。派生特征缓存键包含源数据哈希、日期范围、公式版本、行业口径；不以当前工作区路径独自作为缓存键。

如果冻结feature_chunks没有所需high/low/open/sector_index，回到同一冻结snapshot或replay_chunks读取，不能从另一时点的生产DB混入。先核对现有缓存列再实现adapter。

### T2 — 策略注册与信号

新增 `core/strategies/high_win_suite.py`：不可变HighWinPolicy、16注册项、`evaluate_signal(history, state, policy)`或等价纯函数。返回eligible、family、variant、event_anchor、rank_score、reason_codes、information_cutoff。按S01—S08严格实现，补STRICT掩码。不得继承旧entry_scores默认掩码导致新策略变成旧族的子集。

### T3 — 回放适配

新增 `core/backtest/high_win_research.py`，优先在现有_RotationReplay/_SearchReplay接口上提取公共执行壳与独立信号提供者；不要为每族写一套账本。两个旧对照调用FactorPolicy原实现；如公共bug修复则所有组一致并记录。原生无bug样例成交和NAV应一致。

每日顺序固定：只执行前一日计划/已待卖 → 记账估值 → 用截至今天收盘信息更新退出/新买计划。为持仓和昨日订单构造行，不能仅依赖今天新候选排名决定是否处理旧订单。候选不足、未知数据、无可卖持仓分别计数。

### T4 — 高胜率指标与选择

新增 `core/backtest/high_win_metrics.py` 与 `core/pipeline/high_win_research.py`：episode净损益、成本场景、Wilson、分期指标、选择冻结和多重试验计数。旧metrics可复用，不把订单attempt计成交易。

新增 `scripts/high_win_backtest.py`，至少支持：
`prepare`、`screen`、`freeze`、`review`、`report`、`--all`（作为一种明确parser设计，可采用all子命令但文档和测试一致）。独立参数 `--experiment-root --output-root --config --workers`；workers仅1或4，使用spawn和独立账户DB。CLI每完成单元打印结构化进度，不打印会导致误解的虚假百分比。

### T5 — 定向检查后正式跑

先小样例测试、20~50只股票/最多160日离线smoke验证数据接线，不拿smoke表现挑策略。正式共享特征一次、252个账户顺序/四进程运行。hash一致的已完成单元复用；失败后只续跑受影响单元，保留旧错误记录。若公共实现变了，同协议受影响组全部对称重跑，不能比较修复前后的不同账本。

### T6 — 报告与GitHub交接

输出中文 `REPORT.md`：主候选是否达标、16候选与两个基准完整收益/胜率/回撤表、早期与后段分开、最佳高胜率/最大收益/最低回撤三个展示角色（展示不等于后段重新选主候选）。给出失败原因和是否推荐保留原策略。没有达标者也完整交付，不用长期开放问题或历史亏损作为停止开发理由。

所有代码/配置/定向测试/核心结果/交接推送 `research/mainboard-high-win-v1`，普通push且核验远端SHA；不合并主分支、不自动启用任何赢家。PR有权限则创建，实际API失败才记录失败回执，不能把pull/new链接当成已经创建成功。原始供应商DB不上传；压缩逐笔派生结果前确认许可/隐私范围，分片≤40MiB。

## 10. 测试预算与验收

必要测试仅以下业务类别，不做无关安全审计：
1. 每族正例/反例与严格版子集关系；各族不是简单重命名旧入场。
2. 追加未来数据不改变历史信号；rolling/锚点/量能分母的端点明确。
3. 行业截面排名与成分变更使用当日资料；缺失窗口不伪造。
4. 分红拆股比较价/原始5元门槛分离；对现有账本公司行动跑定向回归。
5. 买当日不可卖，收盘触发次日成交；实际开盘跳空不按3%止损价虚构成交。
6. 现金/席位、待卖未成、top6过滤、订单去重与费用最低额。
7. 一次episode计一次胜负；亏损未平仓、不完整NAV、零交易输出正确。
8. 选择只读取早期，后段报告不改变selection_freeze；66日中点不重置。
9. 1/4进程在小fixture的财务结果一致，固定任务恢复不重复开仓。

上述每类可包含多个断言。跑新增测试及被修改共同模块的相关回归一次；失败针对性修复重跑，不用为了数量凑数或反复跑全仓187/236项。唯一验收不是测试条数，而是结果可追溯、两成本同口径、252单元实际完成或具体失败清单。

## 11. 必须交付的文件

```
configs/high_win_suite_v1.json
core/factors/high_win_features.py
core/strategies/high_win_suite.py
core/backtest/high_win_research.py
core/backtest/high_win_metrics.py
core/pipeline/high_win_research.py
scripts/high_win_backtest.py
tests/v2/test_high_win_*.py
docs/high_win_strategy_lab/HANDOFF.md
artifacts/high_win_strategy_lab/<run>/
  protocol_frozen.json
  SOURCE_AUDIT.md
  previous_runs_index.json
  data_binding.json
  selection_summary.csv
  selection_freeze.json
  review_summary.csv
  all_candidates.csv
  per_sector_summary.csv
  coverage_and_blockers.csv
  uncertainty.json
  test_report.json
  REPORT.md
  <policy>/<window>/<cost>/{metrics.json,daily_nav.csv,trades.csv,orders.csv,decisions.csv.gz}
  artifact_manifest.json
  release_receipt.json
```

文件名可为接口适配做小范围合理调整，但完成时交接要列真实路径。最终回执包含数据SHA、协议SHA、实现SHA、运行时长、成功/失败/复用单元、全部尝试数、推送SHA和模型没有上线的状态。

拟新增CLI的运行方式（Codex实现后才能使用，不是声称仓库现在已有此命令）：

```bash
/home/ww/vv/quant/.venv/bin/python -m scripts.high_win_backtest --all   --experiment-root cache/experiments/rank_rotation/frozen-mainboard-3d-20260929-v1   --output-root artifacts/high_win_strategy_lab/mainboard-high-win-v1   --config configs/high_win_suite_v1.json --workers 4
```

## 12. 推荐报告结论模板

“在相同主板股票池、相同成本、允许空仓的条件下，预先冻结主候选X在后段连续132交易日净收益___、扣费后胜率___（___笔，Wilson区间___）、PF___、最大回撤___；相对BASE_ENV净收益差___。压力成本下___。结论为达标/样本不足/胜率不达标/收益不达标/资料限制。其余候选完整列示，不在后段重新选优。历史已多次使用，本结论不是未来胜率保证，未启用实盘。”

不要写“胜率一定高”“稳赢”“保证每天赚钱”。也不要因为全部方案没通过就偷偷更改阈值、删除亏损样本、扩大最大持有期或靠摊平提高平仓胜率。

## 13. 来源绑定

下列来源支持现有代码事实和研究取舍；8族的阈值本身是本文件的研究假设。完整索引见SOURCE_MAP.md。

- [SRC01] 主板分支：https://api.github.com/repos/Orangekostar/standard/git/ref/heads/fix/mainboard-only-universe
  本次固定源码完整SHA。
- [SRC02] 既有36组技术规则：https://github.com/Orangekostar/standard/blob/3df7170a6a492097461ebbd9fafb2f450c0c4f5d/core/backtest/strategy_search_v2.py
  Candidate/entry_scores/_SearchReplay；三族×两强度×三期限×两止盈，search_day目前仅compact前6名，且沿用score3/market_state筛选。
- [SRC03] 现有因子组合12组：https://github.com/Orangekostar/standard/blob/3df7170a6a492097461ebbd9fafb2f450c0c4f5d/core/backtest/factor_portfolio_research.py
  FactorPolicy/policy_grid/ranking_values/FactorReplay/select_frontier；主板、不买已知ST、房地产排除、原始价≥5元。
- [SRC04] 因子组合研究编排：https://github.com/Orangekostar/standard/blob/3df7170a6a492097461ebbd9fafb2f450c0c4f5d/core/pipeline/factor_portfolio_research.py
  load_factor_market/run_cells/run_factor_portfolio；spawn四账户、冻结、结果与哈希。
- [SRC05] 环境与既有筛选资格：https://github.com/Orangekostar/standard/blob/3df7170a6a492097461ebbd9fafb2f450c0c4f5d/core/backtest/stability_research_v2.py
  stability_windows/environment_mask/summarize_stability；原资格要求5/6正窗口，但没有将65%胜率作为硬门槛。
- [SRC06] 量价阶段研究说明：https://github.com/Orangekostar/standard/blob/3df7170a6a492097461ebbd9fafb2f450c0c4f5d/docs/prism_v2_compare/PRICE_VOLUME_RESEARCH.md
  已研究放量突破、回撤后突破、阶段与环境；说明历史已反复使用及资料限制。
- [SRC07] 因子组合研究说明：https://github.com/Orangekostar/standard/blob/3df7170a6a492097461ebbd9fafb2f450c0c4f5d/docs/prism_v2_compare/FACTOR_PORTFOLIO_RESEARCH.md
  12组、4%止盈/3%止损/5日/3只、无真实可验证历史Jev、不修改生产。
- [SRC08] 板块回撤研究说明：https://github.com/Orangekostar/standard/blob/3df7170a6a492097461ebbd9fafb2f450c0c4f5d/docs/prism_v2_compare/SECTOR_PULLBACK_RESEARCH.md
  防止把数据质量过滤收益当成板块量价策略收益；保留拆分对照。
- [SRC09] 长上影退出研究：https://github.com/Orangekostar/standard/blob/3df7170a6a492097461ebbd9fafb2f450c0c4f5d/docs/prism_v2_compare/WICK_EXIT_RESEARCH.md
  已有固定止盈/无止盈/长上影退出；本轮不重复将其包装成新入场策略。
- [SRC10] 冻结数据和旧比较交接：https://github.com/Orangekostar/standard/blob/3df7170a6a492097461ebbd9fafb2f450c0c4f5d/docs/prism_v2_compare/HANDOFF.md
  旧A/B/C为零交易且资料有限；这些不是本次16候选的结果，不得替代最新本地报告。
- [SRC11] 主板冻结数据载入：https://github.com/Orangekostar/standard/blob/3df7170a6a492097461ebbd9fafb2f450c0c4f5d/core/pipeline/rank_rotation_research.py
  load_rotation_config/prepare_rotation；历史ST研究口径与资料限制明确，不能将冻结名称过滤当作历史ST真值。
- [R01] Trading Costs of Asset Pricing Anomalies：https://www.aqr.com/Insights/Research/Working-Paper/Trading-Costs-of-Asset-Pricing-Anomalies
  支持短期反转尤其应检查交易成本；不是本方案8种规则在A股的盈利证明。
- [R02] Time Series Momentum：https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum
  趋势研究背景为跨资产期货/远期及较长期限，不能据此断言5日A股高胜率。
- [R03] The Probability of Backtest Overfitting：https://scholarworks.wmich.edu/math_pubs/42/
  多策略筛选存在选择偏差；限制搜索、完整记录失败方案、保护新前瞻数据。
- [R04] Short-term reversals, returns to liquidity provision and the costs of immediacy：https://doi.org/10.1016/j.jbankfin.2022.106430
  短期相对收益反转研究背景；公开文中部分策略收益为未扣成本，不直接迁移收益数字。
