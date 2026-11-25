"""Native fake-broker one-each portfolio admission and actual entry verification."""
from dataclasses import asdict
from datetime import timedelta
import json
from types import SimpleNamespace as Record

import pytest

from test_demo_executor import rig
from test_gold_execution import gold, candidate
from engine.demo_executor import DemoExecutor, MAGIC
from engine.demo_series import DemoSeriesAuthorization
from engine.portfolio_guard import SCOPE, TwoInstrumentGuard
from engine.execution_audit import atomic_json


@pytest.fixture
def portfolio(gold):
    root=gold.rig.directory
    guard=TwoInstrumentGuard(root)
    gold.connector.portfolio_guard=guard
    identifier='e'*32
    auth=dict(authorization_id=identifier,portfolio_scope=SCOPE,symbol='EURUSDm',
        account_identity={'login':464192812,'server':'Exness-MT5Trial17'})
    atomic_json(root/'authorizations'/f'{identifier}.json',auth)
    guard.register('EURUSDm',Record(data=auth,authorization_id=identifier))
    position=Record(ticket=888,identifier=999,symbol='EURUSDm',magic=MAGIC,type=0,
        volume=.01,price_open=1.1,sl=1.099,tp=1.102)
    state=dict(status='open_verified',account_identity=auth['account_identity'],symbol='EURUSDm',
        position_id=999,position_ticket=888,position_type=0,executed_volume=.01,
        request={'sl':1.099,'tp':1.102},entry_verified_deals=[{'ticket':777}],
        session_authorization={'authorization_id':identifier},
        protection=dict(policy={'target_mode':'adaptive_pullback_v2','risk_percent':1,'max_lots':.1},
            tick_size=.00001,planned_loss_with_reserves=1.2,risk_budget=5.,risk_basis_amount=500.))
    path=root/'sessions/other-eur/demo_test_state.json'
    atomic_json(path,state)
    gold.rig.ledger['positions']=[position]
    return Record(gold=gold,guard=guard,position=position,state=state,path=path,root=root)


def test_valid_other_instrument_is_accepted_but_same_symbol_is_blocked(portfolio):
    p=portfolio
    p.guard.inventory(p.gold.connector,(p.position,),(),symbol='XAUUSDm',opening=True)
    with pytest.raises(ValueError,match='already has a position'):
        p.guard.inventory(p.gold.connector,(p.position,),(),symbol='EURUSDm',opening=True)


@pytest.mark.parametrize('change',['duplicate','third','unknown_symbol','pending','unknown_inventory'])
def test_duplicate_third_unknown_or_pending_inventory_is_rejected(portfolio,change):
    p=portfolio;positions=(p.position,);orders=()
    if change=='duplicate':positions=(p.position,p.position)
    if change=='third':positions=(p.position,p.position,p.position)
    if change=='unknown_symbol':p.position.symbol='BTCUSDm'
    if change=='pending':orders=(Record(ticket=9),)
    if change=='unknown_inventory':positions=None
    with pytest.raises(ValueError):p.guard.inventory(p.gold.connector,positions,orders,symbol='XAUUSDm',opening=True)


@pytest.mark.parametrize('change',['magic','volume','sl','tp','direction','missing_proof','wrong_auth','risk','uncertain','missing_native','wrong_account','real','contest'])
def test_unverified_or_changed_existing_position_blocks_second_entry(portfolio,change):
    p=portfolio;positions=(p.position,)
    if change=='magic':p.position.magic=0
    if change=='volume':p.position.volume=.02
    if change=='sl':p.position.sl=0
    if change=='tp':p.position.tp=0
    if change=='direction':p.position.type=1
    if change=='missing_proof':p.state['entry_verified_deals']=[]
    if change=='wrong_auth':p.state['session_authorization']['authorization_id']='f'*32
    if change=='risk':p.state['protection']['policy']['risk_percent']=5
    if change=='uncertain':p.state['status']='open_unverified'
    if change=='missing_native':positions=()
    if change=='wrong_account':p.gold.rig.account.login=1234
    if change=='real':p.gold.rig.account.trade_mode=2
    if change=='contest':p.gold.rig.account.trade_mode=1
    atomic_json(p.path,p.state)
    with pytest.raises((ValueError,RuntimeError)):
        p.guard.inventory(p.gold.connector,positions,(),symbol='XAUUSDm',opening=True)
    p.gold.rig.api.order_send.assert_not_called()


def test_combined_current_budget_is_capped_at_two_percent(portfolio):
    p=portfolio
    p.state['protection']['planned_loss_with_reserves']=4.99
    atomic_json(p.path,p.state)
    p.gold.rig.account.balance=p.gold.rig.account.equity=480.
    assert p.guard.budget(p.gold.connector,'XAUUSDm',{'planned_loss_with_reserves':4.6})==(9.59,9.6)
    with pytest.raises(ValueError,match='2%'):
        p.guard.budget(p.gold.connector,'XAUUSDm',{'planned_loss_with_reserves':4.7})


@pytest.mark.parametrize('side',['buy','sell'])
def test_actual_fake_gold_entry_verifies_while_eur_position_remains_unchanged(portfolio,monkeypatch,side):
    p=portfolio;g=p.gold
    intent=candidate(g,monkeypatch,side)
    path=p.root/'gold_policy.json';path.write_text(json.dumps(asdict(g.policy)))
    auth=DemoSeriesAuthorization.create(g.connector,p.root,path,expires_at=g.clock.now+timedelta(hours=1),
        user_instruction='One Gold and one EUR/USD DEMO position, 1% each, 2% combined.')
    p.guard.register('XAUUSDm',auth)
    assert auth.data['portfolio_scope']==SCOPE
    original=g.rig.api.order_send.side_effect
    def send(request):
        result=original(request)
        g.rig.ledger['positions'].append(p.position)
        return result
    g.rig.api.order_send.side_effect=send
    executor=DemoExecutor(g.connector,p.root,session_id='portfolio-gold',strategy_policy=g.policy,strategy_intent=intent)
    preview=executor.prepare_entry()
    assert preview['ready']
    result=executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='c'*32)
    assert result['status']=='open_verified' and result['executed_volume']==.01
    assert len(g.rig.ledger['positions'])==2 and p.position.sl==1.099 and p.position.volume==.01
    assert g.rig.api.order_send.call_count==1
    duplicate=DemoExecutor(g.connector,p.root,session_id='portfolio-duplicate',strategy_policy=g.policy,strategy_intent=intent)
    with pytest.raises((ValueError,PermissionError)):duplicate.prepare_entry()
    assert g.rig.api.order_send.call_count==1


def test_default_connector_still_blocks_any_existing_position(portfolio,monkeypatch):
    p=portfolio;g=p.gold
    intent=candidate(g,monkeypatch)
    del g.connector.portfolio_guard
    executor=DemoExecutor(g.connector,p.root,session_id='default-one-position',strategy_policy=g.policy,strategy_intent=intent)
    preview=executor.prepare_entry()
    assert not preview['ready']
    g.rig.api.order_send.assert_not_called()


def test_portfolio_authority_cannot_be_used_without_its_scope_guard(portfolio):
    p=portfolio;g=p.gold
    path=p.root/'gold_policy.json';path.write_text(json.dumps(asdict(g.policy)))
    auth=DemoSeriesAuthorization.create(g.connector,p.root,path,expires_at=g.clock.now+timedelta(hours=1),user_instruction='One per instrument.')
    del g.connector.portfolio_guard
    with pytest.raises(ValueError,match='Portfolio scope'):auth.validate(g.connector)
    g.rig.api.order_send.assert_not_called()


def test_other_position_appearing_after_order_check_prevents_send(portfolio,monkeypatch):
    p=portfolio;g=p.gold
    intent=candidate(g,monkeypatch)
    path=p.root/'gold_policy.json';path.write_text(json.dumps(asdict(g.policy)))
    auth=DemoSeriesAuthorization.create(g.connector,p.root,path,expires_at=g.clock.now+timedelta(hours=1),user_instruction='One per instrument.')
    p.guard.register('XAUUSDm',auth)
    executor=DemoExecutor(g.connector,p.root,session_id='guarded-gold',strategy_policy=g.policy,strategy_intent=intent)
    preview=executor.prepare_entry()
    result=g.rig.api.order_check.return_value
    def check(request):
        g.rig.ledger['positions'].append(Record(symbol='XAUUSDm',ticket=9999,identifier=9999))
        return result
    g.rig.api.order_check.side_effect=check
    with pytest.raises((ValueError,PermissionError)):
        executor.open_authorized_session(preview['preview_id'],auth.authorization_id,monitor_run_id='c'*32)
    g.rig.api.order_send.assert_not_called()
