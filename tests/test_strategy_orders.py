from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace as Record
from unittest.mock import Mock

import pytest

from engine import strategy_orders as module
from engine.protection import RiskProtection
from engine.strategy_orders import StrategyIntent, StrategyPolicy, build_strategy_protection, evaluate_strategy_levels, risk_budget


@pytest.fixture
def sizing(monkeypatch):
    now=datetime(2026,11,13,8,0,30,tzinfo=timezone.utc)
    monkeypatch.setattr(module,'utc_now',lambda:now)
    def profit(side,symbol,volume,entry,exit_price):
        amount=(Decimal(str(exit_price))-Decimal(str(entry)))*Decimal(str(volume))*100000
        if side==1: amount=-amount
        return float(amount.quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
    api=Record(ORDER_TYPE_BUY=0,ORDER_TYPE_SELL=1,order_calc_profit=Mock(side_effect=profit))
    info=Record(name='EURUSDm',currency_base='EUR',currency_profit='USD',order_mode=127,chart_mode=0,
                trade_tick_size=.00001,point=.00001,volume_min=.01,volume_max=200,volume_step=.01,
                trade_stops_level=0)
    return Record(api=api,info=info,quote={'bid':1.1,'ask':1.10008},
                  funds={'balance':499.75,'equity':499.75,'currency':'USD'},
                  intent=StrategyIntent('EURUSDm','buy',1.09808,.001,'2026-11-13T08:00:00+00:00','a'*64,'pullback'),
                  policy=StrategyPolicy(),now=now)


def build(sizing,**changes):
    args=dict(api=sizing.api,info=sizing.info,quote=sizing.quote,funds=sizing.funds,intent=sizing.intent,policy=sizing.policy)
    args.update(changes)
    return build_strategy_protection(**args)


def test_one_percent_defaults_and_budget_is_maximum_not_minimum(sizing):
    assert StrategyPolicy().risk_percent == RiskProtection().risk_percent == 1
    plan=build(sizing)
    assert plan['raw_percent_budget']==4.9975
    assert plan['risk_budget']==4.99
    assert plan['volume_lots']==.02
    assert plan['estimated_loss_at_sl']==4
    assert plan['planned_loss_with_reserves']==pytest.approx(4.33)
    assert plan['planned_loss_with_reserves']<=4.99
    assert plan['reward_to_risk']>=2
    assert plan['stop_loss']==sizing.intent.stop_loss


def test_uses_lower_balance_or_equity(sizing):
    sizing.funds['equity']=400
    assert build(sizing)['risk_budget']==4


def test_wider_strategy_stop_reduces_lots_without_changing_stop(sizing):
    near=build(sizing)
    far=build(sizing,intent=replace(sizing.intent,stop_loss=1.09608))
    assert near['volume_lots']==.02 and far['volume_lots']==.01
    assert far['stop_loss']==1.09608


def test_minimum_lot_over_budget_is_rejected(sizing):
    with pytest.raises(ValueError,match='Minimum lot'):
        build(sizing,intent=replace(sizing.intent,stop_loss=1.09008))


def test_sell_uses_bid_entry_ask_stop_and_downward_target(sizing):
    plan=build(sizing,intent=replace(sizing.intent,side='sell',stop_loss=1.102))
    assert plan['reference_entry']==1.1
    assert plan['stop_loss']==1.102
    assert plan['take_profit']<1.1
    assert plan['volume_lots']==.02
    assert plan['reward_to_risk']>=2


def test_cap_and_volume_step_always_round_down(sizing):
    plan=build(sizing,policy=replace(sizing.policy,max_lots=.01))
    assert plan['volume_lots']==.01
    sizing.info.volume_step=.02
    sizing.info.volume_min=.02
    assert build(sizing)['volume_lots']==.02


@pytest.mark.parametrize('change',[{'risk_percent':1.01},{'max_lots':.11},{'risk_percent':True},
                                  {'reward_to_risk':3},{'slippage_reserve_points_per_side':0},
                                  {'additional_cost_reserve_fraction':float('nan')}])
def test_policy_cannot_raise_authorized_scope(change):
    with pytest.raises(ValueError): StrategyPolicy(**change)


@pytest.mark.parametrize('kind',['currency','tick','missing_tp','pnl','stop_grid','min_distance','atr','spread'])
def test_invalid_preflight_conditions_fail_closed(sizing,kind):
    if kind=='currency': sizing.funds['currency']='EUR'
    if kind=='tick': sizing.info.trade_tick_size=0
    if kind=='missing_tp': sizing.info.order_mode &= ~32
    if kind=='pnl': sizing.api.order_calc_profit.side_effect=lambda *args:None
    if kind=='stop_grid': sizing.intent=replace(sizing.intent,stop_loss=1.098081)
    if kind=='min_distance': sizing.info.trade_stops_level=300
    if kind=='atr': sizing.intent=replace(sizing.intent,atr=.003)
    if kind=='spread': sizing.quote['ask']=1.1005
    with pytest.raises(ValueError): build(sizing)


@pytest.mark.parametrize('offset',[-1,301])
def test_stale_or_future_signal_is_not_executable(sizing,monkeypatch,offset):
    decision=datetime.fromisoformat(sizing.intent.decision_time_utc)
    monkeypatch.setattr(module,'utc_now',lambda:decision+timedelta(seconds=offset))
    with pytest.raises(ValueError,match='five minutes'): build(sizing)


def test_session_is_enforced_even_for_fresh_signal(sizing,monkeypatch):
    when=datetime(2026,11,13,7,0,tzinfo=timezone.utc)
    monkeypatch.setattr(module,'utc_now',lambda:when)
    with pytest.raises(ValueError,match='entry session'):
        build(sizing,intent=replace(sizing.intent,decision_time_utc=when.isoformat()))


def test_fill_evaluation_does_not_reserve_entry_slippage_twice(sizing):
    plan=build(sizing)
    before=evaluate_strategy_levels(sizing.api,'EURUSDm',plan['volume_lots'],plan['reference_entry'],plan['stop_loss'],plan['take_profit'],plan)
    filled=evaluate_strategy_levels(sizing.api,'EURUSDm',plan['volume_lots'],plan['reference_entry']+.00001,plan['stop_loss'],plan['take_profit'],plan,at_fill=True)
    assert filled['planned_loss_with_reserves'] < before['planned_loss_with_reserves']
    assert filled['reward_to_risk']>=2


def test_broker_failure_never_falls_back_to_contract_arithmetic(sizing):
    sizing.api.order_calc_profit.side_effect=lambda *args:float('nan')
    with pytest.raises(ValueError,match='P&L'): build(sizing)


def test_signal_history_excludes_unfinished_hour_and_returns_wait_without_orders(sizing,monkeypatch,tmp_path):
    import numpy as np
    import pandas as pd
    timestamps=pd.date_range(end='2026-11-13T08:00:00Z',periods=3001,freq='h')
    frame=pd.DataFrame({'time':timestamps.as_unit('s').astype('int64'),'open':1.1,'high':1.101,
                        'low':1.099,'close':1.1,'spread':8,'tick_volume':100,'real_volume':0})
    api=sizing.api
    api.TIMEFRAME_H1=16385
    api.copy_rates_from_pos=Mock(return_value=frame.to_records(index=False))
    api.symbol_info=Mock(return_value=sizing.info)
    api.order_send=Mock()
    api.order_check=Mock()
    def features(data):
        assert data.index.max()==pd.Timestamp('2026-11-13T07:00:00Z')
        return pd.DataFrame({'decision_time':[pd.Timestamp('2026-11-13T08:00:00Z')],
                             'pullback_long':[False],'breakout_long':[False],
                             'pullback_short':[False],'breakout_short':[False],'session':[True]})
    monkeypatch.setattr(module,'signal_features',features)
    connector=Record(terminal_api=api,inspect=Mock(return_value={'quote':{'symbol':'EURUSDm'},'account':{'account_type':'DEMO'}}))
    intent,report=module.latest_strategy_intent(connector,tmp_path)
    assert intent is None and report['status']=='WAIT' and report['orders_sent']==0
    assert report['bars']==3000
    api.order_send.assert_not_called()
    api.order_check.assert_not_called()
