"""Consume one explicit BTC app activation; no credentials or automatic retry."""
from __future__ import annotations
import argparse
import ctypes
import os
from pathlib import Path
import sys
import threading

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from dashboard import btc_control
from dashboard.monitor_control import read_object


def run_session(root,identifier,until,*,start=None):
    root=Path(root).resolve()
    if not isinstance(identifier,str) or not btc_control.REQUEST_ID.fullmatch(identifier): raise ValueError('Invalid BTC app request.')
    path=root/'logs/btc_app_control'/f'{identifier}.json'
    from dashboard.monitor_control import control_lock, write_object, now_utc
    with control_lock(root,timeout=5):
        request=read_object(path)
        if (request.get('activation_id')!=identifier or request.get('phase')!='requested' or request.get('until')!=until or
            request.get('source')!='explicit_local_btc_button' or request.get('demo_only') is not True or
            type(request.get('risk_percent')) is not int or request.get('risk_percent')!=1):
            raise ValueError('Matching unused explicit BTC button request required.')
        setups=request.get('setup_ids')
        if not isinstance(setups,list) or not setups or any(not isinstance(x,str) for x in setups) or len(set(setups))!=len(setups) or not set(setups)<=set(btc_control.SETUP_IDS):
            raise ValueError('Invalid BTC setup selection.')
        request.update(phase='checking',worker_pid=os.getpid(),updated_at_utc=now_utc().isoformat())
        write_object(path,request)
    finished=threading.Event()

    def keep_awake():
        # Windows thread-scoped request releases automatically if this process dies.
        if os.name!='nt': return
        api=ctypes.WinDLL('kernel32',use_last_error=True)
        from ctypes import wintypes
        api.SetThreadExecutionState.argtypes=[wintypes.DWORD]
        api.SetThreadExecutionState.restype=wintypes.DWORD
        try:
            while not finished.wait(.25):
                try: status=read_object(root/'logs/btc_monitor/status.json')
                except FileNotFoundError: continue
                if status.get('activation_id')==identifier and status.get('status')=='running':
                    btc_control.update_request(root,identifier,phase='running',run_id=status['run_id'])
                    if not api.SetThreadExecutionState(0x80000001): raise ctypes.WinError(ctypes.get_last_error())
                    finished.wait()
                    return
        except Exception as exc:
            btc_control.update_request(root,identifier,keep_awake_warning=str(exc))
        finally: api.SetThreadExecutionState(0x80000000)
    watcher=threading.Thread(target=keep_awake,daemon=True);watcher.start()
    try:
        if start is None:
            from scripts.activate_btc_demo import start
        result=start(root,identifier,until=until,setup_ids=tuple(setups))
        finished.set();watcher.join(timeout=5)
        failed=result.get('status')=='failed'
        btc_control.update_request(root,identifier,phase='failed' if failed else 'finished',
            error=result.get('stop_reason') if failed else None,orders_sent=result.get('orders_sent'))
        return result
    except BaseException as exc:
        finished.set();watcher.join(timeout=5)
        btc_control.update_request(root,identifier,phase='failed',error=f'{type(exc).__name__}: {exc}')
        raise
    finally: finished.set();watcher.join(timeout=5)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--activation-id',required=True);parser.add_argument('--until',required=True)
    args=parser.parse_args();return int(run_session(ROOT,args.activation_id,args.until).get('status')=='failed')


if __name__=='__main__': raise SystemExit(main())
