"""Offline Gold manual-start lifecycle; native connector is never initialized."""
from datetime import datetime, timedelta, timezone
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as Record
from unittest.mock import Mock

import pytest

from engine import gold_demo
from engine.market_monitor import monitor_lock
from engine.strategy_orders import StrategyPolicy
from scripts import start_gold_demo as cli


@pytest.fixture
def pilot(tmp_path):
    root=Path(__file__).resolve().parents[1]
    (tmp_path/'config').mkdir()
    plan=json.loads((root/gold_demo.PLAN).read_text(encoding='utf-8'))
    policy=StrategyPolicy(target_mode='gold_intraday_v1',slippage_reserve_points_per_side=50)
    path=tmp_path/gold_demo.POLICY
    path.write_text(json.dumps(asdict(policy)))
    plan['source_sha256']={gold_demo.POLICY.as_posix():hashlib.sha256(path.read_bytes()).hexdigest()}
    (tmp_path/gold_demo.PLAN).write_text(json.dumps(plan))
    connector=Mock()
    connector.__enter__=Mock(return_value=connector)
    connector.__exit__=Mock(return_value=None)
    connector.terminal_api.positions_get.return_value=()
    connector.terminal_api.orders_get.return_value=()
    connector.inspect.return_value={'quote':{'symbol':'XAUUSDm'}}
    factory=Mock(return_value=connector)
    auth=Record(policy=policy,data={'symbol':'XAUUSDm'},authorization_id='a'*32,
                revoked_path=tmp_path/'logs/authorizations/revoked.json')
    authorization=Record(create=Mock(return_value=auth))
    def run():
        with monitor_lock(tmp_path/'logs/monitor/monitor.lock'):pass
        return dict(status='stopped',orders_sent=0)
    monitor=Record(run=Mock(side_effect=run))
    make_monitor=Mock(return_value=monitor)
    clock=lambda:datetime(2026,11,16,8,tzinfo=timezone.utc)
    options=dict(now=clock,connector_factory=factory,authorization_factory=authorization,monitor_factory=make_monitor)
    return Record(root=tmp_path,plan=plan,policy=policy,connector=connector,factory=factory,auth=auth,
                  authorization=authorization,monitor=monitor,options=options)


def test_gold_status_offline_does_not_claim_running(pilot):
    result=gold_demo.describe(pilot.root)
    assert result['status']=='not_running' and result['scheduled'] is False
    assert result['maximum_reward_r']==4 and result['risk_percent']==1
    pilot.factory.assert_not_called()


@pytest.mark.parametrize('observation,expected',[
    ('recent_heartbeat','running'),('stale_or_overdue','status_unverified'),(None,'status_unverified')])
def test_gold_status_requires_recent_heartbeat(pilot,monkeypatch,observation,expected):
    monkeypatch.setattr(gold_demo,'read_status',lambda _:dict(status='running',
        policy={'target_mode':'gold_intraday_v1'},status_observation=observation))
    assert gold_demo.describe(pilot.root)['status']==expected
    pilot.factory.assert_not_called()


def test_gold_status_recognizes_combined_controller_without_claiming_a_fill(pilot,monkeypatch):
    monkeypatch.setattr(gold_demo,'read_status',lambda _:dict(status='running',mode='dual_demo_series',
        status_observation='recent_heartbeat',instruments={'XAUUSDm':{'phase':'waiting_for_session'}}))
    result=gold_demo.describe(pilot.root)
    assert result['status']=='running' and result['combined_controller'] is True
    assert result['maximum_open_positions_account_wide']==2 and result['maximum_positions_per_symbol']==1
    assert result['instrument_phase']=='waiting_for_session'


def test_gold_start_creates_only_six_hour_bounded_authority_and_revokes_on_exit(pilot):
    result=gold_demo.start(pilot.root,**pilot.options)
    assert result['status']=='stopped'
    call=pilot.authorization.create.call_args
    assert call.kwargs['expires_at']==datetime(2026,11,16,14,tzinfo=timezone.utc)
    assert 'Gold DEMO' in call.kwargs['user_instruction']
    assert pilot.auth.revoked_path.exists()
    with pytest.raises(ValueError,match='already reserved'):
        gold_demo.start(pilot.root,**pilot.options)
    assert pilot.factory.call_count==1


@pytest.mark.parametrize('which',['positions_get','orders_get'])
@pytest.mark.parametrize('value',[None,(Record(ticket=1,symbol='EURUSDm'),)])
def test_gold_never_stacks_with_existing_or_unknown_inventory(pilot,which,value):
    getattr(pilot.connector.terminal_api,which).return_value=value
    with pytest.raises(ValueError,match='flat account'):
        gold_demo.start(pilot.root,**pilot.options)
    pilot.authorization.create.assert_not_called()


def test_gold_old_unresolved_state_blocks_before_connection(pilot):
    path=pilot.root/'logs/sessions/prior/demo_test_state.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'status':'open_unverified'}))
    with pytest.raises(ValueError,match='earlier execution'):
        gold_demo.start(pilot.root,**pilot.options)
    pilot.factory.assert_not_called()


def test_gold_policy_hash_change_blocks_before_connection(pilot):
    (pilot.root/gold_demo.POLICY).write_text('{}')
    with pytest.raises(ValueError,match='source changed'):
        gold_demo.start(pilot.root,**pilot.options)
    pilot.factory.assert_not_called()


def test_gold_stop_does_not_stop_forex_monitor(pilot,monkeypatch):
    monkeypatch.setattr(gold_demo,'read_status',lambda _:dict(status='running',policy={'target_mode':'adaptive_pullback_v2'}))
    stop=Mock()
    monkeypatch.setattr(gold_demo,'request_stop',stop)
    assert gold_demo.stop(pilot.root)['status']=='no_gold_monitor_running'
    stop.assert_not_called()


@pytest.mark.parametrize('argv',[[],['--plan'],['--status']])
def test_gold_cli_default_offline(pilot,monkeypatch,capsys,argv):
    monkeypatch.setattr(cli,'ROOT',pilot.root)
    start=Mock()
    monkeypatch.setattr(gold_demo,'start',start)
    assert cli.main(argv)==0
    assert json.loads(capsys.readouterr().out)['scheduled'] is False
    start.assert_not_called()
