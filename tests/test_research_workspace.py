"""Offline evidence, causal replay and actual Streamlit component integration."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from analytics.activation_signals import gold_features, eur_features
from dashboard import replay
from dashboard.research_model import (diagnostics, load_bars, local_path, monitor_health,
                                      price_charts, saved_decision)
from test_gold_strategy import bars as gold_bars
from test_candidate_signals import setup as eur_bars

APP = Path(__file__).resolve().parents[1]/'dashboard/research_app.py'


def save_input(root, symbol, frame):
    folder = root/'logs/inputs'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder/f'{symbol}.csv'
    path.write_text(frame.to_csv(float_format='%.17g', date_format='%Y-%m-%dT%H:%M:%SZ', lineterminator='\n'), encoding='utf-8')
    step = pd.Timedelta(minutes=5) if symbol == 'XAUUSDm' else pd.Timedelta(hours=1)
    return dict(symbol=symbol, input_file=str(path), context_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                decision_time_utc=(frame.index[-1]+step).isoformat(), entry_timing='user_activation_v1', status='WAIT', reason='Test saved decision')


@pytest.mark.parametrize('symbol', ['XAUUSDm','EURUSDm'])
@pytest.mark.parametrize('side', ['buy','sell'])
def test_diagnostic_uses_exact_activation_kernel_and_closes_only(symbol, side):
    frame = gold_bars(side) if symbol == 'XAUUSDm' else eur_bars('trend_reclaim', side)
    result = diagnostics(frame, symbol)
    expected = gold_features(frame) if symbol == 'XAUUSDm' else eur_features(frame)
    assert result['features'] == expected
    assert result['side'] == side
    assert result['gates'][-1]['BUY'] == 'Not evaluated'
    assert result['gates'][-1]['SELL'] == 'Not evaluated'
    assert result['gates'][-2][side.upper()] == 'Passed'
    entry, trend = price_charts(result)
    for figure in (entry, trend):
        assert all(pd.Timestamp(x) < pd.Timestamp(result['decision_time_utc']) for trace in figure.data for x in trace.x)


def test_saved_decision_rejects_corrupt_input_wrong_time_and_path_escape(tmp_path):
    frame = gold_bars()
    item = save_input(tmp_path,'XAUUSDm',frame)
    assert saved_decision(tmp_path,item,'XAUUSDm')['side']=='buy'
    with pytest.raises(ValueError,match='end at'):
        saved_decision(tmp_path,dict(item,decision_time_utc='2026-11-13T10:05:00Z'),'XAUUSDm')
    with pytest.raises(ValueError,match='inside'):
        local_path(tmp_path,str(tmp_path.parent/'outside.csv'))
    Path(item['input_file']).write_text('corrupted',encoding='utf-8')
    with pytest.raises(ValueError,match='checksum'):
        saved_decision(tmp_path,item,'XAUUSDm')


def test_missing_warmup_and_recent_gap_are_not_presented_as_ready():
    assert diagnostics(gold_bars().iloc[-50:],'XAUUSDm')['side'] is None
    frame=gold_bars().drop(gold_bars().index[-2])
    assert diagnostics(frame,'XAUUSDm')['gates'][0]['BUY']=='Failed'


@pytest.mark.parametrize('age,alive,status,expected', [(0,True,'running','RUNNING'),(46,True,'running','STALE'),
    (-10,True,'running','STALE'),(0,False,'running','STOPPED'),(0,True,'stopped','STOPPED')])
def test_health_requires_fresh_heartbeat_and_live_process(age,alive,status,expected):
    now=pd.Timestamp('2026-11-16T10:00Z')
    item=dict(heartbeat_utc=(now-pd.Timedelta(seconds=age)).isoformat(),status=status,deadline_utc='2026-11-16T14:00Z')
    assert monitor_health(item,now=now,alive=alive)['state']==expected


def test_expired_authority_and_missing_heartbeat():
    assert monitor_health({},now=pd.Timestamp('2026-11-16T10:00Z'),alive=True)['state']=='UNKNOWN'
    item=dict(heartbeat_utc='2026-11-16T14:00Z',deadline_utc='2026-11-16T14:00Z',status='running')
    assert monitor_health(item,now=pd.Timestamp('2026-11-16T14:00Z'),alive=True)['state']=='EXPIRED'


def test_replay_screening_ignores_future_mutations_and_clock_filter():
    frame=gold_bars()
    # Move a valid setup outside the old London trading window.
    frame.index=frame.index-pd.Timedelta(hours=10)
    extra=frame.iloc[-1:].copy()
    extra.index+=pd.Timedelta(minutes=5)
    full=pd.concat([frame,extra])
    start=extra.index[0]
    signals,_=replay.prepare_current_signals(full,'XAUUSDm',start,start+pd.Timedelta(minutes=5))
    assert signals[1200]['side']=='buy'
    other=full.copy()
    other.loc[extra.index,['open','high','low','close']]+=10000
    changed,_=replay.prepare_current_signals(other,'XAUUSDm',start,start+pd.Timedelta(minutes=5))
    assert signals==changed


@pytest.mark.parametrize('symbol',['XAUUSDm','EURUSDm'])
@pytest.mark.parametrize('side',['buy','sell'])
def test_fast_replay_screen_matches_full_kernel_on_every_fixture_decision(symbol,side):
    frame=gold_bars(side) if symbol=='XAUUSDm' else eur_bars('trend_reclaim',side)
    interval=pd.Timedelta(minutes=5) if symbol=='XAUUSDm' else pd.Timedelta(hours=1)
    for _ in range(5):
        row=frame.iloc[-1:].copy()
        row.index+=interval
        row['open']=row['close']
        row['close']+=.005 if symbol=='XAUUSDm' else .00001
        row['high']=row[['open','close']].max(axis=1)+(.01 if symbol=='XAUUSDm' else .00002)
        row['low']=row[['open','close']].min(axis=1)-(.01 if symbol=='XAUUSDm' else .00002)
        frame=pd.concat([frame,row])
    warm=1200 if symbol=='XAUUSDm' else 3000
    signals,_=replay.prepare_current_signals(frame,symbol,frame.index[warm],frame.index[-1]+interval)
    expected={}
    for i in range(warm-1,len(frame)-1):
        window=frame.iloc[max(0,i-warm+1):i+1]
        features=gold_features(window) if symbol=='XAUUSDm' else eur_features(window)
        valid=features.get('side') if symbol=='XAUUSDm' else bool(features.get('trend_reclaim_long')) != bool(features.get('trend_reclaim_short'))
        if valid:
            expected[i+1]=features
    assert signals==expected


@pytest.mark.parametrize('symbol',['XAUUSDm','EURUSDm'])
def test_rescue_is_minimum_only_and_requires_strong_context_and_2r(monkeypatch,symbol):
    observed=[]
    def build(signal,bid,basis,settings,**kwargs):
        observed.append(settings)
        if settings.risk_percent==1:
            return None,'minimum_lot_risk'
        return dict(planned_risk=7.5,selected_reward_r=2,lots=settings.max_lots),''
    monkeypatch.setattr(replay,'gold_plan',build)
    monkeypatch.setattr(replay,'policy_plan',build)
    prefix='m15' if symbol=='XAUUSDm' else 'h4'
    signal={f'{prefix}_ema50':10.,f'{prefix}_ema200':8.,f'{prefix}_ema50_slope5':.5,f'{prefix}_atr14':1.,'volatility_ratio':1.,'spread':8}
    plan,reason=replay.model_plan(symbol,signal,10.,500.,replay.ModelSettings())
    assert not reason and plan['minimum_lot_rescue'] and plan['lots']==.01
    assert observed[-1].risk_percent==2 and observed[-1].max_lots==.01
    signal[f'{prefix}_ema50_slope5']=.1
    assert replay.model_plan(symbol,signal,10.,500.,replay.ModelSettings())[1]=='minimum_lot_requires_strong_context'


def test_minimum_lot_exception_rejects_one_r_target(monkeypatch):
    def build(signal,bid,basis,settings):
        return (None,'minimum_lot_risk') if settings.risk_percent==1 else (dict(selected_reward_r=1),'')
    monkeypatch.setattr(replay,'gold_plan',build)
    signal=dict(m15_ema50=10.,m15_ema200=8.,m15_ema50_slope5=.5,m15_atr14=1.,volatility_ratio=1.,spread=240)
    assert replay.model_plan('XAUUSDm',signal,10.,500.,replay.ModelSettings())[1]=='minimum_lot_requires_2r_room'


def tiny_bars(symbol,n=4):
    step='5min' if symbol=='XAUUSDm' else 'h'
    return pd.DataFrame(dict(open=[100.]*n,high=[100.1]*n,low=[99.9]*n,close=[100.]*n,
                             spread=[1.]*n,tick_volume=[100]*n,real_volume=[0]*n),
                        index=pd.date_range('2026-11-16T10:00Z',periods=n,freq=step))


def fake_plan(symbol,signal,bid,basis,settings):
    fee=.25 if settings.cost_model=='allowance_debit' else 0.
    return dict(direction=1,entry_price=100.,reference_entry=100.,stop_loss=90.,take_profit=110.,
                lots=.01,planned_risk=signal.get('risk',4.),risk_budget=5.,entry_fee=fee,cost_reserve=.25,
                slip=0.,contract_size=100.,margin_estimate=1.,selected_reward_r=2),''


def inject(monkeypatch,signals):
    monkeypatch.setattr(replay,'prepare_current_signals',lambda frame,s,*args: (signals.get(s,{}),{}))
    monkeypatch.setattr(replay,'model_plan',fake_plan)


def test_two_symbols_share_risk_cap_and_eur_has_priority(monkeypatch):
    inject(monkeypatch,{'EURUSDm':{0:{'risk':6.}},'XAUUSDm':{0:{'risk':6.}}})
    frames={s:tiny_bars(s) for s in ('EURUSDm','XAUUSDm')}
    run=replay.replay_portfolio(frames,'2026-11-16T10:00Z','2026-11-16T11:00Z')
    assert len(run['open_positions'])==1 and run['open_positions'][0]['symbol']=='EURUSDm'
    assert run['rejections'][0]['reason']=='combined_2_percent_risk_ceiling'


def test_one_each_without_duplicates_and_open_marks_are_not_wins(monkeypatch):
    inject(monkeypatch,{'EURUSDm':{0:{'risk':4.}},'XAUUSDm':{0:{'risk':4.},1:{'risk':4.}}})
    run=replay.replay_portfolio({s:tiny_bars(s) for s in ('EURUSDm','XAUUSDm')},'2026-11-16T10:00Z','2026-11-16T11:00Z')
    assert len(run['open_positions'])==2 and run['closed_trades']==0 and run['win_rate'] is None
    assert run['ending_balance']==499.5
    assert any(r['reason']=='instrument_position_open' for r in run['rejections'])


@pytest.mark.parametrize('cost,net', [('allowance_debit',9.75),('reserve_only',10.)])
def test_cash_reconciliation_and_ambiguous_stop_first(monkeypatch,cost,net):
    inject(monkeypatch,{'XAUUSDm':{0:{'risk':4.}}})
    frame=tiny_bars('XAUUSDm',1)
    frame['high']=115.
    run=replay.replay_portfolio({'XAUUSDm':frame},'2026-11-16T10:00Z','2026-11-16T10:05Z',replay.ModelSettings(cost_model=cost))
    assert run['trades'][0]['net_pnl']==net and run['ending_balance']==500+net
    frame['low']=85.
    other=replay.replay_portfolio({'XAUUSDm':frame},'2026-11-16T10:00Z','2026-11-16T10:05Z')
    assert other['trades'][0]['exit_reason']=='stop' and other['trades'][0]['ambiguous_bar']
    assert other['trades'][0]['net_pnl']==-10.25


def test_h1_future_exit_cannot_release_risk_for_earlier_gold_entry(monkeypatch):
    inject(monkeypatch,{'EURUSDm':{0:{'risk':6.}},'XAUUSDm':{1:{'risk':6.}}})
    eur=tiny_bars('EURUSDm',1)
    eur['high']=115.  # Exit known only at 11:00, not at the 10:05 Gold decision.
    run=replay.replay_portfolio({'EURUSDm':eur,'XAUUSDm':tiny_bars('XAUUSDm',3)},'2026-11-16T10:00Z','2026-11-16T11:00Z')
    assert run['rejections'][0]['reason']=='combined_2_percent_risk_ceiling'
    assert run['trades'][0]['exit_time_utc']=='2026-11-16T11:00:00+00:00'


def test_partial_end_bar_is_excluded(monkeypatch):
    inject(monkeypatch,{'EURUSDm':{0:{'risk':4.}}})
    with pytest.raises(ValueError,match='No complete bars'):
        replay.replay_portfolio({'EURUSDm':tiny_bars('EURUSDm',1)},'2026-11-16T10:00Z','2026-11-16T10:30Z')


def test_app_empty_workspace_and_pages_run_without_broker(tmp_path,monkeypatch):
    monkeypatch.setenv('PROJECT_QUANT_ROOT',str(tmp_path))
    app=AppTest.from_file(str(APP),default_timeout=30).run()
    app.radio(key='workspace').set_value('Live monitor').run()
    assert not app.exception and any('No readable monitor' in item.value for item in app.warning)
    for page in ['Historical replay','Actual trade journal','How to read this app']:
        app.radio(key='workspace').set_value(page).run()
        assert not app.exception


def test_app_explains_verified_snapshot_and_flags_stale_status(tmp_path,monkeypatch):
    monkeypatch.setenv('PROJECT_QUANT_ROOT',str(tmp_path))
    decision=save_input(tmp_path,'XAUUSDm',gold_bars())
    status=dict(status='running',heartbeat_utc=datetime.now(timezone.utc).isoformat(),pid=os.getpid(),
                deadline_utc='2099-01-01T00:00:00Z',open_position_count=0,orders_sent=0,
                instruments={'XAUUSDm':{'latest_decision':decision}})
    path=tmp_path/'logs/monitor/status.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(status))
    app=AppTest.from_file(str(APP),default_timeout=30).run()
    app.radio(key='workspace').set_value('Live monitor').run()
    assert not app.exception and not app.error
    assert app.metric[0].value=='RUNNING' and len(app.get('plotly_chart'))==2
    assert app.dataframe[0].value.iloc[-1]['BUY']=='Not evaluated'
    status['heartbeat_utc']='2020-01-01T00:00:00Z'
    path.write_text(json.dumps(status))
    app.run()
    assert app.metric[0].value=='STALE' and app.warning
