"""Offline accounting, evidence gates and documentation for actual demo records."""

from copy import deepcopy
from datetime import datetime, timezone
import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from analytics.trade_journal import build_journal, capture_broker_snapshot, load_sources, replay_signal, write_journal
from test_setup_research import bars


def state(*, position=100, closed=False, strategy=True):
    start = int(datetime(2026,11,13,11,tzinfo=timezone.utc).timestamp()*1000)
    entry = dict(ticket=position+1,order=position,position_id=position,time_msc=start,entry=0,type=1,
                 volume=.01,price=1.10493,profit=0.,commission=-.1,fee=-.01,swap=0.)
    result = dict(status='closed_verified' if closed else 'open_verified',
        account_identity=dict(login=1234,server='Demo'),
        account=dict(demo_verified=True,trade_mode=0,currency='USD',broker='Broker',server='Demo',account_id_masked='****1234'),
        symbol='EURUSDm',request=dict(type=1,sl=1.10745,tp=1.09902),
        quote=dict(spread_pips=.8),point=.00001,position_id=position,position_ticket=position,
        executed_volume=.01,executed_price=1.10493,open_result=dict(order=position,deal=position+1),
        started_at_utc='2026-11-13T11:00:00+00:00',verified_at_utc='2026-11-13T11:00:01+00:00',
        entry_verified_deals=[entry],entry_slippage_points=0,
        protection=dict(mode='strategy_stop_first',risk_percent=1.,risk_budget=5.,risk_basis_amount=500.,
                        planned_loss_with_reserves=2.81,estimated_loss_at_sl=2.52,estimated_profit_at_tp=5.91,policy={'risk_percent':1.}),
        protection_at_fill=dict(planned_loss_with_reserves=2.79))
    if not strategy:
        result.pop('protection')
        result.pop('protection_at_fill')
    if closed:
        result['close_verified_deals'] = [dict(ticket=position+2,order=position+3,position_id=position,
            time_msc=start+600000,entry=1,type=0,reason=5,volume=.01,price=1.09902,
            profit=5.91,commission=-.1,fee=-.02,swap=-.03)]
        result['realized_net_pnl'] = 5.65
    return result


def save(root, value, name='trade'):
    path = root / 'logs/sessions' / name / 'demo_test_state.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value),encoding='utf-8')
    return path


def snapshot(value, *, open_position=False):
    deals = deepcopy(value['entry_verified_deals'] + value.get('close_verified_deals',[]))
    for d in deals:
        d['symbol'] = value['symbol']
    positions = [dict(identifier=value['position_id'],ticket=value['position_ticket'],symbol=value['symbol'],
                      type=1,volume=.01,profit=1.5,swap=-.04,sl=1.10745,tp=1.09902)] if open_position else []
    return dict(captured_at_utc='2026-11-13T12:00:00+00:00',account=deepcopy(value['account']),
        account_identity=deepcopy(value['account_identity']),deals=deals,positions=positions,
        orders=[dict(ticket=d['order'],position_id=d['position_id'],symbol=value['symbol'],type=d['type'],state=4) for d in deals])


def test_execution_tests_and_open_trades_do_not_become_strategy_results(tmp_path):
    save(tmp_path,state(position=100,closed=True,strategy=False),'test')
    save(tmp_path,state(position=200),'strategy')
    journal=build_journal(tmp_path/'logs')
    assert journal['strategy_summary']['completed']==0
    assert journal['strategy_summary']['win_rate_percent'] is None
    assert journal['strategy_summary']['expectancy_usd_per_completed_trade'] is None
    assert journal['strategy_summary']['profit_factor'] is None
    assert journal['execution_test_summary']['realized_net_pnl_usd']==pytest.approx(5.65)
    assert journal['strategy_groups'][0]['completed']==0
    open_trade=next(t for t in journal['trades'] if t['category']=='strategy')
    assert open_trade['realized_net_pnl_usd'] is None and open_trade['realized_r'] is None
    assert open_trade['unrealized_price_pnl_usd'] is None


def test_costs_are_counted_once_and_r_uses_frozen_preentry_plan(tmp_path):
    value=state(closed=True)
    source=save(tmp_path,value)
    original=source.read_bytes()
    journal=build_journal(tmp_path/'logs')
    t=journal['trades'][0]
    assert t['realized_net_pnl_usd']==pytest.approx(5.65)
    assert t['realized_r']==pytest.approx(5.65/2.81)  # Not the later 2.79 fill recomputation.
    assert t['commission_usd']==pytest.approx(-.2)
    assert t['fee_usd']==pytest.approx(-.03)
    assert t['booked_swap_usd']==pytest.approx(-.03)
    assert t['holding_seconds']==600
    assert t['entry_time_manila'].startswith('2026-11-13T19:00:00')
    assert t['planned_risk_percent_used']==pytest.approx(.562)
    write_journal(tmp_path,journal)
    write_journal(tmp_path,journal)
    with (tmp_path/'data/processed/trade_journal.csv').open(newline='',encoding='utf-8') as f:
        assert len(list(csv.DictReader(f)))==1
    assert len(list((tmp_path/'data/processed/trade_journal_history').glob('*.json')))==1
    assert source.read_bytes()==original
    assert 'Spread and slippage are already reflected' in next((tmp_path/'reports/trades').glob('*.md')).read_text(encoding='utf-8')


@pytest.mark.parametrize('reason,evaluation,rule_count,manual_count',[
    (0,'manual_override',0,1),(1,'manual_override',0,1),(2,'manual_override',0,1),
    (4,'rule_exit',1,0),(5,'rule_exit',1,0),(6,'other_exit',0,0)])
def test_manual_override_keeps_cash_but_is_excluded_from_exit_rule_results(tmp_path,reason,evaluation,rule_count,manual_count):
    value=state(closed=True)
    value['close_verified_deals'][0]['reason']=reason
    save(tmp_path,value)
    journal=build_journal(tmp_path/'logs')
    assert journal['trades'][0]['exit_evaluation']==evaluation
    assert journal['strategy_summary']['realized_net_pnl_usd']==pytest.approx(5.65)
    assert journal['strategy_rule_exit_summary']['completed']==rule_count
    assert journal['manual_override_summary']['completed']==manual_count
    write_journal(tmp_path,journal)
    assert 'SL/TP-rule results' in (tmp_path/'reports/trade_journal.md').read_text(encoding='utf-8')


def test_native_snapshot_can_document_exit_after_monitor_expiry_without_changing_execution_state(tmp_path):
    opened=state()
    path=save(tmp_path,opened)
    actual=snapshot(state(closed=True))
    result=build_journal(tmp_path/'logs',snapshot=actual)
    assert result['trades'][0]['status']=='closed'
    assert result['trades'][0]['exit_reason']=='take_profit'
    assert result['strategy_summary']['completed']==1
    assert result['strategy_summary']['realized_net_pnl_usd']==pytest.approx(5.65)
    assert json.loads(path.read_text())['status']=='open_verified'


def test_current_floating_pnl_is_not_a_completed_win(tmp_path):
    value=state()
    save(tmp_path,value)
    result=build_journal(tmp_path/'logs',snapshot=snapshot(value,open_position=True))
    trade=result['trades'][0]
    assert trade['status']=='open' and trade['unrealized_price_pnl_usd']==1.5
    assert trade['unrealized_swap_usd']==-.04
    assert trade['realized_net_pnl_usd'] is None and trade['booked_cashflows_usd']==pytest.approx(-.11)
    assert result['strategy_summary']['wins']==0 and result['strategy_summary']['completed']==0


def test_position_absence_without_exit_deal_does_not_prove_closed(tmp_path):
    value=state()
    save(tmp_path,value)
    result=build_journal(tmp_path/'logs',snapshot=snapshot(value))
    assert result['trades'][0]['status']=='unverified'
    assert result['strategy_summary']['completed']==0


def test_partial_exit_is_visible_and_excluded_from_completed_metrics(tmp_path):
    value=state()
    save(tmp_path,value)
    actual=snapshot(state(closed=True),open_position=True)
    actual['deals'][1].update(volume=.005,profit=2.955)
    actual['positions'][0]['volume']=.005
    result=build_journal(tmp_path/'logs',snapshot=actual)
    assert result['trades'][0]['status']=='partially_closed'
    assert result['strategy_summary']['completed']==0


@pytest.mark.parametrize('problem',['duplicate','missing_order','bad_entry_price','nonfinite','wrong_position'])
def test_corrupt_or_mismatched_broker_evidence_is_rejected(tmp_path,problem):
    value=state(closed=True)
    save(tmp_path,value)
    actual=snapshot(value)
    if problem=='duplicate': actual['deals'].append(deepcopy(actual['deals'][1]))
    if problem=='missing_order': actual['orders'].pop()
    if problem=='bad_entry_price': actual['deals'][0]['price']+=.001
    if problem=='nonfinite': actual['deals'][1]['fee']=float('nan')
    if problem=='wrong_position': actual['deals'][0]['position_id']=999
    with pytest.raises(ValueError): build_journal(tmp_path/'logs',snapshot=actual)


def test_identical_copies_are_deduplicated_conflicting_copies_are_rejected(tmp_path):
    value=state(closed=True)
    save(tmp_path,value,'a')
    copy=save(tmp_path,value,'b')
    result=build_journal(tmp_path/'logs')
    assert len(result['trades'])==1 and result['warnings']
    value['executed_price']+=.001
    copy.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='Conflicting'): build_journal(tmp_path/'logs')


def test_stale_snapshot_cannot_overwrite_newer_verified_close(tmp_path):
    closed=state(closed=True)
    closed['verified_at_utc']='2026-11-13T13:00:00+00:00'
    save(tmp_path,closed)
    result=build_journal(tmp_path/'logs',snapshot=snapshot(state(),open_position=True))
    assert result['trades'][0]['status']=='closed'
    assert 'ignored' in result['trades'][0]['warnings'][0]


@pytest.mark.parametrize('mode',[1,2,False,None])
def test_real_contest_missing_and_boolean_account_modes_are_rejected(tmp_path,mode):
    value=state()
    value['account']['trade_mode']=mode
    save(tmp_path,value)
    with pytest.raises(ValueError,match='DEMO'): build_journal(tmp_path/'logs')


def test_native_collector_only_reads_and_rejects_unavailable_history(tmp_path):
    value=state()
    save(tmp_path,value)
    sources,_=load_sources(tmp_path/'logs')
    sample=snapshot(value,open_position=True)
    def record(d):
        return SimpleNamespace(**d,_asdict=lambda:d)
    api=SimpleNamespace(history_deals_get=Mock(return_value=[record(d) for d in sample['deals']]),
        history_orders_get=Mock(return_value=[record(o) for o in sample['orders']]),
        positions_get=Mock(return_value=[record(p) for p in sample['positions']]),order_send=Mock(),order_check=Mock())
    connector=SimpleNamespace(account_identity=(1234,'Demo'),terminal_api=api,verify_demo_account=Mock(return_value=value['account']))
    actual=capture_broker_snapshot(connector,sources)
    assert len(actual['deals'])==1 and len(actual['positions'])==1
    api.order_send.assert_not_called()
    api.order_check.assert_not_called()
    api.history_deals_get.return_value=None
    with pytest.raises(ValueError,match='unavailable'): capture_broker_snapshot(connector,sources)
    connector.account_identity=(999,'Other')
    with pytest.raises(ValueError,match='exact recorded'): capture_broker_snapshot(connector,sources)


def test_offline_cli_does_not_initialize_mt5(tmp_path,monkeypatch):
    from scripts import update_trade_journal
    import engine.mt5_connector
    save(tmp_path,state())
    connector=Mock(side_effect=AssertionError('Offline journal must not start MT5.'))
    monkeypatch.setattr(engine.mt5_connector,'MT5Connector',connector)
    monkeypatch.setattr(update_trade_journal,'ROOT',tmp_path)
    assert update_trade_journal.main([])==0
    connector.assert_not_called()
    assert (tmp_path/'reports/trade_journal.md').is_file()


def test_entry_signal_replay_checks_frozen_hash_and_completed_bar_rules(tmp_path):
    data=bars()
    data.index=pd.date_range(end='2026-11-13T10:00:00Z',periods=len(data),freq='h')
    close=data.high.iloc[-21:-1].max()+.001
    data.iloc[-1,data.columns.get_loc('close')]=close
    data.iloc[-1,data.columns.get_loc('high')]=close+.0001
    raw=data.to_csv(float_format='%.10g',date_format='%Y-%m-%dT%H:%M:%SZ',lineterminator='\n').encode()
    fingerprint=hashlib.sha256(raw).hexdigest()
    directory=tmp_path/'logs'
    folder=directory/'strategy_inputs'
    folder.mkdir(parents=True)
    path=folder/f'EURUSDm_H1_{fingerprint}.csv'
    path.write_bytes(raw)
    value=state()
    value['protection']['strategy_intent']=dict(context_sha256=fingerprint,decision_time_utc='2026-11-13T11:00:00+00:00',side='buy',trigger='breakout')
    result=replay_signal(value,directory)
    assert result['status']=='verified' and result['h4_trend']=='bullish' and result['matching_trigger']
    path.write_bytes(raw+b'\n')
    assert replay_signal(value,directory)['status']=='unverified'


def test_empty_journal_has_undefined_metrics_and_valid_json(tmp_path):
    result=build_journal(tmp_path/'logs')
    assert result['strategy_summary']['win_rate_percent'] is None
    assert result['strategy_summary']['profit_factor'] is None
    write_journal(tmp_path,result)
    json.dumps(result,allow_nan=False)
