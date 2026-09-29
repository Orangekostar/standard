"""Causal, calendar-complete features for the fixed high-win experiment.

No provider calls. Stock chunks may be computed separately, but cross-sectional
ranks MUST be computed once after concatenating every stock chunk.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from core.technical_v2.contracts import ContractError

FORMULA_VERSION = "high-win-features.v1"


def _calendar(frame, sessions, key):
    if frame.duplicated([key, "date"]).any():
        raise ContractError("duplicate high-win input key/date")
    parts = []
    for value, group in frame.groupby(key, sort=True):
        indexed = group.set_index("date").reindex(sessions)
        indexed[key] = value
        indexed.index.name = "date"
        parts.append(indexed.reset_index())
    return pd.concat(parts, ignore_index=True) if parts else frame.copy()


def context_features(market_context, sector_context, sessions, as_of):
    dates = [d for d in sessions if d <= as_of]
    market = market_context.loc[market_context.date.le(as_of)].copy()
    if market.date.duplicated().any():
        raise ContractError("duplicate market dates")
    market = market.set_index("date").reindex(dates)
    market.index.name = "date"
    market["market_return1"] = market.index_level / market.index_level.shift(1) - 1
    market["market_ell5"] = np.log(market.index_level / market.index_level.shift(5))
    market["market_ell5"] = market.market_ell5.where(market.index_level.rolling(6).count().eq(6))
    market["market_breadth_delta1"] = market.breadth20.diff()
    market["market_breadth_delta5"] = market.breadth20.diff(5)
    market["market_breadth_prev5_min"] = market.breadth20.shift(1).rolling(5).min()
    market = market.rename(columns={"breadth20": "market_breadth20", "status": "market_context_status"})
    market_cols = ["market_return1", "market_ell5", "market_breadth_delta1",
                   "market_breadth_delta5", "market_breadth_prev5_min", "market_breadth20", "market_context_status"]
    sectors = sector_context.loc[sector_context.date.le(as_of)].copy()
    if "namespace" in sectors:
        sectors = sectors.loc[sectors.namespace.eq("SW_L1")]
    sectors = _calendar(sectors, dates, "sector_id")
    g = sectors.groupby("sector_id", sort=False)
    for n in (3, 20, 60):
        complete = g.index_level.transform(lambda s: s.rolling(n+1).count()).eq(n+1)
        sectors[f"sector_ell{n}"] = np.log(sectors.index_level / g.index_level.shift(n)).where(complete)
    sectors["sector_breadth_delta3"] = g.breadth20.diff(3)
    valid = (sectors.status.eq("OK") & sectors.covered_count.ge(5)
             & sectors.coverage.ge(.90) & np.isfinite(sectors.sector_ell20))
    values = sectors.sector_ell20.where(valid)
    grouped = values.groupby(sectors.date)
    sectors["sector_return20_pct"] = grouped.rank(method="average", pct=True).where(grouped.transform("count").ge(5))
    sectors["sector_return20_pct_prev5"] = sectors.groupby("sector_id").sector_return20_pct.shift(5)
    rotation = (sectors.sector_return20_pct.ge(.75) & sectors.sector_return20_pct_prev5.le(.50)
                & sectors.sector_ell20.gt(0) & sectors.breadth20.ge(.55))
    sectors["sector_rotation_event"] = sectors.date.where(rotation)
    sectors["sector_rotation_anchor"] = None
    for lag in range(5, 1, -1):
        anchor = sectors.groupby("sector_id").sector_rotation_event.shift(lag)
        sectors["sector_rotation_anchor"] = sectors.sector_rotation_anchor.where(anchor.isna(), anchor)
    sectors = sectors.rename(columns={"breadth20": "sector_breadth20", "status": "sector_context_status"})
    cols = ["date", "sector_id", "sector_ell3", "sector_ell20", "sector_ell60",
            "sector_breadth20", "sector_breadth_delta3", "sector_context_status",
            "sector_return20_pct", "sector_return20_pct_prev5", "sector_rotation_anchor"]
    return market[market_cols].reset_index(), sectors[cols]


def build_high_win_features(panel, market_context, sector_context, sessions, as_of):
    """Compute one stock block; finalize_cross_section is required after concat."""
    if sessions != sorted(set(sessions)) or as_of not in sessions:
        raise ContractError("invalid high-win calendar/as_of")
    dates = [d for d in sessions if d <= as_of]
    f = _calendar(panel.loc[panel.date.le(as_of)].copy(), dates, "code")
    required = ["comparison_open", "comparison_high", "comparison_low", "comparison_close", "amount_cny"]
    for column in required:
        if column not in f:
            f[column] = np.nan
    valid = f[required[:4]].apply(np.isfinite).all(axis=1) & f[required[:4]].gt(0).all(axis=1)
    valid &= (f.comparison_high.ge(f[["comparison_open", "comparison_close"]].max(axis=1))
              & f.comparison_low.le(f[["comparison_open", "comparison_close"]].min(axis=1)))
    if "real_bar" in f:
        valid &= f.real_bar.eq(True)
    f.loc[~valid, required[:4]] = np.nan
    f.loc[~valid | f.amount_cny.lt(0), "amount_cny"] = np.nan
    g = f.groupby("code", sort=False)
    c, o, h, l = (f[k] for k in ("comparison_close", "comparison_open", "comparison_high", "comparison_low"))
    for n in (1, 2, 3, 5, 20, 60):
        ratio = c / g.comparison_close.shift(n)
        # Endpoint-only returns must still have complete intervening bars.
        complete = g.comparison_close.transform(lambda s: s.rolling(n + 1).count()).eq(n + 1)
        f[f"R{n}"] = (ratio - 1).where(complete)
        f[f"ell{n}"] = np.log(ratio).where(complete)
    for n in (5, 10, 20, 60):
        f[f"MA{n}"] = g.comparison_close.transform(lambda s: s.rolling(n).mean())
    f["sigma20"] = f.groupby("code").ell1.transform(lambda s: s.rolling(20).std(ddof=0))
    prev = g.comparison_close.shift(1)
    f["TR"] = pd.concat([h-l, (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1, skipna=False)
    for n in (5, 14, 20):
        f[f"ATR{n}"] = f.groupby("code").TR.transform(lambda s: s.rolling(n).mean())
    f["atr_fraction"] = f.ATR14 / c
    for n in (10, 20):
        f[f"HH{n}"] = g.comparison_high.transform(lambda s: s.rolling(n).max())
        f[f"LL{n}"] = g.comparison_low.transform(lambda s: s.rolling(n).min())
    f["HH20prev"] = g.comparison_high.transform(lambda s: s.shift(1).rolling(20).max())
    f["LL20prev"] = g.comparison_low.transform(lambda s: s.shift(1).rolling(20).min())
    f["DD20"] = c / g.comparison_close.transform(lambda s: s.rolling(20).max()) - 1
    spread = (h-l).where(h.gt(l))
    f["CLV"] = (2*c-h-l) / spread
    f["LW"] = (pd.concat([o, c], axis=1).min(axis=1)-l) / spread
    f["UW"] = (h-pd.concat([o, c], axis=1).max(axis=1)) / spread
    f["VA"] = f.amount_cny / g.amount_cny.transform(lambda s: s.shift(1).rolling(20).median()).replace(0, np.nan)
    f["VR3"] = (g.amount_cny.transform(lambda s: s.rolling(3).mean())
                / g.amount_cny.transform(lambda s: s.shift(3).rolling(20).median()).replace(0, np.nan))
    f["ADV20"] = g.amount_cny.transform(lambda s: s.rolling(20).mean())
    numerator = f.groupby("code").ell1.transform(lambda s: s.rolling(20).sum()).abs()
    denominator = f.groupby("code").ell1.transform(lambda s: s.abs().rolling(20).sum())
    f["ER20"] = (numerator / denominator).mask(denominator.eq(0), 0.)
    market, sectors = context_features(market_context, sector_context, sessions, as_of)
    # Always join the independently computed sector history to today's membership.
    # Shifting a stock's sector index would mix two sectors at a membership change.
    drop = (set(market.columns) | set(sectors.columns)) - {"date", "sector_id"}
    f = f.drop(columns=list(drop & set(f.columns)))
    f = f.merge(market, on="date", how="left", validate="many_to_one")
    f = f.merge(sectors, on=["date", "sector_id"], how="left", validate="many_to_one")
    f["RS60"] = f.ell60 - f.sector_ell60
    f["Zrel3"] = (f.ell3-f.sector_ell3) / (f.sigma20.clip(lower=.005)*np.sqrt(3))
    f["high_win_feature_reason"] = np.where(valid, "", "INVALID_OR_MISSING_BAR")
    return f


def finalize_cross_section(panel):
    f = panel.copy()
    if f.duplicated(["date", "code"]).any():
        raise ContractError("duplicate stocks in high-win cross section")
    valid = f.RS60.where(f.sector_context_status.eq("OK") & np.isfinite(f.RS60))
    grouped = valid.groupby([f.date, f.sector_id])
    f["rs60_within_sector_pct"] = grouped.rank(method="average", pct=True).where(grouped.transform("count").ge(10))
    f["high_win_rank_score"] = (.5*f.rs60_within_sector_pct + .3*f.sector_return20_pct + .2*(f.CLV+1)/2)
    return f
