"""Keep Windows out of idle sleep only for the currently running local monitor.

No power-plan changes, display requirement, terminal interaction or trade calls.
Explicit sleep/shutdown can still stop monitoring. The OS releases the request
when this process exits, including termination without Python cleanup.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.execution_audit import atomic_json
from engine.market_monitor import monitor_lock, read_status


def main() -> None:
    if os.name != 'nt':
        raise RuntimeError('The keep-awake helper requires Windows.')
    folder = ROOT / 'logs'
    with monitor_lock(folder / 'monitor/keep_awake.lock'):
        status = read_status(folder)
        if status.get('status') != 'running' or status.get('status_observation') != 'recent_heartbeat':
            raise RuntimeError('A running monitor with a recent heartbeat is required.')
        run_id, pid = status['run_id'], status['pid']
        deadline = datetime.fromisoformat(status['deadline_utc'])
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if not 0 < remaining <= 86400:
            raise RuntimeError('Monitor deadline must be within the next 24 hours.')
        monotonic_deadline = time.monotonic() + remaining

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.SetThreadExecutionState.argtypes = [wintypes.DWORD]
        kernel.SetThreadExecutionState.restype = wintypes.DWORD
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only.
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        state_path = folder / 'monitor/keep_awake_status.json'
        evidence = dict(pid=os.getpid(), monitor_pid=pid, monitor_run_id=run_id,
                        deadline_utc=deadline.isoformat(), power_plan_changed=False,
                        display_required=False)
        reason = 'Monitor or deadline ended.'
        try:
            flags = 0x80000001  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED.
            if not kernel.SetThreadExecutionState(flags):
                raise ctypes.WinError(ctypes.get_last_error())
            # The previous state returned by this second call must include our
            # first request, confirming this thread holds the requested flags.
            prior = kernel.SetThreadExecutionState(flags)
            if prior & flags != flags:
                raise RuntimeError('Windows did not retain the keep-awake flags.')
            evidence['verified_execution_state'] = hex(prior)
            while datetime.now(timezone.utc) < deadline and time.monotonic() < monotonic_deadline:
                current = read_status(folder)
                if current.get('run_id') != run_id or current.get('status') != 'running':
                    break
                if current.get('heartbeat_age_seconds', float('inf')) > 180:
                    reason = 'Monitor heartbeat became stale.'
                    break
                atomic_json(state_path, dict(evidence, status='active', checked_at_utc=datetime.now(timezone.utc).isoformat()))
                result = kernel.WaitForSingleObject(handle, 10000)
                if result == 0:  # Exact monitor process exited; PID reuse cannot transfer this handle.
                    break
                if result != 258:  # WAIT_TIMEOUT.
                    raise RuntimeError(f'Cannot verify monitor process lifetime: {result}.')
        except BaseException as exc:
            reason = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            cleared = bool(kernel.SetThreadExecutionState(0x80000000))
            kernel.CloseHandle(handle)
            atomic_json(state_path, dict(evidence, status='released', request_cleared=cleared,
                        reason=reason, checked_at_utc=datetime.now(timezone.utc).isoformat()))


if __name__ == '__main__':
    main()
