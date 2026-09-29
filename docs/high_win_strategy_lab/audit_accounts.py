"""Independent cents-ledger audit of this frozen experiment (no actions in source).

Run after all cells complete: python docs/high_win_strategy_lab/audit_accounts.py ROOT
This verifier does not import the replay implementation or its metrics formulas.
"""
import argparse
import hashlib
import json
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path

import numpy as np
import pandas as pd


def read(path): return json.loads(path.read_text())
def cents(x): return int((x*100).quantize(Decimal(1),rounding=ROUND_HALF_UP))
def dec(x): return Decimal(str(x))


def audit(root):
    prep=read(root/'prepared.json');protocol=read(root/'protocol_frozen.json')
    source=Path(prep['input_root'])
    assert pd.read_parquet(source/'corporate_actions.parquet').empty, 'this audit requires the disclosed no-action dataset'
    features=read(source/'feature_manifest.json')
    cols=['code','date','execution_open','valuation_close','adj_factor','Q03','Q02','sector_id','market_state']
    parts=[pd.read_parquet(r['path'],columns=cols) for r in features['feature_chunks']]
    market=pd.concat(parts,ignore_index=True).set_index(['date','code']);del parts
    replay=pd.read_parquet(prep['replay_path']).set_index(['date','code'])
    early=read(root/'selection_cells.json');later=read(root/'review_cells.json')
    assert len(early)==216 and len(later)==36
    fills_count=trades_count=0;account_ids=set()
    for k,m in enumerate(early+later,1):
        p,w,c=m['strategy_id'],m['split'],m['cost_scenario'];cell=root/p/w/c
        receipt=read(cell/'cell_complete.json')
        assert receipt['binding']==prep['binding']
        for relative,record in receipt['files'].items():
            file=cell/relative
            assert file.stat().st_size==record['bytes']
            assert hashlib.sha256(file.read_bytes()).hexdigest()==record['sha256']
        nav=pd.read_csv(cell/'daily_nav.csv.gz',dtype={'date':str})
        fills=pd.read_csv(cell/'fills.csv.gz',dtype={'trade_date':str,'code':str})
        orders=pd.read_csv(cell/'orders.csv.gz',dtype={'signal_date':str,'code':str}).set_index('order_id')
        trades=pd.read_csv(cell/'trades.csv',dtype={'entry_date':str,'closed_at':str,'code':str})
        assert not fills.order_id.duplicated().any()
        dates=protocol['windows'][w];ix={d:i for i,d in enumerate(dates)}
        assert nav.date.tolist()==dates
        if len(fills):
            assert fills.account_id.nunique()==1
            account=fills.account_id.iloc[0];assert account not in account_ids;account_ids.add(account)
        cash=100_000_000;positions={};entry={};cost={};ended=[];last_marks={}
        previous_nav=100_000_000;fee_sum=0
        for n,row in enumerate(nav.itertuples()):
            dayfills=fills.loc[fills.trade_date.eq(row.date)].sort_values('execution_sequence')
            sold_today=set()
            for f in dayfills.itertuples():
                a=market.loc[(row.date,f.code)];o=orders.loc[f.order_id]
                notional=dec(f.price)*int(f.quantity)
                expected_fee=cents(max(Decimal(5),notional*Decimal('.0003')))+cents(notional*Decimal('.00001'))
                if f.side=='SELL': expected_fee+=cents(notional*Decimal('.0005'))
                assert f.fee_cents==expected_fee,(p,w,c,'fee',f)
                fee_sum+=expected_fee
                slip=Decimal('.001') if c=='base' else Decimal('.002')
                direction=1 if f.side=='BUY' else -1
                px=(dec(a.execution_open)*(1+direction*slip)*100).to_integral_value(
                    rounding=ROUND_CEILING if direction==1 else ROUND_FLOOR)/100
                assert dec(f.price)==px,(p,w,c,'fill_price')
                if f.side=='BUY':
                    assert f.code not in positions and f.code not in sold_today and len(positions)<3
                    assert f.quantity>0 and f.quantity%100==0
                    assert o.signal_date==dates[n-1] and n<len(dates)-11
                    prior=market.loc[(o.signal_date,f.code)]
                    assert prior.valuation_close>=5 and a.execution_open>=5
                    assert prior.sector_id not in ('','UNKNOWN','801180.SI') and pd.notna(prior.sector_id)
                    assert a.sector_id not in ('','UNKNOWN','801180.SI') and pd.notna(a.sector_id)
                    scale=Decimal('.5') if prior.market_state=='RANGE' else Decimal(1)
                    target=int(Decimal(previous_nav//3)*scale)
                    if p=='BASE_DEF': target=min(target,int(Decimal(previous_nav//3)*scale*min(Decimal(1),Decimal('.015')/dec(prior.Q02))))
                    amount=cents(notional)+expected_fee
                    assert amount<=min(target,cash,cents(dec(prior.Q03)*Decimal('.01')))
                    if p.startswith('S'):
                        signal=replay.loc[(o.signal_date,f.code)]
                        assert bool(signal[p])
                    cash-=amount;positions[f.code]=int(f.quantity);cost[f.code]=amount;entry[f.code]=row.date
                else:
                    assert f.code in positions and row.date>entry[f.code]
                    assert f.quantity==positions[f.code], 'base rule never deliberately partially exits'
                    cash+=cents(notional)-expected_fee
                    ended.append((f.code,entry[f.code],row.date,cents(notional)-expected_fee-cost[f.code]))
                    del positions[f.code];del cost[f.code];del entry[f.code];sold_today.add(f.code)
                assert cash>=0 and len(positions)<=3
            value=0
            for code,quantity in positions.items():
                price=market.loc[(row.date,code)].valuation_close
                if pd.notna(price) and price>0: last_marks[code]=dec(price)
                assert code in last_marks, 'unavailable valuation must be explicitly audited'
                value+=cents(last_marks[code]*quantity)
            assert row.cash_cents==cash and row.position_value_cents==value
            assert row.nav_cents==cash+value and row.position_count==len(positions)
            previous_nav=cash+value
        observed=[(r.code,r.entry_date,r.closed_at,int(r.realized_pnl_cents)) for r in trades.itertuples()]
        assert sorted(ended)==sorted(observed),(p,w,c,'episode_pnl')
        assert m['closed_episode_count']==len(ended)
        wins=sum(x[3]>0 for x in ended)
        assert m['wins']==wins and m['win_rate']==(wins/len(ended) if ended else None)
        assert abs(m['net_return']-(previous_nav/100_000_000-1))<1e-12
        assert m['open_position_count']==len(positions)
        assert abs(m['fees_total']-fee_sum/100)<1e-8
        if not positions: assert cash-100_000_000==sum(x[3] for x in ended)
        fills_count+=len(fills);trades_count+=len(trades)
        if k%36==0: print(json.dumps(dict(event='independent_account_audit',accounts=k)),flush=True)
    result=dict(status='PASS',accounts=252,fills=fills_count,episodes=trades_count,
        verified=['artifact_hashes','independent_fee_rounding','raw_open_tick_slippage','cash_and_NAV',
            'T_PLUS_1','slot_cash_ADV_budget','raw_5_yuan_and_sector_bans','next_open_and_tail',
            'episode_PNL','aggregate_wins','flat_reconciliation'],
        corporate_actions='ZERO_IN_THIS_FROZEN_SOURCE_SEPARATE_REGRESSIONS_COVER_ACTIONS')
    (root/'independent_account_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('output',type=Path);a=p.parse_args();audit(a.output)
