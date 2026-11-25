"""Offline start-now eligibility, immutable evidence, risk and no-retry checks."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace as Record
from unittest.mock import Mock
import uuid

import pandas as pd
import pytest

from analytics.activation_signals import gold_features, eur_features
from analytics.gold_strategy import window_features
from analytics.trade_journal import replay_signal
from engine import activation_demo as activation, dual_demo as dual, gold_orders, adaptive_orders, strategy_orders
from engine.entry_timing import ACTIVATION, context_profile, metadata, validate_policy
from engine.demo_executor import DemoExecutor
from engine.demo_series import DemoSeriesAuthorization
from scripts import activate_demo as cli
from test_gold_strategy import bars
from test_candidate_signals import setup, append_bar
from test_gold_execution import gold, candidate, rig as native_rig
from test_demo_executor import rig
from test_adaptive_orders import native
from test_dual_demo import rig as controller_rig, FakeChild


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_same_in_session_technical_features_are_unchanged(side):
    assert gold_features(bars(side)) == window_features(bars(side))
    assert eur_features(setup('trend_reclaim', side)) == adaptive_orders._features(setup('trend_reclaim', side))


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('decision', ['2026-11-16T06:00Z', '2026-11-15T06:00Z', '2026-11-13T13:30Z'])
def test_gold_outside_old_clock_gates_still_requires_actual_setup(side, decision):
    frame = bars(side)
    frame.index += pd.Timestamp(decision) - (frame.index[-1]+pd.Timedelta(minutes=5))
    assert window_features(frame)['side'] is None
    result = gold_features(frame)
    assert result['side'] == side and result['reason'] == 'valid_entry_setup'
    flat = frame.copy()
    flat[['open', 'high', 'low', 'close']] = [3500., 3500.01, 3499.99, 3500.]
    assert gold_features(flat)['side'] is None


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_eur_outside_session_context_replays_from_actual_closed_input(native, monkeypatch, tmp_path, side):
    frame = setup('trend_reclaim', side)
    frame.index -= pd.Timedelta(hours=8)
    assert not adaptive_orders._features(frame)
    completed = append_bar(frame)
    now = (frame.index[-1]+pd.Timedelta(hours=1, seconds=30)).to_pydatetime()
    monkeypatch.setattr(strategy_orders, 'utc_now', lambda: now)
    native.api.copy_rates_from_pos = Mock(return_value=completed.assign(time=completed.index.as_unit('s').astype('int64')).to_records(index=False))
    native.api.symbol_info = Mock(return_value=native.info)
    connector = Record(terminal_api=native.api, inspect=Mock(return_value=dict(account={'account_type':'DEMO'},
        quote={'symbol':'EURUSDm', 'bid':float(frame.close.iloc[-1]), 'ask':float(frame.close.iloc[-1])+.00008})))
    policy = replace(native.policy, target_mode='adaptive_pullback_v2', entry_timing=ACTIVATION)
    intent, report = strategy_orders.latest_strategy_intent(connector, tmp_path, policy=policy)
    assert intent and intent.side == side and report['entry_timing'] == ACTIVATION
    assert adaptive_orders._verified_context(intent, native.info, target_mode=policy.target_mode) == eur_features(frame)
    replay = replay_signal({'protection':{'policy':asdict(policy), 'strategy_intent':asdict(intent)}}, tmp_path)
    assert replay['status'] == 'verified'
    with pytest.raises(ValueError, match='timing differs'):
        validate_policy(replace(policy, entry_timing='clock_session'), intent)
    native.api.order_send.assert_not_called()
    native.api.order_check.assert_not_called()


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_gold_outside_session_context_replays_and_remains_freshness_limited(gold, monkeypatch, side):
    frame = bars(side)
    frame.index -= pd.Timedelta(hours=4)
    gold.clock.now = (frame.index[-1]+pd.Timedelta(minutes=5, seconds=30)).to_pydatetime()
    gold.rig.api.copy_rates_from_pos = Mock(return_value=frame.assign(time=frame.index.as_unit('s').astype('int64')).to_records(index=False))
    policy = replace(gold.policy, entry_timing=ACTIVATION)
    intent, report = gold_orders.latest_gold_intent(gold.connector, gold.rig.directory, policy=policy)
    assert intent and report['entry_timing'] == ACTIVATION
    assert gold_orders.verified_context(intent, gold.rig.symbol) == gold_features(frame)
    assert replay_signal({'protection':{'policy':asdict(policy), 'strategy_intent':asdict(intent)}}, gold.rig.directory)['status'] == 'verified'
    gold.clock.now += timedelta(seconds=31)
    with pytest.raises(ValueError, match='expired'):
        strategy_orders.validate_intent(intent)
    gold.rig.api.order_send.assert_not_called()


def test_activation_does_not_remove_recent_gap_checks():
    for frame, function in [(bars(), gold_features), (setup('trend_reclaim'), eur_features)]:
        previous = frame.iloc[:1].copy()
        previous.index -= frame.index[1]-frame.index[0]
        broken = pd.concat([previous, frame.drop(frame.index[-3])])
        result = function(broken)
        assert not result or not result.get('side') and result.get('reason') == 'recent_m5_gap'


@pytest.mark.parametrize('key', ['entry_timing', 'entry_timing_spec_sha256', 'entry_timing_kernel_sha256'])
def test_unrecognized_or_changed_timing_evidence_fails_closed(key):
    data = metadata(ACTIVATION)
    data[key] = 'changed'
    with pytest.raises(ValueError):
        context_profile(data)


@pytest.mark.parametrize('trade_mode', [0, 1, 2])
def test_activated_native_executor_keeps_demo_gate_and_single_send(gold, monkeypatch, trade_mode):
    intent = candidate(gold, monkeypatch)
    gold.policy = replace(gold.policy, entry_timing=ACTIVATION)
    gold.clock.now = gold.clock.now.replace(hour=6)
    gold.rig.tick.time_msc = int(gold.clock.now.timestamp()*1000)
    context = dict(intent.adaptive_context, **metadata(ACTIVATION))
    intent = replace(intent, decision_time_utc='2026-11-13T06:00:00+00:00', adaptive_context=context)
    gold.rig.account.trade_mode = trade_mode
    executor = DemoExecutor(gold.connector, gold.rig.directory, session_id='activation-native',
        strategy_policy=gold.policy, strategy_intent=intent)
    if trade_mode:
        with pytest.raises((ValueError, RuntimeError)):
            executor.prepare_entry()
        gold.rig.api.order_send.assert_not_called()
        return
    path = gold.rig.directory/'activated_policy.json'
    path.write_text(json.dumps(asdict(gold.policy)))
    auth = DemoSeriesAuthorization.create(gold.connector, gold.rig.directory, path,
        expires_at=gold.clock.now+timedelta(hours=1), user_instruction='Explicit offline test authority.')
    preview = executor.prepare_entry()
    assert preview['ready'] and preview['protection']['planned_loss_with_reserves'] <= 5
    result = executor.open_authorized_session(preview['preview_id'], auth.authorization_id, monitor_run_id='f'*32)
    assert result['status'] == 'open_verified' and result['entry_verified_deals']
    assert gold.rig.api.order_send.call_args.args[0] == gold.rig.api.order_check.call_args.args[0]
    with pytest.raises((ValueError, RuntimeError)):
        executor.open_authorized_session(preview['preview_id'], auth.authorization_id, monitor_run_id='f'*32)
    assert gold.rig.api.order_send.call_count == 1


def configure_controller(rig):
    begins = rig.clock.value
    return dual.DualController(rig.root, rig.base, now=lambda:rig.clock.value,
        monotonic=lambda:rig.ticks.value, sleep=lambda _:None,
        authorization_factory=rig.factory, monitor_factories={s:FakeChild for s in dual.ORDER},
        journal_refresh=Mock(), windows={s:(begins,begins+timedelta(hours=6)) for s in dual.ORDER},
        policies=dual.POLICIES, activation_id='explicit-new-activation', source_verifier=lambda _: {})


def test_controller_arms_both_immediately_and_does_not_force_setup(controller_rig):
    c = configure_controller(controller_rig)
    c.tick()
    assert set(c.children) == set(dual.ORDER) and c.status['phase'] == 'monitoring_setups'
    assert not controller_rig.base.sends
    assert c.status['entry_timing'] == ACTIVATION
    assert all('user_activation_v1' in str(child.progress_path) for child in c.children.values())


def test_research_observer_failure_cannot_change_execution_scope(controller_rig):
    controller_rig.clock.value=controller_rig.clock.value.replace(minute=0,second=0,microsecond=0)
    c=configure_controller(controller_rig)
    c.observer=Record(observe=Mock(side_effect=ValueError('research data unavailable')),summary=Mock())
    controller_rig.base.signals={s:True for s in dual.ORDER}
    c.tick()
    assert controller_rig.base.sends==list(dual.ORDER)
    assert c.status['status']=='running'
    assert 'research data unavailable' in c.status['strategy_comparison_warning']
    assert all(not a.revoked_path.exists() for a in c.authorizations.values())
    c.observer.summary.assert_not_called()


def test_start_now_accepts_current_fresh_close_but_rejects_stale_and_future(controller_rig):
    c = configure_controller(controller_rig)
    current = controller_rig.clock.value
    for symbol, seconds in [('EURUSDm',300), ('XAUUSDm',60)]:
        assert c.entry_blocker(symbol, (current-timedelta(seconds=seconds)).isoformat()) is None
        assert c.entry_blocker(symbol, (current-timedelta(seconds=seconds+1)).isoformat())
        assert c.entry_blocker(symbol, (current+timedelta(seconds=1)).isoformat())


def test_expiry_revokes_authority_and_leaves_positions_alone(controller_rig):
    c = configure_controller(controller_rig)
    c.tick()
    controller_rig.clock.value += timedelta(hours=6)
    result = c.run()
    assert result['status'] == 'stopped' and not controller_rig.base.sends
    assert all(a.revoked_path.exists() for a in c.authorizations.values())


@pytest.mark.parametrize('days', [0, -1, 1, 6])
def test_new_manual_activation_has_no_date_gate_and_no_consumed_id_retry(controller_rig, monkeypatch, days):
    r = controller_rig
    monkeypatch.setattr(activation, 'verify_sources', lambda _: {})
    r.clock.value += timedelta(days=days)
    controller = Record(run_id=uuid.uuid4().hex, run=Mock(return_value={'status':'stopped'}))
    factory = Mock(return_value=r.base)
    build = Mock(return_value=controller)
    # Preserve old consumed day reservations. New explicit authority has a new ID.
    path = r.root/dual.RESERVATION
    path.write_text('{"status":"consumed"}')
    activation.start(r.root, 'user-request-1', now=lambda:r.clock.value, connector_factory=factory, controller_factory=build)
    kwargs = build.call_args.kwargs
    assert all(a == r.clock.value and b-a == timedelta(hours=6) for a,b in kwargs['windows'].values())
    assert json.loads(path.read_text())['status'] == 'consumed'
    with pytest.raises(ValueError, match='consumed'):
        activation.start(r.root, 'user-request-1', now=lambda:r.clock.value, connector_factory=factory)
    assert factory.call_count == 1


def test_changed_sources_block_before_any_native_access(tmp_path, monkeypatch):
    monkeypatch.setattr(activation, 'verify_sources', Mock(side_effect=ValueError('source changed')))
    factory = Mock()
    with pytest.raises(ValueError, match='source changed'):
        activation.start(tmp_path, 'new-user-request', connector_factory=factory)
    factory.assert_not_called()


def test_cli_defaults_to_offline_status_and_start_requires_id(monkeypatch, capsys):
    status = Mock(return_value={'status':'stopped'})
    monkeypatch.setattr(activation, 'status', status)
    start = Mock()
    monkeypatch.setattr(activation, 'start', start)
    assert cli.main([]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'stopped'
    with pytest.raises(SystemExit): cli.main(['--start'])
    start.assert_not_called()


@pytest.mark.parametrize('until', ['2026-11-16T22:00:00+08:00', '2026-11-16T14:00:00+00:00'])
def test_explicit_manila_deadline_overrides_only_session_duration(controller_rig, monkeypatch, until):
    r = controller_rig
    monkeypatch.setattr(activation, 'verify_sources', lambda _: {})
    factory = Mock(return_value=Record(run_id=uuid.uuid4().hex, run=Mock(return_value={'status':'stopped'})))
    activation.start(r.root, 'explicit-until-10', until=until, now=lambda:r.clock.value,
        connector_factory=Mock(return_value=r.base), controller_factory=factory)
    expected = datetime(2026,11,16,14,tzinfo=timezone.utc)
    assert all(begin==r.clock.value and end==expected for begin,end in factory.call_args.kwargs['windows'].values())
    assert factory.call_args.kwargs['policies'] == activation.POLICIES
    assert expected.isoformat() in factory.call_args.kwargs['instruction']
    audit = json.loads((r.root/'logs/activations/explicit-until-10.launch.json').read_text())
    assert audit['explicit_until'] == until and audit['deadline_utc'] == expected.isoformat()


@pytest.mark.parametrize('until', ['2026-11-16T22:00:00', '2026-11-16T03:00:00Z',
    '2026-11-17T05:00:00Z', 'bad-time', True])
def test_invalid_deadlines_stop_before_native_connection(controller_rig, until):
    factory = Mock()
    with pytest.raises(ValueError):
        activation.start(controller_rig.root, 'invalid-deadline', until=until,
            now=lambda:controller_rig.clock.value, connector_factory=factory)
    factory.assert_not_called()


def test_cli_requires_explicit_start_to_supply_deadline(monkeypatch, capsys):
    call = Mock(return_value={'status':'stopped'})
    monkeypatch.setattr(activation, 'start', call)
    with pytest.raises(SystemExit): cli.main(['--status','--until','2026-11-16T22:00:00+08:00'])
    call.assert_not_called()
    assert cli.main(['--start','--activation-id','explicit-request','--until','2026-11-16T22:00:00+08:00']) == 0
    assert call.call_args.kwargs == {'until':'2026-11-16T22:00:00+08:00'}
