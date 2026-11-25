"""Versioned 4R ceiling, preserving the frozen 1R/2R/3R fallback exactly.

This is a target-selection hypothesis, not an estimated success probability.
Stops, lot sizing and account risk are delegated to the unchanged base planner.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from analytics.account_research import ReplaySettings, make_plan
from analytics.adaptive_targets import _confirmed_obstacle, policy_plan as base_policy_plan


TARGET_MODE = 'adaptive_pullback_v2'
EXIT_POLICY = 'adaptive_1_2_3_4'
SPEC_SHA = '0e3ea1516921f25a033999a9394dece75884ef394816bcbadc1151adad1db2b4'
BASE_SPEC_SHA = 'c149c1412cbb1ca78ca9f7bf341c6d251d226cd86da42051d96238fb0dcb17f8'
POLICIES = ('fixed_2r', 'adaptive_1_2_3', EXIT_POLICY)


def policy_plan(signal: dict, bid: float, balance: float, settings: ReplaySettings,
                *, policy: str) -> tuple[dict | None, str]:
    """Use 4R only when its rounded target fits and all base 3R gates hold.

    All other cases keep the base policy's selection. The fourth room flag is
    calculated even when weak context limits the selection, making rejection
    evidence inspectable without changing entry eligibility.
    """
    if policy not in POLICIES:
        raise ValueError(f'Unknown exit policy: {policy}')
    if policy != EXIT_POLICY:
        return base_policy_plan(signal, bid, balance, settings, policy=policy)
    base, reason = base_policy_plan(signal, bid, balance, settings, policy='adaptive_1_2_3')
    if base is None:
        return None, reason
    four, four_reason = make_plan(signal, bid, balance, replace(settings, reward_r=4.))
    if four is None:
        # Reward alone cannot change affordability, spread or stop filters.
        # Refuse an unexpected planner inconsistency instead of hiding it.
        raise ValueError(f'4R planning changed shared eligibility: {four_reason}')
    for key in ('direction', 'entry_price', 'reference_entry', 'stop_loss', 'lots',
                'planned_risk', 'risk_budget', 'cost_reserve', 'entry_fee', 'margin_estimate'):
        if four[key] != base[key]:
            raise ValueError(f'4R planning changed shared geometry: {key}')
    direction = base['direction']
    # Recreate the exact decimal reference used by the base kernel. Converting
    # a previously float-rounded quote would lose a fractional-tick boundary.
    obstacle = _confirmed_obstacle(signal, Decimal(str(base['reference_entry'])), direction)
    if obstacle is None:
        raise ValueError('The base plan lost its confirmed obstacle.')
    _, quote = obstacle
    boundary = quote - direction * Decimal('.1') * Decimal(str(signal['atr14']))
    target = Decimal(str(four['take_profit']))
    room_four = bool(target <= boundary if direction == 1 else target >= boundary)
    base_reward = base['selected_reward_r']
    selected = 4 if base_reward == 3 and room_four else base_reward
    if room_four and not all(base['target_room_flags'].values()):
        raise ValueError('4R room must imply room for all shorter targets.')
    result = dict(base)
    if selected == 4:
        result.update(four)
        result['branch_reason'] = '4r_room_strong_trend_normal_volatility'
    result.update(exit_policy=EXIT_POLICY, selected_reward_r=selected,
                  market_branch_r=selected, base_selected_reward_r=base_reward,
                  target_room_flags=dict(base['target_room_flags'], **{'4': room_four}),
                  extended_spec_sha256=SPEC_SHA, base_spec_sha256=BASE_SPEC_SHA)
    return result, ''
