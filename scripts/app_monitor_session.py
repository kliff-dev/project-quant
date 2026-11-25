"""One explicitly clicked DEMO activation, independent of the desktop app's job."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard.monitor_control import read_object, update_request, clean_environment, REQUEST_ID, control_lock, write_object, now_utc


def run_session(root, identifier, until, *, start=None):
    if not isinstance(identifier, str) or not REQUEST_ID.fullmatch(identifier):
        raise ValueError('Invalid app activation ID.')
    root = Path(root).resolve()
    path = root/'logs/app_control'/f'{identifier}.json'
    import os
    with control_lock(root, timeout=5):
        request = read_object(path)
        limits = dict(baseline_risk_percent=1, conditional_ceiling_percent=2, combined_ceiling_percent=8,
                      maximum_positions_per_symbol=2, maximum_open_positions_account_wide=4,
                      portfolio_scope='gold_eurusd_two_each_8pct_v1')
        if (request.get('activation_id') != identifier or request.get('phase') != 'requested' or
                request.get('until') != until or request.get('source') != 'explicit_local_app_button' or
                request.get('demo_only') is not True or
                any(type(request.get(k)) is not type(v) or request.get(k)!=v for k,v in limits.items())):
            raise ValueError('A matching, unused explicit app request with reviewed portfolio limits is required.')
        request.update(phase='checking', worker_pid=os.getpid(), updated_at_utc=now_utc().isoformat())
        write_object(path,request)
    finished = threading.Event()

    def awake_after_start():
        while not finished.wait(.25):
            try:
                status = read_object(root/'logs/monitor/status.json')
                if status.get('activation_id') != identifier or status.get('status') != 'running':
                    continue
                update_request(root, identifier, phase='running', run_id=status['run_id'])
                folder = root/'logs/app_control'
                with (folder/f'{identifier}.awake.log').open('ab') as log:
                    process = subprocess.Popen([sys.executable, '-B', str(root/'scripts/keep_monitor_awake.py')],
                        cwd=root, env=clean_environment(), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                update_request(root, identifier, keep_awake_launcher_pid=process.pid)
                return
            except FileNotFoundError:
                continue  # No status exists until native preflight has completed.
            except Exception as exc:
                update_request(root, identifier, keep_awake_warning=str(exc))
                return

    watcher = threading.Thread(target=awake_after_start, daemon=True)
    watcher.start()
    try:
        if start is None:
            from engine.activation_demo import start
        result = start(root, identifier, until=until)
        finished.set()
        watcher.join(timeout=6)
        failed = result.get('status') == 'failed'
        update_request(root, identifier, phase='failed' if failed else 'finished',
                       error=result.get('stop_reason') if failed else None,
                       monitor_status=result.get('status'), orders_sent=result.get('orders_sent'))
        return result
    except BaseException as exc:
        finished.set()
        watcher.join(timeout=6)
        update_request(root, identifier, phase='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        finished.set()
        watcher.join(timeout=6)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activation-id', required=True)
    parser.add_argument('--until', required=True)
    args = parser.parse_args()
    if not REQUEST_ID.fullmatch(args.activation_id):
        parser.error('Invalid app activation ID.')
    result = run_session(ROOT, args.activation_id, args.until)
    return int(result.get('status') == 'failed')


if __name__ == '__main__':
    raise SystemExit(main())
