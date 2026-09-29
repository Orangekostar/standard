from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from core.backtest.stability_research_v2 import (
    control_policy, policy_grid, replay_environment, select_stability,
    stability_windows, summarize_stability,
)
from core.backtest.strategy_search_v2 import SearchMarket, enrich_features, qualification
from core.data.symbols import is_buyable_mainboard_ts_code, is_risk_warning_name
from core.pipeline.prism_compare_config import file_sha256, implementation_identity, write_json
from core.pipeline.prism_compare_data import _verify_cache_file, experiment_lock
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.strategy_search_research import SEARCH_COLUMNS, _artifact_records
from core.technical_v2.contracts import ContractError, sha256_json

ENVIRONMENT_COLUMNS = list(dict.fromkeys([*SEARCH_COLUMNS, "sector_log_return20",
    "sector_breadth20", "market_breadth20", "sector_context_status", "market_context_status"]))


def research_protocol():
    return dict(schema_version="stability-environment-research.v1", candidate_count=12,
        control="ORIGINAL_TREND_L2_H10_T4_S3_WITHOUT_NEW_ENVIRONMENT_FILTER",
        selection_windows=6, selection_sessions_per_window=63,
        holdout_windows=2, holdout_sessions_per_window=66, settlement_tail_sessions=11,
        selection_min_profitable_windows_each_cost=5, selection_min_closed_trades_each_cost=60,
        selection_min_traded_dates_each_cost=40, selection_worst_window_return_floor=-.03,
        maximum_window_drawdown=.10, holdout_min_closed_per_window_each_cost=10,
        holdout_min_traded_dates_per_window_each_cost=10,
        holdout_every_window_positive_both_costs=True, flat_after_tail_required=True,
        selection_rank="MAXIMIZE_WORST_COST_LOWER_QUARTILE_THEN_WORST_COST_MEAN_THEN_LOWER_DRAWDOWN_THEN_ID",
        return_aggregation="MEAN_INDEPENDENT_EQUAL_CAPITAL_WINDOW_RETURNS_NOT_CONTINUOUS_COMPOUNDING",
        gate_grid=["SECTOR20_POSITIVE", "BOTH_BREADTH20_AT_LEAST_HALF", "BOTH_GATES"],
        holding_grid=[5, 10], close_take_profit=.04, close_stop_loss=.03,
        range_new_entry_scale_grid=[.5, 1.], range_scaling="SIGNAL_CLOSE_STATE_NEW_ENTRIES_ONLY",
        max_names=3, entry_budget="MIN_SCALED_PRIOR_NAV_THIRD_ACTUAL_CASH_ONE_PERCENT_PRIOR_ADV20",
        holdout_policy="FREEZE_ONE_CHOICE_AND_FIXED_CONTROL_BEFORE_LOADING_HOLDOUT_NO_RESELECTION",
        no_qualified_policy="RECHECK_ONE_RANKED_DIAGNOSTIC_WITHOUT_PROMOTING_IT",
        data_scope="REUSED_HISTORY_NOT_FRESH_OUT_OF_SAMPLE", production_activation=False, jev_api_calls=False)


def _verified_source(root, config):
    manifest = json.loads((root/"rank_dataset_manifest.json").read_text())
    if manifest["status"] != "COMPLETE" or manifest["configuration_sha256"] != sha256_json(config):
        raise ContractError("stability research and frozen data configuration differ")
    for name,key in (("feature_manifest.json","feature_manifest_sha256"), ("roster.parquet","roster_sha256"),
                     ("corporate_actions.parquet","corporate_actions_sha256"), ("data_audit.json","data_audit_sha256")):
        if file_sha256(root/name) != manifest[key]:
            raise ContractError("stability input SHA256 differs: "+name)
    features = json.loads((root/"feature_manifest.json").read_text())
    if (features["status"] != "COMPLETE" or features["feature_identity"] != manifest["feature_identity"]
            or features["snapshot_sha256"] != manifest["snapshot"]["snapshot_sha256"]):
        raise ContractError("stability feature cache identity differs")
    for record in features["feature_chunks"]:
        _verify_cache_file(record)
    if file_sha256(manifest["snapshot"]["snapshot_db"]) != manifest["snapshot"]["snapshot_sha256"]:
        raise ContractError("stability snapshot SHA256 differs")
    roster = pd.read_parquet(root/"roster.parquet")
    if (not roster.code.map(is_buyable_mainboard_ts_code).all()
            or roster.name.fillna("").map(is_risk_warning_name).any()):
        raise ContractError("stability data contains excluded securities")
    return manifest, features


def _load_market(root, manifest, features, start, end):
    names = pd.read_parquet(root/"roster.parquet")[["code","name"]]
    pieces = []
    for record in features["feature_chunks"]:
        frame = pd.read_parquet(record["path"],columns=ENVIRONMENT_COLUMNS,filters=[("date","<=",end)])
        frame = enrich_features(frame)
        pieces.append(frame.loc[frame.date.ge(start)&frame.date.le(end)])
    panel = pd.concat(pieces,ignore_index=True).merge(names,on="code",how="left",validate="many_to_one")
    market = SearchMarket({str(date):frame for date,frame in panel.groupby("date",sort=True)})
    expected_dates = [date for date in features["sessions"] if start <= date <= end]
    if list(market.frames) != expected_dates or any(len(frame) != manifest["roster_count"] for frame in market.frames.values()):
        raise ContractError("stability replay coverage differs from frozen dates or roster")
    return market


def run_stability(experiment_root, output_root):
    root,output = Path(experiment_root).resolve(),Path(output_root).resolve()
    if root == output or root in output.parents or output in root.parents:
        raise ContractError("stability results must be isolated from source data")
    config,protocol = load_rotation_config(),research_protocol()
    manifest,features = _verified_source(root,config)
    source = implementation_identity()
    binding = dict(dataset_sha256=file_sha256(root/"rank_dataset_manifest.json"),
        configuration_sha256=sha256_json(dict(base=config,protocol=protocol)),
        implementation_hash=source["implementation_hash"])
    with experiment_lock(output):
        result_path = output/"run_manifest.json"
        if result_path.exists():
            archived = json.loads(result_path.read_text())
            if archived["status"] != "COMPLETE" or archived["binding"] != binding or archived["files"] != _artifact_records(output):
                raise ContractError("completed stability result differs; preserve it and use another namespace")
            return {**archived["result"],"reused":True}
        if any(path.name != ".compare.lock" for path in output.iterdir()):
            raise ContractError("incomplete stability result is preserved; use another namespace")
        windows = stability_windows(features["sessions"],manifest["windows"]["test"])
        policies = [control_policy(),*policy_grid()]
        flags = sorted(set(manifest["scope_flags"]) | {"REUSED_HOLDOUT","POST_DIAGNOSTIC_STRATEGY_RESEARCH"})
        write_json(output/"frozen_protocol.json",dict(binding=binding,protocol=protocol,base_parameters=config,
            windows=windows,policies=[dict(policy_id=p.policy_id,**asdict(p)) for p in policies],
            source_identity=source,scope_flags=flags),immutable=True)
        actions = pd.read_parquet(root/"corporate_actions.parquet")
        market = _load_market(root,manifest,features,windows["selection_1"][0],windows["selection_6"][-1])
        summaries,selection_cells = [],[]
        run_id = "stability-"+binding["dataset_sha256"][:12]
        for index,policy in enumerate(policies):
            cells = []
            for split,dates in windows.items():
                if not split.startswith("selection_"):
                    continue
                for cost in ("base","stress"):
                    result = replay_environment(market,sessions=dates,policy=policy,config=config,
                        cost_scenario=cost,output_dir=output/"selection"/policy.policy_id/split/cost,
                        run_id=run_id,split=split,scope_flags=flags,corporate_actions=actions)
                    cells.append(result["metrics"])
            summary = summarize_stability(policy,cells)
            summaries.append(summary)
            selection_cells.extend(cells)
            print(json.dumps(dict(event="stability_policy_complete",completed=index+1,total=len(policies),
                policy_id=policy.policy_id,qualified=summary["qualified"],
                profitable_windows={cost:a["profitable_windows"] for cost,a in summary["cost_aggregates"].items()},
                robust_lower_quartile=summary["robust_lower_quartile"],
                robust_mean_return=summary["robust_mean_return"],worst_drawdown=summary["worst_drawdown"],
                rejection_reasons=summary["rejection_reasons"])),flush=True)
        del market
        selection = select_stability(summaries)
        target = selection["chosen_policy_id"] or selection["diagnostic_policy_id"]
        by_id = {row["policy_id"]:row for row in summaries}
        flat = [dict(policy_id=key,qualified=by_id[key]["qualified"],
            robust_lower_quartile=by_id[key]["robust_lower_quartile"],
            robust_mean_return=by_id[key]["robust_mean_return"],worst_drawdown=by_id[key]["worst_drawdown"],
            rejection_reasons="|".join(by_id[key]["rejection_reasons"]))
            for key in [*selection["ranking"],control_policy().policy_id]]
        pd.DataFrame(flat).to_csv(output/"leaderboard.csv",index=False)
        write_json(output/"selection_cells.json",selection_cells,immutable=True)
        write_json(output/"selection_freeze.json",dict(binding=binding,selection=selection,
            holdout_policy_id=target,control_policy_id=control_policy().policy_id,
            diagnostic_only=selection["chosen_policy_id"] is None,frozen_at=datetime.now(timezone.utc).isoformat(),
            leaderboard_sha256=file_sha256(output/"leaderboard.csv"),
            selection_cells_sha256=file_sha256(output/"selection_cells.json")),immutable=True)
        print(json.dumps(dict(event="stability_selection_frozen",**selection)),flush=True)
        market = _load_market(root,manifest,features,windows["holdout_1"][0],windows["holdout_2"][-1])
        holdout_cells,rejections = [],[]
        chosen = next(policy for policy in policies if policy.policy_id == target)
        for policy in (chosen,control_policy()):
            for split in ("holdout_1","holdout_2"):
                for cost in ("base","stress"):
                    result = replay_environment(market,sessions=windows[split],policy=policy,config=config,
                        cost_scenario=cost,output_dir=output/"holdout"/policy.policy_id/split/cost,
                        run_id=run_id,split=split,scope_flags=flags,corporate_actions=actions)
                    metrics = result["metrics"]
                    holdout_cells.append(metrics)
                    if policy.policy_id == target:
                        rejections.extend(split+":"+cost+":"+reason for reason in
                            qualification(metrics,min_closed=10,min_dates=10,max_drawdown=.10))
                    print(json.dumps(dict(event="stability_holdout_complete",policy_id=policy.policy_id,
                        split=split,cost=cost,net_return=metrics["net_return"],max_drawdown=metrics["max_drawdown"],
                        closed=metrics["closed_trade_count"])),flush=True)
        verdict = ("NO_QUALIFIED_CANDIDATE" if selection["chosen_policy_id"] is None else
                   "SELECTION_PASS_HOLDOUT_FAIL" if rejections else "STABILITY_SCREEN_PASS_RESEARCH_ONLY")
        result = dict(status="COMPLETE",verdict=verdict,selection=selection,summaries=summaries,
            holdout_policy=dict(policy_id=target,**asdict(chosen)),holdout_cells=holdout_cells,
            holdout_rejections=rejections,output_root=str(output),api_calls=0,reused=False)
        _write_report(output,result,windows,flags)
        write_json(result_path,dict(status="COMPLETE",binding=binding,files=_artifact_records(output),result=result),immutable=True)
        return result


def _write_report(output,result,windows,flags):
    def pct(value):
        return "N/A" if value is None else f"{value*100:.2f}%"
    selection = result["selection"]
    lines = ["# 环境过滤与收益稳定性研究","",f"结论状态：{result['verdict']}","",
        f"合格候选：{selection['qualified_policy_count']}/12。固定原策略另作对照。",
        f"冻结选择：{selection['chosen_policy_id']}；后段诊断/复核对象：{result['holdout_policy']['policy_id']}。","",
        "## 六个独立筛选窗口","",
        "| 策略 | 合格 | 盈利窗 标准/压力 | 标准平均收益 | 压力平均收益 | 最差成本下四分位收益 | 最差回撤 | 平仓数 标准/压力 |",
        "|---|---|---:|---:|---:|---:|---:|---:|"]
    by_id = {row["policy_id"]:row for row in result["summaries"]}
    for key in [*selection["ranking"],control_policy().policy_id]:
        row = by_id[key]
        base,stress = row["cost_aggregates"]["base"],row["cost_aggregates"]["stress"]
        lines.append(f"| {key} | {row['qualified']} | {base['profitable_windows']}/6 · {stress['profitable_windows']}/6 | "
            f"{pct(base['mean_window_return'])} | {pct(stress['mean_window_return'])} | {pct(row['robust_lower_quartile'])} | "
            f"{pct(row['worst_drawdown'])} | {base['closed_trade_count']}/{stress['closed_trade_count']} |")
    lines += ["","## 冻结对象与固定对照的后段复核","",
        "| 策略 | 窗口 | 成本 | 净收益 | 最大回撤 | 平仓数 | 胜率 | 未平仓 |",
        "|---|---|---|---:|---:|---:|---:|---:|"]
    for row in result["holdout_cells"]:
        lines.append(f"| {row['strategy_id']} | {row['split']} | {row['cost_scenario']} | {pct(row['net_return'])} | "
            f"{pct(row['max_drawdown'])} | {row['closed_trade_count']} | {pct(row['closed_trade_win_rate'])} | {row['open_position_count']} |")
    lines += ["","复核未达标原因："+("；".join(result["holdout_rejections"]) or "无"),"",
        "## 固定实验口径","",
        "- 12个候选：原加强趋势回踩入场 × 板块20日绝对动量为正/市场与板块宽度均≥50%/两者同时满足 × RANGE新买额度为原额50%/100% × 最长持有5/10个交易日。原H10策略单独作固定对照。",
        "- 市场与板块上下文须有效；保留原STRESS/UNKNOWN禁买。大盘与板块是冻结主板股票池及行业成员的合成等权指数，不是官方指数。",
        "- RANGE减半只影响前一收盘信号对应的新买入预算，不强制清空或再平衡已有仓位，不保证组合敞口始终低于50%。",
        "- 最多3只；买入预算为前收盘NAV/3按状态缩放，再受实际现金和前信号日ADV20的1%上限约束。持仓按4%收盘止盈、3%收盘止损或5/10日到期提交下一可成交开盘卖单。",
        "- 保留T+1、涨跌停、停牌、手续费最低额及分项取整、单次滑点计价、实际卖出现金与失败订单约束。每窗口末11个交易日停止新买，任何未平仓都不合格。",
        "- 初筛每种成本均需≥5/6窗口净盈利、平均净收益>0、最差窗口≥−3%、最大窗口回撤≤10%、累计≥60笔平仓与≥40个成交日。",
        "- 合格对象按两成本较弱的季度收益25%分位数排序，再比较较弱平均收益、较小回撤和固定ID。禁止用复核收益重新选策略。",
        "- 后段每种成本、每个窗口均需净盈利、回撤≤10%、≥10笔平仓、≥10个成交日和零未平仓。初筛无合格时仅复核预定排序的诊断对象，无论后段如何都不升级为通过。",
        "- 所有窗口和成本各有独立100万元账户；平均窗口收益不是连续账户复利收益。新后段分为两个66日窗口，不能直接与旧132日连续回测的−4.87%混比。",
        "- 没有JEV/API调用，没有生产启用。完整账本、决策、成交、未成交原因、持仓、净值和源数据/实现哈希均归档。","",
        "## 证据边界","",
        "策略由已查看历史结果后提出，后段也是重复使用，不能称为新的独立样本外验证。历史ST、历史成分和公司行动/分红资料不完整，冻结名称和数据修订存在选择偏差；即使达标，也只是本轮历史研究筛选通过，不是未来稳定盈利保证。",
        "","范围标记："+", ".join(flags),"","## 日期","" ]
    for name,dates in windows.items():
        lines.append(f"- {name}: {dates[0]} — {dates[-1]}，{len(dates)}个交易日。")
    (output/"RESEARCH_REPORT.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
