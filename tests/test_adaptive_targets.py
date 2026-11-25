"""Synthetic checks of a shared entry gate and causal, frozen target decisions."""

from dataclasses import replace
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from analytics.account_research import ReplaySettings, make_plan
from analytics.adaptive_targets import confirmed_h4_swings, policy_plan, prepare_pullback_signals
from analytics.candidate_signals import prepare_candidate_signals, wilder_atr
from test_candidate_signals import append_bar, setup


SETTINGS = ReplaySettings()


def signal(side='buy', *, ratio=1., strong=True):
    return dict(pullback_long=side == 'buy', pullback_short=side == 'sell',
        breakout_long=False, breakout_short=False, atr14=.0015, low5=1.0999,
        high5=1.1001, spread=8, decision_time_utc='2026-11-13T11:00:00+00:00',
        h4_ema50=1.102 if strong else 1.1002, h4_ema200=1.1,
        h4_ema50_slope5=.0005 if strong else .00001, h4_atr14=.001,
        volatility_ratio=ratio, h4_swing_highs=[], h4_swing_lows=[])


def obstacle_for(signal_value, reward, *, extra_room=0., settings=SETTINGS):
    """Place a known reference around an independently calculated rounded TP."""
    raw, reason = make_plan(signal_value, 1.1, 500., replace(settings, reward_r=reward))
    assert not reason
    direction = raw['direction']
    boundary = Decimal(str(raw['take_profit'])) + direction * Decimal(str(extra_room))
    quote = boundary + direction * Decimal('.1') * Decimal(str(signal_value['atr14']))
    price = quote - (Decimal(str(signal_value['spread'])) * Decimal('.00001') if direction == -1 else 0)
    item = dict(price=float(price), pivot_open_utc='2026-11-12T00:00:00Z',
                confirmed_at_utc='2026-11-12T12:00:00Z')
    key = 'h4_swing_highs' if direction == 1 else 'h4_swing_lows'
    return dict(signal_value, **{key: [item]})


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('branch', [1, 2, 3])
def test_target_room_equality_selects_1_2_or_3r_in_both_directions(side, branch):
    raw = obstacle_for(signal(side), branch)
    plan, reason = policy_plan(raw, 1.1, 500., SETTINGS, policy='adaptive_1_2_3')
    assert not reason and plan['selected_reward_r'] == branch
    assert plan['market_branch_r'] == branch
    assert plan['target_room_flags'] == {str(r): r <= branch for r in (1, 2, 3)}
    assert plan['strong_context'] and plan['normal_volatility']


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('boundary', [1, 2, 3])
def test_a_fractional_tick_short_of_actual_rounded_target_does_not_fit(side, boundary):
    raw = obstacle_for(signal(side), boundary, extra_room=-.000001)
    plan, reason = policy_plan(raw, 1.1, 500., SETTINGS, policy='adaptive_1_2_3')
    if boundary == 1:
        assert plan is None and reason == 'insufficient_1r_target_room'
    else:
        assert not reason and plan['selected_reward_r'] == boundary - 1
        assert not plan['target_room_flags'][str(boundary)]


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('ratio,expected', [(.75, 3), (1.5, 3), (.749999, 2), (1.500001, 2)])
def test_volatility_range_is_inclusive_and_required_only_for_3r(side, ratio, expected):
    raw = obstacle_for(signal(side, ratio=ratio), 3)
    plan, _ = policy_plan(raw, 1.1, 500., SETTINGS, policy='adaptive_1_2_3')
    assert plan['selected_reward_r'] == expected


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_weak_context_limits_target_to_2r_even_with_3r_room(side):
    raw = obstacle_for(signal(side, strong=False), 3)
    plan, _ = policy_plan(raw, 1.1, 500., SETTINGS, policy='adaptive_1_2_3')
    assert plan['selected_reward_r'] == 2 and not plan['strong_context']


@pytest.mark.parametrize('field,value,expected', [
    ('h4_ema50', 1.101, 3), ('h4_ema50', 1.100999, 2),
    ('h4_ema50_slope5', .00025, 3), ('h4_ema50_slope5', .000249999, 2),
    ('h4_ema50_slope5', -.00025, 3)])
def test_trend_strength_thresholds_use_absolute_inclusive_values(field, value, expected):
    raw = signal()
    raw[field] = value
    raw = obstacle_for(raw, 3)
    plan, _ = policy_plan(raw, 1.1, 500., SETTINGS, policy='adaptive_1_2_3')
    assert plan['selected_reward_r'] == expected


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('branch', [1, 2, 3])
def test_fixed_and_adaptive_keep_geometry_and_reserves_identical(side, branch):
    raw = obstacle_for(signal(side), branch)
    adaptive, _ = policy_plan(raw, 1.1, 500., SETTINGS, policy='adaptive_1_2_3')
    fixed, _ = policy_plan(raw, 1.1, 500., SETTINGS, policy='fixed_2r')
    assert fixed['selected_reward_r'] == 2 and fixed['market_branch_r'] == branch
    for key in ['stop_loss', 'lots', 'planned_risk', 'risk_budget', 'cost_reserve',
                'entry_fee', 'entry_price', 'reference_entry', 'margin_estimate']:
        assert adaptive[key] == fixed[key]
    expected, _ = make_plan(raw, 1.1, 500., replace(SETTINGS, reward_r=branch))
    assert adaptive['take_profit'] == expected['take_profit']
    if branch == 1:
        assert not fixed['target_room_flags']['2']
        assert fixed['take_profit'] != adaptive['take_profit']


@pytest.mark.parametrize('policy', ['fixed_2r', 'adaptive_1_2_3'])
@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_missing_obstacle_or_insufficient_1r_room_reject_both_policies(policy, side):
    raw = signal(side)
    assert policy_plan(raw, 1.1, 500., SETTINGS, policy=policy) == (None, 'missing_confirmed_obstacle')
    short_room = obstacle_for(raw, 1, extra_room=-.00001)
    assert policy_plan(short_room, 1.1, 500., SETTINGS, policy=policy) == (None, 'insufficient_1r_target_room')


def test_short_obstacles_are_converted_from_bid_chart_to_ask_exit_quote():
    raw = obstacle_for(signal('sell'), 2)
    plan, _ = policy_plan(raw, 1.1, 500., SETTINGS, policy='adaptive_1_2_3')
    assert plan['selected_reward_r'] == 2
    assert plan['obstacle_exit_quote'] == pytest.approx(plan['obstacle_bid_price'] + .00008)
    # A bid-chart low below entry is ineligible when its ask equivalent is above entry.
    near = dict(price=1.09999, pivot_open_utc='2026-11-12T00:00:00Z', confirmed_at_utc='2026-11-12T12:00:00Z')
    raw['h4_swing_lows'].append(near)
    after, _ = policy_plan(raw, 1.1, 500., SETTINGS, policy='adaptive_1_2_3')
    assert after == plan


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_nearest_eligible_obstacle_is_used_without_dropping_historical_breached_swings(side):
    near = obstacle_for(signal(side), 1)
    far = obstacle_for(signal(side), 3)
    key = 'h4_swing_highs' if side == 'buy' else 'h4_swing_lows'
    near[key] = far[key] + near[key]
    plan, _ = policy_plan(near, 1.1, 500., SETTINGS, policy='adaptive_1_2_3')
    assert plan['selected_reward_r'] == 1
    assert plan['obstacle_bid_price'] == near[key][-1]['price']


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_future_confirmation_cannot_qualify_an_obstacle(side):
    raw = obstacle_for(signal(side), 3)
    key = 'h4_swing_highs' if side == 'buy' else 'h4_swing_lows'
    raw[key][0]['confirmed_at_utc'] = '2026-11-13T12:00:00Z'
    assert policy_plan(raw, 1.1, 500., SETTINGS, policy='adaptive_1_2_3') == (None, 'missing_confirmed_obstacle')
    raw[key][0]['confirmed_at_utc'] = raw['decision_time_utc']
    plan, reason = policy_plan(raw, 1.1, 500., SETTINGS, policy='adaptive_1_2_3')
    assert plan is not None and not reason


def h4_rows(high, low=None):
    high = np.asarray(high, dtype=float)
    return pd.DataFrame(dict(high=high, low=np.full(len(high), .5) if low is None else low),
                        index=pd.date_range('2026-10-01', periods=len(high), freq='4h', tz='UTC'))


@pytest.mark.parametrize('unit', ['us', 'ns'])
def test_two_right_candles_must_be_completed_before_pivot_exists(unit):
    rows = h4_rows([1., 1.1, 1.5, 1.2, 1.1], [.8, .7, .4, .6, .7])
    rows.index = rows.index.as_unit(unit)
    early = rows.index[-1]
    assert not any(confirmed_h4_swings(rows, early).values())
    decision = rows.index[-1] + pd.Timedelta(hours=4)
    swings = confirmed_h4_swings(rows, decision)
    for key, price in [('h4_swing_highs', 1.5), ('h4_swing_lows', .4)]:
        assert swings[key] == [dict(price=price, pivot_open_utc=rows.index[2].isoformat(),
                                    confirmed_at_utc=decision.isoformat())]


def test_strict_pivots_exclude_ties_and_retain_later_breached_pivots():
    tied = h4_rows([1., 1.2, 1.5, 1.5, 1.2, 1.])
    assert not confirmed_h4_swings(tied, tied.index[-1] + pd.Timedelta(hours=4))['h4_swing_highs']
    rows = h4_rows([1., 1.1, 1.5, 1.2, 1.1, 1.6, 1.2, 1.1])
    swings = confirmed_h4_swings(rows, rows.index[-1] + pd.Timedelta(hours=4))
    assert [row['price'] for row in swings['h4_swing_highs']] == [1.5, 1.6]


def test_only_last_120_closed_h4_observations_supply_pivot_and_all_neighbors():
    rows = h4_rows(np.ones(126))
    rows.iloc[2, 0] = 3.
    rows.iloc[7, 0] = 2.
    rows.iloc[10, 0] = 4.
    swings = confirmed_h4_swings(rows, rows.index[-1] + pd.Timedelta(hours=4))
    assert [row['price'] for row in swings['h4_swing_highs']] == [4.]


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('unit', ['us', 'ns'])
def test_preparation_preserves_frozen_entry_and_uses_same_window_atr_median(side, unit):
    original = setup('trend_reclaim', side, unit)
    frame = append_bar(original)
    first = frame.index[-1]
    candidates = prepare_candidate_signals(frame, first_entry=first)['trend_reclaim']
    prepared = prepare_pullback_signals(frame, first_entry=first)
    assert prepared.keys() == candidates.keys() and prepared
    actual = prepared[len(frame) - 1]
    assert {key: actual[key] for key in candidates[len(frame) - 1]} == candidates[len(frame) - 1]
    atr = wilder_atr(original)
    assert actual['volatility_ratio'] == pytest.approx(atr.iloc[-1] / atr.iloc[-100:].median(), abs=1e-14)
    for key in ['h4_swing_highs', 'h4_swing_lows']:
        assert actual[key]
        assert all(pd.Timestamp(p['confirmed_at_utc']) <= first for p in actual[key])


def test_appended_future_prices_do_not_change_prefix_features_or_pivots():
    prefix = append_bar(setup('trend_reclaim'))
    changed = prefix.copy()
    for _ in range(8):
        changed = append_bar(changed)
    changed.iloc[len(prefix):, changed.columns.get_indexer(['open', 'high', 'low', 'close'])] += .05
    changed.iloc[len(prefix):, changed.columns.get_loc('spread')] = 99
    first = prefix.index[-1]
    before = prepare_pullback_signals(prefix, first_entry=first)
    after = prepare_pullback_signals(changed, first_entry=first)
    assert before and before == {k: v for k, v in after.items() if k < len(prefix)}


def test_unknown_policy_and_invalid_context_are_rejected():
    with pytest.raises(ValueError, match='Unknown exit policy'):
        policy_plan(signal(), 1.1, 500., SETTINGS, policy='optimize')
    raw = obstacle_for(signal(), 3)
    raw['volatility_ratio'] = float('nan')
    with pytest.raises(ValueError, match='Finite H4 context'):
        policy_plan(raw, 1.1, 500., SETTINGS, policy='adaptive_1_2_3')
    raw = signal()
    raw['spread'] = 30
    assert policy_plan(raw, 1.1, 500., SETTINGS, policy='fixed_2r') == (None, 'spread_filter')
