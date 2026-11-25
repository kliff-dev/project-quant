"""Combined orchestration tests; fake children/native API, never MT5 or orders."""
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
from types import SimpleNamespace as Record
from unittest.mock import Mock
import uuid

import pytest

from engine import dual_demo as dual
from engine.execution_audit import atomic_json
from engine.strategy_orders import StrategyPolicy
from scripts import start_dual_demo as cli
from engine.portfolio_guard import SCOPE


class FakeAuth:
    def __init__(self,root,symbol,policy,expires):
        self.authorization_id=uuid.uuid4().hex
        self.policy,self.data,self.expires=policy,{'symbol':symbol,'portfolio_scope':SCOPE},expires
        self.revoked_path=root/'logs/authorizations'/f'{self.authorization_id}.revoked.json'
        self.rows=[]

    def records(self):return self.rows
    def summary(self):
        return dict(authorization_id=self.authorization_id,verified_entries=len(self.rows),
                    closed_trades=sum(r['status']=='closed_verified' for r in self.rows),
                    revoked=self.revoked_path.exists())


class FakeView:
    def __init__(self,base,symbol):self.base,self.symbol=base,symbol
    def inspect(self):
        self.base.verify_pinned_account()
        return dict(account={'account_trade_allowed':True,'account_expert_allowed':True},
            permissions={'terminal_trade_allowed':True,'external_python_trading_disabled':False})


class FakeChild:
    def __init__(self,view,logs,policy_path,authorization_id,**kwargs):
        self.view,self.symbol,self.directory=view,view.symbol,logs
        self.policy=StrategyPolicy.load(policy_path)
        self.authorization=view.base.registry[authorization_id]
        self.run_id=uuid.uuid4().hex
        self.now=kwargs['now']
        self.progress={}
        self.evaluated=[]
        self.polled=0
    def _health(self):
        if self.now()>=self.authorization.expires or self.authorization.revoked_path.exists():
            raise ValueError('Expired/revoked authorization')
        return {}
    def _load_progress(self):pass
    def _event(self,*args,**kwargs):pass
    def _closed_candle(self,now):
        width=300 if self.symbol=='XAUUSDm' else 3600
        return datetime.fromtimestamp(int(now.timestamp())//width*width,timezone.utc).isoformat()
    def _evaluate(self,candle):
        if candle in self.evaluated:return
        self.evaluated.append(candle)
        reason=self.coordinator.entry_blocker(self.symbol,candle)
        self.status['latest_decision']={'status':'BLOCKED' if reason else 'WAIT','reason':reason or 'No setup'}
        if not reason and self.view.base.signals.get(self.symbol):
            self.view.base.sends.append(self.symbol)
            session='fake-'+uuid.uuid4().hex[:16]
            self.authorization.rows.append(dict(status='entry_verified',session_id=session,decision_time_utc=candle))
            atomic_json(self.directory/'sessions'/session/'demo_test_state.json',dict(status='open_verified'))
            self.view.base.positions.append(Record(ticket=100+len(self.view.base.positions),symbol=self.symbol))
    def _poll_position(self):
        self.polled+=1
        active=[r for r in self.authorization.rows if r['status']=='entry_verified']
        if not any(p.symbol==self.symbol for p in self.view.base.positions) and active:
            if not self.view.base.exit_proven:raise ValueError('Missing actual exit proof')
            for row in active:
                row.update(status='closed_verified',exit_time_msc=int(self.now().timestamp()*1000))
                atomic_json(self.directory/'sessions'/row['session_id']/'demo_test_state.json',dict(status='closed_verified'))


@pytest.fixture
def rig(tmp_path,monkeypatch):
    clock=Record(value=datetime(2026,11,16,3,55,tzinfo=timezone.utc))
    base=Mock()
    base.positions=[];base.pending=[];base.registry={};base.signals={};base.sends=[];base.exit_proven=False
    base.verify_pinned_account.return_value={'demo_verified':True,'account_type':'DEMO'}
    base.terminal_api.positions_get.side_effect=lambda:tuple(base.positions)
    base.terminal_api.orders_get.side_effect=lambda:tuple(base.pending)
    base.__enter__=Mock(return_value=base);base.__exit__=Mock(return_value=None)
    for symbol,relative in dual.POLICIES.items():
        path=tmp_path/relative;path.parent.mkdir(exist_ok=True)
        policy=StrategyPolicy(target_mode='gold_intraday_v1' if symbol=='XAUUSDm' else 'adaptive_pullback_v2',
            slippage_reserve_points_per_side=50 if symbol=='XAUUSDm' else 2)
        path.write_text(json.dumps(asdict(policy)))
    (tmp_path/'logs').mkdir()
    (tmp_path/'reports').mkdir()
    def create(view,logs,policy_path,expires_at,user_instruction):
        assert not any(p.symbol==view.symbol for p in base.positions) and not base.pending
        auth=FakeAuth(tmp_path,view.symbol,StrategyPolicy.load(policy_path),expires_at)
        base.registry[auth.authorization_id]=auth
        return auth
    factory=Record(create=Mock(side_effect=create))
    monkeypatch.setattr(dual,'verify_sources',lambda _: {})
    monkeypatch.setattr(dual,'InstrumentView',FakeView)
    class FakeGuard:
        def __init__(self,logs):self.directory=logs
        def register(self,symbol,auth):pass
        def inventory(self,view,positions,orders,**kwargs):
            if orders or kwargs.get('opening') and any(p.symbol==kwargs.get('symbol') for p in positions):
                raise ValueError('Duplicate position')
    monkeypatch.setattr(dual,'TwoInstrumentGuard',FakeGuard)
    ticks=Record(value=0)
    controller=dual.DualController(tmp_path,base,now=lambda:clock.value,sleep=lambda seconds:None,
        monotonic=lambda:ticks.value,authorization_factory=factory,monitor_factories={s:FakeChild for s in dual.ORDER},journal_refresh=Mock())
    controller.reconcile_absent_position=lambda symbol,row:controller.children[symbol]._poll_position()
    return Record(root=tmp_path,clock=clock,ticks=ticks,base=base,factory=factory,c=controller)


def test_before_windows_monitoring_waits_without_authority_or_send(rig):
    rig.c.tick()
    assert rig.c.status['phase']=='waiting_for_session'
    assert all(x['phase']=='waiting_for_session' for x in rig.c.status['instruments'].values())
    rig.factory.create.assert_not_called()
    assert not rig.base.sends


def test_journal_refreshes_on_execution_state_changes_not_every_heartbeat(rig):
    rig.c.tick();rig.c.tick()
    assert rig.c.journal_refresh.call_count==1
    rig.clock.value=rig.clock.value.replace(hour=8,minute=0)
    rig.base.signals={'EURUSDm':True}
    rig.c.tick()
    assert rig.c.journal_refresh.call_count==2


@pytest.mark.parametrize('hour,minute,expected',[(7,0,{'XAUUSDm'}),(8,0,{'XAUUSDm','EURUSDm'})])
def test_arms_only_instruments_whose_window_has_started(rig,hour,minute,expected):
    rig.clock.value=rig.clock.value.replace(hour=hour,minute=minute)
    rig.c.tick()
    assert set(rig.c.children)==expected
    for symbol,auth in rig.c.authorizations.items():assert auth.expires==dual.WINDOWS[symbol][1]


def test_both_fresh_signals_open_one_each_without_duplicates(rig):
    rig.clock.value=rig.clock.value.replace(hour=8,minute=0)
    rig.base.signals={'EURUSDm':True,'XAUUSDm':True}
    rig.c.tick()
    assert rig.base.sends==['EURUSDm','XAUUSDm']
    assert len(rig.base.positions)==2
    assert rig.c.status['instruments']['XAUUSDm']['phase']=='position_open'
    rig.c.tick()
    assert rig.base.sends==['EURUSDm','XAUUSDm']


def test_flat_inventory_without_exit_proof_cannot_release_next_entry(rig):
    rig.clock.value=rig.clock.value.replace(hour=8,minute=0)
    rig.base.signals={'EURUSDm':True,'XAUUSDm':True}
    rig.c.tick()
    rig.base.positions.clear()
    with pytest.raises(ValueError,match='Missing actual exit'):rig.c.tick()
    assert rig.base.sends==['EURUSDm','XAUUSDm']


def test_same_instrument_requires_fresh_signal_other_slot_is_independent(rig):
    rig.clock.value=rig.clock.value.replace(hour=8,minute=0)
    rig.base.signals={'EURUSDm':True}
    rig.c.tick()
    rig.clock.value+=timedelta(minutes=2)
    rig.base.positions.clear();rig.base.exit_proven=True;rig.base.signals={}
    rig.c.tick()
    assert 'fresh candle' in rig.c.entry_blocker('EURUSDm','2026-11-16T08:00:00+00:00')
    assert rig.c.entry_blocker('XAUUSDm','2026-11-16T08:00:00+00:00') is None
    assert rig.c.entry_blocker('XAUUSDm','2026-11-16T08:05:00+00:00') is None


@pytest.mark.parametrize('symbol,before', [('XAUUSDm','2026-11-16T06:55:00+00:00'),('EURUSDm','2026-11-16T07:00:00+00:00')])
def test_no_pre_session_signal_is_reused(rig,symbol,before):
    rig.clock.value=rig.clock.value.replace(hour=8,minute=0)
    assert 'precedes' in rig.c.entry_blocker(symbol,before)


def test_no_new_gold_authority_after_its_six_hour_window(rig):
    rig.clock.value=rig.clock.value.replace(hour=13,minute=0)
    rig.c.tick()
    assert set(rig.c.children)=={'EURUSDm'}
    assert rig.c.status['instruments']['XAUUSDm']['phase']=='entry_window_expired'


def test_no_post_window_send_even_if_preview_was_ready(rig):
    rig.clock.value=rig.clock.value.replace(hour=13,minute=0)
    assert 'Outside' in rig.c.entry_blocker('XAUUSDm','2026-11-16T12:55:00+00:00')


@pytest.mark.parametrize('kind',['position','pending','unknown','unresolved'])
def test_unowned_or_uncertain_inventory_stops_both(rig,kind):
    rig.clock.value=rig.clock.value.replace(hour=8,minute=0)
    if kind=='position':rig.base.positions.append(Record(ticket=4,symbol='XAUUSDm'))
    elif kind=='pending':rig.base.pending.append(Record(ticket=5))
    elif kind=='unknown':rig.base.terminal_api.positions_get.side_effect=lambda:None
    else:atomic_json(rig.root/'logs/sessions/other/demo_test_state.json',dict(status='open_unverified'))
    with pytest.raises(ValueError):rig.c.tick()
    assert not rig.base.sends


def test_clock_backwards_stops_both(rig):
    rig.clock.value-=timedelta(seconds=1)
    with pytest.raises(ValueError,match='clock'):rig.c.tick()


def test_failed_or_reserved_record_stops_without_retry(rig):
    rig.clock.value=rig.clock.value.replace(hour=8,minute=0)
    rig.c.tick()
    rig.c.authorizations['EURUSDm'].rows.append({'status':'reserved'})
    with pytest.raises(ValueError,match='uncertain'):rig.c.tick()
    assert not rig.base.sends


def test_final_run_revokes_both_existing_authorities(rig):
    rig.clock.value=rig.clock.value.replace(hour=8,minute=0)
    result=rig.c.run(once=True)
    assert result['status']=='stopped'
    assert len(rig.c.authorizations)==2
    assert all(a.revoked_path.exists() for a in rig.c.authorizations.values())


def test_failure_revokes_existing_authorities(rig,monkeypatch):
    rig.clock.value=rig.clock.value.replace(hour=8,minute=0)
    rig.c.tick()
    monkeypatch.setattr(rig.c,'tick',Mock(side_effect=ValueError('bad history')))
    result=rig.c.run(once=True)
    assert result['status']=='failed'
    assert all(a.revoked_path.exists() for a in rig.c.authorizations.values())


def test_failed_unknown_send_is_not_reported_as_zero_calls(rig,monkeypatch):
    rig.clock.value=rig.clock.value.replace(hour=8,minute=0)
    rig.c.tick()
    rig.c.children['EURUSDm'].status['orders_sent']=None
    monkeypatch.setattr(rig.c,'tick',Mock(side_effect=ValueError('uncertain send')))
    result=rig.c.run(once=True)
    assert result['status']=='failed' and result['orders_sent'] is None


def test_stop_revokes_both_and_blocks_a_prepared_candidate(rig):
    rig.clock.value=rig.clock.value.replace(hour=8,minute=0)
    rig.c.tick()
    assert dual.stop(rig.root)['status']=='stop_requested'
    assert all(a.revoked_path.exists() for a in rig.c.authorizations.values())
    assert 'stopping' in rig.c.entry_blocker('EURUSDm',rig.clock.value.isoformat())


@pytest.mark.parametrize('observation,expected',[('recent_heartbeat','running'),('stale_or_overdue','status_unverified')])
def test_status_does_not_claim_old_heartbeat_is_live(rig,monkeypatch,observation,expected):
    monkeypatch.setattr(dual,'read_status',lambda _:dict(mode=dual.MODE,status='running',status_observation=observation))
    assert dual.status(rig.root)['status']==expected


@pytest.mark.parametrize('delta',[timedelta(days=-1),timedelta(days=1),timedelta(hours=10)])
def test_wrong_date_or_ended_window_blocks_before_native_connection(rig,delta):
    factory=Mock()
    with pytest.raises(ValueError):dual.start(rig.root,now=lambda:rig.clock.value+delta,connector_factory=factory)
    factory.assert_not_called()


def test_start_reserves_both_launches_and_never_restarts(rig):
    factory=Mock(return_value=rig.base)
    controller=Record(run_id=uuid.uuid4().hex,run=Mock(return_value={'status':'stopped'}))
    dual.start(rig.root,now=lambda:rig.clock.value,connector_factory=factory,controller_factory=Mock(return_value=controller))
    assert all((rig.root/p).exists() for p in (dual.RESERVATION,'logs/gold_launch_2026-11-16.json','logs/monday_2026-11-16.launch.json'))
    with pytest.raises(ValueError,match='consumed'):
        dual.start(rig.root,now=lambda:rig.clock.value,connector_factory=factory)
    assert factory.call_count==1


def test_source_change_blocks_start_before_native_connection(rig,monkeypatch):
    monkeypatch.setattr(dual,'verify_sources',Mock(side_effect=ValueError('source changed')))
    factory=Mock()
    with pytest.raises(ValueError,match='source changed'):
        dual.start(rig.root,now=lambda:rig.clock.value,connector_factory=factory)
    factory.assert_not_called()


def test_cli_default_is_offline_status(rig,monkeypatch,capsys):
    monkeypatch.setattr(cli,'ROOT',rig.root)
    start=Mock();monkeypatch.setattr(dual,'start',start)
    assert cli.main([])==0
    assert json.loads(capsys.readouterr().out)['status']=='not_running'
    start.assert_not_called()
