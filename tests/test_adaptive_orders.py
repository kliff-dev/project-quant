"""Offline broker-native adapter tests. No terminal, check or send is called."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace as Record
from unittest.mock import Mock

import pandas as pd
import pytest

from analytics.account_research import ReplaySettings, make_plan
from analytics.adaptive_targets import policy_plan
from engine import adaptive_orders as module
from engine import strategy_orders as orders
from test_candidate_signals import append_bar, setup


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr(orders, 'utc_now', lambda: datetime(2026, 11, 13, 11, 0, 30, tzinfo=timezone.utc))
    def profit(side, symbol, volume, opening, closing):
        amount = (Decimal(str(closing)) - Decimal(str(opening))) * Decimal(str(volume)) * 100000
        if side == 1:
            amount = -amount
        return float(amount.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
    api = Record(ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1, TIMEFRAME_H1=16385,
                 order_calc_profit=Mock(side_effect=profit), order_send=Mock(), order_check=Mock())
    info = Record(name='EURUSDm', currency_base='EUR', currency_profit='USD', chart_mode=0,
                  order_mode=127, trade_tick_size=.00001, point=.00001,
                  volume_min=.01, volume_max=200, volume_step=.01, trade_stops_level=0)
    return Record(api=api, info=info, quote={'bid': 1.1, 'ask': 1.10008},
                  funds={'balance': 500., 'equity': 500., 'currency': 'USD'},
                  policy=orders.StrategyPolicy(target_mode=module.TARGET_MODE))


def fixture_signal(side='buy'):
    return dict(pullback_long=side == 'buy', pullback_short=side == 'sell',
                breakout_long=False, breakout_short=False, atr14=.0015,
                low5=1.0999, high5=1.1001, spread=8,
                decision_time_utc='2026-11-13T11:00:00+00:00',
                h4_ema50=1.102, h4_ema200=1.1, h4_ema50_slope5=.0005,
                h4_atr14=.001, volatility_ratio=1., h4_swing_highs=[], h4_swing_lows=[])


def synthetic_intent(signal, side):
    raw, reason = make_plan(signal, 1.1, 500., ReplaySettings())
    assert not reason
    return orders.StrategyIntent('EURUSDm', side, raw['stop_loss'], signal['atr14'],
                                 signal['decision_time_utc'], 'a' * 64, 'pullback',
                                 {'context_payload_sha256': 'b' * 64})


def add_obstacle(native, signal, intent, branch, *, extra=0.):
    plan = orders._build_strategy_protection(native.api, native.info, native.quote,
                native.funds, intent, native.policy, target_reward_r=branch)
    direction = 1 if intent.side == 'buy' else -1
    boundary = Decimal(str(plan['take_profit'])) + direction * Decimal(str(extra))
    price = boundary + direction * Decimal('.1') * Decimal(str(signal['atr14']))
    if direction == -1:
        price -= Decimal(str(native.quote['ask'])) - Decimal(str(native.quote['bid']))
    key = 'h4_swing_highs' if direction == 1 else 'h4_swing_lows'
    signal[key] = [dict(price=float(price), pivot_open_utc='2026-11-12T00:00:00+00:00',
                        confirmed_at_utc='2026-11-12T12:00:00+00:00')]


def build(native, intent):
    return orders.build_strategy_protection(native.api, native.info, native.quote,
                                            native.funds, intent, native.policy)


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('branch', [1, 2, 3])
def test_native_targets_match_frozen_research_and_preserve_risk(native, monkeypatch, side, branch):
    signal = fixture_signal(side)
    intent = synthetic_intent(signal, side)
    add_obstacle(native, signal, intent, branch)
    monkeypatch.setattr(module, '_verified_context', lambda *_: signal)
    actual = build(native, intent)
    expected, reason = policy_plan(signal, 1.1, 500., ReplaySettings(), policy='adaptive_1_2_3')
    assert not reason and actual['selected_reward_r'] == branch
    assert actual['reward_to_risk'] == branch and actual['planned_reward_to_risk'] >= branch
    assert actual['take_profit'] == expected['take_profit']
    assert actual['stop_loss'] == expected['stop_loss']
    assert actual['volume_lots'] == expected['lots']
    assert actual['risk_budget'] == expected['risk_budget'] == 5.
    assert actual['planned_loss_with_reserves'] == expected['planned_risk'] <= 5.
    assert actual['policy']['reward_to_risk'] == 2
    assert actual['target_room_flags'] == {str(r): r <= branch for r in (1, 2, 3)}
    assert actual['adaptive_target_evidence']['selected_reward_r'] == branch
    native.api.order_send.assert_not_called()
    native.api.order_check.assert_not_called()


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_a_fractional_tick_short_of_1r_rejects_without_changing_stop(native, monkeypatch, side):
    signal = fixture_signal(side)
    intent = synthetic_intent(signal, side)
    add_obstacle(native, signal, intent, 1, extra=-.000001)
    monkeypatch.setattr(module, '_verified_context', lambda *_: signal)
    with pytest.raises(ValueError, match='Insufficient room'):
        build(native, intent)
    native.api.order_send.assert_not_called()


@pytest.mark.parametrize('field,value', [('h4_ema50_slope5', .000249), ('volatility_ratio', .749999),
                                       ('volatility_ratio', 1.500001)])
def test_3r_requires_strong_trend_and_normal_volatility(native, monkeypatch, field, value):
    signal = fixture_signal()
    signal[field] = value
    intent = synthetic_intent(signal, 'buy')
    add_obstacle(native, signal, intent, 3)
    monkeypatch.setattr(module, '_verified_context', lambda *_: signal)
    assert build(native, intent)['selected_reward_r'] == 2


def test_sell_uses_current_spread_for_ask_obstacle_and_can_reject_changed_quote(native, monkeypatch):
    signal = fixture_signal('sell')
    intent = synthetic_intent(signal, 'sell')
    add_obstacle(native, signal, intent, 1)
    monkeypatch.setattr(module, '_verified_context', lambda *_: signal)
    before = build(native, intent)
    assert before['obstacle_exit_quote'] == pytest.approx(before['obstacle_bid_price'] + .00008)
    native.quote['ask'] += .00001
    with pytest.raises(ValueError, match='Insufficient room'):
        build(native, intent)


def test_native_broker_failure_cannot_use_linear_research_fallback(native, monkeypatch):
    signal = fixture_signal()
    intent = synthetic_intent(signal, 'buy')
    add_obstacle(native, signal, intent, 3)
    monkeypatch.setattr(module, '_verified_context', lambda *_: signal)
    native.api.order_calc_profit.side_effect = lambda *_: None
    with pytest.raises(ValueError, match='P&L'):
        build(native, intent)


def test_default_legacy_remains_2r_and_non2r_private_targets_require_adaptive_mode(native):
    intent = synthetic_intent(fixture_signal(), 'buy')
    legacy = orders.StrategyPolicy()
    result = orders.build_strategy_protection(native.api, native.info, native.quote, native.funds, intent, legacy)
    assert result['reward_to_risk'] == 2 and 'selected_reward_r' not in result
    for reward in (1, 3):
        with pytest.raises(ValueError, match='require the adaptive'):
            orders._build_strategy_protection(native.api, native.info, native.quote,
                    native.funds, intent, legacy, target_reward_r=reward)


def live_fixture(native, side='buy'):
    window = setup('trend_reclaim', side)
    frame = append_bar(window)
    native.api.copy_rates_from_pos = Mock(return_value=frame.assign(
        time=frame.index.as_unit('s').astype('int64')).to_records(index=False))
    native.api.symbol_info = Mock(return_value=native.info)
    quote = {'symbol': 'EURUSDm', 'bid': float(window.close.iloc[-1]),
             'ask': float(window.close.iloc[-1]) + .00008}
    connector = Record(terminal_api=native.api, inspect=Mock(return_value={
        'quote': quote, 'account': {'account_type': 'DEMO'}}))
    return connector, window


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_closed_window_roundtrips_all_features_and_excludes_unfinished_bar(native, tmp_path, side):
    connector, window = live_fixture(native, side)
    intent, report = orders.latest_strategy_intent(connector, tmp_path, policy=native.policy)
    assert intent and intent.side == side and intent.trigger == 'pullback'
    assert report['bars'] == 3000 and report['orders_sent'] == 0
    assert pd.Timestamp(intent.decision_time_utc) == window.index[-1] + pd.Timedelta(hours=1)
    actual = module._verified_context(intent, native.info)
    assert actual['strategy_family'] == 'trend_reclaim'
    assert not actual['breakout_long'] and not actual['breakout_short']
    assert actual['input_window_rows'] == 3000
    assert actual == intent.adaptive_context['features']
    native.api.order_check.assert_not_called()
    native.api.order_send.assert_not_called()


@pytest.mark.parametrize('kind', ['checksum', 'metadata', 'input', 'features', 'decision', 'stop'])
def test_tampered_evidence_is_rejected_even_with_rehashed_metadata(native, tmp_path, kind):
    connector, _ = live_fixture(native)
    intent, _ = orders.latest_strategy_intent(connector, tmp_path, policy=native.policy)
    context = deepcopy(intent.adaptive_context)
    if kind == 'checksum': context['features']['atr14'] *= 2
    if kind == 'metadata': context['research_spec_sha256'] = '0' * 64
    if kind == 'features': context['features']['h4_ema50_slope5'] *= 2
    if kind == 'decision': context['decision_time_utc'] = '2026-11-13T12:00:00+00:00'
    if kind == 'stop': context['frozen_stop_loss'] += .00001
    if kind == 'input':
        from pathlib import Path
        with Path(context['input_file']).open('ab') as handle: handle.write(b'\n')
    if kind != 'checksum':
        payload = {k: v for k, v in context.items() if k != 'context_payload_sha256'}
        context['context_payload_sha256'] = module._sha(module._canonical(payload))
    with pytest.raises(ValueError, match='Adaptive|adaptive|Frozen'):
        module._verified_context(replace(intent, adaptive_context=context), native.info)


def test_frozen_research_kernel_or_spec_tampering_blocks_adapter(native, tmp_path, monkeypatch):
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='Frozen adaptive research input changed'):
        module.verify_frozen_kernel()
    native.api.order_send.assert_not_called()


def test_changed_broker_chart_type_cannot_reuse_bid_chart_context(native, tmp_path):
    connector, _ = live_fixture(native)
    intent, _ = orders.latest_strategy_intent(connector, tmp_path, policy=native.policy)
    native.info.chart_mode = 1
    with pytest.raises(ValueError, match='bid-chart EUR/USD'):
        module._verified_context(intent, native.info)


@pytest.mark.parametrize('kind', ['stale', 'gap', 'no_signal'])
def test_wait_conditions_never_create_orders(native, tmp_path, monkeypatch, kind):
    connector, window = live_fixture(native)
    if kind == 'stale':
        monkeypatch.setattr(orders, 'utc_now', lambda: datetime(2026, 11, 13, 11, 5, 1, tzinfo=timezone.utc))
    else:
        if kind == 'gap': window = window.drop(window.index[-3])
        if kind == 'no_signal': window.iloc[-1, window.columns.get_loc('open')] = window.close.iloc[-1]
        frame = append_bar(window)
        native.api.copy_rates_from_pos.return_value = frame.assign(
            time=frame.index.as_unit('s').astype('int64')).to_records(index=False)
    intent, report = orders.latest_strategy_intent(connector, tmp_path, policy=native.policy)
    assert intent is None and report['status'] == 'WAIT' and report['orders_sent'] == 0
    native.api.order_send.assert_not_called()
    native.api.order_check.assert_not_called()


@pytest.mark.parametrize('kind', ['crossed', 'stale', 'legacy_context'])
def test_invalid_execution_context_rejected_before_native_send(native, monkeypatch, kind):
    signal = fixture_signal()
    intent = synthetic_intent(signal, 'buy')
    if kind == 'legacy_context':
        intent = replace(intent, adaptive_context=None)
    else:
        monkeypatch.setattr(module, '_verified_context', lambda *_: signal)
    if kind == 'crossed': native.quote['ask'] = native.quote['bid'] - .00001
    if kind == 'stale':
        monkeypatch.setattr(orders, 'utc_now', lambda: datetime(2026, 11, 13, 11, 6, tzinfo=timezone.utc))
    with pytest.raises(ValueError): build(native, intent)
    native.api.order_send.assert_not_called()
