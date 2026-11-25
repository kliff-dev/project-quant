"""Control/authority tests use synthetic status and fake launches, never MT5."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from dashboard import monitor_control as control
from dashboard.research_model import process_alive
from desktop.runtime import OwnedJob
from scripts.app_monitor_session import run_session

NOW = datetime(2026, 11, 17, 5, tzinfo=timezone.utc)
UNTIL = NOW+timedelta(hours=9)


def status(root, **changes):
    item = dict(status='running', mode=control.MODE, entry_timing='user_activation_v1',
                run_id='a'*32, pid=os.getpid(), heartbeat_utc=NOW.isoformat(),
                deadline_utc=UNTIL.isoformat(), account=dict(demo_verified=True, trade_mode=0))
    item.update(changes)
    control.write_object(root/'logs/monitor/status.json', item)
    return item


def execution_workspace(root):
    for name in ('.venv/Scripts/python.exe', 'scripts/app_monitor_session.py',
                 'scripts/activate_demo.py', 'config/activation_demo_plan.json'):
        target = root/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('fixture')


def request(root, identifier='app-test', **changes):
    item = dict(activation_id=identifier, phase='requested', until=UNTIL.isoformat(),
                requested_at_utc=NOW.isoformat(), source='explicit_local_app_button', demo_only=True,
                baseline_risk_percent=1, conditional_ceiling_percent=2, combined_ceiling_percent=8,
                maximum_positions_per_symbol=2, maximum_open_positions_account_wide=4,
                portfolio_scope='gold_eurusd_two_each_8pct_v1')
    item.update(changes)
    control.write_object(root/'logs/app_control'/f'{identifier}.json', item)
    control.write_object(root/'logs/app_control/latest.json', dict(activation_id=identifier))
    return item


def test_green_requires_verified_live_bounded_demo_and_no_stop(tmp_path):
    status(tmp_path)
    assert control.snapshot(tmp_path, now=NOW)['active']
    control.write_object(tmp_path/'logs/monitor/stop.json', dict(run_id='a'*32))
    state = control.snapshot(tmp_path, now=NOW)
    assert not state['active'] and not state['can_start'] and not state['can_stop']
    assert 'Stop requested' in state['reason']


@pytest.mark.parametrize('changes', [
    {'heartbeat_utc': (NOW-timedelta(seconds=46)).isoformat()},
    {'heartbeat_utc': (NOW+timedelta(seconds=6)).isoformat()},
    {'deadline_utc': NOW.isoformat()}, {'deadline_utc': None}, {'deadline_utc': 'bad'},
    {'status': 'failed'}, {'mode': 'other'}, {'entry_timing': 'other'},
    {'account': {'demo_verified': True, 'trade_mode': 2}},
    {'account': {'demo_verified': True, 'trade_mode': 1}},
    {'account': {'demo_verified': True, 'trade_mode': False}},
    {'account': {}},
])
def test_inactive_for_expired_stale_unknown_real_or_contest(tmp_path, changes):
    status(tmp_path, **changes)
    state = control.snapshot(tmp_path, now=NOW)
    assert not state['active'] and not state['can_start']


def test_dead_process_and_revoked_authority_are_red(tmp_path):
    status(tmp_path)
    assert not control.snapshot(tmp_path, now=NOW, alive=lambda pid: False)['active']
    status(tmp_path, authorizations={'EURUSDm': {'authorization_id': 'b'*32}})
    control.write_object(tmp_path/f"logs/authorizations/{'b'*32}.revoked.json", {})
    assert not control.snapshot(tmp_path, now=NOW)['active']


def test_corrupt_status_is_fail_closed_and_missing_setup_explained(tmp_path):
    assert not control.snapshot(tmp_path, now=NOW)['active']
    execution_workspace(tmp_path)
    path = tmp_path/'logs/monitor/status.json'
    path.parent.mkdir(parents=True)
    path.write_text('{partial')
    result = control.snapshot(tmp_path, now=NOW)
    assert not result['can_start'] and 'cannot be verified' in result['reason']


def test_click_launches_once_with_exact_deadline_and_independent_process(tmp_path, monkeypatch):
    execution_workspace(tmp_path)
    calls = []
    monkeypatch.setenv('PYTHONPATH', 'frozen/ui/packages')
    monkeypatch.setenv('PROJECT_QUANT_CODE_ROOT', 'frozen/ui/source')
    monkeypatch.setattr(control.subprocess, 'Popen', lambda *args, **kwargs:
                        calls.append((args, kwargs)) or SimpleNamespace(pid=os.getpid()))
    record = control.request_start(tmp_path, UNTIL, now=NOW)
    command, options = calls[0][0][0], calls[0][1]
    assert command[-1] == UNTIL.isoformat() and '--activation-id' in command
    assert command[0] == str(tmp_path/'.venv/Scripts/python.exe')
    assert 'PYTHONPATH' not in options['env'] and 'PROJECT_QUANT_CODE_ROOT' not in options['env']
    if os.name == 'nt':
        assert options['creationflags'] & subprocess.CREATE_BREAKAWAY_FROM_JOB
    assert control.load_request(tmp_path)['activation_id'] == record['activation_id']
    assert (record['maximum_positions_per_symbol'],record['maximum_open_positions_account_wide'],record['combined_ceiling_percent'])==(2,4,8)
    assert not control.snapshot(tmp_path, now=NOW)['active']  # Popen alone is not active.
    with pytest.raises(RuntimeError):
        control.request_start(tmp_path, UNTIL, now=NOW)
    assert len(calls) == 1


@pytest.mark.parametrize('until', [NOW, NOW-timedelta(seconds=1), NOW+timedelta(hours=25),
                                  datetime(2026, 11, 17, 22)])
def test_no_launch_for_invalid_or_unbounded_deadline(tmp_path, monkeypatch, until):
    monkeypatch.setattr(control.subprocess, 'Popen', lambda *a, **kw: pytest.fail('No launch expected'))
    with pytest.raises(ValueError):
        control.request_start(tmp_path, until, now=NOW)


def test_launch_failure_is_recorded_and_never_retried(tmp_path, monkeypatch):
    execution_workspace(tmp_path)
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise OSError('cannot break away')
    monkeypatch.setattr(control.subprocess, 'Popen', fail)
    with pytest.raises(RuntimeError, match='Could not start'):
        control.request_start(tmp_path, UNTIL, now=NOW)
    assert len(calls) == 1 and control.load_request(tmp_path)['phase'] == 'failed'


def test_stop_uses_existing_entry_revocation_command_and_run_identity(tmp_path, monkeypatch):
    execution_workspace(tmp_path)
    status(tmp_path, heartbeat_utc=datetime.now(timezone.utc).isoformat(),
           deadline_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat())
    calls = []
    monkeypatch.setattr(control.subprocess, 'run', lambda *a, **kw:
        calls.append(a[0]) or SimpleNamespace(returncode=0, stdout=json.dumps(
            dict(status='stop_requested', positions_closed=0))))
    with pytest.raises(RuntimeError, match='state changed'):
        control.request_stop(tmp_path, 'wrong')
    result = control.request_stop(tmp_path, 'a'*32)
    assert result['positions_closed'] == 0 and len(calls) == 1 and calls[0][-1] == '--stop'


def test_worker_records_rejected_native_preflight_and_does_not_retry(tmp_path, monkeypatch):
    request(tmp_path)
    calls = []
    def reject(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('DEMO verification rejected REAL account')
    monkeypatch.setattr(control.subprocess, 'Popen', lambda *a, **kw: pytest.fail('No helper expected'))
    with pytest.raises(RuntimeError, match='REAL'):
        run_session(tmp_path, 'app-test', UNTIL.isoformat(), start=reject)
    assert calls == [1] and control.load_request(tmp_path)['phase'] == 'failed'
    with pytest.raises(ValueError, match='unused'):
        run_session(tmp_path, 'app-test', UNTIL.isoformat(), start=reject)
    assert calls == [1]


@pytest.mark.parametrize('changes',[{'combined_ceiling_percent':9},{'maximum_positions_per_symbol':3},
    {'maximum_open_positions_account_wide':5},{'portfolio_scope':'gold_eurusd_one_each_v1'},
    {'baseline_risk_percent':True},{'conditional_ceiling_percent':3}])
def test_worker_refuses_changed_or_old_request_limits_before_native_access(tmp_path,changes):
    from unittest.mock import Mock
    request(tmp_path,**changes)
    native=Mock()
    with pytest.raises(ValueError,match='reviewed portfolio limits'):
        run_session(tmp_path,'app-test',UNTIL.isoformat(),start=native)
    native.assert_not_called()


def test_control_ui_click_and_status_transitions_without_broker(tmp_path, monkeypatch):
    state = dict(active=False, reason='Stopped', can_start=True, can_stop=False,
                 status={}, capability={'available': True})
    monkeypatch.setattr(control, 'snapshot', lambda root: state)
    calls = []
    def start(root, until):
        calls.append(('start', root, until))
        state.update(active=True, can_start=False, can_stop=True, reason='Watching for setups',
                     status={'run_id': 'a'*32, 'deadline_utc': until.isoformat()})
    def stop(root, run):
        calls.append(('stop', root, run))
        state.update(active=False, can_start=False, can_stop=False, reason='Stop requested')
    monkeypatch.setattr(control, 'request_start', start)
    monkeypatch.setattr(control, 'request_stop', stop)
    app = AppTest.from_string(f"from pathlib import Path\nfrom dashboard.trading_controls import trading_controls\ntrading_controls(Path({str(tmp_path)!r}))").run()
    assert not app.exception and '🔴' in app.markdown[0].value
    app.button(key='activate_demo').click().run()
    assert not app.exception and '🟢' in app.markdown[0].value
    assert app.button(key='activate_demo').disabled and not app.button(key='stop_demo').disabled
    assert calls[0][2].utcoffset() == timedelta(hours=8)
    app.run()
    assert len(calls) == 1  # Page reruns do not activate again.
    app.button(key='stop_demo').click().run()
    assert not app.exception and '🔴' in app.markdown[0].value and calls[-1][0] == 'stop'


def test_default_end_is_ten_pm_in_manila_with_future_date():
    assert control.default_end(NOW).isoformat() == '2026-11-17T22:00:00+08:00'
    assert control.default_end(datetime(2026, 11, 17, 15, tzinfo=timezone.utc)).isoformat() == '2026-11-18T22:00:00+08:00'
    edge = datetime(2026, 11, 17, 13, 59, 59, tzinfo=timezone.utc)
    assert 3.6 < (control.default_end(edge)-edge).total_seconds() <= 86400


@pytest.mark.skipif(os.name != 'nt', reason='Windows frozen-app DLL isolation')
def test_frozen_child_uses_standard_dll_search_and_restores_parent(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetDllDirectoryW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    api.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
    original = ctypes.create_unicode_buffer(32768)
    api.GetDllDirectoryW(len(original), original)
    bundle = tmp_path/'bundle'
    bundle.mkdir()
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(bundle), raising=False)
    monkeypatch.setenv('PATH', os.pathsep.join([str(bundle), str(bundle/'bin'), str(tmp_path/'external')]))
    try:
        assert api.SetDllDirectoryW(str(bundle))
        with pytest.raises(RuntimeError, match='probe'):
            with control.external_dll_search():
                current = ctypes.create_unicode_buffer(32768)
                api.GetDllDirectoryW(len(current), current)
                assert current.value == ''
                assert control.clean_environment()['PATH'] == str(tmp_path/'external')
                raise RuntimeError('probe')
        restored = ctypes.create_unicode_buffer(32768)
        api.GetDllDirectoryW(len(restored), restored)
        assert restored.value == str(bundle)
    finally:
        api.SetDllDirectoryW(original.value or None)


@pytest.mark.skipif(os.name != 'nt', reason='Windows job breakaway lifecycle')
def test_independent_worker_survives_app_job_close(tmp_path):
    """Actual OS lifecycle with a harmless sleeper, no trading modules."""
    ready, child_pid, stop = [tmp_path/name for name in ('ready', 'child_pid', 'stop')]
    child_code = f"from pathlib import Path; import time; p=Path({str(stop)!r})\nwhile not p.exists(): time.sleep(.05)"
    parent_code = ("import subprocess, time\nfrom pathlib import Path\n"
        f"ready=Path({str(ready)!r})\nwhile not ready.exists(): time.sleep(.02)\n"
        f"p=subprocess.Popen([{getattr(sys, '_base_executable', sys.executable)!r}, '-c', {child_code!r}], "
        "creationflags=subprocess.CREATE_NO_WINDOW|subprocess.CREATE_BREAKAWAY_FROM_JOB)\n"
        f"Path({str(child_pid)!r}).write_text(str(p.pid))\ntime.sleep(30)")
    job = OwnedJob()
    parent = subprocess.Popen([getattr(sys, '_base_executable', sys.executable), '-c', parent_code],
                              creationflags=subprocess.CREATE_NO_WINDOW)
    pid = None
    try:
        job.assign(parent)
        ready.write_text('assigned')
        deadline = time.monotonic()+10
        while not child_pid.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        pid = int(child_pid.read_text())
        job.close()
        parent.wait(timeout=5)
        assert process_alive(pid) is True
    finally:
        job.close()
        stop.write_text('finish test')
        parent.wait(timeout=5)
        if pid:
            deadline = time.monotonic()+5
            while process_alive(pid) and time.monotonic() < deadline:
                time.sleep(.05)
            assert process_alive(pid) is False
