"""Offline sequential trades against the actual guarded executor and fake broker."""

import csv
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace as Record
from unittest.mock import Mock

import pytest

from test_demo_executor import rig, authorized_rig  # Shared deterministic broker fixture.
from engine import market_monitor
from engine.demo_authorization import DemoEntryAuthorization, load_entry_authorization
from engine.demo_executor import DemoExecutor, MAGIC
from engine.demo_series import DemoSeriesAuthorization, DemoSeriesMonitor
from engine.market_monitor import MonitorConfig, request_stop
from engine.strategy_orders import StrategyIntent


@pytest.fixture
def series(rig, monkeypatch):
    ctx = authorized_rig(rig, monkeypatch)
    approval = DemoSeriesAuthorization.create(rig.executor.connector, rig.directory, ctx.policy,
        expires_at=ctx.clock.now + timedelta(hours=3),
        user_instruction='Allow repeated sequential DEMO entries on valid new setups; one position at a time, 1% risk.')
    ctx.authorization = approval
    ctx.rig = rig
    ctx.signals = True
    ctx.side = 'buy'
    ctx.after_sleep = lambda: None
    ctx.entry_count = 0

    def send(request):
        assert 'position' not in request, 'Series must not submit an automatic manual close.'
        assert not rig.ledger['positions'], 'Series must never stack positions.'
        ctx.entry_count += 1
        order = 1000 + 10 * ctx.entry_count
        deal, identifier, ticket = order + 1, order + 2, order + 3
        price = rig.tick.ask if request['type'] == 0 else rig.tick.bid
        stamp = int(ctx.clock.now.timestamp() * 1000)
        rig.ledger['orders'][order] = (Record(ticket=order, symbol='EURUSDm', magic=MAGIC, type=request['type'], state=4,
                                            position_id=identifier),)
        rig.ledger['deals'][order] = (Record(ticket=deal, order=order, position_id=identifier, symbol='EURUSDm', magic=MAGIC,
            type=request['type'], entry=0, volume=request['volume'], price=price, profit=0., commission=-.02, fee=0., swap=0., time_msc=stamp),)
        rig.ledger['positions'] = [Record(ticket=ticket, identifier=identifier, symbol='EURUSDm', magic=MAGIC,
            type=request['type'], volume=request['volume'], price_open=price, sl=request['sl'], tp=request['tp'])]
        return Record(retcode=10009, order=order, deal=deal, volume=request['volume'], price=price,
                      bid=rig.tick.bid, ask=rig.tick.ask, comment='Done', request_id=ctx.entry_count, retcode_external=0)
    rig.api.order_send.side_effect = send
    def deals(*args, **kwargs):
        if 'ticket' in kwargs:
            return rig.ledger['deals'].get(kwargs['ticket'], ())
        return tuple(deal for items in rig.ledger['deals'].values() for deal in items)
    rig.api.history_deals_get.side_effect = deals
    rig.api.TIMEFRAME_H1 = 16385
    def rates(*args):
        hour = int(ctx.clock.now.replace(minute=0, second=0, microsecond=0).timestamp())
        return [{'time': hour - 3600}, {'time': hour}]
    rig.api.copy_rates_from_pos = Mock(side_effect=rates)
    def decision(*args):
        stamp = ctx.clock.now.replace(minute=0, second=0, microsecond=0).isoformat()
        report = dict(status='signal' if ctx.signals else 'WAIT', decision_time_utc=stamp,
                      orders_sent=0, reason='Test signal' if ctx.signals else 'No setup')
        intent = StrategyIntent('EURUSDm', ctx.side, 1.09812 if ctx.side == 'buy' else 1.102,
                                .001, stamp, f'{int(ctx.clock.now.timestamp()):064x}', 'pullback')
        return (intent if ctx.signals else None), report
    monkeypatch.setattr(market_monitor, 'latest_strategy_intent', decision)

    def advance(seconds):
        ctx.clock.now += timedelta(seconds=seconds)
        ctx.clock.ticks += seconds
        rig.tick.time_msc = int(ctx.clock.now.timestamp()*1000)
    ctx.advance = advance
    def sleep(seconds):
        advance(seconds)
        ctx.after_sleep()
    ctx.make = lambda **kw: DemoSeriesMonitor(rig.executor.connector, rig.directory, ctx.policy,
        authorization_id=approval.authorization_id, config=MonitorConfig(3, 60),
        report_path=rig.directory / 'series_report.md', now=lambda: ctx.clock.now,
        monotonic=lambda: ctx.clock.ticks, sleep=sleep, emit=lambda _: None, **kw)

    def close(*, profit=10., reason=5, partial=False):
        position = rig.ledger['positions'][0]
        order = 1000 + 10*ctx.entry_count + 4
        deal = order + 1
        price = position.tp if reason == 5 else position.sl
        volume = position.volume / 2 if partial else position.volume
        rig.ledger['orders'][order] = (Record(ticket=order, symbol='EURUSDm', type=1-position.type, state=4,
                                             position_id=position.identifier),)
        rig.ledger['deals'][order] = (Record(ticket=deal, order=order, position_id=position.identifier, symbol='EURUSDm',
             type=1-position.type, entry=1, reason=reason, volume=volume, price=price, profit=profit,
             commission=-.02, fee=0., swap=-.01, time_msc=int(ctx.clock.now.timestamp()*1000)),)
        if partial:
            position.volume -= volume
        else:
            rig.ledger['positions'] = []
        rig.account.balance += profit - .05
        rig.account.equity = rig.account.margin_free = rig.account.balance
    ctx.close = close
    return ctx


def open_first(series):
    report = series.make().run(once=True)
    assert report['status'] == 'stopped'
    assert report['mode'] == 'demo_series'
    assert report['orders_sent'] == report['entry_attempts'] == 1
    assert report['open_position_count'] == 1
    assert not report.get('finish_requested')
    return report


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_two_sequential_entries_after_verified_exit_and_new_candle(series, side):
    series.side = side
    first = open_first(series)
    first_session = series.authorization.records()[0]['session_id']
    series.advance(600)
    series.close()
    monitor = series.make()
    closed = monitor.run(once=True)
    assert closed['status'] == 'stopped' and closed['closed_trades'] == 1
    assert closed['execution_result']['status'] == 'closed_verified'
    assert closed['orders_sent'] == 1
    assert series.rig.api.order_send.call_count == 1
    with monitor.trade_data_path.open(newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1 and rows[0]['side'] == side
    assert float(rows[0]['holding_seconds']) == 600
    assert float(rows[0]['realized_net_pnl']) == pytest.approx(9.95)
    assert float(rows[0]['selected_reward_r']) == 2
    assert rows[0]['target_mode'] == 'fixed_2r'
    assert rows[0]['target_branch_reason'] == 'fixed_2r'
    series.advance(3000)
    second = series.make().run(once=True)
    assert second['status'] == 'stopped' and second['orders_sent'] == 2
    assert second['entry_attempts'] == 2 and second['closed_trades'] == 1
    assert second['risk_budget'] > first['risk_budget']  # Recompute 1% after a profitable close.
    assert series.rig.api.order_send.call_count == 2
    records = series.authorization.records()
    assert records[0]['status'] == 'closed_verified' and records[1]['status'] == 'entry_verified'
    assert records[1]['session_id'] != first_session
    assert len(series.rig.ledger['positions']) == 1


def test_continuous_loop_can_open_twice_without_manual_restart(series):
    monitor = series.make()
    def progress():
        if series.entry_count == 1:
            series.close()
            series.advance(3540)  # New 09:00 close, after a real 08:01 exit.
        elif series.entry_count == 2:
            request_stop(series.rig.directory)
    series.after_sleep = progress
    report = monitor.run()
    assert report['status'] == 'stopped' and report['stop_reason'] == 'User requested stop.'
    assert report['orders_sent'] == 2
    assert report['closed_trades'] == 1
    assert series.authorization.revoked_path.exists()
    assert len(series.rig.ledger['positions']) == 1  # Stop does not close the second trade.
    assert series.rig.api.order_send.call_count == 2


def test_existing_owned_position_blocks_all_new_entries_across_hours(series):
    open_first(series)
    series.advance(3600)
    report = series.make().run(once=True)
    assert report['status'] == 'stopped'
    assert report['latest_decision']['status'] == 'BLOCKED'
    assert report['active_position_ticket'] == series.rig.ledger['positions'][0].ticket
    assert series.rig.api.order_send.call_count == 1


def test_same_candle_never_gets_a_second_order_after_exit(series):
    open_first(series)
    record = series.authorization.records()[0]
    series.advance(60)
    series.close()
    series.make().run(once=True)
    bound = DemoSeriesAuthorization(series.rig.directory, series.authorization.authorization_id,
                                    session_id='different-context', decision_time_utc=record['decision_time_utc'])
    with pytest.raises(ValueError, match='reserved/consumed'):
        bound.reserve(series.rig.executor.connector, preview_id='other', session_id='different-context', run_id='d'*32)
    assert series.rig.api.order_send.call_count == 1


@pytest.mark.parametrize('problem', ['sl', 'tp', 'partial', 'extra_position', 'pending', 'missing_exit', 'manual_exit', 'history_error'])
def test_unverified_exit_or_position_change_halts_without_another_order(series, problem):
    open_first(series)
    series.advance(60)
    rig = series.rig
    if problem in ('sl', 'tp'): setattr(rig.ledger['positions'][0], problem, 0)
    if problem == 'partial': series.close(partial=True)
    if problem == 'extra_position': rig.ledger['positions'].append(Record(ticket=99, identifier=99))
    if problem == 'pending': rig.api.orders_get.return_value = (Record(ticket=99),)
    if problem == 'missing_exit': rig.ledger['positions'] = []
    if problem == 'manual_exit': series.close(reason=0)
    if problem == 'history_error':
        series.close()
        rig.api.history_deals_get.side_effect = lambda *args, **kwargs: None
    report = series.make().run(once=True)
    assert report['status'] == 'failed'
    assert series.authorization.records()[0]['status'] == 'entry_verified'
    assert rig.api.order_send.call_count == 1


@pytest.mark.parametrize('failure', ['none', 'exception', 'reject', 'partial_fill'])
def test_failed_send_halts_entire_series_including_new_signals(series, failure):
    original = series.rig.api.order_send.side_effect
    def send(request):
        if failure == 'none': return None
        if failure == 'exception': raise TimeoutError('IPC timeout')
        result = original(request)
        result.retcode = 10030 if failure == 'reject' else 10010
        return result
    series.rig.api.order_send.side_effect = send
    report = series.make().run(once=True)
    assert report['status'] == 'failed'
    assert report['authorization']['halted']
    assert series.authorization.records()[0]['status'] == 'consumed_stopped_or_unverified'
    series.advance(3600)
    assert series.make().run(once=True)['status'] == 'failed'
    assert series.rig.api.order_send.call_count == 1


def test_reserved_attempt_is_not_retried_after_crash(series):
    stamp = series.clock.now.replace(minute=0, second=0).isoformat()
    bound = DemoSeriesAuthorization(series.rig.directory, series.authorization.authorization_id,
                                    session_id='interrupted', decision_time_utc=stamp)
    bound.reserve(series.rig.executor.connector, preview_id='interrupted', session_id='interrupted', run_id='d'*32)
    assert series.make().run(once=True)['status'] == 'failed'
    series.rig.api.order_send.assert_not_called()


def test_closed_trade_csv_is_rebuilt_idempotently_on_restart(series):
    open_first(series)
    series.advance(60)
    series.close(profit=-4., reason=4)
    monitor = series.make()
    first = monitor.run(once=True)
    before = monitor.trade_data_path.read_bytes()
    again = series.make().run(once=True)
    assert monitor.trade_data_path.read_bytes() == before
    assert first['realized_net_pnl'] == again['realized_net_pnl'] == pytest.approx(-4.05)
    monitor.trade_data_path.unlink()  # Only this fixture's temporary output.
    series.make().run(once=True)
    assert monitor.trade_data_path.read_bytes() == before
    assert series.rig.api.order_send.call_count == 1


def test_first_trade_can_remain_open_at_deadline_without_forced_close(series):
    open_first(series)
    series.advance(3*3600)
    report = series.make().run(once=True)
    assert report['status'] == 'failed' and 'expired' in report['stop_reason']
    assert len(series.rig.ledger['positions']) == 1
    assert series.rig.api.order_send.call_count == 1


def test_no_signal_produces_no_orders_or_reserved_attempts(series):
    series.signals = False
    report = series.make().run(once=True)
    assert report['status'] == 'stopped' and report['latest_decision']['status'] == 'WAIT'
    assert series.authorization.records() == []
    series.rig.api.order_check.assert_not_called()
    series.rig.api.order_send.assert_not_called()


@pytest.mark.parametrize('problem', ['real', 'contest', 'missing_account', 'identity', 'policy', 'revoked'])
def test_account_or_policy_failure_stops_series_before_first_order(series, problem):
    rig = series.rig
    monitor = series.make()
    if problem == 'real': rig.account.trade_mode = 2
    if problem == 'contest': rig.account.trade_mode = 1
    if problem == 'missing_account': rig.api.account_info.return_value = None
    if problem == 'identity': rig.account.login += 1
    if problem == 'revoked': series.authorization.revoked_path.write_text('{}')
    if problem == 'policy':
        policy = json.loads(series.policy.read_text())
        policy['risk_percent'] = .5
        series.policy.write_text(json.dumps(policy))
    assert monitor.run(once=True)['status'] == 'failed'
    rig.api.order_send.assert_not_called()


def test_one_entry_approval_cannot_silently_become_a_series(series):
    rig = series.rig
    single = DemoEntryAuthorization.create(rig.executor.connector, rig.directory, series.policy,
        expires_at=series.authorization.expires, user_instruction='One entry only')
    with pytest.raises(ValueError): DemoSeriesAuthorization(rig.directory, single.authorization_id)
    assert isinstance(load_entry_authorization(rig.directory, single.authorization_id, session_id='one',
        decision_time_utc=series.clock.now.replace(minute=0, second=0).isoformat()), DemoEntryAuthorization)


def test_series_risk_guard_recomputes_current_budget(series):
    bound = DemoSeriesAuthorization(series.rig.directory, series.authorization.authorization_id,
        session_id='guard', decision_time_utc=series.clock.now.replace(minute=0, second=0).isoformat())
    intent = StrategyIntent('EURUSDm', 'buy', 1.09812, .001, bound.decision_time_utc, 'a'*64, 'pullback')
    executor = DemoExecutor(series.rig.executor.connector, series.rig.directory, session_id='guard',
                            strategy_policy=series.authorization.policy, strategy_intent=intent)
    preview = executor.prepare_entry()
    preview['protection']['risk_budget'] = 500
    with pytest.raises(ValueError, match='exceeds'):
        bound.validate(series.rig.executor.connector, preview)
    series.rig.api.order_send.assert_not_called()
