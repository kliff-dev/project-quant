"""Offline native sizing, authority, portfolio and failure tests for the 2% exception."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
import json

import pytest

from engine import strategy_orders as orders, gold_orders, adaptive_orders
from engine.conditional_risk import MODE, favorable, validate_plan
from engine.entry_timing import ACTIVATION, metadata
from engine.demo_executor import DemoExecutor
from engine.demo_series import DemoSeriesAuthorization
from test_demo_executor import rig
from test_gold_execution import gold, candidate
from test_portfolio_guard import portfolio
from test_adaptive_orders import native, fixture_signal, synthetic_intent, add_obstacle


def configure(gold, monkeypatch, *, side='buy', distance=7.5, branch=2):
    intent = candidate(gold, monkeypatch, side)
    features = gold_orders.verified_context(intent, gold.rig.symbol)
    gold.policy = replace(gold.policy, risk_percent=2, risk_mode=MODE, entry_timing=ACTIVATION)
    reference = Decimal(str(gold.rig.tick.ask if side=='buy' else gold.rig.tick.bid))
    direction = 1 if side=='buy' else -1
    intent = replace(intent, stop_loss=float(reference-direction*Decimal(str(distance))),
        adaptive_context=dict(intent.adaptive_context, **metadata(ACTIVATION)))
    raw = orders._build_strategy_protection(gold.rig.api, gold.rig.symbol,
        {'bid':gold.rig.tick.bid, 'ask':gold.rig.tick.ask}, {'balance':500.,'equity':500.,'currency':'USD'},
        intent, gold.policy, target_reward_r=branch)
    price = Decimal(str(raw['take_profit']))+direction*Decimal('.4')-(Decimal('.24') if direction==-1 else 0)
    features['m15_swing_highs' if side=='buy' else 'm15_swing_lows'] = [dict(price=float(price),
        pivot_open_utc='2026-11-13T08:00:00Z', confirmed_at_utc='2026-11-13T08:45:00Z')]
    return intent, features


def build(gold, intent):
    return orders.build_strategy_protection(gold.rig.api, gold.rig.symbol,
        {'bid':gold.rig.tick.bid,'ask':gold.rig.tick.ask},
        {'balance':gold.rig.account.balance,'equity':gold.rig.account.equity,'currency':'USD'}, intent, gold.policy)


@pytest.mark.parametrize('side',['buy','sell'])
@pytest.mark.parametrize('distance,expected_risk',[(5.9,1.3),(7.5,1.62),(9.4,2.)])
def test_exception_uses_only_minimum_volume_and_natural_fractional_risk(gold,monkeypatch,side,distance,expected_risk):
    intent,_ = configure(gold,monkeypatch,side=side,distance=distance)
    result = build(gold,intent)
    assert result['volume_lots']==.01 and result['actual_planned_risk_percent']==pytest.approx(expected_risk)
    assert result['planned_loss_with_reserves']<=10 and result['risk_budget']==10
    assert result['conditional_risk_evidence']['minimum_lot_rescue'] is True
    assert result['stop_loss']==intent.stop_loss and result['selected_reward_r']==2
    validate_plan(result,gold.policy,info=gold.rig.symbol,replay=True)
    gold.rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('side',['buy','sell'])
def test_affordable_baseline_setup_never_sizes_toward_two_percent(gold,monkeypatch,side):
    intent,_=configure(gold,monkeypatch,side=side,distance=4.)
    result=build(gold,intent)
    assert result['risk_percent']==1 and result['risk_budget']==5
    assert result['volume_lots']==.01 and result['planned_loss_with_reserves']<5
    assert result['conditional_risk_evidence']['minimum_lot_rescue'] is False
    assert result['actual_planned_risk_percent']<1


def test_one_cent_above_ceiling_rejects_without_narrowing_stop(gold,monkeypatch):
    intent,_=configure(gold,monkeypatch,distance=9.4)
    intent=replace(intent,stop_loss=intent.stop_loss-.011)
    with pytest.raises(ValueError,match='2% budget'): build(gold,intent)
    gold.rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('key,value',[('m15_ema50_slope5',.1),('m15_ema50',3501.),
    ('volatility_ratio',.7499),('volatility_ratio',1.5001)])
def test_stronger_context_and_normal_volatility_are_mandatory(gold,monkeypatch,key,value):
    intent,features=configure(gold,monkeypatch)
    features[key]=value
    with pytest.raises(ValueError,match='strong trend and normal volatility'):build(gold,intent)
    gold.rig.api.order_send.assert_not_called()


def test_exception_rejects_one_r_target_even_when_entry_and_sizing_pass(gold,monkeypatch):
    intent,_=configure(gold,monkeypatch,branch=1)
    with pytest.raises(ValueError,match='at least 2R'):build(gold,intent)
    gold.rig.api.order_send.assert_not_called()


def test_equity_reduction_can_make_minimum_volume_unaffordable(gold,monkeypatch):
    intent,_=configure(gold,monkeypatch,distance=9.4)
    gold.rig.account.equity=499.
    with pytest.raises(ValueError,match='2% budget'):build(gold,intent)


@pytest.mark.parametrize('changes',[{'risk_percent':3},{'risk_percent':2.001},
    {'risk_mode':'fixed'}, {'entry_timing':'clock_session'}, {'target_mode':'fixed_2r'}])
def test_explicit_policy_cannot_expand_risk_scope(gold,changes):
    values=dict(target_mode='gold_intraday_v1',entry_timing=ACTIVATION,risk_mode=MODE,risk_percent=2)
    values.update(changes)
    with pytest.raises(ValueError):orders.StrategyPolicy(**values)


@pytest.mark.parametrize('side',['buy','sell'])
def test_same_exception_applies_to_native_eur_adaptive_planner(native,monkeypatch,side):
    signal=fixture_signal(side)
    monkeypatch.setattr(adaptive_orders,'_verified_context',lambda *args,**kwargs:signal)
    intent=synthetic_intent(signal,side)
    native.policy=replace(native.policy,target_mode='adaptive_pullback_v2',entry_timing=ACTIVATION,
                          risk_mode=MODE,risk_percent=2)
    reference=Decimal(str(native.quote['ask' if side=='buy' else 'bid']))
    intent=replace(intent,stop_loss=float(reference-(1 if side=='buy' else -1)*Decimal('.0075')),
                   adaptive_context=dict(intent.adaptive_context,**metadata(ACTIVATION)))
    add_obstacle(native,signal,intent,2)
    result=orders.build_strategy_protection(native.api,native.info,native.quote,native.funds,intent,native.policy)
    assert result['volume_lots']==.01 and 1<result['actual_planned_risk_percent']<2
    assert result['selected_reward_r']==2 and result['stop_loss']==intent.stop_loss
    validate_plan(result,native.policy,info=native.info,replay=True)
    native.api.order_send.assert_not_called()


@pytest.mark.parametrize('change',['flag','volume','source','budget','minimum','actual_percent'])
def test_tampered_conditional_plan_is_rejected(gold,monkeypatch,change):
    intent,_=configure(gold,monkeypatch)
    plan=deepcopy(build(gold,intent));e=plan['conditional_risk_evidence']
    if change=='flag':e['minimum_lot_rescue']=False
    if change=='volume':plan['volume_lots']=.02
    if change=='source':e['risk_spec_sha256']='0'*64
    if change=='budget':plan['risk_budget']=11
    if change=='minimum':e['broker_minimum_lots']=.02
    if change=='actual_percent':plan['actual_planned_risk_percent']=1.
    with pytest.raises(ValueError):validate_plan(plan,gold.policy,info=gold.rig.symbol,replay=True)


def authorize(gold,intent,session='conditional-gold'):
    path=gold.rig.directory/'conditional_policy.json'
    path.write_text(json.dumps(asdict(gold.policy)))
    auth=DemoSeriesAuthorization.create(gold.connector,gold.rig.directory,path,
        expires_at=gold.clock.now+timedelta(hours=1),user_instruction='1% baseline; qualified minimum lot only up to 2%; 2% combined.')
    executor=DemoExecutor(gold.connector,gold.rig.directory,session_id=session,strategy_policy=gold.policy,strategy_intent=intent)
    return auth,executor


@pytest.mark.parametrize('side',['buy','sell'])
def test_native_demo_execution_checks_then_verifies_one_actual_fake_entry(gold,monkeypatch,side):
    intent,_=configure(gold,monkeypatch,side=side)
    auth,executor=authorize(gold,intent)
    preview=executor.prepare_entry()
    result=executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='c'*32)
    assert result['status']=='open_verified' and result['entry_verified_deals']
    assert result['executed_volume']==.01 and result['protection']['actual_planned_risk_percent']==pytest.approx(1.62)
    assert gold.rig.api.order_send.call_args.args[0]==gold.rig.api.order_check.call_args.args[0]
    with pytest.raises((ValueError,RuntimeError)):
        executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='c'*32)
    assert gold.rig.api.order_send.call_count==1


@pytest.mark.parametrize('failure',['real','contest','stale','permissions','weak_after_preview'])
def test_final_native_guard_failure_never_sends(gold,monkeypatch,failure):
    intent,features=configure(gold,monkeypatch)
    auth,executor=authorize(gold,intent)
    preview=executor.prepare_entry()
    if failure=='real':gold.rig.account.trade_mode=2
    if failure=='contest':gold.rig.account.trade_mode=1
    if failure=='stale':gold.clock.now+=timedelta(minutes=2)
    if failure=='permissions':gold.rig.terminal.trade_allowed=False
    if failure=='weak_after_preview':features['m15_ema50_slope5']=.1
    with pytest.raises((ValueError,RuntimeError,PermissionError)):
        executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='c'*32)
    gold.rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('side',['buy','sell'])
def test_real_frozen_gold_snapshot_proves_conditional_decision_without_feature_stubs(gold,side):
    from test_gold_strategy import bars
    from unittest.mock import Mock
    frame=bars(side)
    columns=['open','high','low','close']
    frame[columns]=(3500+(frame[columns]-3500)*180).round(3)
    # A historical confirmed reference, well before the decision/warmup tail.
    key='high' if side=='buy' else 'low'
    frame.iloc[-150,frame.columns.get_loc(key)]=float(frame.close.iloc[-1])+(100 if side=='buy' else -100)
    gold.rig.tick.bid=float(frame.close.iloc[-1])
    gold.rig.tick.ask=float(Decimal(str(gold.rig.tick.bid))+Decimal('.24'))
    gold.rig.api.copy_rates_from_pos=Mock(return_value=frame.assign(time=frame.index.as_unit('s').astype('int64')).to_records(index=False))
    gold.policy=replace(gold.policy,risk_percent=2,risk_mode=MODE,entry_timing=ACTIVATION)
    intent,report=gold_orders.latest_gold_intent(gold.connector,gold.rig.directory,policy=gold.policy)
    assert intent is not None, report
    result=build(gold,intent)
    assert result['conditional_risk_evidence']['minimum_lot_rescue'] is True
    assert result['volume_lots']==.01 and 1<result['actual_planned_risk_percent']<=2
    validate_plan(result,gold.policy,info=gold.rig.symbol,replay=True)
    gold.rig.api.order_send.assert_not_called()


def test_conditional_position_keeps_native_portfolio_proof_and_combined_ceiling(portfolio,monkeypatch):
    p=portfolio;g=p.gold
    intent,_=configure(g,monkeypatch)
    auth,executor=authorize(g,intent)
    p.guard.register('XAUUSDm',auth)
    original=g.rig.api.order_send.side_effect
    def send(request):
        result=original(request);g.rig.ledger['positions'].append(p.position);return result
    g.rig.api.order_send.side_effect=send
    preview=executor.prepare_entry()
    opened=executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='c'*32)
    assert opened['status']=='open_verified' and len(g.rig.ledger['positions'])==2
    position=next(row for row in g.rig.ledger['positions'] if row.symbol=='XAUUSDm')
    _,state=p.guard.state_for(g.connector,position)
    assert state['protection']['risk_percent']==2
    assert p.guard.budget(g.connector,'XAUUSDm',opened['protection'],include_candidate=False)==(9.3,10.)


def test_other_position_risk_blocks_an_otherwise_valid_exception(portfolio,monkeypatch):
    p=portfolio;g=p.gold
    intent,_=configure(g,monkeypatch)
    p.state['protection']['planned_loss_with_reserves']=3.
    from engine.execution_audit import atomic_json
    atomic_json(p.path,p.state)
    auth,executor=authorize(g,intent)
    p.guard.register('XAUUSDm',auth)
    with pytest.raises(ValueError,match='Combined planned risk exceeds 2%'):executor.prepare_entry()
    g.rig.api.order_send.assert_not_called()
