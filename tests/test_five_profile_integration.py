"""Native adapter and authorization tests for the ten executable hypotheses."""
from copy import deepcopy
from dataclasses import asdict,replace
from datetime import timedelta
import hashlib,json
from pathlib import Path
from types import SimpleNamespace as Record
from unittest.mock import Mock

import pandas as pd
import pytest

from analytics.five_setups import SETUPS,evaluate_setups
from engine import adaptive_orders,gold_orders,strategy_orders,setup_profiles
from engine.conditional_risk import favorable,favorable_for_intent
from engine.demo_executor import DemoExecutor
from engine.demo_series import DemoSeriesAuthorization
from engine.entry_timing import ACTIVATION,metadata
from test_five_setups import fixture_frame
from test_gold_execution import rig,gold,candidate
from test_adaptive_orders import native
from test_adaptive_orders import fixture_signal,add_obstacle
from test_demo_executor import authorized_rig,dynamic_fake_send


@pytest.mark.parametrize('symbol',['EURUSDm','XAUUSDm'])
@pytest.mark.parametrize('setup_id',SETUPS)
@pytest.mark.parametrize('side',['buy','sell'])
def test_every_family_roundtrips_real_closed_snapshot_and_preserves_actual_trigger(native,gold,tmp_path,monkeypatch,symbol,setup_id,side):
    frame=fixture_frame(symbol,setup_id,side)
    step=pd.Timedelta(minutes=5 if symbol=='XAUUSDm' else 60)
    decision=frame.index[-1]+step
    if symbol=='XAUUSDm':
        gold.clock.now=(decision+pd.Timedelta(seconds=30)).to_pydatetime()
        gold.rig.tick.bid=float(frame.close.iloc[-1]);gold.rig.tick.ask=gold.rig.tick.bid+.24
        gold.rig.tick.time_msc=int(gold.clock.now.timestamp()*1000)
        api,info=gold.rig.api,gold.rig.symbol
        connector=gold.connector
        policy=replace(gold.policy,entry_timing=ACTIVATION,entry_setups=setup_profiles.MULTI)
    else:
        monkeypatch.setattr(strategy_orders,'utc_now',lambda:(decision+pd.Timedelta(seconds=30)).to_pydatetime())
        api,info=native.api,native.info
        connector=Record(terminal_api=api,inspect=lambda:dict(account={'account_type':'DEMO'},quote={
            'symbol':symbol,'bid':float(frame.close.iloc[-1]),'ask':float(frame.close.iloc[-1])+.00008}))
        api.symbol_info=Mock(return_value=info)
        policy=replace(native.policy,target_mode='adaptive_pullback_v2',entry_timing=ACTIVATION,entry_setups=setup_profiles.MULTI)
    incomplete=frame.iloc[-1:].copy();incomplete.index+=step
    full=pd.concat([frame,incomplete]);api.copy_rates_from_pos=Mock(return_value=full.assign(time=full.index.as_unit('s').astype('int64')).to_records(index=False))
    intent,report=(gold_orders.latest_gold_intent if symbol=='XAUUSDm' else adaptive_orders.latest_adaptive_intent)(connector,tmp_path,policy=policy)
    assert intent is not None,report
    assert intent.side==side and report['setup_id']==setup_id
    expected={'trend_reclaim':'pullback','trend_sweep':'sweep','channel_breakout':'breakout','compression_breakout':'breakout','range_reentry':'range'}[setup_id]
    assert intent.trigger==expected
    features=gold_orders.verified_context(intent,info) if symbol=='XAUUSDm' else adaptive_orders._verified_context(intent,info,target_mode=policy.target_mode)
    assert features['strategy_family']==setup_id and features['side']==side
    assert features==intent.adaptive_context['features']
    setup_profiles.validate_policy(policy,intent)
    api.order_send.assert_not_called();api.order_check.assert_not_called()
    # Relabeling a setup with a recomputed outer checksum still fails reproduction.
    changed=deepcopy(intent.adaptive_context);changed['setup_id']=next(k for k in SETUPS if k!=setup_id)
    canonical=gold_orders.canonical if symbol=='XAUUSDm' else adaptive_orders._canonical
    changed['context_payload_sha256']=hashlib.sha256(canonical({k:v for k,v in changed.items() if k!='context_payload_sha256'})).hexdigest()
    wrong=replace(intent,trigger=setup_profiles.trigger(changed),adaptive_context=changed)
    with pytest.raises(ValueError):
        gold_orders.verified_context(wrong,info) if symbol=='XAUUSDm' else adaptive_orders._verified_context(wrong,info,target_mode=policy.target_mode)


@pytest.mark.parametrize('setup_id',SETUPS)
@pytest.mark.parametrize('side',['buy','sell'])
def test_each_new_gold_family_can_use_checked_once_only_executor_with_own_identity(gold,monkeypatch,setup_id,side):
    """Stub signal context only; real risk/authorization/check/send/deal verification."""
    intent=candidate(gold,monkeypatch,side)
    ctx=dict(intent.adaptive_context,**metadata(ACTIVATION),**setup_profiles.metadata(setup_profiles.MULTI),setup_id=setup_id)
    intent=replace(intent,trigger=setup_profiles.trigger(ctx),adaptive_context=ctx)
    policy=replace(gold.policy,entry_timing=ACTIVATION,entry_setups=setup_profiles.MULTI)
    path=gold.rig.directory/'five_policy.json';path.write_text(json.dumps(asdict(policy)))
    auth=DemoSeriesAuthorization.create(gold.connector,gold.rig.directory,path,expires_at=gold.clock.now+timedelta(hours=1),
        user_instruction='Explicit offline fake-broker five-setup test only.')
    executor=DemoExecutor(gold.connector,gold.rig.directory,session_id='five-fake',strategy_policy=policy,strategy_intent=intent)
    preview=executor.prepare_entry();assert preview['ready']
    result=executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='c'*32)
    assert result['status']=='open_verified'
    assert result['protection']['strategy_intent']['adaptive_context']['setup_id']==setup_id
    assert result['protection']['planned_loss_with_reserves']<=5
    assert gold.rig.api.order_send.call_args.args[0]==gold.rig.api.order_check.call_args.args[0]
    with pytest.raises(ValueError):executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='c'*32)
    assert gold.rig.api.order_send.call_count==1


@pytest.mark.parametrize('side',['buy','sell'])
def test_nontrend_setup_cannot_borrow_two_percent_rescue_despite_absolute_strength(side):
    f=dict(h4_ema50=1.1,h4_ema200=1.11,h4_ema50_slope5=.002,h4_atr14=.001,volatility_ratio=1.,trend_long=False,trend_short=False)
    intent=Record(side=side,adaptive_context={'entry_setups':setup_profiles.MULTI})
    assert favorable(f,gold=False)
    assert not favorable_for_intent(intent,f,gold=False)
    f['trend_long' if side=='buy' else 'trend_short']=True
    assert favorable_for_intent(intent,f,gold=False)


@pytest.mark.parametrize('setup_id',SETUPS)
@pytest.mark.parametrize('side',['buy','sell'])
def test_each_new_eur_family_uses_checked_once_only_executor(rig,monkeypatch,setup_id,side):
    ctx=authorized_rig(rig,monkeypatch,side)
    policy=replace(ctx.executor.strategy_policy,target_mode='adaptive_pullback_v2',entry_timing=ACTIVATION,entry_setups=setup_profiles.MULTI)
    base=ctx.executor.strategy_intent
    context=dict(context_payload_sha256='b'*64,target_mode=policy.target_mode,research_spec_sha256=adaptive_orders.EXTENDED_SPEC_SHA256,
        **metadata(ACTIVATION),**setup_profiles.metadata(setup_profiles.MULTI),setup_id=setup_id)
    intent=replace(base,trigger=setup_profiles.trigger(context),adaptive_context=context)
    signal=fixture_signal(side);signal.update(side=side,setup_id=setup_id,strategy_family=setup_id,
        trend_long=side=='buy' and setup_id not in ('range_reentry','compression_breakout'),
        trend_short=side=='sell' and setup_id not in ('range_reentry','compression_breakout'))
    info=rig.symbol;info.chart_mode=0
    api_context=Record(api=rig.api,info=info,quote={'bid':rig.tick.bid,'ask':rig.tick.ask},funds={'balance':500.,'equity':500.,'currency':'USD'},policy=policy)
    add_obstacle(api_context,signal,intent,4,extra=.0002)
    monkeypatch.setattr(adaptive_orders,'_verified_context',lambda *_a,**_kw:signal)
    path=rig.directory/'five_eur_policy.json';path.write_text(json.dumps(asdict(policy)))
    auth=DemoSeriesAuthorization.create(rig.executor.connector,rig.directory,path,expires_at=ctx.clock.now+timedelta(hours=1),user_instruction='Offline five EUR fake broker check only.')
    executor=DemoExecutor(rig.executor.connector,rig.directory,session_id='five-eur-fake',strategy_policy=policy,strategy_intent=intent)
    dynamic_fake_send(rig)
    preview=executor.prepare_entry();assert preview['ready']
    opened=executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='c'*32)
    assert opened['status']=='open_verified'
    assert opened['protection']['strategy_intent']['adaptive_context']['setup_id']==setup_id
    assert opened['protection']['planned_loss_with_reserves']<=5
    assert opened['protection']['selected_reward_r']<=2 if setup_id in ('range_reentry','compression_breakout') else True
    assert rig.api.order_send.call_args.args[0]==rig.api.order_check.call_args.args[0]
    with pytest.raises(ValueError):executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='c'*32)
    assert rig.api.order_send.call_count==1


def test_unknown_family_or_scope_never_becomes_an_intent():
    ctx=dict(setup_profiles.metadata(setup_profiles.MULTI),setup_id='unknown')
    with pytest.raises(ValueError):setup_profiles.context_profile(ctx)
    with pytest.raises(ValueError):strategy_orders.StrategyPolicy(entry_setups=setup_profiles.MULTI)
