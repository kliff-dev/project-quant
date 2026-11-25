"""BTC UI authority, one-use worker and source scope; no terminal or orders."""
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from dashboard import btc_control as btc
from dashboard import monitor_control as common
from scripts import app_btc_session as app
from scripts import activate_btc_demo as activation


NOW = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
UNTIL = NOW + timedelta(hours=2)


def workspace(root):
    for name in ('.venv/Scripts/python.exe', 'scripts/app_btc_session.py',
                 'scripts/activate_btc_demo.py', 'config/btc_demo_plan.json',
                 'scripts/app_monitor_session.py', 'scripts/activate_demo.py',
                 'config/activation_demo_plan.json'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture', encoding='utf-8')


def status(root, *, btc_status=True, **changes):
    record = dict(status='running', mode=btc.MODE if btc_status else common.MODE,
                  entry_timing='user_activation_v1', run_id='c' * 32, pid=12345,
                  heartbeat_utc=NOW.isoformat(), deadline_utc=UNTIL.isoformat(),
                  account=dict(demo_verified=True, trade_mode=0))
    record.update(changes)
    common.write_object(root / ('logs/btc_monitor/status.json' if btc_status else 'logs/monitor/status.json'), record)
    return record


def request(root, identifier='btc-app-test', **changes):
    record = dict(activation_id=identifier, phase='requested', until=UNTIL.isoformat(),
                  requested_at_utc=NOW.isoformat(), source='explicit_local_btc_button',
                  demo_only=True, setup_ids=['B1', 'B4'], risk_percent=1, retry_allowed=False)
    record.update(changes)
    common.write_object(root / f'logs/btc_app_control/{identifier}.json', record)
    common.write_object(root / 'logs/btc_app_control/latest.json', dict(activation_id=identifier))
    return record


@pytest.fixture
def no_awake_thread(monkeypatch):
    class NoThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def join(self, *args, **kwargs):
            pass
    monkeypatch.setattr(app, 'threading', SimpleNamespace(Event=threading.Event, Thread=NoThread))


def test_snapshot_is_read_only_and_never_launches_or_connects(tmp_path, monkeypatch):
    workspace(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('Reading status must not launch or connect to MT5.')
    monkeypatch.setattr(btc.subprocess, 'Popen', forbidden)
    from engine.gold_connector import GoldConnector
    monkeypatch.setattr(GoldConnector, '__enter__', forbidden)
    assert btc.snapshot(tmp_path, now=NOW, alive=lambda pid: False)['can_start']
    status(tmp_path)
    result = btc.snapshot(tmp_path, now=NOW, alive=lambda pid: True)
    assert result['active'] and result['state'] == 'ACTIVE'
    assert not result['can_start']


@pytest.mark.parametrize('changes', [
    {'account': {}}, {'account': {'demo_verified': True, 'trade_mode': 1}},
    {'account': {'demo_verified': True, 'trade_mode': 2}},
    {'account': {'demo_verified': True, 'trade_mode': False}},
    {'account': {'demo_verified': False, 'trade_mode': 0}},
    {'mode': 'real_trade'}, {'status': 'failed'},
    {'deadline_utc': NOW.isoformat()}, {'deadline_utc': None},
    {'deadline_utc': 'unverified'}, {'deadline_utc': (NOW + timedelta(hours=25)).isoformat()},
    {'heartbeat_utc': (NOW - timedelta(seconds=46)).isoformat()},
    {'heartbeat_utc': (NOW + timedelta(seconds=6)).isoformat()},
])
def test_green_requires_fresh_verified_bounded_demo(tmp_path, changes):
    workspace(tmp_path)
    status(tmp_path, **changes)
    result = btc.snapshot(tmp_path, now=NOW, alive=lambda pid: True)
    assert not result['active']
    assert result['can_start'] is (changes.get('status')=='failed')


@pytest.mark.parametrize('payload', ['{partial', '[]', '{"account": []}'])
def test_malformed_btc_status_fails_closed(tmp_path, payload):
    workspace(tmp_path)
    path = tmp_path / 'logs/btc_monitor/status.json'
    path.parent.mkdir(parents=True)
    path.write_text(payload)
    result = btc.snapshot(tmp_path, now=NOW, alive=lambda pid: True)
    assert not result['active'] and not result['can_start']
    assert result['state'] == 'UNVERIFIED'


def test_unknown_liveness_blocks_duplicate_and_cannot_show_green(tmp_path):
    workspace(tmp_path)
    status(tmp_path)
    result = btc.snapshot(tmp_path, now=NOW, alive=lambda pid: None)
    assert result['state'] == 'UNVERIFIED'
    assert not result['active'] and not result['can_start']


def test_btc_and_gold_eur_sessions_are_mutually_exclusive(tmp_path):
    workspace(tmp_path)
    status(tmp_path, btc_status=False)
    btc_state = btc.snapshot(tmp_path, now=NOW, alive=lambda pid: True)
    assert btc_state['other_busy'] and not btc_state['can_start']
    (tmp_path / 'logs/monitor/status.json').unlink()
    status(tmp_path)
    other_state = common.snapshot(tmp_path, now=NOW, alive=lambda pid: True)
    assert not other_state['can_start']
    assert 'BTC' in other_state['reason']


def test_corrupt_other_market_status_also_blocks_btc(tmp_path):
    workspace(tmp_path)
    path = tmp_path / 'logs/monitor/status.json'
    path.parent.mkdir(parents=True)
    path.write_text('{partial')
    result = btc.snapshot(tmp_path, now=NOW, alive=lambda pid: False)
    assert not result['can_start']
    assert 'verif' in result['reason'].lower()


def test_starting_btc_request_also_blocks_gold_eur(tmp_path):
    workspace(tmp_path)
    request(tmp_path, launcher_pid=12345)
    result = common.snapshot(tmp_path, now=NOW, alive=lambda pid: True)
    assert not result['can_start']
    assert 'BTC' in result['reason']


def test_starting_gold_eur_request_also_blocks_btc(tmp_path):
    workspace(tmp_path)
    common.write_object(tmp_path / 'logs/app_control/latest.json', dict(activation_id='app-test'))
    common.write_object(tmp_path / 'logs/app_control/app-test.json',
        dict(activation_id='app-test', phase='requested', requested_at_utc=NOW.isoformat(),
             source='explicit_local_app_button', demo_only=True, until=UNTIL.isoformat()))
    result = btc.snapshot(tmp_path, now=NOW, alive=lambda pid: True)
    assert not result['can_start'] and result['other_busy']


def test_explicit_click_writes_exact_scope_and_launches_once(tmp_path, monkeypatch):
    workspace(tmp_path)
    calls = []
    monkeypatch.setenv('PYTHONPATH', 'frozen/ui/packages')
    monkeypatch.setenv('PROJECT_QUANT_CODE_ROOT', 'frozen/ui/source')
    monkeypatch.setattr(btc.subprocess, 'Popen', lambda *args, **kwargs:
                        calls.append((args[0], kwargs)) or SimpleNamespace(pid=os.getpid()))
    record = btc.request_start(tmp_path, UNTIL, setup_ids=('B4',), now=NOW)
    assert record['demo_only'] is True and record['risk_percent'] == 1
    assert record['retry_allowed'] is False and record['setup_ids'] == ['B4']
    assert record['source'] == 'explicit_local_btc_button'
    assert len(calls) == 1
    command, options = calls[0]
    assert command[0] == str(tmp_path / '.venv/Scripts/python.exe')
    assert command[-1] == UNTIL.isoformat()
    assert command[command.index('--activation-id') + 1] == record['activation_id']
    assert 'PYTHONPATH' not in options['env'] and 'PROJECT_QUANT_CODE_ROOT' not in options['env']
    assert not btc.snapshot(tmp_path, now=NOW)['active']
    with pytest.raises(RuntimeError):
        btc.request_start(tmp_path, UNTIL, now=NOW)
    assert len(calls) == 1


@pytest.mark.parametrize('until', [NOW, NOW - timedelta(seconds=1), NOW + timedelta(hours=25),
                                  NOW.replace(tzinfo=None)])
def test_invalid_deadline_never_launches(tmp_path, monkeypatch, until):
    monkeypatch.setattr(btc.subprocess, 'Popen', lambda *a, **kw: pytest.fail('No launch expected.'))
    with pytest.raises(ValueError):
        btc.request_start(tmp_path, until, now=NOW)


@pytest.mark.parametrize('setups', [(), ('B5',), ('B1', 'B1'), ('R1',), ('B1', 'EURUSDm')])
def test_invalid_setup_selection_never_launches(tmp_path, monkeypatch, setups):
    monkeypatch.setattr(btc.subprocess, 'Popen', lambda *a, **kw: pytest.fail('No launch expected.'))
    with pytest.raises(ValueError):
        btc.request_start(tmp_path, UNTIL, setup_ids=setups, now=NOW)


def test_launch_failure_recorded_without_retry(tmp_path, monkeypatch):
    workspace(tmp_path)
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise OSError('synthetic launch failure')
    monkeypatch.setattr(btc.subprocess, 'Popen', fail)
    with pytest.raises(RuntimeError, match='could not launch'):
        btc.request_start(tmp_path, UNTIL, now=NOW)
    record = btc.request_record(tmp_path)
    assert calls == [1] and record['phase'] == 'failed'
    assert record['retry_allowed'] is False


def test_stop_is_exact_run_scoped_and_does_not_close_positions(tmp_path, monkeypatch):
    workspace(tmp_path)
    monkeypatch.setattr(common, 'now_utc', lambda: NOW)
    status(tmp_path)
    monkeypatch.setattr(btc, 'process_alive', lambda pid: True)
    with pytest.raises(RuntimeError, match='changed'):
        btc.request_stop(tmp_path, 'wrong')
    result = btc.request_stop(tmp_path, 'c' * 32)
    assert result == dict(status='stop_requested', positions_closed=0)
    assert common.read_object(tmp_path / 'logs/btc_monitor/stop.json')['run_id'] == 'c' * 32
    assert not btc.snapshot(tmp_path, now=NOW, alive=lambda pid: True)['active']


def test_worker_consumes_one_explicit_request_once(tmp_path, no_awake_thread):
    request(tmp_path)
    calls = []
    def start(root, identifier, **kwargs):
        assert btc.request_record(root)['phase'] == 'checking'
        calls.append((root, identifier, kwargs))
        return dict(status='completed', orders_sent=0)
    result = app.run_session(tmp_path, 'btc-app-test', UNTIL.isoformat(), start=start)
    assert result['orders_sent'] == 0 and len(calls) == 1
    assert calls[0][2] == dict(until=UNTIL.isoformat(), setup_ids=('B1', 'B4'))
    assert btc.request_record(tmp_path)['phase'] == 'finished'
    with pytest.raises(ValueError, match='unused'):
        app.run_session(tmp_path, 'btc-app-test', UNTIL.isoformat(), start=start)
    assert len(calls) == 1


def test_duplicate_workers_atomically_claim_one_request(tmp_path, monkeypatch, no_awake_thread):
    request(tmp_path)
    calls, errors = [], []
    rendezvous = threading.Barrier(2)
    original_read = app.read_object
    def delayed_read(path):
        record = original_read(path)
        if Path(path).name == 'btc-app-test.json' and record.get('phase') == 'requested':
            # Widen the read-to-claim race. A shared lock must cover both the
            # read and phase transition, so the other worker cannot also win.
            time.sleep(.05)
        return record
    monkeypatch.setattr(app, 'read_object', delayed_read)
    def start(*args, **kwargs):
        calls.append(1)
        return dict(status='completed', orders_sent=0)
    def worker():
        try:
            rendezvous.wait(timeout=3)
            app.run_session(tmp_path, 'btc-app-test', UNTIL.isoformat(), start=start)
        except BaseException as exc:
            errors.append(exc)
    workers = [threading.Thread(target=worker) for _ in range(2)]
    for child in workers:
        child.start()
    for child in workers:
        child.join(timeout=6)
    assert not any(child.is_alive() for child in workers)
    assert calls == [1]
    assert len(errors) == 1 and isinstance(errors[0], ValueError)
    assert btc.request_record(tmp_path)['phase'] == 'finished'


@pytest.mark.parametrize('changes', [
    {'demo_only': False}, {'source': 'not_a_user_click'}, {'phase': 'finished'},
    {'until': (UNTIL + timedelta(hours=1)).isoformat()}, {'risk_percent': 2},
    {'risk_percent': True}, {'setup_ids': []}, {'setup_ids': ['B1', 'B1']},
    {'setup_ids': ['B1', 'R1']}, {'setup_ids': 'B1'},
])
def test_worker_rejects_modified_or_reused_request_scope(tmp_path, no_awake_thread, changes):
    request(tmp_path, **changes)
    with pytest.raises((ValueError, TypeError)):
        app.run_session(tmp_path, 'btc-app-test', UNTIL.isoformat(),
                        start=lambda *a, **kw: pytest.fail('No native start permitted.'))


def test_worker_failure_is_durable_and_never_retried(tmp_path, no_awake_thread):
    request(tmp_path)
    calls = []
    def reject(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('Cannot verify DEMO account.')
    with pytest.raises(RuntimeError, match='DEMO'):
        app.run_session(tmp_path, 'btc-app-test', UNTIL.isoformat(), start=reject)
    assert btc.request_record(tmp_path)['phase'] == 'failed'
    with pytest.raises(ValueError, match='unused'):
        app.run_session(tmp_path, 'btc-app-test', UNTIL.isoformat(), start=reject)
    assert calls == [1]


def reviewed_plan(root):
    pins = {}
    for name in ('engine/btc_demo.py', 'engine/execution_state.py', 'analytics/btc_setups.py', 'analytics/candidate_signals.py',
                 'scripts/activate_btc_demo.py', 'scripts/app_btc_session.py', 'dashboard/btc_control.py'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('synthetic reviewed source\n')
        pins[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    plan = dict(version=1, demo_only=True, symbol='BTCUSDm', mode='btc_demo_series', risk_percent=1,
        maximum_open_positions=1, maximum_lots=.1, maximum_session_hours=24, reward_r=2,
        slippage_price=2, fee_reserve_fraction=.05, account_mode='exclusive_btc',
        setup_ids=['B1', 'B2', 'B3', 'B4'], source_sha256=pins)
    common.write_object(root / activation.PLAN, plan)
    return plan


def test_btc_verifier_checks_existing_policy_and_exact_source_bytes(tmp_path, monkeypatch):
    plan = reviewed_plan(tmp_path)
    from engine import activation_demo
    calls = []
    monkeypatch.setattr(activation_demo, 'verify_sources', lambda root: calls.append(root))
    assert activation.verify_sources(tmp_path) == plan
    assert calls == [tmp_path.resolve()]
    (tmp_path / 'analytics/btc_setups.py').write_text('changed rule')
    with pytest.raises(ValueError, match='source changed'):
        activation.verify_sources(tmp_path)


@pytest.mark.parametrize('changes', [
    {'demo_only': False}, {'symbol': 'BTCUSDT'}, {'risk_percent': 2}, {'risk_percent': True},
    {'maximum_open_positions': 2}, {'maximum_lots': .2}, {'maximum_session_hours': 25},
    {'reward_r': 3}, {'slippage_price': 0}, {'fee_reserve_fraction': 0.},
    {'account_mode': 'shared'}, {'setup_ids': ['B1']}, {'version': True},
])
def test_plan_cannot_silently_expand_reviewed_scope(tmp_path, monkeypatch, changes):
    plan = reviewed_plan(tmp_path)
    plan.update(changes)
    common.write_object(tmp_path / activation.PLAN, plan)
    from engine import activation_demo
    monkeypatch.setattr(activation_demo, 'verify_sources', lambda root: None)
    with pytest.raises(ValueError, match='reviewed DEMO scope'):
        activation.verify_sources(tmp_path)


def test_missing_source_review_and_escape_paths_fail_closed(tmp_path, monkeypatch):
    plan = reviewed_plan(tmp_path)
    from engine import activation_demo
    monkeypatch.setattr(activation_demo, 'verify_sources', lambda root: None)
    del plan['source_sha256']['analytics/btc_setups.py']
    common.write_object(tmp_path / activation.PLAN, plan)
    with pytest.raises(ValueError, match='not finalized'):
        activation.verify_sources(tmp_path)
    plan = reviewed_plan(tmp_path)
    plan['source_sha256']['../outside.py'] = 'f' * 64
    common.write_object(tmp_path / activation.PLAN, plan)
    with pytest.raises(ValueError, match='source changed'):
        activation.verify_sources(tmp_path)
