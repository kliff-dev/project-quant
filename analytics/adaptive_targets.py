"""Frozen, causal target policies for one offline trend-reclaim entry setup.

Swing prices are historical references, including subsequently breached swings;
they are not a claim that a price level will act as future support/resistance.
The provisional 1R branch has no outcome-based qualification in this module.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import math

import pandas as pd

from analytics.account_research import ReplaySettings, make_plan
from analytics.candidate_signals import prepare_candidate_signals, wilder_atr
from analytics.setup_research import validate_bars


H4 = pd.Timedelta(hours=4)
POLICIES = ('fixed_2r', 'adaptive_1_2_3')


def confirmed_h4_swings(h4: pd.DataFrame, decision: pd.Timestamp) -> dict[str, list[dict]]:
    """Strict two-left/two-right pivots entirely inside the last 120 closed H4s.

    A pivot is observable only when its second right-hand H4 candle has closed.
    Four neighbors must be strictly lower for highs, or strictly higher for lows.
    Missing market hours do not manufacture additional candle observations.
    """
    decision = pd.Timestamp(decision)
    if decision.tz is None or not isinstance(h4.index, pd.DatetimeIndex) or h4.index.tz is None:
        raise ValueError('Timezone-aware H4 and decision timestamps required.')
    if h4.index.hasnans or h4.index.has_duplicates or not h4.index.is_monotonic_increasing:
        raise ValueError('Valid, unique, increasing H4 timestamps required.')
    decision = decision.tz_convert('UTC')
    closed = h4.copy()
    closed.index = closed.index.tz_convert('UTC')
    closed = closed.loc[closed.index + H4 <= decision].iloc[-120:]
    result: dict[str, list[dict]] = {'h4_swing_highs': [], 'h4_swing_lows': []}
    for i in range(2, len(closed) - 2):
        neighbors = [i - 2, i - 1, i + 1, i + 2]
        confirmed = closed.index[i + 2] + H4
        for column, key, high in [('high', 'h4_swing_highs', True), ('low', 'h4_swing_lows', False)]:
            price = float(closed[column].iloc[i])
            values = closed[column].iloc[neighbors]
            if bool((values < price).all() if high else (values > price).all()):
                result[key].append(dict(price=price, pivot_open_utc=closed.index[i].isoformat(),
                                        confirmed_at_utc=confirmed.isoformat()))
    return result


def prepare_pullback_signals(data: pd.DataFrame, *, first_entry: pd.Timestamp,
                             window_bars: int = 3000) -> dict[int, dict]:
    """Keep the existing trend-reclaim entries and append prior-only target context.

    The ATR median includes the decision candle among its last 100 completed H1
    values. Every feature uses the same bounded rolling window as the entry.
    """
    data = validate_bars(data)
    entries = prepare_candidate_signals(data, first_entry=first_entry,
                                        window_bars=window_bars)['trend_reclaim']
    result = {}
    for i, signal in entries.items():
        window = data.iloc[max(0, i - window_bars):i]
        decision = pd.Timestamp(signal['decision_time_utc'])
        atr = wilder_atr(window)
        median = float(atr.iloc[-100:].median())
        if not math.isfinite(median) or median <= 0:
            raise ValueError('Positive finite 100-observation ATR median required.')
        h4 = window.resample('4h', origin='epoch').agg(
            {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
        result[i] = dict(signal, volatility_ratio=float(atr.iloc[-1]) / median,
                         **confirmed_h4_swings(h4, decision))
    return result


def _confirmed_obstacle(signal: dict, reference: Decimal, direction: int) -> tuple[dict, Decimal] | None:
    decision = pd.Timestamp(signal['decision_time_utc'])
    if decision.tz is None:
        raise ValueError('Timezone-aware decision timestamp required.')
    key = 'h4_swing_highs' if direction == 1 else 'h4_swing_lows'
    spread = Decimal(str(signal['spread'])) * Decimal('.00001')
    candidates = []
    for pivot in signal.get(key, []):
        confirmed = pd.Timestamp(pivot['confirmed_at_utc'])
        opened = pd.Timestamp(pivot['pivot_open_utc'])
        if confirmed.tz is None or opened.tz is None:
            raise ValueError('Timezone-aware pivot timestamps required.')
        if confirmed > decision or opened >= confirmed:
            continue
        price = Decimal(str(pivot['price']))
        if not price.is_finite() or price <= 0:
            raise ValueError('Positive finite obstacle prices required.')
        quote = price + (spread if direction == -1 else 0)
        if (quote > reference if direction == 1 else quote < reference):
            candidates.append((pivot, quote))
    if not candidates:
        return None
    return (min(candidates, key=lambda item: item[1]) if direction == 1
            else max(candidates, key=lambda item: item[1]))


def policy_plan(signal: dict, bid: float, balance: float, settings: ReplaySettings,
                *, policy: str) -> tuple[dict | None, str]:
    """Compare fixed 2R with predeclared context/room-based 1R, 2R or 3R.

    Both policies require room for the actual rounded 1R target. Fixed 2R may
    then extend past that reference obstacle deliberately: it is the comparator.
    Stops, lots and all reserves remain identical when only the target changes.
    """
    if policy not in POLICIES:
        raise ValueError(f'Unknown exit policy: {policy}')
    plans = {}
    for reward in (1, 2, 3):
        plan, reason = make_plan(signal, bid, balance, replace(settings, reward_r=float(reward)))
        if plan is None:
            return None, reason
        plans[reward] = plan
    reference = Decimal(str(plans[1]['reference_entry']))
    direction = plans[1]['direction']
    obstacle = _confirmed_obstacle(signal, reference, direction)
    if obstacle is None:
        return None, 'missing_confirmed_obstacle'
    pivot, quote = obstacle
    buffer = Decimal('.1') * Decimal(str(signal['atr14']))
    boundary = quote - direction * buffer
    room = {str(reward): bool(Decimal(str(plan['take_profit'])) <= boundary if direction == 1
                             else Decimal(str(plan['take_profit'])) >= boundary)
            for reward, plan in plans.items()}
    if not room['1']:
        return None, 'insufficient_1r_target_room'
    context = [signal[name] for name in ('h4_ema50', 'h4_ema200', 'h4_ema50_slope5',
                                        'h4_atr14', 'volatility_ratio')]
    if any(isinstance(v, bool) or not math.isfinite(v) for v in context) or min(context[-2:]) <= 0:
        raise ValueError('Finite H4 context and positive ATR/volatility ratio required.')
    ema50, ema200, slope, h4_atr, ratio = map(lambda value: Decimal(str(value)), context)
    strong = abs(ema50 - ema200) >= h4_atr and abs(slope) >= Decimal('.25') * h4_atr
    normal = Decimal('.75') <= ratio <= Decimal('1.5')
    if room['3'] and strong and normal:
        branch, branch_reason = 3, '3r_room_strong_trend_normal_volatility'
    elif room['2']:
        branch, branch_reason = 2, '2r_room_without_all_3r_conditions'
    else:
        branch, branch_reason = 1, 'only_1r_room_provisional_branch'
    selected = 2 if policy == 'fixed_2r' else branch
    plan = dict(plans[selected], exit_policy=policy, selected_reward_r=selected,
                market_branch_r=branch, branch_reason=branch_reason,
                obstacle_bid_price=float(pivot['price']), obstacle_exit_quote=float(quote),
                obstacle_pivot_open_utc=pivot['pivot_open_utc'],
                obstacle_confirmed_at_utc=pivot['confirmed_at_utc'],
                obstacle_buffer_price=float(buffer), strong_context=bool(strong),
                normal_volatility=bool(normal), volatility_ratio=float(ratio),
                target_room_flags=room)
    return plan, ''
