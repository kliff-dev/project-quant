"""Offline monitor lifecycle tests; no terminal or network required."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace as Record
from unittest.mock import Mock

import pytest

from engine import market_monitor as module
from engine.execution_audit import atomic_json
from engine.market_monitor import MarketMonitor, MonitorConfig, monitor_lock, read_status, request_stop
from engine.mt5_connector import DemoAccountRequiredError, MT5ConnectionError
from engine.strategy_orders import StrategyIntent, StrategyPolicy


class Clock:
    def __init__(self):
        self.current = datetime(2026, 11, 13, 8, 0, 30, tzinfo=timezone.utc)
        self.seconds = 0
        self.after_sleep = lambda: None

    def now(self):
        return self.current

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)
        self.seconds += seconds

    def sleep(self, seconds):
        self.advance(seconds)
        self.after_sleep()


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    clock = Clock()
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps(asdict(StrategyPolicy())), encoding='utf-8')
    account = Record(login=12345, server='Broker-Demo', currency='USD', balance=499.75, equity=499.75)
    api = Record(account_info=Mock(return_value=account), positions_get=Mock(return_value=()),
                 orders_get=Mock(return_value=()), order_send=Mock(), order_check=Mock(), TIMEFRAME_H1=16385)

    def rates(*args):
        hour = int(clock.now().replace(minute=0, second=0, microsecond=0).timestamp())
        return [{'time': hour - 7200}, {'time': hour - 3600}, {'time': hour}]

    api.copy_rates_from_pos = Mock(side_effect=rates)
    snapshot = dict(account=dict(broker='Test Broker', server='Broker-Demo', currency='USD',
                                account_type='DEMO', demo_verified=True,
                                account_trade_allowed=True, account_expert_allowed=True),
                    quote=dict(symbol='EURUSDm', bid=1.1, ask=1.10008, spread_pips=.8, age_seconds=0),
                    permissions=dict(terminal_trade_allowed=True, external_python_trading_disabled=False))
    connector = Record(terminal_api=api, terminal_path=tmp_path / 'terminal64.exe', account_identity=(12345, 'Broker-Demo'),
                       inspect=Mock(return_value=snapshot), verify_demo_account=Mock())

    def decision(*args):
        close = clock.now().replace(minute=0, second=0, microsecond=0).isoformat()
        return None, dict(status='WAIT', reason='No completed-bar signal.', decision_time_utc=close, orders_sent=0)

    evaluate = Mock(side_effect=decision)
    monkeypatch.setattr(module, 'latest_strategy_intent', evaluate)

    def make(config=None):
        return MarketMonitor(connector, tmp_path / 'logs', policy, config=config or MonitorConfig(.01, 10),
                             report_path=tmp_path / 'report.md', now=clock.now,
                             monotonic=lambda: clock.seconds, sleep=clock.sleep, emit=lambda _: None)

    return Record(clock=clock, policy=policy, api=api, snapshot=snapshot, connector=connector,
                  evaluate=evaluate, make=make, directory=tmp_path / 'logs', tmp_path=tmp_path)


def test_hours_of_polling_evaluate_each_closed_candle_once_without_sends(watcher):
    report = watcher.make(MonitorConfig(2, 60)).run()
    assert report['status'] == 'stopped'
    assert report['poll_count'] == 120
    assert watcher.evaluate.call_count == 2  # 08:00 and 09:00; last poll is 09:59:30.
    assert watcher.api.copy_rates_from_pos.call_count == 120
    watcher.api.order_check.assert_not_called()
    watcher.api.order_send.assert_not_called()
    assert 'Session deadline' in report['stop_reason']


def test_restart_does_not_repeat_candle_or_preview(watcher):
    watcher.make().run(once=True)
    second = watcher.make().run(once=True)
    assert second['total_candles_evaluated'] == 1
    watcher.evaluate.assert_called_once()
    watcher.api.order_send.assert_not_called()


def test_crash_reservation_prevents_recalculation_on_restart(watcher):
    watcher.evaluate.side_effect = OSError('disk failure')
    assert watcher.make().run(once=True)['status'] == 'failed'
    watcher.evaluate.reset_mock()
    result = watcher.make().run(once=True)
    assert result['status'] == 'stopped'
    assert result['latest_decision']['status'] == 'SKIPPED'
    watcher.evaluate.assert_not_called()


def test_new_candle_is_seen_after_restart(watcher):
    watcher.make().run(once=True)
    watcher.clock.advance(3600)
    report = watcher.make().run(once=True)
    assert report['total_candles_evaluated'] == 2
    assert watcher.evaluate.call_count == 2


def test_stop_request_is_acknowledged_and_never_closes_positions(watcher):
    watcher.clock.after_sleep = lambda: request_stop(watcher.directory)
    report = watcher.make().run()
    assert report['status'] == 'stopped' and report['poll_count'] == 1
    assert report['stop_reason'] == 'User requested stop.'
    watcher.api.order_send.assert_not_called()


def test_old_stop_request_does_not_stop_a_new_run(watcher):
    atomic_json(watcher.directory / 'monitor/stop.json', {'run_id': 'old-run'})
    assert watcher.make().run(once=True)['poll_count'] == 1


@pytest.mark.parametrize('problem', ['real', 'contest', 'disconnect', 'funds', 'inventory', 'identity', 'policy', 'clock'])
def test_health_failure_stops_without_retrying_or_sending(watcher, problem):
    def change():
        if problem in ('real', 'contest'):
            watcher.connector.inspect.side_effect = DemoAccountRequiredError(problem.upper())
        elif problem == 'disconnect':
            watcher.connector.inspect.side_effect = MT5ConnectionError('Disconnected')
        elif problem == 'funds':
            watcher.api.account_info.return_value = None
        elif problem == 'inventory':
            watcher.api.positions_get.return_value = None
        elif problem == 'identity':
            watcher.connector.account_identity = (67890, 'Other-Demo')
        elif problem == 'policy':
            policy = asdict(StrategyPolicy())
            policy['risk_percent'] = .5
            watcher.policy.write_text(json.dumps(policy), encoding='utf-8')
        elif problem == 'clock':
            watcher.clock.current -= timedelta(minutes=1)
    watcher.clock.after_sleep = change
    report = watcher.make().run()
    assert report['status'] == 'failed'
    assert report['poll_count'] == 1
    watcher.api.order_send.assert_not_called()


@pytest.mark.parametrize('bars', [None, [], [{'time': 0}, {'time': 0}], [{'time': 1}],
                                  [{'time': 3600}, {'time': 0}], [{'time': 4102444800}]])
def test_invalid_bar_probe_stops_before_strategy_evaluation(watcher, bars):
    watcher.api.copy_rates_from_pos.side_effect = None
    watcher.api.copy_rates_from_pos.return_value = bars
    assert watcher.make().run(once=True)['status'] == 'failed'
    watcher.evaluate.assert_not_called()
    watcher.api.order_send.assert_not_called()


def ready_signal(watcher):
    close = watcher.clock.now().replace(minute=0, second=0, microsecond=0).isoformat()
    intent = StrategyIntent('EURUSDm', 'buy', 1.098, .001, close, 'a' * 64, 'pullback')
    watcher.evaluate.side_effect = lambda *args: (intent, dict(status='signal', decision_time_utc=close, orders_sent=0))
    return intent


@pytest.mark.parametrize('blocker', ['position', 'pending', 'permission', 'external_api', 'expert', 'spread', 'stale'])
def test_blocked_signal_is_logged_without_even_checking_an_order(watcher, monkeypatch, blocker):
    ready_signal(watcher)
    if blocker == 'position': watcher.api.positions_get.return_value = (Record(ticket=1),)
    if blocker == 'pending': watcher.api.orders_get.return_value = (Record(ticket=1),)
    if blocker == 'permission': watcher.snapshot['permissions']['terminal_trade_allowed'] = False
    if blocker == 'external_api': watcher.snapshot['permissions']['external_python_trading_disabled'] = True
    if blocker == 'expert': watcher.snapshot['account']['account_expert_allowed'] = False
    if blocker == 'spread': watcher.snapshot['quote']['spread_pips'] = 3
    if blocker == 'stale': watcher.snapshot['quote']['age_seconds'] = 121
    executor = Mock()
    monkeypatch.setattr(module, 'DemoExecutor', executor)
    result = watcher.make().run(once=True)
    assert result['latest_decision']['status'] == 'BLOCKED'
    executor.assert_not_called()
    watcher.api.order_send.assert_not_called()


def test_signal_creates_one_reviewable_preview_then_expires_without_submission(watcher, monkeypatch):
    intent = ready_signal(watcher)
    preview = dict(ready=True, preview_id='review-me', created_at_utc=watcher.clock.now().isoformat(),
                   blockers=[], account_identity=dict(login=12345, server='Broker-Demo'),
                   volume_lots=.02, protection={'risk_budget': 4.99})
    executor = Mock(preview_path=watcher.tmp_path / 'preview.json')
    executor.prepare_entry.return_value = preview
    factory = Mock(return_value=executor)
    monkeypatch.setattr(module, 'DemoExecutor', factory)
    result = watcher.make(MonitorConfig(.1, 60)).run()
    factory.assert_called_once()
    assert factory.call_args.kwargs['strategy_intent'] == intent
    executor.prepare_entry.assert_called_once()
    executor.open_test.assert_not_called()
    executor.close_test.assert_not_called()
    assert result['preview_status'].startswith('expired')
    assert result['latest_decision']['preview_id'] == 'review-me'
    assert '--strategy --open' in result['latest_decision']['manual_open_command']
    assert 'monitor only' in (watcher.tmp_path / 'report.md').read_text(encoding='utf-8')
    watcher.api.order_send.assert_not_called()


def test_broker_rejecting_preview_is_not_retried(watcher, monkeypatch):
    ready_signal(watcher)
    executor = Mock()
    executor.prepare_entry.side_effect = ValueError('Minimum lot exceeds budget')
    monkeypatch.setattr(module, 'DemoExecutor', Mock(return_value=executor))
    result = watcher.make().run()
    assert result['status'] == 'stopped'
    assert result['latest_decision']['status'] == 'BLOCKED'
    executor.prepare_entry.assert_called_once()
    executor.open_test.assert_not_called()


def test_history_race_discards_candidate(watcher):
    ready_signal(watcher)
    watcher.evaluate.side_effect = lambda *args: (None, {'decision_time_utc': '2026-11-13T07:00:00+00:00'})
    result = watcher.make().run(once=True)
    assert result['status'] == 'failed' and 'History changed' in result['stop_reason']
    watcher.api.order_check.assert_not_called()


def test_account_switch_during_history_read_prevents_preview(watcher, monkeypatch):
    intent = ready_signal(watcher)
    def switch(*args):
        watcher.connector.account_identity = (67890, 'Other-Demo')
        return intent, dict(status='signal', decision_time_utc=intent.decision_time_utc)
    watcher.evaluate.side_effect = switch
    executor = Mock()
    monkeypatch.setattr(module, 'DemoExecutor', executor)
    assert watcher.make().run(once=True)['status'] == 'failed'
    executor.assert_not_called()


def test_unreadable_checkpoint_fails_closed(watcher):
    path = watcher.directory / 'monitor/state.json'
    path.parent.mkdir(parents=True)
    path.write_text('invalid json', encoding='utf-8')
    assert watcher.make().run(once=True)['status'] == 'failed'
    watcher.evaluate.assert_not_called()


def test_status_does_not_claim_old_heartbeat_is_a_live_process(watcher):
    def observe():
        fresh = read_status(watcher.directory, now=watcher.clock.now())
        assert fresh['status_observation'] == 'recent_heartbeat'
        stale = read_status(watcher.directory, now=watcher.clock.now() + timedelta(minutes=5))
        assert stale['status_observation'] == 'stale_or_overdue'
        request_stop(watcher.directory)
    watcher.clock.after_sleep = observe
    watcher.make().run()
    assert read_status(watcher.directory)['status'] == 'stopped'


def test_os_lock_excludes_second_runner_and_releases_after_error(tmp_path):
    path = tmp_path / 'monitor.lock'
    with pytest.raises(ValueError):
        with monitor_lock(path):
            with pytest.raises(RuntimeError, match='Another market monitor'):
                with monitor_lock(path):
                    pytest.fail('Second runner acquired lock')
            raise ValueError('Simulated crash unwinding')
    with monitor_lock(path):
        assert path.exists()  # OS lock lifetime, not pathname existence, controls access.


@pytest.mark.parametrize('values', [{'hours': 0}, {'hours': 25}, {'hours': True},
                                    {'hours': float('nan')}, {'poll_seconds': 0}, {'poll_seconds': 61}])
def test_invalid_session_bounds(values):
    with pytest.raises(ValueError): MonitorConfig(**values)


def test_cli_status_and_stop_do_not_connect_to_mt5(tmp_path, monkeypatch, capsys):
    from scripts import monitor_market as cli
    connector = Mock(side_effect=AssertionError('Read-only status must not initialize MT5'))
    monkeypatch.setattr(cli, 'MT5Connector', connector)
    assert cli.main(['--status', '--log-dir', str(tmp_path)]) == 0
    assert 'not_started' in capsys.readouterr().out
    assert cli.main(['--stop', '--log-dir', str(tmp_path)]) == 0
    connector.assert_not_called()


def test_lock_is_released_even_after_abrupt_process_exit(tmp_path):
    import subprocess
    import sys
    path = tmp_path / 'monitor.lock'
    source = ('import os, sys\nfrom pathlib import Path\n'
              'from engine.market_monitor import monitor_lock\n'
              'with monitor_lock(Path(sys.argv[1])):\n    os._exit(7)\n')
    result = subprocess.run([sys.executable, '-B', '-c', source, str(path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 7, result.stderr
    with monitor_lock(path):
        assert path.exists()


def test_launcher_run_identity_is_published_in_heartbeat(watcher):
    monitor = watcher.make()
    launch_id = 'b' * 32
    # Exercise the actual constructor argument used by the Windows launcher.
    launched = MarketMonitor(watcher.connector, watcher.directory, watcher.policy,
                             run_id=launch_id, now=watcher.clock.now, emit=lambda _: None)
    result = launched.run(once=True)
    assert result['run_id'] == launch_id
    assert read_status(watcher.directory)['run_id'] == launch_id
    assert monitor.run_id != launch_id


@pytest.mark.parametrize('value', ['', 'not-a-uuid', 'A' * 32, 'a' * 31])
def test_invalid_launcher_identity_is_rejected(watcher, value):
    with pytest.raises(ValueError):
        MarketMonitor(watcher.connector, watcher.directory, watcher.policy, run_id=value)
