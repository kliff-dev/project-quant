"""Causal five-pattern DEMO hypotheses; no terminal, orders or network access.

Input consists exclusively of completed bid candles. Freshness against the
broker clock and affordability are separate, mandatory native-engine checks.
One row is reserved for the previous-condition baseline: both snapshots use
the same finite indicator seed length, never future or partial context bars.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from analytics.adaptive_targets import confirmed_h4_swings
from analytics.candidate_signals import wilder_atr, wilder_rsi
from analytics.gold_strategy import complete_m15, confirmed_m15_swings, validate_m5
from analytics.setup_research import validate_bars

PROFILE = 'multi_setup_v1'
SETUPS = ('trend_reclaim', 'trend_sweep', 'channel_breakout',
          'compression_breakout', 'range_reentry')
INPUT_ROWS = {'EURUSDm': 3000, 'XAUUSDm': 1200}
SPEC_PATH = Path(__file__).resolve().parents[1] / 'config/five_setup_spec.json'


def load_spec() -> dict:
    """Read the reviewed human-readable specification without changing it."""
    return json.loads(SPEC_PATH.read_text(encoding='utf-8'))


def _calendar(decision: pd.Timestamp) -> dict:
    london = decision.tz_convert('Europe/London')
    ny = decision.tz_convert('America/New_York')
    l_open, n_open = 7 <= london.hour < 17, 8 <= ny.hour < 17
    session = ('london_new_york_overlap' if l_open and n_open else
               'london' if l_open else 'new_york' if n_open else 'outside_london_new_york')
    return dict(weekday=decision.day_name(), weekday_number=decision.day_of_week,
                month=decision.month, london_hour=london.hour, new_york_hour=ny.hour,
                london_time=london.isoformat(), new_york_time=ny.isoformat(),
                session_context=session, calendar_performance_estimate=None)


def _context(window: pd.DataFrame, decision: pd.Timestamp, gold: bool) -> pd.DataFrame:
    if gold:
        return complete_m15(window, decision)
    result = window.resample('4h', origin='epoch').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
    counts = window.close.resample('4h', origin='epoch').count()
    return result.loc[(counts == 4) & (result.index + pd.Timedelta(hours=4) <= decision)].dropna()


def _classify_regime(*, gold: bool, ema50: float, ema200: float,
                     last_close: float, slope: float, atr: float) -> tuple[bool, bool, bool]:
    """Exclusive low-trend precedence; EUR restores its preregistered H4 rule."""
    gap_limit, slope_limit = (.75, .2) if gold else (2., .5)
    ranging = bool(abs(ema50-ema200) <= gap_limit*atr and abs(slope) <= slope_limit*atr)
    up = bool(not ranging and ema50 > ema200 and last_close > ema50 and slope > 0)
    down = bool(not ranging and ema50 < ema200 and last_close < ema50 and slope < 0)
    return ranging, up, down


def _snapshot(window: pd.DataFrame, symbol: str) -> dict:
    """Internal validated finite snapshot; callers enforce full input warmup."""
    gold = symbol == 'XAUUSDm'
    interval = pd.Timedelta(minutes=5) if gold else pd.Timedelta(hours=1)
    context_interval = pd.Timedelta(minutes=15) if gold else pd.Timedelta(hours=4)
    prefix = 'm15' if gold else 'h4'
    base = 'm5' if gold else 'h1'
    decision = window.index[-1] + interval
    result = dict(decision_time_utc=decision.isoformat(), symbol=symbol,
                  input_window_rows=len(window), input_window_start_utc=window.index[0].isoformat(),
                  reason='', **_calendar(decision))
    if not window.index[-6:].to_series().diff().dropna().eq(interval).all():
        return dict(result, reason='recent_missing_candles')
    if (window.high.iloc[-6:] == window.low.iloc[-6:]).any():
        return dict(result, reason='recent_flat_quote_candle')
    context = _context(window, decision, gold)
    minimum = 250 if gold else 600
    if len(context) < minimum:
        return dict(result, reason=f'insufficient_{minimum}_complete_{prefix}_warmup')
    if decision - (context.index[-1] + context_interval) >= context_interval:
        return dict(result, reason='stale_or_incomplete_context')
    ema20 = window.close.ewm(span=20, adjust=False, min_periods=20).mean()
    ema50 = window.close.ewm(span=50, adjust=False, min_periods=50).mean()
    c50 = context.close.ewm(span=50, adjust=False, min_periods=50).mean()
    c200 = context.close.ewm(span=200, adjust=False, min_periods=200).mean()
    atr_series = wilder_atr(window)
    atr, median = float(atr_series.iloc[-1]), float(atr_series.iloc[-100:].median())
    c_atr, rsi = float(wilder_atr(context).iloc[-1]), float(wilder_rsi(window.close).iloc[-1])
    slope = float(c50.iloc[-1] - c50.iloc[-6])
    if any(not math.isfinite(value) or value <= 0 for value in (atr, median, c_atr)) or not math.isfinite(rsi):
        return dict(result, reason='invalid_indicator_context')
    ratio = atr / median
    ranging, up, down = _classify_regime(gold=gold, ema50=float(c50.iloc[-1]),
        ema200=float(c200.iloc[-1]), last_close=float(context.close.iloc[-1]), slope=slope, atr=c_atr)
    last, previous = window.iloc[-1], window.iloc[-2]
    prior = window.iloc[-21:-1]
    mean, std = float(prior.close.mean()), float(prior.close.std(ddof=0))
    rolling_mean = window.close.rolling(20).mean()
    widths = 4*window.close.rolling(20).std(ddof=0)/rolling_mean
    # The squeeze is measured before the breakout, against earlier widths.
    prior_width = float(widths.iloc[-2])
    threshold = float(widths.iloc[:-2].iloc[-120:].quantile(.2))
    result.update(atr14=atr, volatility_ratio=ratio, spread=float(last.spread),
        low3=float(window.low.iloc[-3:].min()), high3=float(window.high.iloc[-3:].max()),
        low5=float(window.low.iloc[-5:].min()), high5=float(window.high.iloc[-5:].max()),
        trend_long=up, trend_short=down, range_regime=ranging,
        market_regime='range' if ranging else 'trend_up' if up else 'trend_down' if down else 'mixed',
        latest_bar_range=float(last.high-last.low),
        prior20_low=float(prior.low.min()), prior20_high=float(prior.high.max()),
        prior20_close_mean=mean, prior20_close_std_population=std,
        prior20_lower_band=mean-2*std, prior20_upper_band=mean+2*std,
        prior20_bandwidth=prior_width, compression_threshold=threshold,
        compression=bool(math.isfinite(threshold) and prior_width <= threshold and std > 0),
        pattern_window_contiguous=bool(window.index[-21:].to_series().diff().dropna().eq(interval).all()),
        previous_close=float(previous.close), previous_ema20=float(ema20.iloc[-2]),
        last_open=float(last.open), last_close=float(last.close),
        last_low=float(last.low), last_high=float(last.high),
        **{f'{base}_close': float(last.close), f'{base}_ema20': float(ema20.iloc[-1]),
           f'{base}_ema50': float(ema50.iloc[-1]), f'{base}_rsi14': rsi,
           f'{prefix}_close': float(context.close.iloc[-1]), f'{prefix}_ema50': float(c50.iloc[-1]),
           f'{prefix}_ema200': float(c200.iloc[-1]), f'{prefix}_ema50_slope5': slope,
           f'{prefix}_atr14': c_atr, f'completed_{prefix}_rows': len(context),
           f'last_completed_{prefix}_open_utc': context.index[-1].isoformat()})
    # Swing extraction is deferred until a fresh setup needs native planning;
    # raw pattern conditions and WAIT observations do not use target pivots.
    result.update({f'{prefix}_swing_highs': [], f'{prefix}_swing_lows': []})
    lo, hi, shock = (.75, 1.75, 2.5) if gold else (.5, 2., 3.)
    if not lo <= ratio <= hi:
        return dict(result, reason='volatility_outside_075_175' if gold else 'volatility_outside_050_200')
    if last.high-last.low > shock*atr:
        return dict(result, reason='shock_candle')
    if decision.day_of_week >= 5:
        return dict(result, reason='weekend_decision')
    return result


def _conditions(f: dict) -> dict[str, tuple[bool, bool]]:
    """Raw pattern conditions. No execution or affordability fallback."""
    if f.get('reason'):
        return {key: (False, False) for key in SETUPS}
    gold = f['symbol'] == 'XAUUSDm'
    base = 'm5' if gold else 'h1'
    last, opening, rsi = f['last_close'], f['last_open'], f[f'{base}_rsi14']
    bullish, bearish = last > opening, last < opening
    up, down = f['trend_long'], f['trend_short']
    continuous = f['pattern_window_contiguous']
    high, low, atr = f['prior20_high'], f['prior20_low'], f['atr14']
    chase = .75 if gold else .5
    break_long = bool(continuous and bullish and high < last <= high+chase*atr)
    break_short = bool(continuous and bearish and low > last >= low-chase*atr)
    return {
        'trend_reclaim': (bool(up and bullish and f['previous_close'] <= f['previous_ema20']
                              and last > f[f'{base}_ema20'] and last > f[f'{base}_ema50']
                              and 50 <= rsi <= (68 if gold else 65)),
                          bool(down and bearish and f['previous_close'] >= f['previous_ema20']
                               and last < f[f'{base}_ema20'] and last < f[f'{base}_ema50']
                               and (32 if gold else 35) <= rsi <= 50)),
        'trend_sweep': (bool(continuous and up and bullish and f['last_low'] < low < last),
                        bool(continuous and down and bearish and f['last_high'] > high > last)),
        'channel_breakout': (bool(up and break_long), bool(down and break_short)),
        'compression_breakout': (bool(f['compression'] and not down and break_long),
                                 bool(f['compression'] and not up and break_short)),
        'range_reentry': (bool(continuous and f['range_regime'] and bullish and
                              f['last_low'] < f['prior20_lower_band'] <= last < f['prior20_close_mean'] and rsi <= 45),
                          bool(continuous and f['range_regime'] and bearish and
                               f['last_high'] > f['prior20_upper_band'] >= last > f['prior20_close_mean'] and rsi >= 55)),
    }


def _side(pair: tuple[bool, bool]) -> str | None:
    return ('buy' if pair[0] else 'sell') if pair[0] != pair[1] else None


def arbitrate(setups: dict[str, dict]) -> tuple[str | None, str]:
    """Priority is predeclared; opposite raw directions reject the whole candle."""
    directions = {f.get('raw_side', f.get('side')) for f in setups.values()} - {None}
    if directions - {'buy', 'sell'}:
        raise ValueError('Unknown setup direction.')
    if len(directions) > 1:
        return None, 'conflicting_setup_directions'
    candidates = [key for key in SETUPS if setups.get(key, {}).get('side')]
    if not candidates:
        return None, 'no_new_technical_setup'
    return candidates[0], 'same_direction_overlap_declared_priority' if len(candidates) > 1 else 'unique_setup'


def evaluate_setups(window: pd.DataFrame, symbol: str) -> dict:
    if symbol not in INPUT_ROWS:
        raise ValueError('Only exact approved EURUSDm and XAUUSDm symbols have five-setup rules.')
    if not isinstance(window, pd.DataFrame):
        raise ValueError('A DataFrame of completed candles is required.')
    limit = INPUT_ROWS[symbol]
    frame = (validate_m5 if symbol == 'XAUUSDm' else validate_bars)(window.iloc[-limit:])
    if len(frame) < limit:
        feature = dict(symbol=symbol, reason=f'insufficient_{limit}_bar_warmup')
        current, previous = {key: (False, False) for key in SETUPS}, {}
    else:
        # Finite, identical seed lengths make prior-condition transition tests
        # reproduce the previous call without a mutable process-memory cache.
        feature = _snapshot(frame.iloc[1:], symbol)
        before = _snapshot(frame.iloc[:-1], symbol)
        current, previous = _conditions(feature), _conditions(before)
        feature.update(input_window_rows=len(frame), indicator_seed_rows=len(frame)-1,
                       input_window_start_utc=frame.index[0].isoformat())
        fresh = any(_side(current[key]) and _side(current[key]) != _side(previous[key]) for key in SETUPS)
        if fresh:
            gold = symbol == 'XAUUSDm'
            decision = pd.Timestamp(feature['decision_time_utc'])
            context = _context(frame.iloc[1:], decision, gold)
            feature.update((confirmed_m15_swings if gold else confirmed_h4_swings)(context, decision))
    setups = {}
    for key in SETUPS:
        raw = _side(current[key])
        prior_side = _side(previous.get(key, (False, False)))
        side = raw if raw != prior_side else None
        reason = feature.get('reason') or ('valid_new_setup' if side else
                 'condition_persists_no_new_trigger' if raw else 'pattern_or_regime_not_present')
        item = dict(feature, setup_id=key, strategy_family=key, raw_side=raw, side=side,
                    prior_condition_side=prior_side, reason=reason,
                    pullback_long=bool(side == 'buy' and key in ('trend_reclaim', 'trend_sweep')),
                    pullback_short=bool(side == 'sell' and key in ('trend_reclaim', 'trend_sweep')),
                    breakout_long=bool(side == 'buy' and key in ('channel_breakout', 'compression_breakout')),
                    breakout_short=bool(side == 'sell' and key in ('channel_breakout', 'compression_breakout')),
                    range_reentry_long=bool(side == 'buy' and key == 'range_reentry'),
                    range_reentry_short=bool(side == 'sell' and key == 'range_reentry'))
        setups[key] = item
    chosen, selection = arbitrate(setups)
    summaries = {key: {name: item.get(name) for name in ('side', 'raw_side', 'reason', 'market_regime')}
                 for key, item in setups.items()}
    selected = dict(setups[chosen]) if chosen else {}
    if selected:
        selected.update(setup_selection_reason=selection, setup_signals=summaries)
    context_keys = ('weekday', 'weekday_number', 'month', 'london_hour', 'new_york_hour',
                    'london_time', 'new_york_time', 'session_context', 'market_regime',
                    'volatility_ratio', 'decision_time_utc', 'calendar_performance_estimate')
    return dict(profile=PROFILE, selected_setup=chosen, selection_reason=selection,
                setups=setups, setup_signals=summaries, selected=selected,
                market_context={key: feature.get(key) for key in context_keys})
