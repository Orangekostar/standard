"""The sixteen preregistered, price/volume-only signal rules.

This module evaluates conditions, not orders. Event consumption belongs to each
account's replay; rejected next-open attempts must never resurrect an anchor.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from core.data.symbols import is_buyable_mainboard_ts_code, is_risk_warning_name
from core.technical_v2.contracts import ContractError


@dataclass(frozen=True)
class HighWinPolicy:
    family: str
    variant: str = "CONFIRMED"

    def __post_init__(self):
        if self.family not in {f"S{i:02d}" for i in range(1, 9)} or self.variant not in {"CONFIRMED", "STRICT"}:
            raise ContractError("policy outside frozen high-win suite")

    @property
    def policy_id(self):
        return f"{self.family}_{self.variant}"


def policy_grid():
    return tuple(HighWinPolicy(f"S{i:02d}", v) for i in range(1, 9) for v in ("CONFIRMED", "STRICT"))


def common_mask(f):
    return (f.real_bar.eq(True) & f.roster_active.eq(True)
        & f.code.map(is_buyable_mainboard_ts_code) & f.listing_board.isin(["MAIN_SH", "MAIN_SZ"])
        & f.instrument_type.eq("stock") & ~f.name.fillna("").map(is_risk_warning_name)
        & ~f.is_risk_warning.eq(True) & f.is_suspended.eq(False)
        & f.valuation_close.ge(5) & f.adj_factor.gt(0)
        & f.sector_id.notna() & ~f.sector_id.isin(["", "UNKNOWN", "801180.SI"])
        & f.market_context_status.eq("OK") & f.sector_context_status.eq("OK")
        & f.market_state.isin(["RANGE", "TREND_EXPANSION"])
        & f.market_breadth20.ge(.45) & f.sigma20.gt(0) & f.sigma20.le(.04)
        & f.atr_fraction.le(.06) & f.ADV20.ge(50_000_000)
        & f.high_win_feature_reason.eq("") & np.isfinite(f.high_win_rank_score)).fillna(False)


def strict_mask(f):
    return (f.sector_return20_pct.ge(.75) & f.rs60_within_sector_pct.ge(.75)
        & f.market_breadth20.ge(.55) & f.market_breadth_delta5.ge(0)
        & f.sigma20.le(.025) & (f.comparison_close/f.MA20).le(1.05)).fillna(False)


def family_conditions(history):
    """One code, complete calendar index. Return raw family flags and anchors."""
    f = history.sort_values("date").reset_index(drop=True)
    if f.code.nunique() != 1 or f.date.duplicated().any():
        raise ContractError("family evaluator requires one code with unique dates")
    C,H,L,O = (f[k] for k in ("comparison_close", "comparison_high", "comparison_low", "comparison_open"))
    confirm = C.gt(H.shift()) & f.CLV.ge(.40)
    positive = f.R1.gt(0) & f.R1.le(.03)
    sector = f.sector_ell20.gt(0) & f.sector_breadth20.ge(.50)
    flags, anchors = {}, {}
    flags['S01'] = (f.MA20.gt(f.MA60) & f.MA20.gt(f.MA20.shift(5)) & sector
        & f.rs60_within_sector_pct.ge(.60) & (C/f.MA20).shift().between(.98,1.01)
        & f.DD20.shift().between(-.10,-.02) & f.VR3.shift().le(.80)
        & confirm & f.R1.between(.003,.03))
    # The most recent anchor is fixed before testing later path conditions.
    breakout = C.gt(f.HH20prev) & f.VA.ge(1.5) & f.R1.gt(0) & f.R1.le(.06)
    selected = pd.Series(np.nan, index=f.index)
    for lag in range(8,1,-1):
        selected = selected.mask(breakout.shift(lag).eq(True), lag)
    flag = pd.Series(False,index=f.index); anchor = pd.Series(None,index=f.index,dtype=object)
    for lag in range(2,9):
        K=f.HH20prev.shift(lag)
        path=(L.rolling(lag).min().ge(.97*K) & C.shift().rolling(lag-1).min().ge(.98*K))
        ok=selected.eq(lag)&path&L.le(1.02*K)&C.ge(K)&confirm&positive&sector
        flag |= ok; anchor=anchor.mask(selected.eq(lag),f.date.shift(lag))
    flags['S02'],anchors['S02']=flag,anchor
    K=f.LL20prev.shift()
    flags['S03']=(L.shift().ge(.97*K)&L.shift().lt(.995*K)&C.shift().gt(K)
        & f.LW.shift().ge(.45)&f.VA.shift().between(1.,2.5)&confirm&positive
        & f.ER20.le(.35)&C.ge(.97*f.MA60)&f.sector_ell20.ge(0))
    anchors['S03']=f.date.shift()
    flags['S04']=(f.Zrel3.shift().between(-2.5,-1.)&f.R3.shift().le(-.02)
        & f.MA20.ge(f.MA60)&C.ge(.98*f.MA60)&f.ell60.ge(0)
        & f.rs60_within_sector_pct.ge(.50)&f.sector_ell20.gt(0)
        & f.sector_breadth20.ge(.55)&f.sigma20.le(.035)&confirm&positive)
    flags['S05']=((f.HH10/f.LL10-1).shift(2).le(.08)
        &(f.ATR5/f.ATR20).shift(2).le(.75)&breakout.shift().eq(True)
        &C.ge(C.shift())&L.ge(.98*f.HH20prev.shift())&f.VA.between(.7,1.8)
        &f.sigma20.le(.03)&f.sector_ell20.gt(0))
    anchors['S05']=f.date.shift()
    flags['S06']=(f.market_breadth_prev5_min.le(.35)&f.market_breadth20.ge(.45)
        &f.market_breadth_delta1.ge(.08)&f.market_return1.gt(0)
        &f.sector_breadth_delta3.ge(.10)&f.sector_ell20.ge(-.02)
        &(f.ell5-f.market_ell5).ge(0)&f.R5.ge(-.08)&f.sigma20.le(.035)
        &C.gt(f.MA5)&confirm)
    flags['S07']=(f.sector_rotation_anchor.notna()&f.sector_return20_pct.ge(.75)
        &f.sector_breadth20.ge(.55)&f.rs60_within_sector_pct.ge(.70)
        &(C/f.MA10).shift().between(.98,1.01)&f.R2.shift().lt(0)
        &f.VR3.shift().le(.80)&confirm)
    # Include the sector in the event identity: a membership change cannot
    # accidentally consume an unrelated industry's event on the same date.
    anchors['S07']=f.sector_id.astype(str)+':'+f.sector_rotation_anchor.fillna('').astype(str)
    launch=f.R1.between(.025,.065)&f.VA.ge(1.8)&f.UW.le(.25)&f.CLV.ge(.60)
    selected=pd.Series(np.nan,index=f.index)
    for lag in range(8,3,-1):
        selected=selected.mask(launch.shift(lag).eq(True),lag)
    flag=pd.Series(False,index=f.index);anchor=pd.Series(None,index=f.index,dtype=object)
    for lag in range(4,9):
        K=((O+C)/2).shift(lag)
        path=(C.shift().rolling(lag-1).min().ge(K)
            &L.shift().rolling(lag-1).min().ge(.98*K)
            &H.shift().rolling(lag-1).max().le(1.03*H.shift(lag))
            &f.amount_cny.shift().rolling(lag-1).mean().le(.70*f.amount_cny.shift(lag)))
        ok=(selected.eq(lag)&path&C.gt(H.shift().rolling(lag).max())
            &f.VA.ge(1.2)&f.R1.gt(0)&f.R1.le(.04)&sector)
        flag|=ok;anchor=anchor.mask(selected.eq(lag),f.date.shift(lag))
    flags['S08'],anchors['S08']=flag,anchor
    out=f[['date','code']].copy()
    for family in flags:
        out[family]=flags[family].fillna(False)
        out[family+'_anchor']=anchors.get(family,f.date)
    return out


def evaluate_signals(history, policy):
    f=history.sort_values('date').reset_index(drop=True)
    raw=family_conditions(f)
    common=common_mask(f)
    strict=strict_mask(f) if policy.variant=='STRICT' else pd.Series(True,index=f.index)
    eligible=common&raw[policy.family]&strict
    return pd.DataFrame(dict(date=f.date,code=f.code,eligible=eligible,
        family=policy.family,variant=policy.variant,event_anchor=raw[policy.family+'_anchor'],
        rank_score=f.high_win_rank_score,information_cutoff=f.date,
        reason_codes=np.select([~common,~raw[policy.family],~strict],
            ['COMMON_MASK','FAMILY_CONDITIONS','STRICT_CONFIRMATION'],default='ELIGIBLE')))
