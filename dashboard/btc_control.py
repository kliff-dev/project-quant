"""Separate, explicit BTC DEMO controls; status reads never connect to MT5."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import os
import re
import subprocess
import uuid

from dashboard import monitor_control as common
from dashboard.research_model import monitor_health, process_alive

MODE='btc_demo_series'
REQUEST_ID=re.compile(r'btc-app-[A-Za-z0-9_-]{1,40}')
SETUP_IDS=('B1','B2','B3','B4')


def capability(root):
    root=Path(root).resolve()
    names=['.venv/Scripts/python.exe','scripts/app_btc_session.py','scripts/activate_btc_demo.py','config/btc_demo_plan.json']
    missing=[name for name in names if not (root/name).is_file() or not (root/name).resolve().is_relative_to(root)]
    return dict(available=not missing and os.name=='nt',python=str(root/names[0]),worker=str(root/names[1]),
        reason='Select the configured Windows Project Quant workspace.' if missing or os.name!='nt' else '')


def request_record(root):
    folder=Path(root)/'logs/btc_app_control'
    if not (folder/'latest.json').is_file(): return {}
    identifier=common.read_object(folder/'latest.json').get('activation_id')
    if not isinstance(identifier,str) or not REQUEST_ID.fullmatch(identifier): raise ValueError('Invalid saved BTC activation identifier.')
    return common.read_object(folder/f'{identifier}.json')


def update_request(root,identifier,**changes):
    if not REQUEST_ID.fullmatch(identifier): raise ValueError('Invalid BTC activation identifier.')
    with common.control_lock(root,timeout=5):
        path=Path(root)/'logs/btc_app_control'/f'{identifier}.json'
        record=common.read_object(path)
        if record.get('activation_id')!=identifier: raise ValueError('BTC request identity changed.')
        record.update(changes,updated_at_utc=common.now_utc().isoformat())
        common.write_object(path,record)
    return record


def snapshot(root,*,now=None,alive=None,check_other=True):
    root=Path(root); current=now or common.now_utc();alive=alive or process_alive
    result=dict(active=False,state='INACTIVE',reason='BTC monitoring has not been started.',can_start=False,can_stop=False,
        busy=False,status={},request={},capability=capability(root))
    try:
        path=root/'logs/btc_monitor/status.json'
        status=common.read_object(path) if path.is_file() else {}
        request=request_record(root);result.update(status=status,request=request)
        live=alive(status.get('pid')) if status.get('pid') else False
        health=monitor_health(status,now=current,alive=live)
        deadline=datetime.fromisoformat(status['deadline_utc']) if status.get('deadline_utc') else None
        bounded=deadline is not None and deadline.utcoffset() is not None and 0<(deadline-current).total_seconds()<=86400
        if 'account' in status and not isinstance(status['account'],dict): raise ValueError('BTC account status must be a verified object.')
        account=status.get('account') or {}
        demo=account.get('demo_verified') is True and type(account.get('trade_mode')) is int and account['trade_mode']==0
        stop_path=root/'logs/btc_monitor/stop.json'
        stopped=stop_path.is_file() and common.read_object(stop_path).get('run_id')==status.get('run_id')
        compatible=status.get('mode')==MODE
        busy=bool(status.get('pid') and live is not False)
        retired=common.retired_session(root,status)
        if retired: busy=False
        pending=False
        if request.get('phase') in ('requested','checking','running'):
            pid=request.get('worker_pid') or request.get('launcher_pid')
            pending=bool(pid and alive(pid) is not False)
            if not pid:
                requested=datetime.fromisoformat(request['requested_at_utc'])
                pending=requested.utcoffset() is not None and 0<=(current-requested).total_seconds()<=30
            if retired and request.get('activation_id')==status.get('activation_id'): pending=False
        active=live is True and health['state']=='RUNNING' and demo and bounded and compatible and not stopped
        reason=('Watching BTCUSDm for one of the selected, affordable research setups.' if active else
            'Stop requested. Existing BTC positions retain their broker SL/TP.' if stopped and busy else
            'Starting BTC: verifying the DEMO account, contract, risk and source files.' if pending and not busy else
            'BTC activation failed: '+str(request.get('error','Inspect the BTC log.')) if request.get('phase')=='failed' and not busy else
            'Previous BTC session ended. Ready for a new DEMO session.' if retired and not pending else
            str(status.get('stop_reason') or health['explanation']) if status else result['reason'])
        other=common.snapshot(root,now=current,alive=alive,check_btc=False) if check_other else {}
        other_busy=bool(other.get('busy')) or other.get('state')=='UNVERIFIED'
        if other_busy and not active and not busy and not pending:
            reason='Gold/EURUSD monitoring is running, starting or unverified. Verify and stop that session before activating BTC.'
        result.update(active=active,state='ACTIVE' if active else 'UNVERIFIED' if busy and (live is None or health['state'] in ('STALE','UNKNOWN')) else 'INACTIVE',
            reason=reason,busy=busy or pending,other_busy=other_busy,
            can_start=result['capability']['available'] and not busy and not pending and not other_busy,
            can_stop=compatible and busy and not stopped and bool(status.get('run_id')))
    except (OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError) as exc:
        result.update(state='UNVERIFIED',reason=f'BTC status cannot be verified: {exc}')
    return result


def request_start(root,until,setup_ids=SETUP_IDS,*,now=None):
    root=Path(root).resolve();current=now or common.now_utc(); selected=tuple(setup_ids)
    if not selected or len(set(selected))!=len(selected) or not set(selected)<=set(SETUP_IDS):
        raise ValueError('Select at least one unique BTC setup from B1–B4.')
    if not isinstance(until,datetime) or until.utcoffset() is None or not 3.6<=(until-current).total_seconds()<=86400:
        raise ValueError('Choose an aware BTC end time within the next 24 hours.')
    with common.control_lock(root):
        state=snapshot(root,now=current)
        if not state['can_start']: raise RuntimeError(state['capability']['reason'] or state['reason'])
        identifier=f'btc-app-{current:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}'
        folder=root/'logs/btc_app_control'
        record=dict(activation_id=identifier,phase='requested',requested_at_utc=current.isoformat(),until=until.isoformat(),
            source='explicit_local_btc_button',demo_only=True,setup_ids=list(selected),risk_percent=1,
            retry_allowed=False,stdout=str(folder/f'{identifier}.stdout.log'),stderr=str(folder/f'{identifier}.stderr.log'))
        common.write_object(folder/f'{identifier}.json',record)
        common.write_object(folder/'latest.json',dict(activation_id=identifier))
        command=[state['capability']['python'],'-B',state['capability']['worker'],'--activation-id',identifier,'--until',until.isoformat()]
        try:
            with Path(record['stdout']).open('wb') as output,Path(record['stderr']).open('wb') as errors:
                with common.external_dll_search():
                    child=subprocess.Popen(command,cwd=root,env=common.clean_environment(),stdin=subprocess.DEVNULL,
                        stdout=output,stderr=errors,close_fds=True,
                        creationflags=(subprocess.CREATE_NO_WINDOW|subprocess.CREATE_BREAKAWAY_FROM_JOB) if os.name=='nt' else 0,
                        start_new_session=os.name!='nt')
            record['launcher_pid']=child.pid
        except OSError as exc:
            record.update(phase='failed',error=str(exc));common.write_object(folder/f'{identifier}.json',record)
            raise RuntimeError(f'BTC monitor could not launch: {exc}') from exc
        common.write_object(folder/f'{identifier}.json',record)
    return record


def request_stop(root,expected_run_id):
    root=Path(root).resolve()
    with common.control_lock(root):
        state=snapshot(root,check_other=False)
        if not state['can_stop'] or state['status'].get('run_id')!=expected_run_id:
            raise RuntimeError('BTC monitor changed. Refresh before stopping.')
        common.write_object(root/'logs/btc_monitor/stop.json',dict(run_id=expected_run_id,
            requested_at_utc=common.now_utc().isoformat(),source='explicit_local_btc_stop'))
    return dict(status='stop_requested',positions_closed=0)
