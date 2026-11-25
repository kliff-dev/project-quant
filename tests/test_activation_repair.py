"""Regression cases from the Windows PID reuse and cross-mode execution audit."""
from pathlib import Path
import json

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from dashboard import btc_control as btc, monitor_control as common
from engine.market_monitor import monitor_lock
from engine.execution_state import unresolved_execution_files
from engine.gold_demo import unresolved
from test_btc_control import workspace,status,NOW
from test_btc_demo import rig,run

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('mode',['BTC','Gold'])
@pytest.mark.parametrize('alive',[True,None])
def test_finished_session_with_reused_or_protected_pid_does_not_block_either_mode(tmp_path,mode,alive):
    workspace(tmp_path)
    status(tmp_path,btc_status=mode=='BTC',status='stopped')
    for snapshot in (btc.snapshot,common.snapshot):
        state=snapshot(tmp_path,now=NOW,alive=lambda pid:alive)
        assert not state['active'] and not state.get('busy') and state['can_start']


@pytest.mark.parametrize('mode',['BTC','Gold'])
def test_stopped_status_cannot_retire_a_worker_that_still_holds_execution_lock(tmp_path,mode):
    workspace(tmp_path)
    status(tmp_path,btc_status=mode=='BTC',status='stopped')
    with monitor_lock(tmp_path/'logs/monitor/monitor.lock'):
        assert common.monitor_lock_free(tmp_path) is False
        assert not btc.snapshot(tmp_path,now=NOW,alive=lambda pid:True)['can_start']
        assert not common.snapshot(tmp_path,now=NOW,alive=lambda pid:True)['can_start']
    assert common.monitor_lock_free(tmp_path) is True


def test_unknown_lock_access_cannot_retire_a_protected_worker(tmp_path,monkeypatch):
    workspace(tmp_path);status(tmp_path,status='stopped')
    monkeypatch.setattr(common,'monitor_lock_free',lambda root:None)
    assert not btc.snapshot(tmp_path,now=NOW,alive=lambda pid:None)['can_start']


def test_btc_rejects_unresolved_legacy_execution_before_send(rig):
    path=rig.root/'logs/sessions/old-gold/demo_test_state.json';path.parent.mkdir(parents=True)
    common.write_object(path,{'status':'sending_outcome_unknown'})
    result=run(rig)
    assert result['status']=='failed' and 'Gold/EURUSD' in result['stop_reason']
    assert rig.api.order_check.call_count==0 and rig.api.order_send.call_count==0


def test_foreign_uncertain_record_appearing_during_check_blocks_btc_send(rig):
    original=rig.api.order_check.return_value
    def check(request):
        common.write_object(rig.root/'logs/demo_test_state.json',{'status':'sending_outcome_unknown'})
        return original
    rig.api.order_check.side_effect=check
    result=run(rig)
    assert rig.api.order_check.call_count==1 and rig.api.order_send.call_count==0
    assert result['latest_decision']['status']=='BLOCKED'


@pytest.mark.parametrize('state',['reserved','sending_outcome_unknown','outcome_uncertain','filled_verified'])
def test_gold_and_eurusd_reject_unresolved_btc_execution(tmp_path,state):
    path=tmp_path/'logs/btc_monitor/attempts/old.json';common.write_object(path,{'status':state})
    assert unresolved(tmp_path)==[path]


@pytest.mark.parametrize('state',['closed_verified','blocked_before_send'])
def test_proven_no_send_or_actual_close_does_not_block_other_modes(tmp_path,state):
    common.write_object(tmp_path/'logs/btc_monitor/attempts/old.json',{'status':state})
    assert not unresolved_execution_files(tmp_path)


def test_btc_chart_markers_stay_inside_displayed_candles(monkeypatch):
    monkeypatch.setenv('PROJECT_QUANT_ROOT',str(ROOT))
    app=AppTest.from_file(str(ROOT/'dashboard/research_app.py'),default_timeout=40).run()
    app.radio(key='workspace').set_value('BTC research & DEMO').run()
    app.date_input(key='btc_chart_first').set_value(pd.Timestamp('2024-11-22').date()).run()
    assert not app.exception
    chart=json.loads(app.get('plotly_chart')[1].proto.spec)
    candles=pd.to_datetime(chart['data'][0]['x'],utc=True)
    for trace in chart['data']:
        if trace.get('mode')=='markers':
            stamps=pd.to_datetime(trace['x'],utc=True)
            assert ((stamps>=candles[0])&(stamps<=candles[-1])).all()
