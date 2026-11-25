"""Offline synthetic checks for the authorized four-R ceiling."""

from dataclasses import replace
from decimal import Decimal
from functools import partial
import hashlib
import json
from pathlib import Path

import pytest

from analytics.account_research import make_plan
from analytics.adaptive_targets import policy_plan as base_policy_plan
from analytics.extended_targets import EXIT_POLICY, SPEC_SHA, policy_plan
from test_adaptive_targets import SETTINGS, obstacle_for, signal
from test_paired_exits import assert_matches_account, frame, quiet


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('room', [1, 2, 3, 4])
def test_inclusive_room_selects_largest_allowed_branch(side, room):
    raw = obstacle_for(signal(side), room)
    plan, reason = policy_plan(raw, 1.1, 500., SETTINGS, policy=EXIT_POLICY)
    assert not reason and plan['selected_reward_r'] == room
    assert plan['market_branch_r'] == room
    assert plan['base_selected_reward_r'] == min(room, 3)
    assert plan['target_room_flags'] == {str(r): r <= room for r in (1, 2, 3, 4)}
    assert plan['strong_context'] and plan['normal_volatility']
    expected, _ = make_plan(raw, 1.1, 500., replace(SETTINGS, reward_r=room))
    for key in expected:
        assert plan[key] == expected[key]


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_fractional_tick_short_of_four_r_falls_back_to_three(side):
    raw = obstacle_for(signal(side), 4, extra_room=-.000001)
    plan, reason = policy_plan(raw, 1.1, 500., SETTINGS, policy=EXIT_POLICY)
    assert not reason and plan['selected_reward_r'] == 3
    assert not plan['target_room_flags']['4']


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_obstacle_comparison_preserves_sub_float_boundary_precision(side):
    raw = obstacle_for(signal(side), 4)
    key = 'h4_swing_highs' if side == 'buy' else 'h4_swing_lows'
    direction = 1 if side == 'buy' else -1
    raw[key][0]['price'] = Decimal(str(raw[key][0]['price'])) - direction * Decimal('1e-20')
    plan, reason = policy_plan(raw, 1.1, 500., SETTINGS, policy=EXIT_POLICY)
    assert not reason and plan['selected_reward_r'] == 3
    assert not plan['target_room_flags']['4']


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('ratio,expected', [(.75, 4), (1.5, 4), (.749999, 2), (1.500001, 2)])
def test_four_r_preserves_inclusive_volatility_thresholds(side, ratio, expected):
    raw = obstacle_for(signal(side, ratio=ratio), 4)
    plan, _ = policy_plan(raw, 1.1, 500., SETTINGS, policy=EXIT_POLICY)
    assert plan['selected_reward_r'] == expected and plan['target_room_flags']['4']


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_four_r_room_does_not_override_weak_trend(side):
    raw = obstacle_for(signal(side, strong=False), 4)
    plan, _ = policy_plan(raw, 1.1, 500., SETTINGS, policy=EXIT_POLICY)
    assert plan['selected_reward_r'] == 2
    assert plan['target_room_flags']['4'] and not plan['strong_context']


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('room', [1, 2, 3])
def test_non_extended_plan_preserves_base_financial_geometry(side, room):
    raw = obstacle_for(signal(side), room)
    base, _ = base_policy_plan(raw, 1.1, 500., SETTINGS, policy='adaptive_1_2_3')
    extended, _ = policy_plan(raw, 1.1, 500., SETTINGS, policy=EXIT_POLICY)
    assert all(extended[key] == value for key, value in base.items()
               if key not in ('exit_policy', 'target_room_flags'))
    assert extended['target_room_flags'] == dict(base['target_room_flags'], **{'4': False})


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_extended_target_does_not_raise_lots_or_dollar_risk(side):
    raw = obstacle_for(signal(side), 4)
    base, _ = base_policy_plan(raw, 1.1, 500., SETTINGS, policy='adaptive_1_2_3')
    plan, _ = policy_plan(raw, 1.1, 500., SETTINGS, policy=EXIT_POLICY)
    for key in ('stop_loss', 'lots', 'planned_risk', 'risk_budget', 'cost_reserve', 'entry_fee'):
        assert plan[key] == base[key]
    assert plan['planned_risk'] <= plan['risk_budget'] <= 5.
    modeled_reward = (plan['direction'] *
                      (plan['take_profit'] - plan['direction'] * plan['slip'] - plan['entry_price'])
                      * 100000 * plan['lots'] - plan['cost_reserve'])
    assert modeled_reward + 1e-8 >= 4 * plan['planned_risk']


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_same_nearest_obstacle_blocks_a_farther_four_r_reference(side):
    near, far = obstacle_for(signal(side), 2), obstacle_for(signal(side), 4)
    key = 'h4_swing_highs' if side == 'buy' else 'h4_swing_lows'
    far[key] += near[key]
    plan, _ = policy_plan(far, 1.1, 500., SETTINGS, policy=EXIT_POLICY)
    assert plan['selected_reward_r'] == 2 and not plan['target_room_flags']['4']
    assert plan['obstacle_bid_price'] == near[key][0]['price']


@pytest.mark.parametrize('policy', ['fixed_2r', 'adaptive_1_2_3'])
def test_legacy_policy_pass_through_is_exact(policy):
    raw = obstacle_for(signal(), 4)
    assert policy_plan(raw, 1.1, 500., SETTINGS, policy=policy) == base_policy_plan(
        raw, 1.1, 500., SETTINGS, policy=policy)


def test_unknown_policy_missing_obstacle_and_missing_room_are_fail_closed():
    with pytest.raises(ValueError, match='Unknown exit policy'):
        policy_plan(signal(), 1.1, 500., SETTINGS, policy='adaptive_unbounded')
    assert policy_plan(signal(), 1.1, 500., SETTINGS, policy=EXIT_POLICY) == (
        None, 'missing_confirmed_obstacle')
    raw = obstacle_for(signal(), 1, extra_room=-.000001)
    assert policy_plan(raw, 1.1, 500., SETTINGS, policy=EXIT_POLICY) == (
        None, 'insufficient_1r_target_room')


def test_extension_spec_and_base_source_pins_are_valid():
    root = Path(__file__).resolve().parents[1]
    path = root / 'reports/market_analysis/extended_target_spec_2026-11-15.json'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SPEC_SHA
    spec = json.loads(path.read_text(encoding='utf-8'))
    assert spec['maximum_reward_r'] == 4
    assert spec['base_specification_sha256'] == hashlib.sha256(
        (root / spec['base_specification']).read_bytes()).hexdigest()
    assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == sha
               for name, sha in spec['base_kernel_sha256'].items())


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('outcome', ['target', 'stop'])
def test_extended_target_and_stop_flow_through_real_offline_account_and_costs(side, outcome):
    move_up = (side == 'buy') == (outcome == 'target')
    final = quiet(high=1.12) if move_up else quiet(low=1.08)
    data = frame([quiet(), final], start='2026-11-13T11:00:00Z')
    raw = obstacle_for(signal(side), 4)
    isolated, account = assert_matches_account(data, raw, SETTINGS,
        plan_builder=partial(policy_plan, policy=EXIT_POLICY))
    trade = isolated['trade']
    assert trade['selected_reward_r'] == 4 and trade['exit_reason'] == outcome
    if outcome == 'target':
        assert trade['realized_r'] >= 4 - 1e-8
        assert account['summary']['final_equity'] > 500.
    else:
        assert trade['net_pnl'] == pytest.approx(-trade['planned_risk'], abs=.005)
        assert account['summary']['final_equity'] < 500.
