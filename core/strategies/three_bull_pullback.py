from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd

from core.data.symbols import filter_non_chinext, filter_non_risk_warning


@dataclass(frozen=True)
class ThreeBullPullbackConfig:
    min_history_days: int = 25
    max_scan_tail_days: int = 9
    min_streak_gain: float = 0.05
    max_streak_gain: float = 0.22
    min_pullback_days: int = 1
    max_pullback_days: int = 3
    min_pullback_depth: float = -0.10
    max_pullback_depth: float = -0.01
    ideal_pullback_depth: float = -0.03
    min_score: float = 60.0
    include_watch: bool = False
    min_watch_volume_ratio: float = 0.75
    min_confirm_volume_ratio: float = 1.20
    min_confirm_gain: float = 0.015
    max_confirm_gain: float = 0.075
    breakout_buffer: float = 0.002
    min_market_score: float = 0.0
    min_sector_score: float = 0.0


def _num(s: pd.Series, default: float = 0.0) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(default)


def _z_to_score(series: pd.Series, dates: pd.Series) -> pd.Series:
    ranked = series.groupby(dates).rank(pct=True, method="average")
    return (ranked.fillna(0.5) * 100.0).clip(lower=0.0, upper=100.0)


def _streak_true(values: pd.Series) -> pd.Series:
    out: list[int] = []
    count = 0
    for value in values.fillna(False).astype(bool):
        count = count + 1 if value else 0
        out.append(count)
    return pd.Series(out, index=values.index)


def _limit_up_threshold(ts_code: pd.Series) -> pd.Series:
    code = ts_code.astype(str)
    is_wide_limit = code.str.startswith(("688", "689", "8", "4"))
    return pd.Series(np.where(is_wide_limit, 19.5, 9.5), index=ts_code.index)


def prepare_three_bull_feature_panel(panel: pd.DataFrame) -> pd.DataFrame:
    if panel is None or panel.empty:
        return pd.DataFrame()

    work = filter_non_chinext(panel.copy())
    if work.empty:
        return pd.DataFrame()
    if "trade_date" not in work.columns:
        raise ValueError("panel 缺少 trade_date 列")
    for col in ["ts_code", "open", "high", "low", "close"]:
        if col not in work.columns:
            raise ValueError(f"panel 缺少 {col} 列")

    work["trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce")
    work = work.dropna(subset=["ts_code", "trade_date", "close"]).copy()
    for col in ["open", "high", "low", "close", "vol", "amount", "turnover_rate", "pe", "pb", "total_mv"]:
        if col not in work.columns:
            work[col] = 0.0
        work[col] = _num(work[col])
    for col in ["name", "industry", "market"]:
        if col not in work.columns:
            work[col] = ""
        work[col] = work[col].fillna("").astype(str)
    work = filter_non_risk_warning(work)
    if work.empty:
        return pd.DataFrame()
    work["industry"] = work["industry"].str.strip()
    work.loc[work["industry"].isin(["", "nan", "None"]), "industry"] = "未分类"

    work = work.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    grp = work.groupby("ts_code", group_keys=False)
    work["close_shift_1"] = grp["close"].shift(1)
    work["close_shift_3"] = grp["close"].shift(3)
    work["close_shift_5"] = grp["close"].shift(5)
    work["close_shift_20"] = grp["close"].shift(20)
    work["pct_chg"] = ((work["close"] / (work["close_shift_1"] + 1e-12)) - 1.0) * 100.0
    work["ret_1"] = work["pct_chg"] / 100.0
    work["ret_3"] = ((work["close"] / (work["close_shift_3"] + 1e-12)) - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    work["ret_5"] = ((work["close"] / (work["close_shift_5"] + 1e-12)) - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    work["ret_20"] = ((work["close"] / (work["close_shift_20"] + 1e-12)) - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    work["ma_5"] = grp["close"].transform(lambda s: s.rolling(5).mean())
    work["ma_10"] = grp["close"].transform(lambda s: s.rolling(10).mean())
    work["ma_20"] = grp["close"].transform(lambda s: s.rolling(20).mean())
    work["high_20"] = grp["high"].transform(lambda s: s.rolling(20).max())
    work["high_20_prev"] = grp["high"].transform(lambda s: s.rolling(20).max().shift(1))
    work["amount_ma_20"] = grp["amount"].transform(lambda s: s.rolling(20).mean())
    work["amount_burst"] = ((work["amount"] / (work["amount_ma_20"] + 1e-12)) - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    work["limit_up_flag"] = (work["pct_chg"].fillna(0.0) >= _limit_up_threshold(work["ts_code"])).astype(float)
    work["strong_flag"] = (work["close"] >= work["ma_20"]).astype(float)
    work["breakout_flag"] = (work["close"] >= work["high_20_prev"].fillna(work["close"]) * 0.995).astype(float)
    work["ema12"] = grp["close"].transform(lambda s: s.ewm(span=12, adjust=False).mean())
    work["ema26"] = grp["close"].transform(lambda s: s.ewm(span=26, adjust=False).mean())
    work["macd_diff"] = work["ema12"] - work["ema26"]
    work["macd_dea"] = work.groupby("ts_code")["macd_diff"].transform(lambda s: s.ewm(span=9, adjust=False).mean())
    work["macd_bull_flag"] = (work["macd_diff"] > work["macd_dea"]).astype(float)
    work["连续上涨天数"] = grp["ret_1"].transform(lambda s: _streak_true(s > 0.0))
    work["距20日高点"] = ((work["close"] / (work["high_20"] + 1e-12)) - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)

    market = work.groupby("trade_date", as_index=False).agg(
        市场上涨占比=("ret_1", lambda s: float((s > 0.0).mean())),
        市场平均涨幅=("pct_chg", "mean"),
        市场涨停占比=("limit_up_flag", "mean"),
        市场成交额=("amount", "sum"),
    )
    market["市场成交额均线"] = market["市场成交额"].rolling(5, min_periods=2).mean()
    market["市场成交热度"] = (market["市场成交额"] / (market["市场成交额均线"] + 1e-12) - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    market["市场环境分"] = (
        100.0
        * (
            0.45 * market["市场上涨占比"].clip(0.0, 1.0)
            + 0.25 * np.clip((market["市场平均涨幅"] + 1.0) / 3.0, 0.0, 1.0)
            + 0.20 * np.clip(market["市场成交热度"] / 0.25, 0.0, 1.0)
            + 0.10 * np.clip(market["市场涨停占比"] / 0.03, 0.0, 1.0)
        )
    ).clip(lower=0.0, upper=100.0)
    work = work.merge(market[["trade_date", "市场环境分"]], on="trade_date", how="left")

    industry = work.groupby(["trade_date", "industry"], as_index=False).agg(
        板块成分股数=("ts_code", "nunique"),
        当日涨停数=("limit_up_flag", "sum"),
        板块1日平均涨幅=("pct_chg", "mean"),
        板块3日平均涨幅=("ret_3", "mean"),
        板块强势占比=("strong_flag", "mean"),
        板块突破占比=("breakout_flag", "mean"),
        板块成交爆发均值=("amount_burst", "mean"),
    )
    industry = industry.sort_values(["industry", "trade_date"]).reset_index(drop=True)
    industry["近3日涨停数"] = industry.groupby("industry")["当日涨停数"].transform(lambda s: s.rolling(3, min_periods=1).sum())
    industry["当日涨停占比"] = industry["当日涨停数"] / industry["板块成分股数"].clip(lower=1.0)
    industry["近3日涨停密度"] = industry["近3日涨停数"] / (industry["板块成分股数"].clip(lower=1.0) * 3.0)
    work = work.merge(industry, on=["trade_date", "industry"], how="left")

    work["涨停扩散原始"] = (
        0.45 * work["当日涨停占比"].fillna(0.0)
        + 0.35 * work["近3日涨停密度"].fillna(0.0)
        + 0.20 * work["limit_up_flag"].fillna(0.0)
    )
    work["板块联动原始"] = (
        0.35 * (work["板块1日平均涨幅"].fillna(0.0) / 100.0)
        + 0.25 * work["板块3日平均涨幅"].fillna(0.0)
        + 0.20 * work["板块强势占比"].fillna(0.0)
        + 0.20 * work["板块突破占比"].fillna(0.0)
    )
    work["成交爆发原始"] = 0.60 * work["amount_burst"].fillna(0.0) + 0.40 * work["板块成交爆发均值"].fillna(0.0)
    work["涨停扩散度"] = _z_to_score(work["涨停扩散原始"], work["trade_date"])
    work["板块联动度"] = _z_to_score(work["板块联动原始"], work["trade_date"])
    work["成交爆发度"] = _z_to_score(work["成交爆发原始"], work["trade_date"])
    work["传播启动分"] = (
        0.35 * work["涨停扩散度"]
        + 0.35 * work["板块联动度"]
        + 0.30 * work["成交爆发度"]
    ).clip(lower=0.0, upper=100.0)

    rank_key = ["trade_date", "industry"]
    group_size = work.groupby(rank_key)["ts_code"].transform("size").clip(lower=1.0)
    work["板块内涨幅排名"] = work.groupby(rank_key)["pct_chg"].rank(method="dense", ascending=False)
    work["板块内成交额排名"] = work.groupby(rank_key)["amount"].rank(method="dense", ascending=False)
    work["板块内放量排名"] = work.groupby(rank_key)["amount_burst"].rank(method="dense", ascending=False)
    work["涨幅排名分位"] = (1.0 - (work["板块内涨幅排名"] - 1.0) / group_size.clip(lower=2.0)).clip(lower=0.0, upper=1.0)
    work["成交额排名分位"] = (1.0 - (work["板块内成交额排名"] - 1.0) / group_size.clip(lower=2.0)).clip(lower=0.0, upper=1.0)
    work["放量排名分位"] = (1.0 - (work["板块内放量排名"] - 1.0) / group_size.clip(lower=2.0)).clip(lower=0.0, upper=1.0)
    work["龙头地位分"] = (
        100.0
        * (
            0.25 * work["涨幅排名分位"]
            + 0.20 * work["成交额排名分位"]
            + 0.20 * work["放量排名分位"]
            + 0.20 * work["breakout_flag"].fillna(0.0)
            + 0.15 * work["macd_bull_flag"].fillna(0.0)
        )
    ).clip(lower=0.0, upper=100.0)
    work["封板确认分"] = (
        100.0
        * (
            0.35 * work["limit_up_flag"].fillna(0.0)
            + 0.25 * work["breakout_flag"].fillna(0.0)
            + 0.20 * work["macd_bull_flag"].fillna(0.0)
            + 0.20 * np.clip(work["amount_burst"].fillna(0.0) / 0.60, 0.0, 1.0)
        )
    ).clip(lower=0.0, upper=100.0)
    work["高位追涨标记"] = (
        ((work["ret_5"] > 0.12) & (work["距20日高点"] > -0.02))
        | ((work["ret_20"] > 0.30) & (work["距20日高点"] > -0.04))
    ).astype(float)
    work["拥挤惩罚分"] = (
        25.0 * work["高位追涨标记"].fillna(0.0)
        + 15.0 * (work["连续上涨天数"].fillna(0.0) >= 4).astype(float)
        + 15.0 * (work["距20日高点"].fillna(0.0) > -0.02).astype(float)
        + 15.0 * (work["涨停扩散度"].fillna(0.0) >= 80.0).astype(float)
        + 10.0 * (work["成交爆发度"].fillna(0.0) >= 85.0).astype(float)
    ).clip(lower=0.0, upper=100.0)
    work["题材催化分"] = (
        0.45 * work["传播启动分"].fillna(0.0)
        + 0.30 * work["龙头地位分"].fillna(0.0)
        + 0.25 * work["封板确认分"].fillna(0.0)
        - 0.15 * work["拥挤惩罚分"].fillna(0.0)
    ).clip(lower=0.0, upper=100.0)
    work["板块强度分"] = (
        0.45 * work["板块联动度"].fillna(0.0)
        + 0.25 * work["涨停扩散度"].fillna(0.0)
        + 0.20 * work["龙头地位分"].fillna(0.0)
        + 0.10 * work["成交爆发度"].fillna(0.0)
    ).clip(lower=0.0, upper=100.0)
    return work.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)


def _find_best_setup(hist: pd.DataFrame, latest_pos: int, config: ThreeBullPullbackConfig) -> dict | None:
    if latest_pos < config.min_history_days:
        return None
    tail_start = max(0, latest_pos - config.max_scan_tail_days + 1)
    tail = hist.iloc[tail_start : latest_pos + 1].reset_index(drop=True)
    if len(tail) < 6:
        return None

    latest = tail.iloc[-1]
    latest_close = float(latest.get("close", 0.0) or 0.0)
    latest_high = float(latest.get("high", 0.0) or 0.0)
    ma5 = float(latest.get("ma_5", 0.0) or 0.0)
    ma10 = float(latest.get("ma_10", 0.0) or 0.0)
    ma20 = float(latest.get("ma_20", 0.0) or 0.0)
    high20 = float(latest.get("high_20", 0.0) or 0.0)
    amount_ma20 = float(latest.get("amount_ma_20", 0.0) or 0.0)
    if latest_close <= 0 or ma10 <= 0 or ma20 <= 0 or high20 <= 0:
        return None

    best: dict | None = None
    pct = tail["close"].pct_change().fillna(0.0)
    for end_idx in range(3, len(tail) - 2):
        streak = pct.iloc[end_idx - 2 : end_idx + 1]
        if len(streak) < 3 or not bool((streak > 0.0).all()):
            continue
        streak_start_close = float(tail["close"].iloc[end_idx - 3] or 0.0)
        streak_high = float(tail["high"].iloc[end_idx - 2 : end_idx + 1].max() or 0.0)
        streak_end_close = float(tail["close"].iloc[end_idx] or 0.0)
        if streak_start_close <= 0 or streak_high <= 0:
            continue
        streak_gain = streak_end_close / (streak_start_close + 1e-12) - 1.0
        if streak_gain < config.min_streak_gain or streak_gain > config.max_streak_gain:
            continue

        pullback_segment = tail.iloc[end_idx + 1 : -1].copy()
        pullback_days = len(pullback_segment)
        if pullback_days < config.min_pullback_days or pullback_days > config.max_pullback_days:
            continue
        pullback_low = float(pd.to_numeric(pullback_segment["low"], errors="coerce").min() or 0.0)
        pullback_high = float(pd.to_numeric(pullback_segment["high"], errors="coerce").max() or 0.0)
        if pullback_low <= 0 or pullback_high <= 0:
            continue
        pullback_depth = pullback_low / (streak_high + 1e-12) - 1.0
        if not (config.min_pullback_depth <= pullback_depth <= config.max_pullback_depth):
            continue

        latest_ret1 = float(latest.get("ret_1", 0.0) or 0.0)
        prev_close = float(tail["close"].iloc[-2] or 0.0)
        volume_ratio = float(latest.get("amount", 0.0) or 0.0) / (amount_ma20 + 1e-12) if amount_ma20 > 0 else 1.0
        dist_high = latest_close / (high20 + 1e-12) - 1.0
        market_score = float(latest.get("市场环境分", 50.0) or 50.0)
        sector_score = float(latest.get("板块强度分", 50.0) or 50.0)
        support_ok = pullback_low >= min(ma5 if ma5 > 0 else pullback_low, ma10 if ma10 > 0 else pullback_low) * 0.985
        dist_ok = -0.12 <= dist_high <= -0.003
        trend_watch = float(latest.get("ret_20", 0.0) or 0.0) > -0.02 and latest_close >= ma10 * 0.985 and ma10 >= ma20 * 0.97
        trend_confirm = float(latest.get("ret_20", 0.0) or 0.0) > 0.0 and latest_close >= ma10 * 0.995 and ma10 >= ma20 * 0.99
        volume_watch = volume_ratio >= float(config.min_watch_volume_ratio)
        price_breakout = (
            latest_close >= max(prev_close, pullback_high * (1.0 + config.breakout_buffer), ma5 * 1.001 if ma5 > 0 else prev_close)
            and latest_high >= pullback_high * (1.0 + config.breakout_buffer)
        )
        momentum_controlled = float(config.min_confirm_gain) <= latest_ret1 <= float(config.max_confirm_gain)
        volume_confirm = volume_ratio >= float(config.min_confirm_volume_ratio)
        market_ok = market_score >= float(config.min_market_score)
        sector_ok = sector_score >= float(config.min_sector_score)
        reconfirm = (
            price_breakout
            and momentum_controlled
            and volume_confirm
            and market_ok
            and sector_ok
        )
        if not (support_ok and dist_ok and trend_watch and volume_watch):
            continue

        depth_quality = float(np.clip(1.0 - abs(abs(pullback_depth) - abs(config.ideal_pullback_depth)) / 0.04, 0.0, 1.0))
        freshness = {1: 100.0, 2: 82.0, 3: 68.0}.get(int(pullback_days), 50.0)
        structure_score = 100.0 * (
            0.35 * min(max(streak_gain / 0.10, 0.0), 1.0)
            + 0.25 * (1.0 if trend_watch else 0.0)
            + 0.20 * (1.0 if trend_confirm else 0.0)
            + 0.20 * (1.0 - float(latest.get("高位追涨标记", 0.0) or 0.0))
        )
        pullback_score = 100.0 * (
            0.45 * depth_quality
            + 0.20 * (1.0 if support_ok else 0.0)
            + 0.20 * (freshness / 100.0)
            + 0.15 * min(max((volume_ratio - float(config.min_watch_volume_ratio)) / 0.55, 0.0), 1.0)
        )
        execution_score = 100.0 * (
            0.35 * (1.0 if price_breakout else 0.0)
            + 0.25 * (1.0 if volume_confirm else min(max(volume_ratio / max(float(config.min_confirm_volume_ratio), 1e-12), 0.0), 1.0))
            + 0.20 * (1.0 if momentum_controlled else 0.0)
            + 0.20 * float(latest.get("macd_bull_flag", 0.0) or 0.0)
        )
        setup_quality = 0.55 * structure_score + 0.45 * pullback_score
        status = "右侧确认候选" if reconfirm and trend_confirm else "右侧观察候选"
        if status == "右侧观察候选" and not config.include_watch:
            continue

        final_score = float(
            0.20 * market_score
            + 0.20 * sector_score
            + 0.20 * setup_quality
            + 0.20 * float(latest.get("题材催化分", 50.0) or 50.0)
            + 0.15 * execution_score
            + 0.05 * (100.0 - float(latest.get("拥挤惩罚分", 0.0) or 0.0))
        )
        weak_market = market_score < 60.0
        if status == "右侧确认候选" and weak_market:
            action = "弱市轻仓"
        elif status == "右侧确认候选":
            action = "确认买点"
        else:
            action = "观察等待"
        check_parts = [
            "放量达标" if volume_confirm else f"量能不足{volume_ratio:.2f}x",
            "突破达标" if price_breakout else "未突破",
            "涨幅合适" if momentum_controlled else f"涨幅{latest_ret1 * 100.0:.1f}%需谨慎",
        ]
        if weak_market:
            check_parts.append(f"市场环境偏弱{market_score:.1f}")
        if not market_ok:
            check_parts.append(f"环境分{market_score:.1f}低于阈值")
        if not sector_ok:
            check_parts.append(f"板块分{sector_score:.1f}低于阈值")
        row = {
            "候选来源": status,
            "右侧形态": "三连阳后回踩确认" if status == "右侧确认候选" else "三连阳后回踩观察",
            "交易动作": action,
            "买点检查": "；".join(check_parts),
            "三阳累计涨幅%": streak_gain * 100.0,
            "回踩天数": float(pullback_days),
            "回踩幅度%": pullback_depth * 100.0,
            "量能比": volume_ratio,
            "结构强度分": float(np.clip(structure_score, 0.0, 100.0)),
            "回踩质量分": float(np.clip(pullback_score, 0.0, 100.0)),
            "执行确认分": float(np.clip(execution_score, 0.0, 100.0)),
            "三阳回踩质量分": float(np.clip(setup_quality, 0.0, 100.0)),
            "策略评分": float(np.clip(final_score, 0.0, 100.0)),
            "入选说明": (
                f"三阳涨幅{streak_gain * 100.0:.1f}%，回踩{abs(pullback_depth) * 100.0:.1f}%"
                f"，{'放量突破确认' if status == '右侧确认候选' else '待放量突破确认'}"
            ),
        }
        if best is None or float(row["策略评分"]) > float(best["策略评分"]):
            best = row
    return best


def build_three_bull_pullback_signals(
    panel: pd.DataFrame,
    config: ThreeBullPullbackConfig | None = None,
) -> pd.DataFrame:
    cfg = config or ThreeBullPullbackConfig()
    features = prepare_three_bull_feature_panel(panel)
    if features.empty:
        return pd.DataFrame()

    rows: list[dict] = []
    for _, hist in features.groupby("ts_code", sort=False):
        hist = hist.sort_values("trade_date").reset_index(drop=True)
        if len(hist) < cfg.min_history_days + cfg.max_pullback_days + 3:
            continue
        for pos in range(cfg.min_history_days, len(hist)):
            latest = hist.iloc[pos]
            name = str(latest.get("name", "") or "")
            setup = _find_best_setup(hist, latest_pos=pos, config=cfg)
            if setup is None or float(setup.get("策略评分", 0.0)) < cfg.min_score:
                continue
            rows.append(
                {
                    "trade_date": latest["trade_date"],
                    "ts_code": str(latest.get("ts_code", "") or ""),
                    "name": name,
                    "industry": str(latest.get("industry", "") or ""),
                    "market": str(latest.get("market", "") or ""),
                    "close": float(latest.get("close", 0.0) or 0.0),
                    "pct_chg": float(latest.get("pct_chg", 0.0) or 0.0),
                    "近5日涨跌%": float(latest.get("ret_5", 0.0) or 0.0) * 100.0,
                    "近20日涨跌%": float(latest.get("ret_20", 0.0) or 0.0) * 100.0,
                    "换手率": float(latest.get("turnover_rate", 0.0) or 0.0),
                    "市场环境分": float(latest.get("市场环境分", 50.0) or 50.0),
                    "板块强度分": float(latest.get("板块强度分", 50.0) or 50.0),
                    "题材催化分": float(latest.get("题材催化分", 50.0) or 50.0),
                    "传播启动分": float(latest.get("传播启动分", 50.0) or 50.0),
                    "龙头地位分": float(latest.get("龙头地位分", 50.0) or 50.0),
                    "封板确认分": float(latest.get("封板确认分", 50.0) or 50.0),
                    "拥挤惩罚分": float(latest.get("拥挤惩罚分", 0.0) or 0.0),
                    **setup,
                }
            )

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
    out["候选优先级"] = out["候选来源"].astype(str).eq("右侧确认候选").astype(int)
    out = out.sort_values(
        ["trade_date", "候选优先级", "策略评分", "执行确认分"],
        ascending=[True, False, False, False],
    ).reset_index(drop=True)
    out["当日排名"] = out.groupby("trade_date").cumcount() + 1
    return out


def append_forward_returns(signals: pd.DataFrame, panel: pd.DataFrame, horizons: Iterable[int] = (1, 3, 5), fee_rate: float = 0.0005) -> pd.DataFrame:
    if signals is None or signals.empty:
        return pd.DataFrame()
    features = prepare_three_bull_feature_panel(panel)
    if features.empty:
        return pd.DataFrame()

    work = features[["ts_code", "trade_date", "open", "high", "low", "close"]].copy()
    work = work.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)
    grp = work.groupby("ts_code", group_keys=False)
    work["next_trade_date"] = grp["trade_date"].shift(-1)
    work["next_open"] = grp["open"].shift(-1)
    work["next_close"] = grp["close"].shift(-1)
    max_horizon = max([int(h) for h in horizons] + [1])
    for horizon in range(1, max_horizon + 1):
        work[f"close_t{horizon}"] = grp["close"].shift(-horizon)
    for horizon in horizons:
        h = int(horizon)
        future_high = pd.concat([grp["high"].shift(-step) for step in range(1, h + 1)], axis=1).max(axis=1)
        future_low = pd.concat([grp["low"].shift(-step) for step in range(1, h + 1)], axis=1).min(axis=1)
        work[f"future_high_{h}"] = future_high
        work[f"future_low_{h}"] = future_low

    out = signals.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce")
    forward_cols = ["ts_code", "trade_date", "next_trade_date", "next_open", "next_close"]
    for horizon in horizons:
        h = int(horizon)
        forward_cols.extend([f"close_t{h}", f"future_high_{h}", f"future_low_{h}"])
    out = out.merge(work[forward_cols], on=["ts_code", "trade_date"], how="left")
    out = out.dropna(subset=["next_open", "next_close"]).copy()
    out["次日收益%"] = ((out["next_close"] / (out["next_open"] + 1e-12)) - 1.0 - 2.0 * float(fee_rate)) * 100.0
    out["次日上涨"] = out["次日收益%"] > 0.0
    for horizon in horizons:
        h = int(horizon)
        out[f"{h}日收盘收益%"] = ((out[f"close_t{h}"] / (out["next_open"] + 1e-12)) - 1.0 - 2.0 * float(fee_rate)) * 100.0
        out[f"{h}日最高收益%"] = ((out[f"future_high_{h}"] / (out["next_open"] + 1e-12)) - 1.0) * 100.0
        out[f"{h}日最大回撤%"] = ((out[f"future_low_{h}"] / (out["next_open"] + 1e-12)) - 1.0) * 100.0
    return out.sort_values(["trade_date", "策略评分"], ascending=[True, False]).reset_index(drop=True)


def summarize_three_bull_backtest(
    trades: pd.DataFrame,
    top_n_values: Iterable[int] = (3, 5, 10),
    target_gain_pct: float = 3.0,
    max_drawdown_pct: float = 3.0,
    horizon_days: int = 3,
) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame()

    rows: list[dict] = []
    for top_n in top_n_values:
        selected = trades[trades["当日排名"] <= int(top_n)].copy()
        if selected.empty:
            continue
        trade_days = selected["trade_date"].nunique()
        daily = selected.groupby("trade_date", as_index=False).agg(组合次日收益=("次日收益%", "mean"))
        daily_ret = daily["组合次日收益"] / 100.0
        nav = (1.0 + daily_ret).cumprod()
        high_col = f"{int(horizon_days)}日最高收益%"
        dd_col = f"{int(horizon_days)}日最大回撤%"
        close_col = f"{int(horizon_days)}日收盘收益%"
        rows.append(
            {
                "分组": f"Top{int(top_n)}",
                "样本数": int(len(selected)),
                "信号交易日": int(trade_days),
                "次日胜率": float(selected["次日上涨"].mean()),
                "次日平均收益": float(selected["次日收益%"].mean() / 100.0),
                "次日中位收益": float(selected["次日收益%"].median() / 100.0),
                f"{int(horizon_days)}日收盘平均收益": float(selected[close_col].mean() / 100.0) if close_col in selected.columns else np.nan,
                f"{int(horizon_days)}日冲高达标率": float((selected[high_col] >= float(target_gain_pct)).mean()) if high_col in selected.columns else np.nan,
                f"{int(horizon_days)}日回撤可控率": float((selected[dd_col] >= -float(max_drawdown_pct)).mean()) if dd_col in selected.columns else np.nan,
                "组合累计次日收益": float(nav.iloc[-1] - 1.0) if not nav.empty else 0.0,
                "日均候选数": float(len(selected) / max(trade_days, 1)),
            }
        )
    return pd.DataFrame(rows)


def summarize_score_bins(
    trades: pd.DataFrame,
    target_gain_pct: float = 3.0,
    max_drawdown_pct: float = 3.0,
    horizon_days: int = 3,
) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame()
    work = trades.copy()
    bins = [0, 60, 70, 80, 85, 90, 100]
    labels = ["<60", "60-70", "70-80", "80-85", "85-90", "90+"]
    work["评分区间"] = pd.cut(work["策略评分"], bins=bins, labels=labels, include_lowest=True, right=False)
    high_col = f"{int(horizon_days)}日最高收益%"
    dd_col = f"{int(horizon_days)}日最大回撤%"
    close_col = f"{int(horizon_days)}日收盘收益%"
    grouped = work.groupby("评分区间", observed=False)
    return grouped.agg(
        样本数=("ts_code", "size"),
        平均评分=("策略评分", "mean"),
        次日胜率=("次日上涨", "mean"),
        次日平均收益=("次日收益%", lambda s: float(s.mean() / 100.0)),
        次日中位收益=("次日收益%", lambda s: float(s.median() / 100.0)),
        **{
            f"{int(horizon_days)}日收盘平均收益": (close_col, lambda s: float(s.mean() / 100.0)),
            f"{int(horizon_days)}日冲高达标率": (high_col, lambda s: float((s >= float(target_gain_pct)).mean())),
            f"{int(horizon_days)}日回撤可控率": (dd_col, lambda s: float((s >= -float(max_drawdown_pct)).mean())),
        },
    ).reset_index()


def rolling_backtest_summary(
    trades: pd.DataFrame,
    top_n: int = 5,
    window_days: int = 20,
    step_days: int = 5,
) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame()
    selected = trades[trades["当日排名"] <= int(top_n)].copy()
    if selected.empty:
        return pd.DataFrame()
    dates = sorted(pd.to_datetime(selected["trade_date"], errors="coerce").dropna().unique())
    rows: list[dict] = []
    for start_idx in range(0, max(0, len(dates) - int(window_days) + 1), max(1, int(step_days))):
        window = dates[start_idx : start_idx + int(window_days)]
        if not window:
            continue
        start = pd.Timestamp(window[0])
        end = pd.Timestamp(window[-1])
        sub = selected[(selected["trade_date"] >= start) & (selected["trade_date"] <= end)].copy()
        if sub.empty:
            continue
        daily = sub.groupby("trade_date", as_index=False).agg(组合次日收益=("次日收益%", "mean"))
        nav = (1.0 + daily["组合次日收益"] / 100.0).cumprod()
        rows.append(
            {
                "窗口开始": start.strftime("%Y-%m-%d"),
                "窗口结束": end.strftime("%Y-%m-%d"),
                "样本数": int(len(sub)),
                "信号交易日": int(sub["trade_date"].nunique()),
                "次日胜率": float(sub["次日上涨"].mean()),
                "次日平均收益": float(sub["次日收益%"].mean() / 100.0),
                "窗口累计次日收益": float(nav.iloc[-1] - 1.0) if not nav.empty else 0.0,
            }
        )
    return pd.DataFrame(rows)
