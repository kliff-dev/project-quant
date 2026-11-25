"""Prior-only follow-through experiment; no broker, orders or persistent state.

Reclaim flags transport the confirmed direction to the existing plan kernels.
The evidence explicitly distinguishes this delayed signal from an immediate
reclaim. Full seed and confirmation windows are reproducible from saved bars.
"""
from __future__ import annotations

import pandas as pd

from analytics.activation_signals import gold_features, eur_features
from analytics.gold_strategy import complete_m15, confirmed_m15_swings, validate_m5
from analytics.setup_research import validate_bars

MODE = 'closed_bar_followthrough_v1'
DEFAULT = 'immediate_reclaim'
ROWS = {'XAUUSDm': 1201, 'EURUSDm': 3001}


def side_of(features: dict, symbol: str):
    if symbol == 'XAUUSDm':
        return features.get('side')
    long, short = bool(features.get('trend_reclaim_long')), bool(features.get('trend_reclaim_short'))
    return ('buy' if long else 'sell') if long != short else None


def confirmed_features(window: pd.DataFrame, symbol: str) -> dict:
    if symbol not in ROWS:
        raise ValueError('Exact approved Gold or EUR/USD symbol required.')
    gold = symbol == 'XAUUSDm'
    data = (validate_m5(window) if gold else validate_bars(window)).iloc[-ROWS[symbol]:]
    interval = pd.Timedelta(minutes=5 if gold else 60)
    decision = data.index[-1]+interval
    result = dict(side=None, reason='confirmation_warmup_missing', entry_confirmation_mode=MODE,
                  decision_time_utc=decision.isoformat(), input_window_rows=len(data))
    if len(data) != ROWS[symbol]:
        return result
    if not data.index[-7:].to_series().diff().dropna().eq(interval).all():
        return dict(result,reason='confirmation_recent_gap')
    kernel = gold_features if gold else eur_features
    seed, current = kernel(data.iloc[:-1]), kernel(data.iloc[1:])
    side = side_of(seed,symbol)
    if not side:
        return dict(result,reason='no_previous_reclaim_to_confirm')
    prior, last = data.iloc[-2], data.iloc[-1]
    evidence = dict(mode=MODE, seed_decision_time_utc=(data.index[-2]+interval).isoformat(),
        seed_side=side, seed_high=float(prior.high), seed_low=float(prior.low),
        seed_close=float(prior.close), confirmation_close=float(last.close),
        decision_time_utc=decision.isoformat(), expires_at_utc=decision.isoformat())
    if not current:
        return dict(result,reason='confirmation_context_unavailable',entry_confirmation=evidence)
    prefix = 'm5' if gold else 'h1'
    rsi = current[f'{prefix}_rsi14']
    direction_ok = bool(last.close>last.open) if side=='buy' else bool(last.close<last.open)
    beyond = bool(last.close>prior.high) if side=='buy' else bool(last.close<prior.low)
    aligned = (bool(current.get('trend_long')) and last.close>current[f'{prefix}_ema20'] and
               last.close>current[f'{prefix}_ema50'] and 50<=rsi<=(68 if gold else 65)) if side=='buy' else (
               bool(current.get('trend_short')) and last.close<current[f'{prefix}_ema20'] and
               last.close<current[f'{prefix}_ema50'] and (32 if gold else 35)<=rsi<=50)
    evidence.update(direction_ok=direction_ok, closed_beyond_seed_extreme=beyond, current_alignment_ok=bool(aligned))
    if gold and (not .75<=current['volatility_ratio']<=1.75 or current['latest_bar_range']>2.5*current['atr14']):
        return dict(result,reason='confirmation_volatility_or_shock',entry_confirmation=evidence)
    if not (direction_ok and beyond and aligned):
        return dict(result,reason='confirmation_failed_or_expired',entry_confirmation=evidence)
    if gold:
        current.update(confirmed_m15_swings(complete_m15(data.iloc[1:],decision),decision))
    else:
        current.update(trend_reclaim_long=side=='buy',trend_reclaim_short=side=='sell',
                       pullback_long=side=='buy',pullback_short=side=='sell')
    return dict(current,side=side,reason='valid_confirmed_reclaim',
        strategy_family='gold_m15_trend_m5_confirmed_reclaim' if gold else 'h4_trend_h1_confirmed_reclaim',
        entry_confirmation_mode=MODE,entry_confirmation=evidence,input_window_rows=len(data),
        input_window_start_utc=data.index[0].isoformat(),decision_time_utc=decision.isoformat())
