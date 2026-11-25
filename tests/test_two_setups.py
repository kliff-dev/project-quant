"""Offline two-pattern behavior, chronology, arbitration and evidence guards."""
from dataclasses import replace
from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from analytics.activation_signals import eur_features, gold_features
from analytics.two_setups import evaluate_setups, arbitrate, PROFILE
from engine import setup_profiles
from test_candidate_signals import setup, append_bar
from test_gold_strategy import bars
from test_adaptive_orders import native
from test_gold_execution import gold, candidate
from test_demo_executor import rig


@pytest.mark.parametrize('symbol,factory,baseline', [
    ('EURUSDm',lambda side:setup('trend_sweep',side),eur_features),
    ('XAUUSDm',bars,gold_features)])
@pytest.mark.parametrize('side', ['buy','sell'])
def test_A_is_unchanged_and_B_uses_prior_range(symbol,factory,baseline,side):
    frame=factory(side)
    if symbol=='XAUUSDm':
        # Ordinary wider historical ranges keep the constructed sweep below
        # the independently retained 2.5 ATR shock-candle limit.
        frame['high']+=.04
        frame['low']-=.04
    # Put a modest wick beyond the range, preserving the body and trend.
    column='low' if side=='buy' else 'high'
    prior=frame[column].iloc[-21:-1]
    value=float(prior.min()-.00001 if symbol=='EURUSDm' and side=='buy' else
                prior.max()+.00001 if symbol=='EURUSDm' else
                prior.min()-.001 if side=='buy' else prior.max()+.001)
    frame.iloc[-1,frame.columns.get_loc(column)]=value
    before=baseline(frame); comparison=evaluate_setups(frame,symbol)
    a=comparison['setups']['trend_reclaim']; b=comparison['setups']['trend_sweep']
    for key,value in before.items():assert a[key]==value
    assert b['side']==side
    assert b['prior20_low']==frame.low.iloc[-21:-1].min()
    assert b['prior20_high']==frame.high.iloc[-21:-1].max()
    assert b['strategy_family']=='trend_sweep'
    assert b['setup_id']=='trend_sweep'


@pytest.mark.parametrize('side', ['buy','sell'])
def test_B_can_qualify_without_an_EMA_crossover(side):
    frame=setup('trend_sweep',side)
    frame.iloc[-2,frame.columns.get_loc('close')]=frame.close.iloc[-3]
    frame.iloc[-2,frame.columns.get_loc('open')]=frame.close.iloc[-3]
    frame.iloc[-2,frame.columns.get_loc('high')]=max(frame.high.iloc[-2],frame.close.iloc[-2])
    frame.iloc[-2,frame.columns.get_loc('low')]=min(frame.low.iloc[-2],frame.close.iloc[-2])
    result=evaluate_setups(frame,'EURUSDm')
    assert result['setups']['trend_sweep']['side']==side
    assert result['setups']['trend_reclaim']['side'] is None
    assert result['selected_setup']=='trend_sweep'


def test_overlap_selects_A_and_opposite_directions_skip_both():
    assert arbitrate({'trend_reclaim':{'side':'buy'},'trend_sweep':{'side':'buy'}})==('trend_reclaim','same_direction_overlap_A_priority')
    assert arbitrate({'trend_reclaim':{'side':'buy'},'trend_sweep':{'side':'sell'}})==(None,'conflicting_setup_directions')
    assert arbitrate({'trend_reclaim':{'side':None},'trend_sweep':{'side':'sell'}})==('trend_sweep','unique_setup')


@pytest.mark.parametrize('symbol,factory', [('EURUSDm',lambda:setup('trend_sweep')),('XAUUSDm',bars)])
def test_gap_and_insufficient_warmup_do_not_enable_B(symbol,factory):
    frame=factory()
    assert evaluate_setups(frame.iloc[-40:],symbol)['selected_setup'] is None
    assert evaluate_setups(frame.drop(frame.index[-3]),symbol)['selected_setup'] is None


@pytest.mark.parametrize('side', ['buy','sell'])
def test_strict_range_breach_is_required(side):
    frame=setup('trend_sweep',side)
    column='low' if side=='buy' else 'high'
    frame.iloc[-1,frame.columns.get_loc(column)]=(frame.low.iloc[-21:-1].min() if side=='buy' else frame.high.iloc[-21:-1].max())
    assert evaluate_setups(frame,'EURUSDm')['setups']['trend_sweep']['side'] is None


def test_gold_common_shock_filter_is_retained():
    frame=bars()
    frame.iloc[-1,frame.columns.get_loc('low')]-=10
    result=evaluate_setups(frame,'XAUUSDm')
    assert result['selected_setup'] is None
    assert result['setups']['trend_sweep']['reason'] in ('shock_candle','volatility_outside_075_175')


def test_future_prices_do_not_enter_closed_window():
    frame=setup('trend_sweep'); expected=evaluate_setups(frame,'EURUSDm')
    later=append_bar(frame); later.iloc[-1,later.columns.get_loc('high')]+=20
    assert evaluate_setups(later.loc[later.index<=frame.index[-1]],'EURUSDm')==expected


def test_profile_source_mutation_and_policy_mismatch_fail_closed(tmp_path,monkeypatch):
    import shutil
    root=setup_profiles.ROOT
    for relative in setup_profiles.PINS:
        target=tmp_path/relative; target.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(root/relative,target)
    monkeypatch.setattr(setup_profiles,'ROOT',tmp_path)
    evidence=setup_profiles.metadata(PROFILE)
    context=dict(evidence,setup_id='trend_sweep')
    assert setup_profiles.trigger(context)=='sweep'
    with pytest.raises(ValueError,match='explicitly authorized'):
        setup_profiles.validate_policy(SimpleNamespace(entry_setups='reclaim_only'),SimpleNamespace(adaptive_context=context))
    with pytest.raises(ValueError,match='Unexpected'):
        setup_profiles.context_profile({'setup_id':'trend_sweep'})
    (tmp_path/'analytics/two_setups.py').write_text('changed',encoding='utf-8')
    with pytest.raises(ValueError,match='changed'):setup_profiles.metadata(PROFILE)


@pytest.mark.parametrize('side',['buy','sell'])
def test_EUR_B_native_intent_reproduces_and_retains_identity(native,tmp_path,monkeypatch,side):
    from engine import strategy_orders,adaptive_orders
    from engine.entry_timing import ACTIVATION
    frame=setup('trend_sweep',side)
    frame.iloc[-2,frame.columns.get_loc('close')]=frame.close.iloc[-3]
    frame.iloc[-2,frame.columns.get_loc('open')]=frame.close.iloc[-3]
    frame.iloc[-2,frame.columns.get_loc('high')]=max(frame.high.iloc[-2],frame.close.iloc[-2])
    frame.iloc[-2,frame.columns.get_loc('low')]=min(frame.low.iloc[-2],frame.close.iloc[-2])
    full=append_bar(frame)
    from unittest.mock import Mock
    native.api.copy_rates_from_pos=Mock(return_value=full.assign(time=full.index.as_unit('s').astype('int64')).to_records(index=False))
    native.api.symbol_info=Mock(return_value=native.info)
    connector=SimpleNamespace(terminal_api=native.api,inspect=lambda:dict(account={'account_type':'DEMO'},
        quote={'symbol':'EURUSDm','bid':float(frame.close.iloc[-1]),'ask':float(frame.close.iloc[-1])+.00008}))
    policy=replace(native.policy,target_mode='adaptive_pullback_v2',entry_timing=ACTIVATION,entry_setups=PROFILE)
    intent,report=adaptive_orders.latest_adaptive_intent(connector,tmp_path,policy=policy)
    assert intent and intent.trigger=='sweep' and report['setup_id']=='trend_sweep'
    assert adaptive_orders._verified_context(intent,native.info,target_mode=policy.target_mode)['setup_id']=='trend_sweep'
    setup_profiles.validate_policy(policy,intent)
    # Even with a recomputed payload checksum, relabeling B as A is rejected.
    import hashlib
    context=deepcopy(intent.adaptive_context); context['setup_id']='trend_reclaim'
    context['context_payload_sha256']=hashlib.sha256(adaptive_orders._canonical({k:v for k,v in context.items() if k!='context_payload_sha256'})).hexdigest()
    with pytest.raises(ValueError):
        adaptive_orders._verified_context(replace(intent,trigger='pullback',adaptive_context=context),native.info,target_mode=policy.target_mode)
    native.api.order_send.assert_not_called()


@pytest.mark.parametrize('side',['buy','sell'])
def test_gold_B_fake_order_is_once_and_uses_shared_guarded_executor(gold,monkeypatch,side):
    from dataclasses import asdict
    from datetime import timedelta
    import json
    from engine.entry_timing import ACTIVATION,metadata
    from engine.demo_executor import DemoExecutor
    from engine.demo_series import DemoSeriesAuthorization
    intent=candidate(gold,monkeypatch,side)
    context=dict(intent.adaptive_context,**metadata(ACTIVATION),**setup_profiles.metadata(PROFILE),setup_id='trend_sweep')
    intent=replace(intent,trigger='sweep',adaptive_context=context)
    policy=replace(gold.policy,entry_timing=ACTIVATION,entry_setups=PROFILE)
    path=gold.rig.directory/'two_setup_policy.json'; path.write_text(json.dumps(asdict(policy)))
    auth=DemoSeriesAuthorization.create(gold.connector,gold.rig.directory,path,
        expires_at=gold.clock.now+timedelta(hours=1),user_instruction='Two-setup DEMO unit test; fake API only.')
    executor=DemoExecutor(gold.connector,gold.rig.directory,session_id='two-setup-fake',strategy_policy=policy,strategy_intent=intent)
    preview=executor.prepare_entry(); assert preview['ready']
    opened=executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='d'*32)
    assert opened['status']=='open_verified'
    assert opened['protection']['strategy_intent']['adaptive_context']['setup_id']=='trend_sweep'
    assert opened['protection']['planned_loss_with_reserves']<=5
    with pytest.raises((ValueError,PermissionError)):
        executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='d'*32)
    assert gold.rig.api.order_send.call_count==1


@pytest.mark.parametrize('failure',['real','contest','expired','wrong_policy'])
def test_gold_B_rejects_account_freshness_and_profile_changes(gold,monkeypatch,failure):
    from datetime import timedelta
    from engine.entry_timing import ACTIVATION,metadata
    from engine.demo_executor import DemoExecutor
    intent=candidate(gold,monkeypatch)
    context=dict(intent.adaptive_context,**metadata(ACTIVATION),**setup_profiles.metadata(PROFILE),setup_id='trend_sweep')
    intent=replace(intent,trigger='sweep',adaptive_context=context)
    policy=replace(gold.policy,entry_timing=ACTIVATION,entry_setups=PROFILE)
    if failure=='real':gold.rig.account.trade_mode=2
    if failure=='contest':gold.rig.account.trade_mode=1
    if failure=='expired':gold.clock.now+=timedelta(minutes=2)
    if failure=='wrong_policy':policy=replace(policy,entry_setups='reclaim_only')
    executor=DemoExecutor(gold.connector,gold.rig.directory,strategy_policy=policy,strategy_intent=intent)
    with pytest.raises((ValueError,RuntimeError)):executor.prepare_entry()
    gold.rig.api.order_send.assert_not_called()


def test_forward_observer_records_both_and_never_repeats_or_calls_order_API(tmp_path):
    import hashlib,json
    from unittest.mock import Mock
    from engine.setup_observer import SetupObserver
    frame=setup('trend_sweep')
    prefix=frame.iloc[:1].copy(); prefix.index-=pd.Timedelta(hours=1)
    full=pd.concat([prefix,frame])
    path=tmp_path/'input.csv'; raw=full.to_csv(float_format='%.17g',date_format='%Y-%m-%dT%H:%M:%SZ',lineterminator='\n').encode(); path.write_bytes(raw.replace(b'\n',b'\r\n'))
    candle=(frame.index[-1]+pd.Timedelta(hours=1)).isoformat()
    record=dict(status='observed',input_file='input.csv',input_sha256=hashlib.sha256(raw).hexdigest(),
        quote_at_observation={'bid':1.1,'ask':1.10008},funds={'balance':500,'equity':500},signal_fresh_at_observation=True)
    watch=SetupObserver(tmp_path,SimpleNamespace())
    watch.collector=SimpleNamespace(observe=Mock(return_value=record))
    event=watch.observe_decision('EURUSDm',candle,position_open=True)
    assert event['status']=='observed' and event['execution_authority'] is False
    assert event['input_storage_sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
    assert set(event['setup_signals'])=={'trend_reclaim','trend_sweep'}
    assert event['execution_status']=='not_evaluated_position_open'
    assert watch.observe('EURUSDm',candle)==event
    restarted=SetupObserver(tmp_path,SimpleNamespace())
    assert restarted.observe('EURUSDm',candle)==event
    assert watch.collector.observe.call_count==1
    assert len(list(watch.folder.glob('events/*.json')))==1
    assert 'trend_sweep' in (tmp_path/'reports/latest_setup_comparison.md').read_text()


@pytest.mark.parametrize('side',['buy','sell'])
def test_gold_B_actual_closed_snapshot_roundtrip(gold,tmp_path,monkeypatch,side):
    from unittest.mock import Mock
    from engine import gold_orders
    from engine.entry_timing import ACTIVATION
    frame=bars(side); frame['high']+=.04; frame['low']-=.04
    frame.iloc[-2,frame.columns.get_loc('close')]=frame.close.iloc[-1]+(.05 if side=='buy' else -.05)
    frame.iloc[-2,frame.columns.get_loc('high')]=max(frame.high.iloc[-2],frame.close.iloc[-2])
    frame.iloc[-2,frame.columns.get_loc('low')]=min(frame.low.iloc[-2],frame.close.iloc[-2])
    field='low' if side=='buy' else 'high'
    frame.iloc[-1,frame.columns.get_loc(field)]=frame.low.iloc[-21:-1].min()-.001 if side=='buy' else frame.high.iloc[-21:-1].max()+.001
    gold.rig.api.copy_rates_from_pos=Mock(return_value=frame.assign(time=frame.index.as_unit('s').astype('int64')).to_records(index=False))
    policy=replace(gold.policy,entry_timing=ACTIVATION,entry_setups=PROFILE)
    intent,report=gold_orders.latest_gold_intent(gold.connector,tmp_path,policy=policy)
    assert intent and intent.trigger=='sweep' and report['setup_id']=='trend_sweep'
    assert gold_orders.verified_context(intent,gold.rig.symbol)['side']==side
    gold.rig.api.order_send.assert_not_called()


def test_journal_separates_setups_costs_and_excludes_manual_exits(tmp_path,monkeypatch):
    from test_trade_journal import state,save
    from analytics import trade_journal
    monkeypatch.setattr(trade_journal,'replay_signal',lambda *_:dict(status='verified'))
    for number,setup_id in enumerate(('trend_reclaim','trend_sweep')):
        value=state(position=100+10*number,closed=True)
        value['protection']['policy']['entry_setups']=PROFILE
        value['protection']['strategy_intent']={'trigger':'pullback' if number==0 else 'sweep',
            'adaptive_context':{'setup_id':setup_id},'context_sha256':'a'*64}
        if number:value['close_verified_deals'][0]['reason']=0
        save(tmp_path,value,setup_id)
    journal=trade_journal.build_journal(tmp_path/'logs')
    assert {t['setup_id'] for t in journal['trades']}=={'trend_reclaim','trend_sweep'}
    groups={g['setup_id']:g for g in journal['setup_groups']}
    assert groups['trend_reclaim']['rule_exit_results']['completed']==1
    assert groups['trend_sweep']['rule_exit_results']['completed']==0
    assert groups['trend_sweep']['actual_results']['realized_net_pnl_usd']==pytest.approx(5.65)
    trade_journal.write_journal(tmp_path,journal)
    assert 'trend_sweep' in (tmp_path/'reports/trade_journal.md').read_text(encoding='utf-8')
    assert 'setup_id' in (tmp_path/'data/processed/trade_journal.csv').read_text().splitlines()[0]
