"""4R native sizing through guarded DEMO send, position proof and exit export.

All terminal responses are in memory. Candle evidence reconstruction is tested
separately; this test isolates the real executor and sequential state machine.
"""

import csv
from dataclasses import asdict
from datetime import timedelta
import json

import pytest

from test_demo_executor import rig
from test_demo_series import series
from test_adaptive_orders import fixture_signal
from engine import adaptive_orders, market_monitor
from engine.demo_series import DemoSeriesAuthorization, DemoSeriesMonitor
from engine.market_monitor import MonitorConfig
from engine.strategy_orders import StrategyIntent, StrategyPolicy


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_four_r_reaches_broker_check_verified_position_and_completed_trade_log(series, monkeypatch, side):
    series.side = side
    policy = StrategyPolicy(target_mode='adaptive_pullback_v2')
    series.policy.write_text(json.dumps(asdict(policy)), encoding='utf-8')
    connector = series.rig.executor.connector
    authorization = DemoSeriesAuthorization.create(connector, series.rig.directory, series.policy,
        expires_at=series.clock.now + timedelta(hours=3), user_instruction='DEMO targets up to 4R; 1% risk.')
    decision_time = series.clock.now.replace(minute=0, second=0, microsecond=0).isoformat()
    signal = fixture_signal(side)
    signal['decision_time_utc'] = decision_time
    pivot = dict(price=1.12 if side == 'buy' else 1.08,
                 pivot_open_utc='2026-11-12T00:00:00+00:00', confirmed_at_utc='2026-11-12T12:00:00+00:00')
    signal['h4_swing_highs' if side == 'buy' else 'h4_swing_lows'] = [pivot]
    monkeypatch.setattr(adaptive_orders, '_verified_context', lambda *_, **__: signal)
    context = dict(target_mode='adaptive_pullback_v2', schema_version=2,
                   research_spec_sha256=adaptive_orders.EXTENDED_SPEC_SHA256,
                   context_payload_sha256='b' * 64)
    intent = StrategyIntent('EURUSDm', side, 1.09812 if side == 'buy' else 1.102,
                            signal['atr14'], decision_time, 'a' * 64, 'pullback', context)
    def decision(*args, policy):
        assert policy.target_mode == 'adaptive_pullback_v2'
        return intent, dict(status='signal', decision_time_utc=decision_time, orders_sent=0, reason='Synthetic 4R case')
    monkeypatch.setattr(market_monitor, 'latest_strategy_intent', decision)
    def monitor():
        return DemoSeriesMonitor(connector, series.rig.directory, series.policy,
            authorization_id=authorization.authorization_id, config=MonitorConfig(3, 60),
            now=lambda: series.clock.now, monotonic=lambda: series.clock.ticks,
            sleep=lambda seconds: series.advance(seconds), emit=lambda _: None)
    first = monitor().run(once=True)
    assert first['status'] == 'stopped', first.get('stop_reason')
    assert first['execution_result']['status'] == 'open_verified'
    assert first['execution_result']['selected_reward_r'] == 4
    assert first['execution_result']['target_mode'] == 'adaptive_pullback_v2'
    assert first['orders_sent'] == 1 and first['open_position_count'] == 1
    request = series.rig.api.order_send.call_args.args[0]
    checked = series.rig.api.order_check.call_args.args[0]
    assert request == checked
    position = series.rig.ledger['positions'][0]
    assert request['tp'] == position.tp and request['sl'] == position.sl
    series.advance(30)
    assert monitor().run(once=True)['orders_sent'] == 1
    assert series.rig.api.order_send.call_count == 1  # No stacking/reuse of the signal.
    gross = series.rig.api.order_calc_profit(request['type'], 'EURUSDm', position.volume,
                                           position.price_open, position.tp)
    series.advance(570)
    series.close(profit=gross)
    last = monitor()
    report = last.run(once=True)
    assert report['execution_result']['status'] == 'closed_verified'
    with last.trade_data_path.open(newline='', encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1 and rows[0]['side'] == side
    assert rows[0]['selected_reward_r'] == '4'
    assert rows[0]['target_mode'] == 'adaptive_pullback_v2'
    assert rows[0]['target_branch_reason'] == '4r_room_strong_trend_normal_volatility'
    assert rows[0]['target_spec_sha256'] == adaptive_orders.EXTENDED_SPEC_SHA256
    assert float(rows[0]['planned_risk_usd']) <= 5 and float(rows[0]['realized_r']) >= 4
    assert series.rig.api.order_send.call_count == 1  # Exit was broker-triggered in fake history.
