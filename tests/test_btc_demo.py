"""Native BTC safety tests use a complete fake broker; no MT5/network needed."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as R
from unittest.mock import Mock

import pandas as pd
import pytest

from engine.btc_demo import (BTCBlocked, BTCOutcomeUncertain, MAGIC, SYMBOL,
                             _closed_trade, prepare_plan, run_session, send_once)
from engine.gold_connector import GoldConnector
from engine.market_monitor import monitor_lock
from engine.mt5_connector import DemoAccountRequiredError

NOW = datetime(2026, 11, 22, 1, 0, 5, tzinfo=timezone.utc)


@pytest.fixture
def rig(tmp_path):
    api = R(ACCOUNT_TRADE_MODE_DEMO=0, ACCOUNT_TRADE_MODE_CONTEST=1, ACCOUNT_TRADE_MODE_REAL=2,
            ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, POSITION_TYPE_BUY=0, POSITION_TYPE_SELL=1,
            ORDER_FILLING_FOK=0, ORDER_FILLING_IOC=1, SYMBOL_TRADE_EXECUTION_REQUEST=0,
            SYMBOL_TRADE_EXECUTION_INSTANT=1, SYMBOL_TRADE_EXECUTION_MARKET=2,
            SYMBOL_TRADE_MODE_FULL=4, SYMBOL_TRADE_MODE_LONGONLY=1, SYMBOL_TRADE_MODE_SHORTONLY=2,
            TRADE_ACTION_DEAL=1, ORDER_TIME_GTC=0, TRADE_RETCODE_DONE=10009,
            ORDER_STATE_FILLED=4, DEAL_ENTRY_IN=0, DEAL_ENTRY_OUT=1, DEAL_ENTRY_OUT_BY=3,
            TIMEFRAME_H1=16385)
    account = R(login=464192812, company='Exness Technologies Ltd', server='Exness-MT5Trial17',
                currency='USD', trade_mode=0, trade_allowed=True, trade_expert=True,
                balance=500., equity=500., margin_free=500.)
    terminal = R(connected=True, trade_allowed=True, tradeapi_disabled=False)
    info = R(name=SYMBOL, currency_base='BTC', currency_profit='USD', chart_mode=0,
             visible=True, trade_contract_size=1., volume_min=.01, volume_max=200.,
             volume_step=.01, point=.01, trade_tick_size=.01, digits=2,
             trade_stops_level=0, trade_freeze_level=0, trade_mode=4, trade_exemode=2,
             filling_mode=3, order_mode=127)
    tick = R(bid=60829.05, ask=60835.45, time_msc=int(NOW.timestamp() * 1000))
    book = {'positions': (), 'orders': (), 'history_orders': {}, 'history_deals': {}}
    api.initialize, api.shutdown = Mock(return_value=True), Mock()
    api.account_info, api.terminal_info = Mock(return_value=account), Mock(return_value=terminal)
    api.symbol_info, api.symbol_info_tick = Mock(return_value=info), Mock(return_value=tick)
    api.symbol_select = Mock(return_value=True)
    api.positions_get = Mock(side_effect=lambda: book['positions'])
    api.orders_get = Mock(side_effect=lambda: book['orders'])
    api.last_error = Mock(return_value=(0, 'fake'))
    api.order_calc_profit = Mock(side_effect=lambda side, symbol, volume, entry, exit_price:
                                (exit_price - entry) * volume * info.trade_contract_size * (1 if side == 0 else -1))
    api.order_calc_margin = Mock(return_value=10.)
    api.order_check = Mock(return_value=R(retcode=0, comment='Done'))
    api.history_orders_get = Mock(side_effect=lambda ticket: book['history_orders'].get(ticket, ()))
    api.history_deals_get = Mock(side_effect=lambda ticket=None, position=None:
                                book['history_deals'].get(ticket, ()) if position is None else
                                tuple(d for rows in book['history_deals'].values() for d in rows if d.position_id == position))

    def send(request):
        price = tick.ask if request['type'] == 0 else tick.bid
        book['history_orders'][111] = (R(ticket=111, symbol=SYMBOL, magic=MAGIC, type=request['type'], state=4),)
        book['history_deals'][222] = (R(ticket=222, order=111, position_id=333, symbol=SYMBOL,
                                       magic=MAGIC, type=request['type'], entry=0, volume=request['volume'],
                                       price=price, profit=0., commission=-.02, fee=0., swap=0.),)
        book['positions'] = (R(ticket=444, identifier=333, symbol=SYMBOL, magic=MAGIC,
                               type=request['type'], volume=request['volume'], price_open=price,
                               sl=request['sl'], tp=request['tp']),)
        return R(retcode=10009, order=111, deal=222, volume=request['volume'], price=price,
                 comment='Done', request_id=1, retcode_external=0)

    api.order_send = Mock(side_effect=send)
    times = pd.date_range(end=NOW.replace(minute=0, second=0), periods=1401, freq='h')
    rows = [{'time': int(t.timestamp()), 'open': 60800., 'high': 60900., 'low': 60700.,
             'close': 60850., 'tick_volume': 1, 'spread': 640, 'real_volume': 0} for t in times]
    api.copy_rates_from_pos = Mock(return_value=rows)
    connector = GoldConnector(api=api)
    connector.connect()
    intent = {'side': 'buy', 'selected_setup': 'B1', 'decision_time_utc': NOW.replace(second=0).isoformat(),
              'atr14': 100., 'stop_distance': 200., 'stop_price': 60635.45, 'reward_r': 2,
              'setup_signals': {'B1': {'side': 'buy'}}, 'reason': 'fake confirmed setup'}
    yield R(api=api, account=account, terminal=terminal, info=info, tick=tick,
            book=book, connector=connector, intent=intent, path=tmp_path / 'attempt.json', root=tmp_path)
    connector.close()


def plan(rig):
    return prepare_plan(rig.connector, rig.intent, now=NOW)


def test_read_only_plan_uses_broker_profit_minimum_step_and_correct_btc_units(rig):
    result = plan(rig)
    assert result['request']['volume'] == .02
    assert result['request']['deviation'] == 200  # $2, not two $0.01 points.
    assert result['request']['type_filling'] == 0
    assert result['request']['sl'] < rig.tick.ask < result['request']['tp']
    assert result['risk']['planned_risk_usd'] <= 5
    assert result['risk']['fee_reserve_usd'] == .25
    assert result['risk']['estimated_stop_loss_usd'] == pytest.approx(4.08)
    rig.api.order_calc_profit.assert_called()
    rig.api.order_check.assert_not_called()
    rig.api.order_send.assert_not_called()


def test_contract_size_is_dynamic_not_assumed_one_btc(rig):
    rig.info.trade_contract_size = 10
    with pytest.raises(BTCBlocked, match='minimum BTC lot'):
        plan(rig)


@pytest.mark.parametrize('mode', [1, 2, None, False, 99])
def test_real_contest_unknown_account_modes_fail_even_with_demo_server_name(rig, mode):
    rig.account.trade_mode = mode
    with pytest.raises(DemoAccountRequiredError):
        plan(rig)
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('condition', ['stale', 'future', 'crossed', 'permissions', 'external', 'account',
                                     'expert', 'disconnected', 'positions', 'orders', 'inventory_unknown',
                                     'wrong_symbol', 'wrong_product', 'chart_unknown', 'no_sl', 'no_tp',
                                     'step', 'maximum', 'margin', 'spread', 'closed_mode', 'no_filling'])
def test_preflight_rejects_unverified_or_unsafe_conditions(rig, condition):
    if condition == 'stale': rig.tick.time_msc -= 16000
    elif condition == 'future': rig.tick.time_msc += 3000
    elif condition == 'crossed': rig.tick.ask = rig.tick.bid - 1
    elif condition == 'permissions': rig.terminal.trade_allowed = False
    elif condition == 'external': rig.terminal.tradeapi_disabled = True
    elif condition == 'account': rig.account.trade_allowed = False
    elif condition == 'expert': rig.account.trade_expert = False
    elif condition == 'disconnected': rig.terminal.connected = False
    elif condition == 'positions': rig.book['positions'] = (R(symbol='EURUSDm'),)
    elif condition == 'orders': rig.book['orders'] = (R(symbol='XAUUSDm'),)
    elif condition == 'inventory_unknown': rig.book['positions'] = None
    elif condition == 'wrong_symbol': rig.info.name = 'BTCUSDT'
    elif condition == 'wrong_product': rig.info.currency_base = 'ETH'
    elif condition == 'chart_unknown': rig.info.chart_mode = None
    elif condition == 'no_sl': rig.info.order_mode &= ~16
    elif condition == 'no_tp': rig.info.order_mode &= ~32
    elif condition == 'step': rig.info.volume_step = .003
    elif condition == 'maximum': rig.info.volume_max = .001
    elif condition == 'margin': rig.api.order_calc_margin.return_value = 490.
    elif condition == 'spread': rig.tick.ask = rig.tick.bid + 30
    elif condition == 'closed_mode': rig.info.trade_mode = 3
    elif condition == 'no_filling': rig.info.filling_mode = 0
    with pytest.raises(BTCBlocked):
        plan(rig)
    rig.api.order_check.assert_not_called()
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('field,value', [('side', 'hold'), ('selected_setup', 'B5'), ('atr14', float('nan')),
                                        ('reward_r', 3), ('stop_distance', -1), ('decision_time_utc', '2026-11-22T00:00:00+00:00'),
                                        ('decision_time_utc', '2026-11-22T01:00:00'),
                                        ('decision_time_utc', '2026-11-22T01:00:01+00:00')])
def test_invalid_or_old_intents_are_never_sent(rig, field, value):
    rig.intent[field] = value
    with pytest.raises(BTCBlocked):
        plan(rig)
    rig.api.order_send.assert_not_called()


def test_tick_rounding_is_outward_and_sell_supported(rig):
    rig.intent.update(side='sell', stop_distance=200.004, stop_price=rig.tick.bid + 200.004)
    result = plan(rig)
    assert result['request']['sl'] >= rig.tick.bid + 200.004
    assert result['request']['tp'] < rig.tick.bid
    assert result['request']['type'] == 1


def test_minimum_size_not_forced_and_volume_cap_enforced(rig):
    rig.account.balance = rig.account.equity = 5
    with pytest.raises(BTCBlocked, match='minimum BTC lot'):
        plan(rig)
    rig.account.balance = rig.account.equity = 100000
    assert plan(rig)['request']['volume'] == .1


def test_order_check_rejection_consumes_ledger_without_send(rig):
    result = plan(rig)
    rig.api.order_check.return_value = R(retcode=10018, comment='Market closed')
    with pytest.raises(BTCBlocked, match='order_check'):
        send_once(rig.connector, result, rig.path, now=lambda: NOW)
    assert json.loads(rig.path.read_text())['status'] == 'blocked_before_send'
    rig.api.order_send.assert_not_called()
    with pytest.raises(FileExistsError):
        send_once(rig.connector, result, rig.path, now=lambda: NOW)
    assert rig.api.order_check.call_count == 1


@pytest.mark.parametrize('change', ['real', 'quote', 'cash', 'position', 'contract', 'permission'])
def test_conditions_rechecked_after_order_check_and_before_send(rig, change):
    result = plan(rig)
    def check(request):
        if change == 'real': rig.account.trade_mode = 2
        elif change == 'quote': rig.tick.time_msc -= 30000
        elif change == 'cash': rig.account.equity = 100
        elif change == 'position': rig.book['positions'] = (R(symbol='EURUSDm'),)
        elif change == 'contract': rig.info.trade_contract_size = 100
        elif change == 'permission': rig.terminal.trade_allowed = False
        return R(retcode=0)
    rig.api.order_check.side_effect = check
    with pytest.raises((BTCBlocked, DemoAccountRequiredError)):
        send_once(rig.connector, result, rig.path, now=lambda: NOW)
    rig.api.order_send.assert_not_called()


def test_source_verification_repeated_before_send(rig):
    verify = Mock(side_effect=[None, ValueError('source changed')])
    with pytest.raises(ValueError, match='source changed'):
        send_once(rig.connector, plan(rig), rig.path, now=lambda: NOW, verify_sources=verify)
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('field,value', [('symbol', 'EURUSDm'), ('action', 5), ('magic', 0),
                                        ('type', 1), ('type_filling', 99), ('deviation', 2),
                                        ('comment', 'unbound')])
def test_mutated_request_cannot_send_another_instrument_or_unchecked_operation(rig, field, value):
    result = plan(rig)
    result['request'][field] = value
    with pytest.raises(BTCBlocked, match='altered'):
        send_once(rig.connector, result, rig.path, now=lambda: NOW)
    rig.api.order_check.assert_not_called()
    rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('move,allowed', [(.1, True), (1., True), (3., False)])
def test_final_tick_movement_uses_funded_envelope_instead_of_exact_equality(rig, move, allowed):
    result = plan(rig)
    original = rig.api.order_calc_margin.side_effect
    calls = {'count': 0}
    def margin(*args):
        calls['count'] += 1
        if calls['count'] == 2:  # final native risk calc after order_check
            rig.tick.bid += move
            rig.tick.ask += move
        return 10.
    rig.api.order_calc_margin.side_effect = margin
    if allowed:
        state = send_once(rig.connector, result, rig.path, now=lambda: NOW)
        assert state['status'] == 'filled_verified'
        assert state['final_quote']['ask'] != state['final_risk']['fresh_entry_reference']
        assert state['fill']['adverse_slippage_price'] == pytest.approx(move)
    else:
        with pytest.raises(BTCBlocked, match='envelope'):
            send_once(rig.connector, result, rig.path, now=lambda: NOW)
        rig.api.order_send.assert_not_called()


def test_clock_is_rechecked_after_blocking_broker_risk_calls(rig):
    result = plan(rig)
    clock = {'time': NOW}
    def margin(*args):
        clock['time'] += timedelta(seconds=16)
        return 10.
    rig.api.order_calc_margin.side_effect = margin
    with pytest.raises(BTCBlocked, match='stale'):
        send_once(rig.connector, result, rig.path, now=lambda: clock['time'])
    rig.api.order_send.assert_not_called()


def test_account_is_rechecked_after_final_blocking_margin_call(rig):
    result = plan(rig)
    calls = {'count': 0}
    def margin(*args):
        calls['count'] += 1
        if calls['count'] == 2:
            rig.account.trade_mode = 2
        return 10.
    rig.api.order_calc_margin.side_effect = margin
    with pytest.raises(DemoAccountRequiredError):
        send_once(rig.connector, result, rig.path, now=lambda: NOW)
    rig.api.order_send.assert_not_called()


def test_send_boundary_is_durable_and_actual_deal_position_verified(rig):
    original_plan = plan(rig)
    native_send = rig.api.order_send.side_effect
    def send(request):
        saved = json.loads(rig.path.read_text())
        assert saved['status'] == 'sending_outcome_unknown'
        assert saved['send_calls'] == 1
        return native_send(request)
    rig.api.order_send.side_effect = send
    result = send_once(rig.connector, original_plan, rig.path, now=lambda: NOW)
    assert result['status'] == 'filled_verified'
    assert result['fill']['position_id'] == 333
    assert result['fill']['order_ticket'] == 111
    assert result['fill']['deal_ticket'] == 222
    assert result['fill']['entry_costs']['commission'] == -.02
    assert rig.api.order_send.call_count == 1
    with pytest.raises(FileExistsError):
        send_once(rig.connector, original_plan, rig.path, now=lambda: NOW)


@pytest.mark.parametrize('failure', ['none', 'exception', 'partial', 'no_deal', 'no_position', 'bad_link', 'no_protection'])
def test_ambiguous_or_unverified_fills_fail_closed_with_no_retry(rig, failure):
    native_send = rig.api.order_send.side_effect
    def send(request):
        if failure == 'none': return None
        if failure == 'exception': raise OSError('IPC interrupted')
        result = native_send(request)
        if failure == 'partial': result.retcode = 10010
        elif failure == 'no_deal': rig.book['history_deals'] = {}
        elif failure == 'no_position': rig.book['positions'] = ()
        elif failure == 'bad_link': rig.book['history_deals'][222][0].order = 999
        elif failure == 'no_protection': rig.book['positions'][0].sl = 0
        return result
    rig.api.order_send.side_effect = send
    with pytest.raises(BTCOutcomeUncertain):
        send_once(rig.connector, plan(rig), rig.path, now=lambda: NOW)
    state = json.loads(rig.path.read_text())
    assert state['status'] == 'outcome_uncertain'
    assert state['send_calls'] == 1
    assert rig.api.order_send.call_count == 1


def test_actual_close_costs_and_volume_verified_without_closing_anything(rig):
    state = send_once(rig.connector, plan(rig), rig.path, now=lambda: NOW)
    assert _closed_trade(rig.connector, state) is None
    rig.book['positions'] = ()
    with pytest.raises(BTCOutcomeUncertain, match='Closing deal volume'):
        _closed_trade(rig.connector, state)
    rig.book['history_deals'][555] = (R(ticket=555, order=556, position_id=333, symbol=SYMBOL,
                                       magic=MAGIC, type=1, entry=1, volume=state['fill']['volume'],
                                       price=state['fill']['tp'], profit=8., commission=-.02,
                                       fee=-.01, swap=-.5, reason=5, time_msc=int(NOW.timestamp() * 1000)),)
    closed = _closed_trade(rig.connector, state)
    assert closed['net_pnl_usd'] == pytest.approx(7.45)
    assert closed['closing_deal_tickets'] == [555]
    assert closed['closed_at_utc'] == NOW.isoformat()
    assert rig.api.order_send.call_count == 1


def test_actual_cost_fields_are_not_silently_replaced_with_zero(rig):
    native_send = rig.api.order_send.side_effect
    def send(request):
        result = native_send(request)
        del rig.book['history_deals'][222][0].fee
        return result
    rig.api.order_send.side_effect = send
    with pytest.raises(BTCOutcomeUncertain, match='fee'):
        send_once(rig.connector, plan(rig), rig.path, now=lambda: NOW)
    assert rig.api.order_send.call_count == 1


def run(rig, **options):
    def factory(**kwargs):
        return GoldConnector(api=rig.api, **kwargs)
    evaluator = options.pop('evaluate', lambda frame, as_of, setup_ids=None: dict(rig.intent))
    return run_session(rig.root, 'test-btc-session', NOW + timedelta(hours=1),
                       connector_factory=factory, now=lambda: NOW, sleep=lambda _: None,
                       evaluate=evaluator,
                       verify_sources=lambda: None, max_iterations=2, **options)


def test_bounded_session_uses_one_attempt_per_closed_candle(rig):
    result = run(rig)
    assert result['status'] == 'stopped'
    assert result['orders_sent'] == 1
    assert result['open_positions_count'] == 1
    assert result['stop_reason'] == 'iteration_limit'
    assert result['active'] is False
    assert rig.api.order_send.call_count == 1
    assert len(list((rig.root / 'logs/btc_monitor/decisions').glob('*.json'))) == 1
    assert 'entry_verified' in (rig.root / 'logs/btc_monitor/events.jsonl').read_text()


def test_restart_does_not_repeat_old_blocked_decision(rig):
    rig.api.order_check.return_value = R(retcode=10018, comment='Market closed')
    first, second = run(rig), run(rig)
    assert first['entry_attempts'] == 1
    assert second['entry_attempts'] == 0
    assert rig.api.order_check.call_count == 1
    rig.api.order_send.assert_not_called()


def test_unresolved_send_stops_next_session_without_retry(rig):
    rig.api.order_send.return_value = None
    rig.api.order_send.side_effect = None
    first = run(rig)
    assert first['status'] == 'failed'
    assert first['orders_sent'] is None
    assert first['open_positions_count'] is None
    second = run(rig)
    assert second['status'] == 'failed'
    assert 'unresolved' in second['stop_reason']
    assert rig.api.order_send.call_count == 1


def test_expired_session_reconciles_actual_broker_close_before_next_session(rig):
    clock = {'now': NOW}
    def factory(**kwargs):
        return GoldConnector(api=rig.api, **kwargs)
    def wait(_):
        clock['now'] = NOW + timedelta(seconds=10)
    first = run_session(rig.root, 'expires', NOW + timedelta(seconds=10), connector_factory=factory,
                        now=lambda: clock['now'], sleep=wait,
                        evaluate=lambda frame, as_of, setup_ids=None: rig.intent, verify_sources=lambda: None)
    assert first['stop_reason'] == 'deadline_reached'
    assert first['open_positions_count'] == 1
    path = next((rig.root / 'logs/btc_monitor/attempts').glob('*.json'))
    state = json.loads(path.read_text())
    assert state['status'] == 'filled_verified'
    rig.book['positions'] = ()
    rig.book['history_deals'][555] = (R(ticket=555, order=556, position_id=333, symbol=SYMBOL,
                                       magic=MAGIC, type=1, entry=1, volume=state['fill']['volume'],
                                       price=state['fill']['tp'], profit=8., commission=-.02,
                                       fee=-.01, swap=-.5, reason=5, time_msc=int(NOW.timestamp() * 1000)),)
    second = run(rig)
    assert second['status'] == 'stopped'
    assert second['reconciled_trades'] == 1
    assert second['realized_net_pnl_usd'] == pytest.approx(7.45)
    assert second['orders_sent'] == 0
    assert json.loads(path.read_text())['status'] == 'closed_verified'
    assert rig.api.order_send.call_count == 1


def test_known_position_still_open_blocks_new_session_and_retains_protection(rig):
    first = run(rig)
    second = run(rig)
    assert first['open_positions_count'] == 1
    assert second['status'] == 'failed'
    assert 'existing protected BTC position' in second['stop_reason']
    assert second['open_positions_count'] == 1
    assert rig.api.order_send.call_count == 1
    assert rig.book['positions'][0].sl > 0


def test_unverified_close_cannot_release_known_position_ledger(rig):
    run(rig)
    rig.book['positions'] = ()
    result = run(rig)
    assert result['status'] == 'failed'
    assert result['execution_outcome_uncertain'] is True
    path = next((rig.root / 'logs/btc_monitor/attempts').glob('*.json'))
    assert json.loads(path.read_text())['status'] == 'filled_verified'
    assert rig.api.order_send.call_count == 1


def test_closed_candle_input_bytes_and_sha_are_saved_before_evaluation(rig):
    def evaluate(frame, as_of, setup_ids=None):
        files = list((rig.root / 'logs/btc_monitor/inputs').glob('*.csv'))
        assert len(files) == 1
        saved = pd.read_csv(files[0], index_col='timestamp_utc', float_precision='round_trip')
        assert len(saved) == len(frame)
        assert saved.close.tolist() == frame.close.tolist()
        return dict(rig.intent)
    result = run(rig, evaluate=evaluate)
    record = result['latest_decision']
    snapshot = rig.root / record['input_file']
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == record['input_sha256']
    assert b'\r\n' not in snapshot.read_bytes()
    attempt = json.loads(next((rig.root / 'logs/btc_monitor/attempts').glob('*.json')).read_text())
    assert attempt['plan']['input']['input_sha256'] == record['input_sha256']
    assert attempt['plan']['session']['run_id'] == result['run_id']


def test_excludes_disabled_setup_and_never_enters_old_h1_candle(rig):
    result = run(rig, setup_ids=('B2',))
    assert result['orders_sent'] == 0
    assert 'not enabled' in result['latest_decision']['reason']
    rig.api.order_send.assert_not_called()


def test_shared_monitor_lock_prevents_parallel_gold_eur_session(rig):
    with monitor_lock(rig.root / 'logs/monitor/monitor.lock'):
        with pytest.raises(RuntimeError, match='Another market monitor'):
            run(rig)
    assert not (rig.root / 'logs/btc_monitor/status.json').exists()
    rig.api.order_send.assert_not_called()


def test_stop_request_must_match_this_exact_run_identity(rig):
    rig.intent.update(side=None, selected_setup=None)
    folder = rig.root / 'logs/btc_monitor'
    folder.mkdir(parents=True)
    (folder / 'stop.json').write_text(json.dumps({'run_id': 'old-run'}))
    def factory(**kwargs):
        return GoldConnector(api=rig.api, **kwargs)
    def wait(_):
        current = json.loads((folder / 'status.json').read_text())
        (folder / 'stop.json').write_text(json.dumps({'run_id': current['run_id']}))
    result = run_session(rig.root, 'stop-test', NOW + timedelta(hours=1), connector_factory=factory,
                         now=lambda: NOW, sleep=wait, evaluate=lambda frame, as_of, setup_ids=None: rig.intent,
                         verify_sources=lambda: None, max_iterations=3)
    assert result['stop_reason'] == 'user_stop_requested'
    assert result['latest_decision']  # stale request did not stop the first poll
    rig.api.order_send.assert_not_called()


def test_stop_received_during_order_check_prevents_send(rig):
    folder = rig.root / 'logs/btc_monitor'
    def check(request):
        status = json.loads((folder / 'status.json').read_text())
        (folder / 'stop.json').write_text(json.dumps({'run_id': status['run_id']}))
        return R(retcode=0)
    rig.api.order_check.side_effect = check
    result = run(rig)
    assert result['status'] == 'stopped'
    assert result['stop_reason'] == 'user_stop_requested'
    assert result['latest_decision']['status'] == 'BLOCKED'
    rig.api.order_send.assert_not_called()


def test_source_verifier_and_bounded_deadline_required(rig):
    with pytest.raises(ValueError, match='source verifier'):
        run_session(rig.root, 'test', NOW + timedelta(hours=1), now=lambda: NOW)
    with pytest.raises(ValueError, match='24 hours'):
        run_session(rig.root, 'test', NOW + timedelta(hours=25), now=lambda: NOW)
    with pytest.raises(ValueError, match='setup IDs'):
        run(rig, setup_ids=('B1', 'B1'))


def test_no_api_trade_methods_called_for_nonsignal_candle(rig):
    rig.intent.update(side=None, selected_setup=None, reason='No technical setup')
    result = run(rig)
    assert result['orders_sent'] == 0
    assert result['latest_decision']['reason'] == 'No technical setup'
    rig.api.order_check.assert_not_called()
    rig.api.order_send.assert_not_called()


def test_actual_pure_rule_module_receives_utc_indexed_closed_native_history(rig):
    from analytics.btc_setups import evaluate
    result = run(rig, evaluate=evaluate)
    assert result['status'] == 'stopped'
    assert result['latest_decision']['intent']['guards']['completed_bars'] == 1400
    assert result['latest_decision']['intent']['reason'] == 'no_technical_setup'
    rig.api.order_send.assert_not_called()


def test_structural_anchor_is_not_shifted_by_entry_quote_and_cannot_be_resurrected(rig):
    rig.intent['stop_price'] = rig.tick.ask - 400
    result = plan(rig)
    assert result['request']['sl'] == rig.intent['stop_price']
    rig.tick.bid = rig.intent['stop_price'] - 1
    rig.tick.ask = rig.tick.bid + 6.4
    with pytest.raises(BTCBlocked, match='crossed'):
        plan(rig)
