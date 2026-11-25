"""Offline tests of rolling signals, compounding, SL/TP, cashflows and selection."""

from dataclasses import replace
from datetime import datetime,timezone
from types import SimpleNamespace as Record

import numpy as np
import pandas as pd
import pytest

from analytics.account_research import ReplaySettings,choose_on_training,make_plan,prepare_signals,replay_account,window_signal
from analytics.setup_research import signal_features
from engine import strategy_orders
from engine.strategy_orders import StrategyIntent,StrategyPolicy,build_strategy_protection
from test_setup_research import bars


@pytest.mark.parametrize('length',[2500,2999,3000])
@pytest.mark.parametrize('side',['buy','sell'])
@pytest.mark.parametrize('timestamp_unit',['us','ns'])
def test_rolling_snapshot_matches_production_features(length,side,timestamp_unit):
    data=bars(length)
    data.index=pd.date_range(end='2026-11-13T10:00:00Z',periods=length,freq='h')
    data.index=data.index.as_unit(timestamp_unit)
    if side=='sell':
        original=data.copy()
        data['open']=2.3-original.open
        data['close']=2.3-original.close
        data['high']=2.3-original.low
        data['low']=2.3-original.high
    last=data.index[-1]
    price=data.high.iloc[-21:-1].max()+.001 if side=='buy' else data.low.iloc[-21:-1].min()-.001
    data.loc[last,'open']=data.close.iloc[-2]
    data.loc[last,'close']=price
    data.loc[last,'high']=max(data.open.iloc[-1],price)+.0001
    data.loc[last,'low']=min(data.open.iloc[-1],price)-.0001
    expected=signal_features(data).iloc[-1]
    actual=window_signal(data)
    assert actual
    for field in ['pullback_long','pullback_short','breakout_long','breakout_short']:
        assert actual[field]==bool(expected[field])
    assert actual['atr14']==pytest.approx(expected.atr14,rel=0,abs=1e-12)
    assert actual['h1_ema20']==pytest.approx(expected.ema20,abs=1e-14)
    assert actual['low5']==expected.low5 and actual['high5']==expected.high5
    assert actual['input_window_rows']==length


def test_signal_preparation_does_not_use_future_bars():
    data=bars(3040)
    data.index=pd.date_range(end='2026-11-13T18:00:00Z',periods=len(data),freq='h')
    # Force possible breakouts at London hours without changing earlier history.
    i=3030
    close=data.high.iloc[i-20:i].max()+.001
    data.iloc[i,data.columns.get_loc('close')]=close
    data.iloc[i,data.columns.get_loc('high')]=close+.0001
    first=data.index[3000]
    before=prepare_signals(data.iloc[:3033],first_entry=first)
    changed=data.copy()
    changed.iloc[3033:,changed.columns.get_indexer(['open','high','low','close'])]+=.1
    after=prepare_signals(changed,first_entry=first)
    assert before=={k:v for k,v in after.items() if k<3033}


def signal(side='buy'):
    return dict(pullback_long=side=='buy',pullback_short=side=='sell',breakout_long=False,breakout_short=False,
        atr14=.0015,low5=1.0999,high5=1.1001,spread=8)


def data(rows,start='2026-11-13T10:00:00Z'):
    return pd.DataFrame(rows,index=pd.date_range(start,periods=len(rows),freq='h'))


def quiet():
    return dict(open=1.1,high=1.1002,low=1.0998,close=1.1,spread=8)


@pytest.mark.parametrize('side',['buy','sell'])
def test_account_plan_matches_live_baseline_calculation(monkeypatch,side):
    raw=signal(side)
    plan,reason=make_plan(raw,1.1,500.,ReplaySettings())
    assert not reason and plan
    class Linear:
        ORDER_TYPE_BUY=0
        ORDER_TYPE_SELL=1
        def order_calc_profit(self,side,symbol,lots,entry,exit):
            # Native USD order_calc_profit was read-only verified to round
            # to two account-currency decimals (see saved precision evidence).
            return round((1 if side==0 else -1)*(exit-entry)*100000*lots,2)
    info=Record(name='EURUSDm',currency_base='EUR',currency_profit='USD',order_mode=48,
        trade_tick_size=.00001,point=.00001,trade_stops_level=0,volume_min=.01,volume_max=200.,volume_step=.01)
    clock=datetime(2026,11,13,11,tzinfo=timezone.utc)
    monkeypatch.setattr(strategy_orders,'utc_now',lambda:clock)
    intent=StrategyIntent('EURUSDm',side,plan['stop_loss'],raw['atr14'],clock.isoformat(),'0'*64,'pullback')
    expected=build_strategy_protection(Linear(),info,dict(bid=1.1,ask=1.10008),
        dict(balance=500.,equity=500.,currency='USD'),intent,StrategyPolicy())
    assert plan['lots']==expected['volume_lots']
    assert plan['take_profit']==pytest.approx(expected['take_profit'],abs=1e-12)
    assert plan['planned_risk']==pytest.approx(expected['planned_loss_with_reserves'])


def test_compounding_recalculates_budget_and_cashflows_reconcile():
    frame=data([dict(quiet(),high=1.11),dict(quiet(),high=1.11)])
    run=replay_account(frame,{0:signal(),1:signal()},frame.index[0],frame.index[-1]+pd.Timedelta(hours=1),ReplaySettings())
    assert len(run['trades'])==2
    a,b=run['trades']
    assert b['balance_before_entry']>a['balance_before_entry']
    assert b['risk_budget']>a['risk_budget']
    assert all(t['planned_risk']<=t['risk_budget']+1e-9 for t in run['trades'])
    assert run['summary']['final_balance']==pytest.approx(500+sum(t['net_pnl'] for t in run['trades']))


def test_open_position_is_marked_not_force_closed():
    frame=data([quiet(),dict(quiet(),close=1.1003,high=1.1004)])
    run=replay_account(frame,{0:signal()},frame.index[0],frame.index[-1]+pd.Timedelta(hours=1),ReplaySettings())
    assert run['summary']['completed_trades']==0 and run['summary']['open_positions']==1
    assert run['summary']['win_rate_percent'] is None
    assert run['open_position']['status']=='open_at_data_end'
    assert run['summary']['final_balance']==500-run['open_position']['entry_fee']
    assert run['summary']['final_equity']==pytest.approx(run['summary']['final_balance']+run['open_position']['unrealized_price_pnl'])


def test_one_open_position_blocks_later_entries():
    frame=data([quiet(),quiet(),dict(quiet(),high=1.11)])
    run=replay_account(frame,{0:signal(),1:signal(),2:signal()},frame.index[0],frame.index[-1]+pd.Timedelta(hours=1),ReplaySettings())
    assert len(run['trades'])==1 and run['summary']['inventory_blocked_signals']==2


def test_gap_loss_can_exceed_planned_one_percent():
    frame=data([quiet(),dict(quiet(),open=1.09,high=1.091,low=1.089,close=1.09)])
    run=replay_account(frame,{0:signal()},frame.index[0],frame.index[-1]+pd.Timedelta(hours=1),ReplaySettings())
    trade=run['trades'][0]
    assert trade['exit_reason']=='stop_gap' and trade['realized_r'] < -1
    assert trade['planned_risk']<=5 and trade['net_pnl'] < -5


def test_ambiguous_bar_bounds_change_outcome():
    frame=data([dict(quiet(),high=1.12,low=1.08)])
    a=replay_account(frame,{0:signal()},frame.index[0],frame.index[0]+pd.Timedelta(hours=1),ReplaySettings())
    b=replay_account(frame,{0:signal()},frame.index[0],frame.index[0]+pd.Timedelta(hours=1),ReplaySettings(ambiguity='target_first'))
    assert a['trades'][0]['net_pnl']<0<b['trades'][0]['net_pnl']
    assert a['summary']['ambiguous_exits']==1


def test_financing_is_booked_once_with_wednesday_triple():
    frame=data([quiet(),quiet(),quiet()],start='2026-11-11T21:00:00Z')
    settings=ReplaySettings(financing_per_001lot_unit=.1)
    run=replay_account(frame,{0:signal()},frame.index[0],frame.index[-1]+pd.Timedelta(hours=1),settings)
    opened=run['open_position']
    assert opened['rollover_units']==3
    assert opened['financing_cost']==pytest.approx(.1*opened['lots']/.01*3)
    assert run['summary']['final_balance']==pytest.approx(500-opened['entry_fee']-opened['financing_cost'])


def test_minimum_lot_is_not_forced_when_balance_cannot_afford_risk():
    plan,reason=make_plan(signal(),1.1,50,ReplaySettings())
    assert plan is None and reason=='minimum_lot_risk'


def test_reserve_is_distinct_from_actual_fee_in_separate_sensitivity():
    conservative,_=make_plan(signal(),1.1,500.,ReplaySettings())
    zero_fee,_=make_plan(signal(),1.1,500.,ReplaySettings(charge_cost_allowance=False))
    for key in ['lots','planned_risk','risk_budget','take_profit','stop_loss','cost_reserve']:
        assert conservative[key]==zero_fee[key]
    assert conservative['entry_fee']>0 and zero_fee['entry_fee']==0
    frame=data([quiet()])
    run=replay_account(frame,{0:signal()},frame.index[0],frame.index[0]+pd.Timedelta(hours=1),ReplaySettings(charge_cost_allowance=False))
    assert run['summary']['final_balance']==500
    assert run['summary']['total_modeled_entry_allowances']==0
    assert run['summary']['total_planning_cost_reserves']>0


def test_selection_uses_earlier_results_and_can_select_nothing():
    def result(name,r,equity,n=60,drawdown=5):
        return dict(summary=dict(config_id=name,mean_completed_r=r,initial_balance=500.,final_equity=equity,
                                 completed_trades=n,max_h1_close_equity_drawdown_percent=drawdown))
    assert choose_on_training([result('loser',-.01,490),result('too_few',.8,600,n=10)]) is None
    assert choose_on_training([result('a',.1,550),result('b',.2,560),result('c',.2,560,drawdown=4)])=='c'


@pytest.mark.parametrize('setting',[dict(initial_balance=0),dict(reward_r=float('nan')),dict(ambiguity='unknown'),dict(financing_per_001lot_unit=-1),dict(charge_cost_allowance='yes')])
def test_invalid_research_settings_are_rejected(setting):
    with pytest.raises(ValueError): ReplaySettings(**setting)
