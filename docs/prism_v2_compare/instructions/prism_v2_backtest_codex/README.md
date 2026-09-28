# Prism与Technical V2回测比较指令包

## 使用

将 `CODEX_PRISM_VS_V2_BACKTEST.md` 交给在项目服务器上工作的Codex，要求它按文件完成开发、回测、报告和Git推送。主MD可独立使用；JSON提供相同协议的机器可读参数副本，不是已经训练好的策略模型。

可直接附上这段话：

> 请执行附件CODEX_PRISM_VS_V2_BACKTEST.md，不要只复述计划。固定V2与Prism源码版本，建立隔离实验工作区和数据库副本，比较A0_V2_F0、B0_PRISM_A_SHARE_V1以及C0仓位对照。只做纯技术面，不调用Jev。先验证并冻结协议再运行最终比较，报告真实收益、回撤、换手、仓位及成交数；完成后将代码、配置、核心结果和交接文档推送到research/prism-v2-backtest并核验远端SHA。不能伪造结果、改生产配置或以没有合格模型为由不完成对照研究。

## 文件

- `CODEX_PRISM_VS_V2_BACKTEST.md`：完整任务、策略、日期、费用、对照、判胜、命令契约与发布要求。
- `prism_v2_compare_plan.json`：参数副本。
- `PROMPT_AUDIT.md` / `.json`：指令审核记录，不是实际回测报告。
- `SHA256SUMS.txt`：本包文件校验值。

**本包不包含已实现的新比较器或回测结果。Prism为思想适配，不是其原生链上策略的A股官方复现。**
