"""Expanded DEMO scope: real executor/ledger with an in-memory hedging broker."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta
import json
from types import SimpleNamespace as Record

import pytest

from test_demo_executor import rig
from test_gold_execution import gold, candidate
from engine.demo_executor import DemoExecutor, MAGIC
from engine.demo_series import DemoSeriesAuthorization
from engine.gold_demo import GoldDemoMonitor
from engine.market_monitor import MonitorConfig
from engine.portfolio_guard import MULTI_SCOPE, SCOPE, TwoInstrumentGuard
from engine.execution_audit import atomic_json
from test_dual_demo import rig as controller_rig, FakeChild
from engine import dual_demo as dual


@pytest.fixture
def multi(gold,monkeypatch):
    g=gold; r=g.rig
    r.account.margin_mode=2
    guard=TwoInstrumentGuard(r.directory,scope=MULTI_SCOPE)
    g.connector.portfolio_guard=guard
    path=r.directory/'gold_policy.json'
    path.write_text(json.dumps(asdict(g.policy)))
    auth=DemoSeriesAuthorization.create(g.connector,r.directory,path,
        expires_at=g.clock.now+timedelta(hours=3),user_instruction='Two per market; 8% combined DEMO ceiling.')
    guard.register('XAUUSDm',auth)
    template=candidate(g,monkeypatch)
    count=Record(value=0)
    def send(request):
        assert 'position' not in request
        count.value+=1
        order=1000+count.value*10
        deal,identifier,ticket=order+1,order+2,order+3
        price=r.tick.ask if request['type']==0 else r.tick.bid
        r.ledger['orders'][order]=(Record(ticket=order,symbol='XAUUSDm',magic=MAGIC,type=request['type'],state=4,position_id=identifier),)
        r.ledger['deals'][order]=(Record(ticket=deal,order=order,position_id=identifier,symbol='XAUUSDm',magic=MAGIC,
            type=request['type'],entry=0,volume=request['volume'],price=price,profit=0.,commission=0.,fee=0.,swap=0.,time_msc=int(g.clock.now.timestamp()*1000)),)
        r.ledger['positions'].append(Record(ticket=ticket,identifier=identifier,symbol='XAUUSDm',magic=MAGIC,
            type=request['type'],volume=request['volume'],price_open=price,sl=request['sl'],tp=request['tp']))
        return Record(retcode=10009,order=order,deal=deal,volume=request['volume'],price=price,bid=r.tick.bid,ask=r.tick.ask,
            comment='Done',request_id=count.value,retcode_external=0)
    r.api.order_send.side_effect=send
    r.api.history_deals_get.side_effect=lambda *a,**kw: r.ledger['deals'].get(kw['ticket'],()) if 'ticket' in kw else tuple(d for rows in r.ledger['deals'].values() for d in rows)
    def advance(minutes=5):
        g.clock.now+=timedelta(minutes=minutes)
        r.tick.time_msc=int(g.clock.now.timestamp()*1000)
    def enter(name):
        intent=replace(template,decision_time_utc=g.clock.now.replace(second=0,microsecond=0).isoformat())
        e=DemoExecutor(g.connector,r.directory,session_id=name,strategy_policy=g.policy,strategy_intent=intent)
        preview=e.prepare_entry()
        assert preview['ready']
        state=e.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='c'*32)
        return e,state
    def close(position):
        order=position.ticket+10000
        r.ledger['orders'][order]=(Record(ticket=order,symbol=position.symbol,type=1-position.type,state=4,position_id=position.identifier),)
        r.ledger['deals'][order]=(Record(ticket=order+1,order=order,position_id=position.identifier,symbol=position.symbol,
            type=1-position.type,entry=1,reason=5,volume=position.volume,price=position.tp,profit=10.,commission=-.02,fee=0.,swap=0.,time_msc=int(g.clock.now.timestamp()*1000)),)
        r.ledger['positions'].remove(position)
    return Record(g=g,r=r,guard=guard,auth=auth,path=path,advance=advance,enter=enter,close=close)


def test_two_native_gold_entries_keep_unique_tickets_and_third_never_sends(multi):
    m=multi
    first,a=m.enter('first')
    m.advance()
    second,b=m.enter('second')
    assert a['position_ticket']!=b['position_ticket'] and a['position_id']!=b['position_id']
    assert all(s['status']=='open_verified' for s in (a,b))
    assert m.auth.summary()['max_open_positions']==2
    assert len(m.r.ledger['positions'])==2
    m.advance()
    with pytest.raises(ValueError,match='permitted positions'):
        m.enter('third')
    assert m.r.api.order_send.call_count==2


def test_same_candle_cannot_enter_twice_even_with_free_slot(multi):
    m=multi;m.enter('first')
    with pytest.raises(ValueError,match='prior exit|old signal|reserved/consumed'):
        m.enter('duplicate-candle')
    assert m.r.api.order_send.call_count==1


def test_exact_position_exit_is_verified_while_same_symbol_sibling_stays(multi):
    m=multi;m.enter('first');m.advance();m.enter('second')
    monitor=GoldDemoMonitor(m.g.connector,m.r.directory,m.path,authorization_id=m.auth.authorization_id,
        config=MonitorConfig(2,10),now=lambda:m.g.clock.now,emit=lambda _:None)
    monitor.status.update(run_id='c'*32,orders_sent=2)
    monitor._health()
    first,sibling=list(m.r.ledger['positions'])
    m.advance();m.close(first)
    monitor._poll_position()
    assert [r['status'] for r in m.auth.records()]==['closed_verified','entry_verified']
    assert m.r.ledger['positions']==[sibling]
    assert m.r.api.order_send.call_count==2  # History verification sends nothing.
    m.advance();m.close(sibling)
    monitor._poll_position()
    assert all(r['status']=='closed_verified' for r in m.auth.records())


@pytest.mark.parametrize('margin_mode',[0,1,None,True])
def test_netting_or_unverified_margin_mode_blocks_without_send(multi,margin_mode):
    m=multi;m.r.account.margin_mode=margin_mode
    with pytest.raises(ValueError,match='hedging'):
        m.enter('forbidden')
    m.r.api.order_send.assert_not_called()


@pytest.mark.parametrize('change',['legacy_scope','cap','per_symbol','total','boolean'])
def test_expanded_authorization_requires_exact_frozen_scope_and_limits(multi,change):
    m=multi
    data=deepcopy(m.auth.data)
    if change=='legacy_scope':data['portfolio_scope']=SCOPE
    if change=='cap':data['maximum_combined_risk_percent']=9
    if change=='per_symbol':data['maximum_positions_per_symbol']=3
    if change=='total':data['maximum_open_positions_account_wide']=5
    if change=='boolean':data['maximum_positions_per_symbol']=True
    atomic_json(m.auth.path,data)
    with pytest.raises(ValueError):DemoSeriesAuthorization(m.r.directory,m.auth.authorization_id).validate(m.g.connector)
    m.r.api.order_send.assert_not_called()


def add_eur(m,index,risk=4.99):
    """Independent saved native proof for testing all four portfolio slots."""
    identifier='e'*32
    if 'EURUSDm' not in m.guard.authorizations:
        data=dict(authorization_id=identifier,portfolio_scope=MULTI_SCOPE,symbol='EURUSDm',account_identity={'login':464192812,'server':'Exness-MT5Trial17'},
            maximum_positions_per_symbol=2,maximum_open_positions_account_wide=4,maximum_combined_risk_percent=8)
        atomic_json(m.r.directory/'authorizations'/f'{identifier}.json',data)
        m.guard.register('EURUSDm',Record(data=data,authorization_id=identifier))
    p=Record(ticket=8000+index,identifier=9000+index,symbol='EURUSDm',magic=MAGIC,type=0,volume=.01,sl=1.099,tp=1.102)
    state=dict(status='open_verified',account_identity={'login':464192812,'server':'Exness-MT5Trial17'},symbol='EURUSDm',
        position_id=p.identifier,position_ticket=p.ticket,position_type=0,executed_volume=.01,request={'sl':p.sl,'tp':p.tp},entry_verified_deals=[{'ticket':7000+index}],
        session_authorization={'authorization_id':identifier},protection=dict(policy={'target_mode':'adaptive_pullback_v2','risk_percent':1,'max_lots':.1},
        tick_size=.00001,planned_loss_with_reserves=risk,risk_budget=5.,risk_basis_amount=500.))
    path=m.r.directory/f'sessions/eur-{index}/demo_test_state.json';atomic_json(path,state)
    m.r.ledger['positions'].append(p)
    return p,path,state


def test_four_positions_are_accepted_but_fifth_and_third_per_market_block(multi):
    m=multi;m.enter('first');m.advance();m.enter('second');add_eur(m,1);add_eur(m,2)
    m.guard.inventory(m.g.connector,m.r.ledger['positions'],())
    for symbol in ('EURUSDm','XAUUSDm'):
        with pytest.raises(ValueError,match='permitted positions'):
            m.guard.inventory(m.g.connector,m.r.ledger['positions'],(),symbol=symbol,opening=True)
    assert m.r.api.order_send.call_count==2


def test_combined_budget_adds_both_same_symbol_positions_and_fill_reserves(multi):
    m=multi;m.enter('first');m.advance();m.enter('second');add_eur(m,1);add_eur(m,2)
    total=sum(json.loads(p.read_text())['protection']['planned_loss_with_reserves'] for p in m.guard.paths() if p.is_file())
    assert m.guard.budget(m.g.connector,'XAUUSDm',{},include_candidate=False)==(pytest.approx(total),40.)
    assert m.guard.budget(m.g.connector,'XAUUSDm',{'planned_loss_with_reserves':40-total})==(40.,40.)
    with pytest.raises(ValueError,match='8%'):
        m.guard.budget(m.g.connector,'XAUUSDm',{'planned_loss_with_reserves':40-total+.01})
    m.r.account.equity=100.
    with pytest.raises(ValueError,match='8%'):
        m.guard.budget(m.g.connector,'XAUUSDm',{},include_candidate=False)


def test_unproven_absence_or_failed_send_halts_following_entries(multi):
    m=multi;m.enter('first');m.r.ledger['positions'].clear();m.advance()
    with pytest.raises(ValueError,match='Unresolved'):
        m.enter('second')
    assert m.r.api.order_send.call_count==1


@pytest.fixture
def multi_controller(controller_rig,monkeypatch):
    r=controller_rig
    r.clock.value=r.clock.value.replace(hour=8,minute=0)
    r.base.terminal_api.account_info.return_value=Record(margin_mode=2)
    class Guard:
        def __init__(self,logs,scope):self.scope=scope
        def register(self,symbol,auth):auth.data['portfolio_scope']=self.scope
        def inventory(self,view,positions,orders,**kw):
            if len(positions)>=4 or sum(p.symbol==kw.get('symbol') for p in positions)>=2:
                raise ValueError('No slot')
    class Child(FakeChild):
        def _evaluate(self,candle):
            previous=len(self.view.base.positions)
            super()._evaluate(candle)
            if len(self.view.base.positions)>previous:
                p=self.view.base.positions[-1];p.identifier=p.ticket+1000
                row=self.authorization.rows[-1]
                atomic_json(self.directory/'sessions'/row['session_id']/'demo_test_state.json',
                    dict(status='open_verified',position_ticket=p.ticket,position_id=p.identifier))
    monkeypatch.setattr(dual,'TwoInstrumentGuard',Guard)
    for path in dual.POLICIES.values():
        policy=json.loads((r.root/path).read_text());policy.update(risk_percent=2,risk_mode='minimum_lot_rescue_v1',entry_timing='user_activation_v1')
        atomic_json(r.root/path,policy)
    c=dual.DualController(r.root,r.base,now=lambda:r.clock.value,monotonic=lambda:r.ticks.value,sleep=lambda _:None,
        windows={s:(r.clock.value,r.clock.value+timedelta(hours=3)) for s in dual.ORDER},activation_id='expanded-test',
        source_verifier=lambda _:None,authorization_factory=r.factory,monitor_factories={s:Child for s in dual.ORDER},
        journal_refresh=lambda _:None,maximum_risk_percent=2,risk_mode='minimum_lot_rescue_v1',portfolio_scope=MULTI_SCOPE)
    reconciled=[]
    def reconcile(symbol,row):
        reconciled.append(row['session_id'])
        row.update(status='closed_verified',exit_time_msc=int(r.clock.value.timestamp()*1000))
        path=r.root/'logs/sessions'/row['session_id']/'demo_test_state.json'
        state=json.loads(path.read_text());state['status']='closed_verified';atomic_json(path,state)
    c.reconcile_absent_position=reconcile
    r.base.signals={s:True for s in dual.ORDER}
    return Record(r=r,c=c,reconciled=reconciled)


def test_controller_adds_second_fresh_setup_and_caps_four_without_same_candle_duplicates(multi_controller):
    m=multi_controller;c=m.c;r=m.r
    c.tick();c.tick()
    assert r.base.sends==['EURUSDm','XAUUSDm']
    r.clock.value+=timedelta(hours=1);c.tick();c.tick()
    assert r.base.sends==['EURUSDm','XAUUSDm','EURUSDm','XAUUSDm']
    assert c.status['open_position_count']==4 and c.status['maximum_combined_risk_percent']==8
    assert all(i['open_positions']==2 for i in c.status['instruments'].values())
    r.clock.value+=timedelta(minutes=5);c.tick()
    assert len(r.base.sends)==4


def test_controller_reconciles_exact_absent_ticket_with_same_symbol_sibling_present(multi_controller):
    m=multi_controller;c=m.c;r=m.r
    c.tick();r.clock.value+=timedelta(hours=1);c.tick()
    first=r.base.positions.pop(0)
    target=next(row for s,row in c.owned_records() if json.loads((r.root/'logs/sessions'/row['session_id']/'demo_test_state.json').read_text())['position_ticket']==first.ticket)
    r.base.signals={};r.clock.value+=timedelta(seconds=10);c.tick()
    assert m.reconciled==[target['session_id']]
    assert target['status']=='closed_verified' and len(r.base.positions)==3
    r.base.positions.clear();c.tick()
    assert len(m.reconciled)==4 and all(row['status']=='closed_verified' for _,row in c.owned_records())
