"""Explicit local-app controls for the existing, separately running DEMO engine.

Reading status never connects to MT5 or launches anything. A button request uses
the workspace's Python and reviewed activation entry point, not the frozen UI's
interpreter. The engine still owns every account, order and risk check.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time as clock_time, timedelta, timezone
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo

from dashboard.research_model import monitor_health, process_alive

MANILA = ZoneInfo('Asia/Manila')
MODE = 'dual_demo_series'
REQUEST_ID = re.compile(r'app-[A-Za-z0-9_-]{1,43}')


def now_utc():
    return datetime.now(timezone.utc)


def read_object(path):
    result = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(result, dict):
        raise ValueError('Expected a local JSON object.')
    return result


def write_object(path, value):
    """Publish complete control records, tolerating short Windows reader locks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name+'.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        for delay in (0.01, 0.025, 0.05, 0.1, 0.2, None):
            try:
                temporary.replace(path)
                return
            except OSError as exc:
                if delay is None or getattr(exc, 'winerror', None) not in (5, 32, 33):
                    raise
                time.sleep(delay)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def control_lock(root, *, timeout=0):
    """Serialize clicks across app windows; crashes release this OS-held lock."""
    path = Path(root)/'logs/app_control/control.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        end = time.monotonic()+timeout
        while True:
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= end:
                    raise RuntimeError('Another app control request is being processed.') from exc
                time.sleep(.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def capability(root):
    root = Path(root).resolve()
    required = ['.venv/Scripts/python.exe', 'scripts/app_monitor_session.py',
                'scripts/activate_demo.py', 'config/activation_demo_plan.json']
    missing = [name for name in required if not (root/name).is_file()
               or not (root/name).resolve().is_relative_to(root)]
    return dict(available=not missing and os.name == 'nt',
                reason=('Choose the configured Project Quant execution workspace; missing: '+', '.join(missing)
                        if missing else 'DEMO execution controls require Windows.' if os.name != 'nt' else ''),
                python=str(root/required[0]), worker=str(root/required[1]))


def load_request(root):
    folder = Path(root)/'logs/app_control'
    if not (folder/'latest.json').exists():
        return {}
    identifier = read_object(folder/'latest.json').get('activation_id', '')
    if not isinstance(identifier, str) or not REQUEST_ID.fullmatch(identifier):
        raise ValueError('Invalid saved app activation ID.')
    return read_object(folder/f'{identifier}.json')


def update_request(root, identifier, **changes):
    if not REQUEST_ID.fullmatch(identifier):
        raise ValueError('Invalid app activation ID.')
    with control_lock(root, timeout=5):
        path = Path(root)/'logs/app_control'/f'{identifier}.json'
        record = read_object(path)
        if record.get('activation_id') != identifier:
            raise ValueError('The saved activation request changed.')
        record.update(changes, updated_at_utc=now_utc().isoformat())
        write_object(path, record)
        return record


def monitor_lock_free(root):
    """Probe the native monitor's OS-held lock without deleting or rewriting it.

    A saved Windows PID can belong to an unrelated service after a reboot.
    Only a completed session AND a free execution lock retire that old PID.
    Unknown file access stays unknown and cannot unblock a session.
    """
    path = Path(root)/'logs/monitor/monitor.lock'
    try:
        with path.open('r+b') as stream:
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                return False if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) else None
            try:
                return True
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return True
    except OSError:
        return None


def retired_session(root, status):
    return status.get('status') in ('stopped', 'failed', 'not_started') and monitor_lock_free(root) is True


def snapshot(root, *, now=None, alive=None, check_btc=True):
    """Green means verified entry authority, never merely an open app window."""
    root, current = Path(root), now or now_utc()
    alive = alive or process_alive
    result = dict(active=False, state='INACTIVE', reason='No monitor has been started.',
                  can_start=False, can_stop=False, status={}, request={}, capability=capability(root))
    try:
        status_path = root/'logs/monitor/status.json'
        status = read_object(status_path) if status_path.exists() else {}
        request = load_request(root)
        result.update(status=status, request=request)
        running_pid = alive(status.get('pid')) if status.get('pid') else False
        health = monitor_health(status, now=current, alive=running_pid)
        reason = health['explanation']
        account = status.get('account') or {}
        demo = (account.get('demo_verified') is True and
                type(account.get('trade_mode')) is int and account['trade_mode'] == 0)
        compatible = status.get('mode') == MODE and status.get('entry_timing') == 'user_activation_v1'
        stopped = False
        stop_file = root/'logs/monitor/stop.json'
        if stop_file.exists():
            stopped = read_object(stop_file).get('run_id') == status.get('run_id')
        authorizations = status.get('authorizations') or {}
        for authorization in authorizations.values():
            identifier = authorization.get('authorization_id')
            if isinstance(identifier, str) and re.fullmatch('[0-9a-f]{32}', identifier):
                stopped |= (root/'logs/authorizations'/f'{identifier}.revoked.json').exists()
        # A deadline is mandatory for an active app-controlled DEMO session.
        deadline = datetime.fromisoformat(status['deadline_utc']) if status.get('deadline_utc') else None
        bounded = deadline is not None and deadline.utcoffset() is not None and 0 < (deadline-current).total_seconds() <= 86400
        active = running_pid is True and health['state'] == 'RUNNING' and demo and compatible and bounded and not stopped
        busy = bool(status.get('pid') and running_pid is not False)
        retired = retired_session(root, status)
        if retired:
            busy = False
        request_busy = False
        if request.get('phase') in ('requested', 'checking', 'running'):
            request_pid = request.get('worker_pid') or request.get('launcher_pid')
            request_busy = bool(request_pid and alive(request_pid) is not False)
            if not request_pid:
                requested = datetime.fromisoformat(request['requested_at_utc'])
                request_busy = 0 <= (current-requested).total_seconds() <= 30
            if retired and request.get('activation_id') == status.get('activation_id'):
                request_busy = False
        if stopped and busy:
            reason = 'Stop requested; new entry authority is being revoked. Positions keep their SL/TP.'
        elif active:
            reason = 'Watching Gold and EUR/USD for valid, affordable setups.'
        elif health['state'] == 'RUNNING':
            reason = 'DEMO account, activation profile or session deadline could not be verified.'
        elif request_busy and not busy:
            reason = 'Starting: checking the DEMO account, positions, permissions and approved sources.'
        elif request.get('phase') in ('requested', 'checking', 'running') and not busy:
            reason = 'The activation process ended before a running session could be verified. Inspect its activation log.'
        elif request.get('phase') == 'failed' and not busy:
            reason = 'Activation failed: '+str(request.get('error', 'Inspect the app activation log.'))
        elif retired:
            reason = 'Previous Gold/EURUSD session ended. Ready for a new DEMO session.'
        label = 'ACTIVE' if active else 'UNVERIFIED' if busy and health['state'] in ('STALE', 'UNKNOWN') else 'INACTIVE'
        result.update(active=active, state=label, reason=reason,
                      health=health, can_start=result['capability']['available'] and not busy and not request_busy,
                      can_stop=compatible and busy and not stopped and bool(status.get('run_id')),
                      busy=busy or request_busy)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        result['state'] = 'UNVERIFIED'
        result['reason'] = f'Status cannot be verified: {exc}'
    if check_btc:
        from dashboard.btc_control import snapshot as btc_snapshot
        btc = btc_snapshot(root, now=current, alive=alive, check_other=False)
        if btc.get('busy') or btc.get('state') == 'UNVERIFIED':
            result['can_start'] = False
            if not result.get('busy'):
                result['reason'] = 'BTC monitoring is running, starting or unverified. Stop and verify that session before activating Gold/EURUSD.'
    return result


def default_end(now=None):
    current = (now or now_utc()).astimezone(MANILA)
    end = datetime.combine(current.date(), clock_time(22), MANILA)
    if end <= current:
        return end+timedelta(days=1)
    return end if end > current+timedelta(seconds=3.6) else current+timedelta(hours=6)


def clean_environment():
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith('PROJECT_QUANT_') and key not in ('PYTHONPATH', 'PYTHONHOME')}
    if getattr(sys, 'frozen', False):
        bundle = Path(sys._MEIPASS).resolve()
        environment['PATH'] = os.pathsep.join(part for part in environment.get('PATH', '').split(os.pathsep)
            if part and not Path(part).resolve().is_relative_to(bundle))
    return environment


@contextmanager
def external_dll_search():
    """Do not let the frozen app's DLL directory leak into workspace Python."""
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        yield
        return
    from ctypes import wintypes
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.GetDllDirectoryW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    api.GetDllDirectoryW.restype = wintypes.DWORD
    api.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
    api.SetDllDirectoryW.restype = wintypes.BOOL
    size = api.GetDllDirectoryW(0, None)
    previous = ctypes.create_unicode_buffer(size+1)
    api.GetDllDirectoryW(len(previous), previous)
    if not api.SetDllDirectoryW(None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        if not api.SetDllDirectoryW(previous.value or None):
            raise ctypes.WinError(ctypes.get_last_error())


def request_start(root, until, *, now=None):
    root, current = Path(root).resolve(), now or now_utc()
    if not isinstance(until, datetime) or until.utcoffset() is None or not 3.6 <= (until-current).total_seconds() <= 86400:
        raise ValueError('Choose an end time in the future, within the next 24 hours.')
    with control_lock(root):
        state = snapshot(root, now=current)
        if not state['can_start']:
            raise RuntimeError(state['capability']['reason'] or state['reason'])
        identifier = f'app-{current:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}'
        folder = root/'logs/app_control'
        record = dict(activation_id=identifier, phase='requested', requested_at_utc=current.isoformat(),
                      until=until.isoformat(), source='explicit_local_app_button', demo_only=True,
                      baseline_risk_percent=1, conditional_ceiling_percent=2, combined_ceiling_percent=8,
                      maximum_positions_per_symbol=2, maximum_open_positions_account_wide=4,
                      portfolio_scope='gold_eurusd_two_each_8pct_v1',
                      retry_allowed=False, stdout=str(folder/f'{identifier}.stdout.log'),
                      stderr=str(folder/f'{identifier}.stderr.log'))
        write_object(folder/f'{identifier}.json', record)
        write_object(folder/'latest.json', dict(activation_id=identifier))
        command = [state['capability']['python'], '-B', state['capability']['worker'],
                   '--activation-id', identifier, '--until', until.isoformat()]
        try:
            with Path(record['stdout']).open('wb') as output, Path(record['stderr']).open('wb') as errors:
                with external_dll_search():
                    process = subprocess.Popen(command, cwd=root, env=clean_environment(), stdin=subprocess.DEVNULL,
                        stdout=output, stderr=errors, close_fds=True,
                        creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB) if os.name == 'nt' else 0,
                        start_new_session=os.name != 'nt')
            record['launcher_pid'] = process.pid
        except OSError as exc:
            record.update(phase='failed', error=str(exc))
            write_object(folder/f'{identifier}.json', record)
            raise RuntimeError(f'Could not start the DEMO monitor: {exc}') from exc
        write_object(folder/f'{identifier}.json', record)
        return record


def request_stop(root, expected_run_id):
    """Use the existing stop command; it revokes entries, never closes positions."""
    root = Path(root).resolve()
    with control_lock(root):
        state = snapshot(root)
        if not state['can_stop'] or state['status'].get('run_id') != expected_run_id:
            raise RuntimeError('Monitor state changed; refresh before stopping new entries.')
        if not state['capability']['available']:
            raise RuntimeError(state['capability']['reason'])
        with external_dll_search():
            completed = subprocess.run([state['capability']['python'], '-B', str(root/'scripts/activate_demo.py'), '--stop'],
                cwd=root, env=clean_environment(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                encoding='utf-8', timeout=20, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if completed.returncode:
            raise RuntimeError((completed.stderr or completed.stdout).strip()[-1500:])
        result = json.loads(completed.stdout)
        if result.get('status') != 'stop_requested':
            raise RuntimeError('The monitor did not acknowledge the stop request; refresh its status.')
        write_object(root/'logs/app_control/last_stop.json', dict(run_id=expected_run_id,
                     requested_at_utc=now_utc().isoformat(), result=result))
        return result
