"""Frozen, resumable offline orchestration for the 252-account laboratory."""
from __future__ import annotations

import json
import multiprocessing
import shutil
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from core.backtest.high_win_metrics import (episode_statistics, nav_statistics, selection_summary,
    choose_primary, review_qualification)
from core.backtest.high_win_research import CONTROLS, replay_high_win
from core.backtest.stability_research_v2 import stability_windows
from core.backtest.strategy_search_v2 import SearchMarket, enrich_features
from core.factors.high_win_features import build_high_win_features, finalize_cross_section, FORMULA_VERSION
from core.pipeline.prism_compare_config import REPOSITORY_ROOT, file_sha256, implementation_identity, write_json
from core.pipeline.prism_compare_data import _verify_cache_file, experiment_lock
from core.pipeline.rank_rotation_research import load_rotation_config
from core.pipeline.stability_research import ENVIRONMENT_COLUMNS, _verified_source
from core.strategies.high_win_suite import policy_grid, family_conditions, common_mask, strict_mask
from core.technical_v2.contracts import ContractError, sha256_json

CONFIG_FILE_SHA = 'bdc8f0210a50a361b6c152a5187633c17712027456b7a0540ab58c708d3853f3'
DEFAULT_CONFIG = REPOSITORY_ROOT/'configs/high_win_suite_v1.json'
ALL_POLICIES = (*policy_grid(), *CONTROLS)
_MARKET = _ACTIONS = _CONFIG = None


def _read(path):
    return json.loads(Path(path).read_text())


def _progress(event, **values):
    print(json.dumps(dict(event=event,**values),ensure_ascii=False),flush=True)


def _records(root, exclude=()):
    return {str(p.relative_to(root)):dict(sha256=file_sha256(p),bytes=p.stat().st_size)
            for p in sorted(root.rglob('*')) if p.is_file() and p.name not in set(exclude)}


def _verify_records(root, records):
    for relative,record in records.items():
        p=root/relative
        if not p.is_file() or p.stat().st_size!=record['bytes'] or file_sha256(p)!=record['sha256']:
            raise ContractError('artifact bytes differ: '+str(p))


def load_suite(path=DEFAULT_CONFIG):
    if file_sha256(path)!=CONFIG_FILE_SHA:
        raise ContractError('configuration differs from the implemented v1 rules; revise protocol before running')
    return _read(path)


def _previous_index(root):
    old_artifacts=root.parents[3]/'artifacts'
    records=[]
    if old_artifacts.exists():
        for category in sorted(old_artifacts.iterdir()):
            if not category.is_dir(): continue
            for run in sorted(category.iterdir()):
                if not run.is_dir(): continue
                for name in ('REPORT.md','run_manifest.json','selection_freeze.json','summary.json'):
                    p=run/name
                    if p.is_file(): records.append(dict(path=str(p),bytes=p.stat().st_size,sha256=file_sha256(p)))
    return dict(history_status='REUSED_HISTORY_AND_REUSED_HOLDOUT',records=records,
        current_new_candidates=16,previous_searches='INDEXED_ARTIFACTS_NOT_INDEPENDENT_EXPERIMENTS',
        prior_attempt_total_status='UNKNOWN_NOT_FABRICATED')


def prepare(experiment_root, output_root, config_path=DEFAULT_CONFIG, *, smoke=False):
    root,output=Path(experiment_root).resolve(),Path(output_root).resolve()
    if root==output or root in output.parents or output in root.parents:
        raise ContractError('output must be isolated from frozen inputs')
    suite=load_suite(config_path);config=load_rotation_config()
    manifest,features=_verified_source(root,config)
    for record in [*features['context'].values(),*features['replay_chunks']]: _verify_cache_file(record)
    identity=implementation_identity()
    binding=dict(dataset_sha256=file_sha256(root/'rank_dataset_manifest.json'),
        snapshot_sha256=manifest['snapshot']['snapshot_sha256'],
        feature_identity=features['feature_identity'],protocol_sha256=sha256_json(suite),
        implementation_sha256=identity['implementation_hash'],smoke=smoke)
    prepared=output/'prepared.json'
    if prepared.exists():
        old=_read(prepared)
        if old['binding']!=binding: raise ContractError('prepared output binding changed; preserve and use a new run')
        _verify_records(output,old['files'])
        _progress('prepare_reused',output=str(output))
        return old
    if (output/'protocol_frozen.json').exists():
        if _read(output/'protocol_frozen.json')['binding']!=binding:
            raise ContractError('partial preparation has a different binding')
    output.mkdir(parents=True,exist_ok=True)
    windows=stability_windows(features['sessions'],manifest['windows']['test'])
    windows['review']=windows.pop('holdout_1')+windows.pop('holdout_2')
    dates=features['sessions']
    if smoke:
        dates=dates[:160]
        windows={'smoke':dates[-63:]}
    scope=sorted(set(manifest['scope_flags'])|{'REUSED_HISTORY','REUSED_HOLDOUT','EVIDENCE_LIMITED'})
    protocol=dict(binding=binding,suite=suite,windows=windows,base_config=config,scope_flags=scope,
        native_controls={k:dict(native_policy_id=p.policy_id,**asdict(p)) for k,p in CONTROLS.items()},
        policies=[asdict(p) for p in policy_grid()],source_identity=identity,
        formula_version=FORMULA_VERSION,created_at=datetime.now(timezone.utc).isoformat(),
        formal_account_count=252,smoke_only=smoke)
    if not (output/'protocol_frozen.json').exists(): write_json(output/'protocol_frozen.json',protocol,immutable=True)
    else: protocol=_read(output/'protocol_frozen.json')
    write_json(output/'data_binding.json',dict(binding=binding,input_root=str(root),scope_flags=scope,
        sessions=len(features['sessions']),roster_count=manifest['roster_count'],
        input_files={name:file_sha256(root/name) for name in ('rank_dataset_manifest.json','feature_manifest.json',
            'roster.parquet','corporate_actions.parquet','data_audit.json')},context=features['context']))
    write_json(output/'previous_runs_index.json',_previous_index(root))
    audit=REPOSITORY_ROOT/'docs/high_win_strategy_lab/SOURCE_AUDIT.md'
    shutil.copyfile(audit,output/'SOURCE_AUDIT.md')
    market_context=pd.read_parquet(features['context']['market']['path'])
    sectors=pd.read_parquet(features['context']['sectors']['path'])
    names=pd.read_parquet(root/'roster.parquet')[['code','name']]
    stock_filter=set(sorted(names.code)[:30]) if smoke else None
    columns=list(dict.fromkeys([*ENVIRONMENT_COLUMNS,'comparison_high','comparison_low','amount_cny','F04','F05','Q02']))
    # Key binds data/formulas/dates/sector construction, never workspace path alone.
    cache_key=sha256_json(dict(binding=binding,dates=[dates[0],dates[-1]],sector_scope='SW_L1_AS_OF_MEMBERSHIP'))
    cache=output/'derived_cache'/cache_key[:20];cache.mkdir(parents=True,exist_ok=True)
    chunks=[]
    for i,record in enumerate(features['feature_chunks']):
        target=cache/f'features_{i:03d}.parquet'
        marker=cache/f'features_{i:03d}.json'
        if marker.exists() and target.exists() and file_sha256(target)==_read(marker)['sha256']:
            frame=pd.read_parquet(target)
        else:
            raw=pd.read_parquet(record['path'],columns=columns,filters=[('date','<=',dates[-1])])
            if stock_filter is not None: raw=raw.loc[raw.code.isin(stock_filter)]
            if raw.empty: continue
            raw=raw.merge(names,on='code',how='left',validate='many_to_one')
            raw=enrich_features(raw)
            for name in ('comparison_open','comparison_close'):
                raw['native_'+name]=raw[name]
            frame=build_high_win_features(raw,market_context,sectors,dates,dates[-1])
            frame.to_parquet(target,index=False)
            write_json(marker,dict(sha256=file_sha256(target),cache_key=cache_key))
        chunks.append(frame)
        _progress('feature_chunk',completed=i+1,total=len(features['feature_chunks']),rows=len(frame),smoke=smoke)
    panel=finalize_cross_section(pd.concat(chunks,ignore_index=True));del chunks
    signal_chunks=[]
    for _,group in panel.groupby('code',sort=True):
        f=group.sort_values('date').reset_index(drop=True)
        raw=family_conditions(f);common=common_mask(f);strict=strict_mask(f)
        for p in policy_grid():
            raw[p.policy_id]=(common&raw[p.family]&(strict if p.variant=='STRICT' else True)).fillna(False)
        signal_chunks.append(raw.drop(columns=[f'S{i:02d}' for i in range(1,9)]))
    signals=pd.concat(signal_chunks,ignore_index=True);del signal_chunks
    panel=panel.merge(signals,on=['code','date'],validate='one_to_one')
    coverage=dict(rows=len(panel),codes=int(panel.code.nunique()),
        historical_st_unknown_ratio=float(panel.is_risk_warning.isna().mean()),
        missing_real_bar_ratio=float(panel.real_bar.ne(True).mean()),
        unknown_sector_ratio=float((panel.sector_id.isna()|panel.sector_id.isin(['','UNKNOWN'])).mean()),
        incomplete_features_ratio=float(panel.high_win_rank_score.isna().mean()),
        candidate_signal_rows={p.policy_id:int(panel[p.policy_id].sum()) for p in policy_grid()},
        missing_feature_reasons=panel.high_win_feature_reason.value_counts().to_dict())
    write_json(output/'feature_coverage.json',coverage)
    # The native controls retain their own input/feature completeness semantics.
    # New OHLC validation affects new signals only, never the control masks.
    for name in ('comparison_open','comparison_close'):
        panel[name]=panel['native_'+name]
    keep=list(dict.fromkeys([*columns,'name','ret1','ret3','close_over_ma20','high_win_rank_score',
        *[p.policy_id for p in policy_grid()],*[f'S{i:02d}_anchor' for i in range(1,9)]]))
    replay=panel.loc[panel.date.ge(min(v[0] for v in windows.values())),keep]
    replay_path=cache/'replay.parquet';replay.to_parquet(replay_path,index=False)
    # The full derived feature cache remains local; release includes results only.
    result=dict(status='PREPARED',binding=binding,cache_key=cache_key,replay_path=str(replay_path),
        input_root=str(root),windows=windows,scope_flags=scope,base_config=config,
        files=_records(output,exclude=('prepared.json','.compare.lock')))
    write_json(prepared,result,immutable=True)
    _progress('prepare_complete',rows=len(replay),codes=coverage['codes'],smoke=smoke)
    return result


def _init_worker(market,actions,config):
    global _MARKET,_ACTIONS,_CONFIG
    _MARKET,_ACTIONS,_CONFIG=market,actions,config


def _augment_cell(output, result, sessions):
    trades=pd.read_csv(output/'closed_trades.csv.gz',dtype={'entry_date':str,'closed_at':str,'code':str})
    nav=pd.read_csv(output/'daily_nav.csv.gz',dtype={'date':str})
    native=result['metrics']
    metrics={**native,**episode_statistics(trades),**nav_statistics(nav,round(native['initial_nav']*100))}
    metrics['classification_accuracy']=None
    metrics['classification_accuracy_status']='NOT_A_RETURN_CLASSIFICATION_EXPERIMENT'
    metrics['economic_episode_definition']='ZERO_POSITION_TO_ZERO_POSITION_INCLUDING_CORPORATE_ENTITLEMENTS'
    if len(sessions)==132:
        segments=[]
        for start,end in ((0,66),(66,132)):
            initial=round(native['initial_nav']*100) if start==0 else nav.iloc[start-1].nav_cents
            segment=nav.iloc[start:end]
            closed=trades.loc[trades.closed_at.ge(sessions[start])&trades.closed_at.le(sessions[end-1])]
            segments.append(dict(start_date=sessions[start],end_date=sessions[end-1],
                **nav_statistics(segment,initial),**episode_statistics(closed),
                trade_attribution='WHOLE_EPISODE_ATTRIBUTED_TO_EXIT_SEGMENT',
                account_reset=False,end_position_count=int(segment.iloc[-1].position_count)))
        metrics['continuous_segments']=segments
    (output/'metrics.json').rename(output/'native_metrics.json')
    write_json(output/'metrics.json',metrics,immutable=True)
    trades['net_episode_return']=trades.realized_pnl_cents/trades.entry_cost_cents
    trades.to_csv(output/'trades.csv',index=False)
    # Existing gzip CSV names are retained as a documented interface adaptation.
    return metrics


def _run_cell(job,binding):
    output=Path(job['output_dir']);receipt=output/'cell_complete.json'
    if receipt.exists():
        old=_read(receipt)
        if old['binding']==binding:
            try:
                _verify_records(output,old['files'])
                return dict(metrics=_read(output/'metrics.json'),reused=True,key=old['key'])
            except ContractError:
                pass
    if output.exists():
        # Keep failed/tampered attempts intact; only this fixed cell is rerun.
        suffix=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        failure=output.parents[2]/'failed_attempts'/('-'.join(output.parts[-3:])+'-'+suffix)
        failure.parent.mkdir(parents=True,exist_ok=True)
        shutil.move(str(output),str(failure))
    try:
        result=replay_high_win(_MARKET,config=_CONFIG,corporate_actions=_ACTIONS,**job)
        metrics=_augment_cell(output,result,job['sessions'])
        key=f"{metrics['strategy_id']}/{metrics['split']}/{metrics['cost_scenario']}"
        write_json(receipt,dict(status='COMPLETE',binding=binding,key=key,
            files=_records(output,exclude=('cell_complete.json',))),immutable=True)
        return dict(metrics=metrics,reused=False,key=key)
    except Exception as exc:
        output.mkdir(parents=True,exist_ok=True)
        write_json(output/'failure.json',dict(error=repr(exc),traceback=traceback.format_exc(),binding=binding))
        return dict(failed=True,output=str(output),error=repr(exc))


def run_jobs(market,actions,config,jobs,binding,workers):
    if workers not in (1,4): raise ContractError('workers must be 1 or 4')
    _init_worker(market,actions,config)
    results=[]
    if workers==1:
        for job in jobs:
            results.append(_run_cell(job,binding))
            _progress('account_complete',completed=len(results),total=len(jobs),**{k:v for k,v in results[-1].items() if k!='metrics'})
    else:
        with ProcessPoolExecutor(max_workers=4,mp_context=multiprocessing.get_context('spawn'),
                initializer=_init_worker,initargs=(market,actions,config)) as pool:
            futures=[pool.submit(_run_cell,job,binding) for job in jobs]
            for future in as_completed(futures):
                result=future.result();results.append(result)
                _progress('account_complete',completed=len(results),total=len(jobs),**{k:v for k,v in result.items() if k!='metrics'})
    return results


def _load_prepared(output):
    prepared=_read(output/'prepared.json')
    if prepared['binding']['implementation_sha256']!=implementation_identity()['implementation_hash']:
        raise ContractError('implementation changed after preparation; use a new frozen run')
    _verify_records(output,prepared['files'])
    return prepared


def run_phase(output,phase,workers):
    output=Path(output).resolve();prepared=_load_prepared(output)
    if phase=='review':
        frozen=_read(output/'selection_freeze.json')
        if frozen['selection_cells_sha256']!=file_sha256(output/'selection_cells.json') or frozen['binding']!=prepared['binding']:
            raise ContractError('selection freeze no longer matches early evidence')
        freeze_sha=file_sha256(output/'selection_freeze.json')
    windows={k:v for k,v in prepared['windows'].items() if k.startswith('selection')== (phase=='screen')}
    if prepared['binding']['smoke']: windows=prepared['windows']
    dates=sorted({d for ds in windows.values() for d in ds})
    panel=pd.read_parquet(prepared['replay_path'],filters=[('date','>=',dates[0]),('date','<=',dates[-1])])
    market=SearchMarket({str(d):f for d,f in panel.groupby('date',sort=True)})
    actions=pd.read_parquet(Path(prepared['input_root'])/'corporate_actions.parquet')
    jobs=[dict(policy=p,sessions=ds,split=split,cost_scenario=cost,
        output_dir=output/(p.policy_id if hasattr(p,'policy_id') else p)/split/cost,
        scope_flags=prepared['scope_flags']) for p in ALL_POLICIES for split,ds in windows.items() for cost in ('base','stress')]
    started=time.perf_counter()
    results=run_jobs(market,actions,prepared['base_config'],jobs,prepared['binding'],workers)
    failures=[r for r in results if r.get('failed')]
    cells=sorted([r['metrics'] for r in results if not r.get('failed')],key=lambda c:(c['strategy_id'],c['split'],c['cost_scenario']))
    filename='selection_cells.json' if phase=='screen' else 'review_cells.json'
    write_json(output/filename,cells)
    receipt=dict(phase=phase,success=len(cells),failed=len(failures),reused=sum(r.get('reused',False) for r in results),
        attempted=len(jobs),elapsed_seconds=time.perf_counter()-started,failures=failures)
    write_json(output/(phase+'_receipt.json'),receipt)
    if phase=='review' and file_sha256(output/'selection_freeze.json')!=freeze_sha:
        raise ContractError('review modified selection freeze')
    if failures: raise ContractError('account failures preserved; rerun same phase to resume: '+str(failures))
    return receipt


def freeze_selection(output):
    output=Path(output);prepared=_load_prepared(output)
    if prepared['binding']['smoke']: raise ContractError('smoke results cannot select a candidate')
    cells=_read(output/'selection_cells.json')
    if len(cells)!=216: raise ContractError('freeze requires all 216 early accounts')
    summaries=[selection_summary(p,[c for c in cells if c['strategy_id']==p]) for p in sorted({c['strategy_id'] for c in cells})]
    selection=choose_primary(summaries)
    frozen=dict(binding=prepared['binding'],selection=selection,summaries=summaries,
        candidate_count=16,total_policies=18,early_account_attempts=216,
        selection_cells_sha256=file_sha256(output/'selection_cells.json'),
        history_status='REUSED_HISTORY_REUSED_HOLDOUT_NOT_NEW_OUT_OF_SAMPLE')
    path=output/'selection_freeze.json'
    if path.exists():
        if _read(path)!=frozen: raise ContractError('cannot replace an existing selection freeze')
    else: write_json(path,frozen,immutable=True)
    _progress('selection_frozen',**selection)
    return frozen


def run_lab(stage,experiment_root,output_root,config_path=DEFAULT_CONFIG,workers=4,smoke=False):
    output=Path(output_root).resolve()
    with experiment_lock(output):
        if stage in ('prepare','all'): prepare(experiment_root,output,config_path,smoke=smoke)
        if stage in ('screen','all'): run_phase(output,'screen',workers)
        if smoke: return dict(status='SMOKE_COMPLETE',output=str(output),not_for_selection=True)
        if stage in ('freeze','all'): freeze_selection(output)
        if stage in ('review','all'): run_phase(output,'review',workers)
        if stage in ('report','all'):
            from core.pipeline.high_win_reporting import generate_report
            return generate_report(output)
    return dict(status='STAGE_COMPLETE',stage=stage,output=str(output))
