"""Offline tests for the manual Monday launcher; never initialize native MT5."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as Record
from unittest.mock import Mock

import pytest

from engine import monday_session as monday
from engine.market_monitor import monitor_lock
from scripts import start_monday_demo as cli


@pytest.fixture
def pilot(tmp_path):
    project = Path(__file__).resolve().parents[1]
    plan = json.loads((project / monday.PLAN_FILE).read_text(encoding='utf-8'))
    policy_path = tmp_path / monday.POLICY_FILE
    policy_path.parent.mkdir()
    policy_path.write_text('{"offline_test_policy": true}', encoding='utf-8')
    plan['source_sha256'] = {monday.POLICY_FILE.as_posix(): hashlib.sha256(policy_path.read_bytes()).hexdigest()}
    (tmp_path / monday.PLAN_FILE).write_text(json.dumps(plan), encoding='utf-8')
    account = Record(login=monday.IDENTITY[0], server=monday.IDENTITY[1],
                     company=monday.IDENTITY[2], currency='USD', trade_mode=0)
    api = Record(ACCOUNT_TRADE_MODE_DEMO=0, account_info=Mock(return_value=account),
                 positions_get=Mock(return_value=()), orders_get=Mock(return_value=()))
    connector = Mock()
    connector.terminal_api = api
    connector.account_identity = monday.IDENTITY[:2]
    connector.inspect.return_value = dict(account=dict(broker=monday.IDENTITY[2],
        server=monday.IDENTITY[1], currency='USD'), quote=dict(symbol=monday.SYMBOL))
    connected = {'value': False}
    def enter(*_):
        connected['value'] = True
        return connector
    def leave(*_):
        connected['value'] = False
    connector.__enter__ = Mock(side_effect=enter)
    connector.__exit__ = Mock(side_effect=leave)
    factory = Mock(return_value=connector)
    authorization = Record(authorization_id='a' * 32,
        revoked_path=tmp_path / 'logs/authorizations' / ('a' * 32 + '.revoked.json'))
    auth_factory = Record(create=Mock(return_value=authorization))
    def run():
        assert connected['value'], 'Native IPC must remain connected while monitoring.'
        with monitor_lock(tmp_path / 'logs/monitor/monitor.lock'):
            pass  # Launcher released this kernel lock before monitor.run().
        assert (tmp_path / monday.RESERVATION).exists()
        return dict(status='stopped', orders_sent=0)
    monitor = Record(run=Mock(side_effect=run))
    monitor_factory = Mock(return_value=monitor)
    policy = Record(target_mode=monday.TARGET_MODE, risk_percent=1.0, max_lots=.1)
    authorization.data = dict(account_identity=dict(login=monday.IDENTITY[0], server=monday.IDENTITY[1]),
                              broker=monday.IDENTITY[2], symbol=monday.SYMBOL)
    authorization.policy = policy
    clock = lambda: monday.OPEN + timedelta(minutes=1)
    options = dict(now=clock, connector_factory=factory, authorization_factory=auth_factory,
                   monitor_factory=monitor_factory, policy_loader=lambda _: policy)
    return Record(root=tmp_path, plan=plan, policy=policy, policy_path=policy_path, api=api,
        account=account, connector=connector, factory=factory, authorization=authorization,
        auth_factory=auth_factory, monitor=monitor, monitor_factory=monitor_factory, options=options)


@pytest.mark.parametrize('when', [monday.OPEN - timedelta(days=2), monday.OPEN - timedelta(microseconds=1),
                                  monday.CLOSE, monday.CLOSE + timedelta(days=1)])
def test_date_guard_precedes_files_and_native_connection(tmp_path, when):
    native = Mock()
    with pytest.raises(ValueError, match='Manual start'):
        monday.start_session(tmp_path, now=lambda: when, connector_factory=native)
    native.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_clock_must_be_aware(tmp_path):
    factory = Mock()
    with pytest.raises(ValueError, match='timezone-aware'):
        monday.start_session(tmp_path, now=lambda: datetime(2026, 11, 16, 9), connector_factory=factory)
    factory.assert_not_called()


def test_plan_status_is_offline_inactive_and_not_scheduled(pilot):
    result = monday.describe_plan(pilot.root, now=monday.OPEN - timedelta(days=2))
    assert result['status'] == 'not_started'
    assert result['scheduled'] is result['background_process_started'] is False
    assert result['start_allowed_now'] is False
    assert result['account']['account_id_masked'] == '****2812'
    assert '16:00-22:00' in result['local_session']
    pilot.factory.assert_not_called()


def test_exact_manual_start_bounds_authority_and_releases_lock(pilot):
    result = monday.start_session(pilot.root, **pilot.options)
    assert result == dict(status='stopped', orders_sent=0)
    pilot.factory.assert_called_once_with()
    pilot.connector.inspect.assert_called_once_with('EURUSDm')
    call = pilot.auth_factory.create.call_args
    assert call.args == (pilot.connector, pilot.root / 'logs', pilot.policy_path)
    assert call.kwargs['expires_at'] == monday.CLOSE
    assert 'instruction received 2026-11-14' in call.kwargs['user_instruction']
    assert 'manually activated 2026-11-16' in call.kwargs['user_instruction']
    monitor = pilot.monitor_factory.call_args
    assert monitor.kwargs['config'].hours == pytest.approx(6 - 1/60)
    assert monitor.kwargs['config'].poll_seconds == 15
    assert monitor.kwargs['report_path'] == pilot.root / 'reports/latest_monitor_status.md'
    pilot.connector.__exit__.assert_called_once()
    assert pilot.authorization.revoked_path.exists()
    reservation = json.loads((pilot.root / monday.RESERVATION).read_text(encoding='utf-8'))
    assert reservation['status'] == 'finished'
    assert reservation['automatic_retry_allowed'] is False


@pytest.mark.parametrize('field,value', [('risk_percent', 5), ('risk_percent', True),
    ('target_mode', 'fixed'), ('max_lots', .2)])
def test_changed_policy_blocks_before_native_connection(pilot, field, value):
    setattr(pilot.policy, field, value)
    with pytest.raises(ValueError, match='separately pinned adaptive policy'):
        monday.start_session(pilot.root, **pilot.options)
    pilot.factory.assert_not_called()


def test_changed_source_blocks_before_native_connection(pilot):
    pilot.policy_path.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='source changed'):
        monday.start_session(pilot.root, **pilot.options)
    pilot.factory.assert_not_called()


def test_unfinalized_fingerprints_cannot_start(pilot):
    pilot.plan['source_sha256'] = {}
    (pilot.root / monday.PLAN_FILE).write_text(json.dumps(pilot.plan), encoding='utf-8')
    with pytest.raises(ValueError, match='not been finalized'):
        monday.start_session(pilot.root, **pilot.options)
    pilot.factory.assert_not_called()


@pytest.mark.parametrize('field,value', [('demo_only', False), ('scheduled', True),
    ('symbol', 'XAUUSDm'), ('starts_at_utc', '2026-11-16T07:00:00+00:00'), ('risk_percent', 5.0)])
def test_modified_manifest_is_rejected(pilot, field, value):
    pilot.plan[field] = value
    (pilot.root / monday.PLAN_FILE).write_text(json.dumps(pilot.plan), encoding='utf-8')
    with pytest.raises(ValueError, match='approved date'):
        monday.start_session(pilot.root, **pilot.options)
    pilot.factory.assert_not_called()


@pytest.mark.parametrize('path', ['logs/demo_test_state.json', 'logs/sessions/old/demo_test_state.json'])
@pytest.mark.parametrize('status', ['open_verified', 'entry_submitting', 'uncertain'])
def test_unresolved_prior_execution_blocks_same_log_directory(pilot, path, status):
    state = pilot.root / path
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps(dict(status=status)), encoding='utf-8')
    with pytest.raises(ValueError, match='earlier execution is unresolved'):
        monday.start_session(pilot.root, **pilot.options)
    pilot.factory.assert_not_called()
    assert not (pilot.root / monday.RESERVATION).exists()


@pytest.mark.parametrize('mode', [1, 2, False, None, '0'])
def test_native_mode_must_be_integer_demo(pilot, mode):
    pilot.account.trade_mode = mode
    with pytest.raises(ValueError, match='Fresh native'):
        monday.start_session(pilot.root, **pilot.options)
    pilot.auth_factory.create.assert_not_called()
    assert not (pilot.root / monday.RESERVATION).exists()


@pytest.mark.parametrize('field,value', [('login', 1), ('server', 'another-demo'),
    ('company', 'Another broker'), ('currency', 'EUR')])
def test_native_identity_must_match_exact_approved_account(pilot, field, value):
    setattr(pilot.account, field, value)
    with pytest.raises(ValueError, match='exact approved'):
        monday.start_session(pilot.root, **pilot.options)
    pilot.auth_factory.create.assert_not_called()


def test_missing_native_account_fails_closed(pilot):
    pilot.api.account_info.return_value = None
    with pytest.raises(ValueError, match='Fresh native'):
        monday.start_session(pilot.root, **pilot.options)
    pilot.auth_factory.create.assert_not_called()


def test_switch_during_inspection_is_rejected(pilot):
    changed = Record(**vars(pilot.account))
    changed.trade_mode = 2
    pilot.api.account_info.side_effect = [pilot.account, changed]
    with pytest.raises(ValueError, match='changed during'):
        monday.start_session(pilot.root, **pilot.options)
    pilot.auth_factory.create.assert_not_called()


@pytest.mark.parametrize('inventory', ['positions_get', 'orders_get'])
@pytest.mark.parametrize('value', [None, (Record(ticket=123),)])
def test_open_or_unavailable_inventory_blocks(pilot, inventory, value):
    getattr(pilot.api, inventory).return_value = value
    with pytest.raises(ValueError, match='empty positions'):
        monday.start_session(pilot.root, **pilot.options)
    pilot.auth_factory.create.assert_not_called()


def test_exact_symbol_must_be_confirmed(pilot):
    pilot.connector.inspect.return_value['quote']['symbol'] = 'EURUSD'
    with pytest.raises(ValueError, match='exact EUR/USD'):
        monday.start_session(pilot.root, **pilot.options)
    pilot.auth_factory.create.assert_not_called()


def test_deadline_is_rechecked_after_native_preflight(pilot):
    clock = Mock(side_effect=[monday.OPEN, monday.CLOSE])
    with pytest.raises(ValueError, match='ended during preflight'):
        monday.start_session(pilot.root, **(pilot.options | dict(now=clock)))
    pilot.auth_factory.create.assert_not_called()
    assert not (pilot.root / monday.RESERVATION).exists()


@pytest.mark.parametrize('changed', ['symbol', 'account', 'policy'])
def test_authorization_rechecks_bound_scope_after_creation(pilot, changed):
    if changed == 'symbol':
        pilot.authorization.data['symbol'] = 'EURUSD'
    elif changed == 'account':
        pilot.authorization.data['account_identity']['login'] = 123
    else:
        pilot.authorization.policy = Record(target_mode='fixed')
    with pytest.raises(ValueError, match='New authorization does not match'):
        monday.start_session(pilot.root, **pilot.options)
    pilot.monitor_factory.assert_not_called()
    assert pilot.authorization.revoked_path.exists()
    state = json.loads((pilot.root / monday.RESERVATION).read_text(encoding='utf-8'))
    assert state['status'] == 'failed_no_retry'


def test_duplicate_launch_never_reconnects_or_creates_second_authority(pilot):
    monday.start_session(pilot.root, **pilot.options)
    with pytest.raises(ValueError, match='already reserved/consumed'):
        monday.start_session(pilot.root, **pilot.options)
    assert pilot.factory.call_count == pilot.auth_factory.create.call_count == 1


@pytest.mark.parametrize('stage', ['authorization', 'run'])
def test_failed_launch_consumes_reservation_without_retry(pilot, stage):
    target = pilot.auth_factory.create if stage == 'authorization' else pilot.monitor.run
    target.side_effect = RuntimeError('simulated failure')
    with pytest.raises(RuntimeError, match='simulated failure'):
        monday.start_session(pilot.root, **pilot.options)
    state = json.loads((pilot.root / monday.RESERVATION).read_text(encoding='utf-8'))
    assert state['status'] == 'failed_no_retry'
    with pytest.raises(ValueError, match='already reserved/consumed'):
        monday.start_session(pilot.root, **pilot.options)
    assert pilot.factory.call_count == 1
    if stage == 'run':
        assert pilot.authorization.revoked_path.exists()


def test_existing_monitor_kernel_lock_prevents_connection(pilot):
    with monitor_lock(pilot.root / 'logs/monitor/monitor.lock'):
        with pytest.raises(RuntimeError, match='Another market monitor'):
            monday.start_session(pilot.root, **pilot.options)
    pilot.factory.assert_not_called()


def test_stop_uses_existing_stop_path_and_never_connects(pilot, monkeypatch):
    stop = Mock(return_value={'status': 'stop_requested'})
    monkeypatch.setattr(monday, 'request_stop', stop)
    assert monday.stop_session(pilot.root) == {'status': 'stop_requested'}
    stop.assert_called_once_with(pilot.root / 'logs')
    pilot.factory.assert_not_called()
    pilot.auth_factory.create.assert_not_called()


@pytest.mark.parametrize('argv', [[], ['--plan'], ['--status']])
def test_cli_default_and_status_never_start(pilot, monkeypatch, capsys, argv):
    monkeypatch.setattr(cli, 'ROOT', pilot.root)
    start = Mock()
    monkeypatch.setattr(cli, 'start_session', start)
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['scheduled'] is False
    start.assert_not_called()


def test_cli_requires_explicit_start_flag(pilot, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'ROOT', pilot.root)
    start = Mock(return_value=dict(status='stopped'))
    monkeypatch.setattr(cli, 'start_session', start)
    assert cli.main(['--start']) == 0
    start.assert_called_once_with(pilot.root)
    assert json.loads(capsys.readouterr().out)['status'] == 'stopped'
