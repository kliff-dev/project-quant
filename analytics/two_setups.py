"""Two frozen, causal entry patterns. No terminal, order or credential access."""
from __future__ import annotations

from analytics.activation_signals import gold_features, eur_features
from analytics.gold_strategy import complete_m15, confirmed_m15_swings

PROFILE = 'reclaim_sweep_v1'
SETUPS = ('trend_reclaim', 'trend_sweep')
LOOKBACK = 20


def side_of(features: dict, symbol: str) -> str | None:
    if symbol == 'XAUUSDm':
        return features.get('side')
    long, short = bool(features.get('pullback_long')), bool(features.get('pullback_short'))
    return ('buy' if long else 'sell') if long != short else None


def arbitrate(setups: dict[str, dict]) -> tuple[str | None, str]:
    """Deterministic A priority; opposite signals fail closed without a send."""
    sides = {key: value.get('side') for key, value in setups.items() if value.get('side')}
    if any(side not in ('buy', 'sell') for side in sides.values()):
        raise ValueError('Unknown setup direction.')
    if len(set(sides.values())) > 1:
        return None, 'conflicting_setup_directions'
    if not sides:
        return None, 'no_technical_setup'
    chosen = next(key for key in SETUPS if key in sides)
    return chosen, 'same_direction_overlap_A_priority' if len(sides) > 1 else 'unique_setup'


def evaluate_setups(window, symbol: str) -> dict:
    if symbol not in ('EURUSDm', 'XAUUSDm'):
        raise ValueError('Only exact approved symbols have two-setup rules.')
    gold = symbol == 'XAUUSDm'
    base = (gold_features if gold else eur_features)(window)
    a = dict(base, setup_id=SETUPS[0], side=side_of(base, symbol))
    b = dict(base, setup_id=SETUPS[1], strategy_family='trend_sweep', side=None)
    reason = base.get('reason', 'insufficient_context_or_recent_gap')
    # Gold returns full context before a no-reclaim verdict; all earlier
    # continuity/volatility/shock verdicts remain common to both patterns.
    common = bool(base) and (not gold or reason in ('valid_entry_setup', 'no_m5_reclaim_in_m15_trend'))
    if common:
        prior, last = window.iloc[-LOOKBACK-1:-1], window.iloc[-1]
        low, high = float(prior.low.min()), float(prior.high.max())
        long = bool(base['trend_long'] and last.low < low and last.close > low and last.close > last.open)
        short = bool(base['trend_short'] and last.high > high and last.close < high and last.close < last.open)
        side = ('buy' if long else 'sell') if long != short else None
        b.update(side=side, trend_sweep_long=long, trend_sweep_short=short,
                 prior20_low=low, prior20_high=high,
                 reason='valid_sweep_setup' if side else 'no_trend_aligned_prior20_sweep')
        if gold and side:
            import pandas as pd
            b.update(confirmed_m15_swings(complete_m15(window.iloc[-1200:], pd.Timestamp(base['decision_time_utc'])),
                                         pd.Timestamp(base['decision_time_utc'])))
        if not gold:
            # Existing research planner uses these direction transport fields.
            # setup_id/strategy_family preserve the actual pattern identity.
            b.update(pullback_long=long, pullback_short=short, breakout_long=False, breakout_short=False)
    else:
        b['reason'] = reason
    setups = {SETUPS[0]: a, SETUPS[1]: b}
    chosen, selection = arbitrate(setups)
    selected = dict(setups[chosen]) if chosen else {}
    summary = {key: dict(side=value.get('side'), reason=value.get('reason') or
                        ('valid_entry_setup' if value.get('side') else 'no_trend_reclaim'))
               for key, value in setups.items()}
    if selected:
        selected.update(setup_selection_reason=selection, setup_signals=summary)
    return dict(profile=PROFILE, selected_setup=chosen, selection_reason=selection,
                setups=setups, setup_signals=summary, selected=selected)
