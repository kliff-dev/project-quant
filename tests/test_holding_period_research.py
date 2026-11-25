"""Offline checks for exit ordering, ambiguity and historical plan consistency."""

from datetime import datetime, timezone
import json
from types import SimpleNamespace as Record

import pandas as pd
import pytest

from engine import strategy_orders
from engine.strategy_orders import StrategyIntent, StrategyPolicy, build_strategy_protection
from scripts.research_holding_period import deadline_for, exit_trade, plan_entry, rollover_units, summarize


@pytest.fixture
def spec():
    return dict(slippage_points_each_side=2,additional_cost_allowance_usd_per_trade=.25,
                risk_budget_usd=4.99,max_lots=.1)


@pytest.mark.parametrize('direction',[1,-1])
@pytest.mark.parametrize('bid',[1.10493,1.1053,1.106])
def test_historical_plan_matches_production_reserved_risk_and_target(spec,monkeypatch,direction,bid):
    row=Record(pullback_long=direction==1,pullback_short=direction==-1,breakout_long=False,
               breakout_short=False,spread=8,atr14=.0015 if direction==1 else .0010796912623032777,low5=1.10458,high5=1.10726)
    plan=plan_entry(row,bid,spec)
    assert plan is not None
    class LinearEURUSD:
        ORDER_TYPE_BUY=0
        ORDER_TYPE_SELL=1
        def order_calc_profit(self,side,symbol,lots,entry,exit):
            return (1 if side==0 else -1)*(exit-entry)*100000*lots
    info=Record(name='EURUSDm',currency_base='EUR',currency_profit='USD',order_mode=48,
        trade_tick_size=.00001,point=.00001,trade_stops_level=0,volume_min=.01,volume_max=200.,volume_step=.01)
    clock=datetime(2026,11,13,11,tzinfo=timezone.utc)
    monkeypatch.setattr(strategy_orders,'utc_now',lambda:clock)
    intent=StrategyIntent('EURUSDm','buy' if direction==1 else 'sell',plan['stop'],row.atr14,clock.isoformat(),'0'*64,'pullback')
    actual=build_strategy_protection(LinearEURUSD(),info,dict(bid=bid,ask=bid+.00008),
        dict(balance=499.75,equity=499.75,currency='USD'),intent,StrategyPolicy())
    assert plan['lots']==actual['volume_lots']
    assert plan['target']==pytest.approx(actual['take_profit'],abs=1e-12)
    assert plan['planned_risk']==pytest.approx(actual['planned_loss_with_reserves'])


def frame(rows, start='2026-11-13T10:00:00Z',freq='h'):
    return pd.DataFrame(rows,index=pd.date_range(start,periods=len(rows),freq=freq))


def simple_plan(direction=1):
    return dict(direction=direction,stop=99. if direction==1 else 101.,target=102. if direction==1 else 98.,
        slip=.1,point=.01,size=1.,lots=1.,planned_risk=1.3,fee=.1,
        executed_entry=100.1 if direction==1 else 99.9)


@pytest.mark.parametrize('direction',[1,-1])
def test_ambiguous_candle_has_stop_first_and_target_first_bounds(direction):
    data=frame([dict(open=100.,high=103.,low=97.,close=100.,spread=0)])
    end=data.index[0]+pd.Timedelta(hours=1)
    stop=exit_trade(data,0,simple_plan(direction),end,'stop_first')
    target=exit_trade(data,0,simple_plan(direction),end,'target_first')
    assert stop['reason']=='stop' and stop['r_before_swap']<0 and stop['ambiguous_bar']
    assert target['reason']=='target' and target['r_before_swap']>0 and target['ambiguous_bar']


def test_gap_stop_uses_worse_open_even_when_time_cap_already_passed():
    data=frame([dict(open=97.,high=98.,low=96.,close=97.,spread=0)])
    result=exit_trade(data,0,simple_plan(),data.index[0]-pd.Timedelta(hours=1),'stop_first')
    assert result['reason']=='stop_gap' and result['exit_price']==pytest.approx(96.9)


def test_time_exit_cannot_look_at_later_high_in_its_candle():
    data=frame([dict(open=100.,high=100.5,low=99.5,close=100.,spread=0),
                dict(open=100.2,high=103.,low=100.,close=102.,spread=0)])
    result=exit_trade(data,0,simple_plan(),data.index[1],'stop_first')
    assert result['reason']=='time_exit' and result['exit_price']==pytest.approx(100.1)


def test_short_trigger_uses_ask_not_just_bid():
    data=frame([dict(open=100.,high=100.5,low=99.5,close=100.,spread=60)])
    result=exit_trade(data,0,simple_plan(-1),data.index[0]+pd.Timedelta(hours=1),'stop_first')
    assert result['reason']=='stop'  # Bid high 100.5 + .60 spread reaches 101 stop.


def test_weekend_time_cap_waits_for_first_available_open():
    data=pd.DataFrame([dict(open=100.,high=100.5,low=99.5,close=100.,spread=0),
                       dict(open=100.,high=100.5,low=99.5,close=100.,spread=0)],
        index=pd.DatetimeIndex(['2026-11-13T20:00:00Z','2026-11-16T00:00:00Z']))
    result=exit_trade(data,0,simple_plan(),pd.Timestamp('2026-11-14T20:00:00Z'),'stop_first')
    assert result['reason']=='time_exit' and result['holding_hours_upper_bound']==52


def test_eod_deadline_tracks_london_daylight_saving():
    assert deadline_for(pd.Timestamp('2026-07-01T10:00:00Z'),'same_day_16_london').hour==15
    assert deadline_for(pd.Timestamp('2026-11-13T10:00:00Z'),'same_day_16_london').hour==16


def test_rollover_units_handle_wednesday_triple_and_weekend():
    assert rollover_units(pd.Timestamp('2026-11-11T21:00:00Z'),pd.Timestamp('2026-11-11T23:00:00Z'))==3
    assert rollover_units(pd.Timestamp('2026-11-13T21:00:00Z'),pd.Timestamp('2026-11-16T23:00:00Z'))==2


def test_raising_swap_cost_can_turn_small_gain_into_loss():
    data=pd.DataFrame([dict(modeled_pnl_before_swap=.05,lots=.01,rollover_units=1,planned_risk=1.,
                            reason='time_exit',holding_hours_upper_bound=24.,ambiguous_bar=False)])
    assert summarize(data,0)['win_rate_percent']==100
    assert summarize(data,.1)['win_rate_percent']==0
    json.dumps(summarize(data,.1),allow_nan=False)


def test_risk_and_spread_failures_reject_entry(spec):
    row=Record(pullback_long=True,pullback_short=False,breakout_long=False,breakout_short=False,
               spread=25,atr14=.001,low5=1.10,high5=1.11)
    assert plan_entry(row,1.105,spec) is None
    row.spread=8
    row.atr14=.1
    assert plan_entry(row,1.105,spec) is None
