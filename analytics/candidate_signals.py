"""Frozen, causal entry candidates for offline research; no terminal imports.

The ``pullback_*`` fields in prepared signals transport direction to the existing
account simulator. ``strategy_family`` records the actual research entry rule;
these aliases do not claim that every candidate is the production pullback.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from analytics.setup_research import validate_bars


FAMILIES = ('trend_reclaim', 'trend_sweep', 'range_reentry')
HOUR = pd.Timedelta(hours=1)


def _period(value: int) -> None:
    if type(value) is not int or value < 1:
        raise ValueError('A positive integer Wilder period is required.')


def _seeded_wilder(values: np.ndarray, period: int) -> np.ndarray:
    """Arithmetic first-N seed followed by the Wilder recursive average."""
    result = np.full(len(values), np.nan)
    if len(values) >= period:
        seeded = np.r_[values[:period].mean(), values[period:]]
        result[period - 1:] = pd.Series(seeded).ewm(alpha=1 / period, adjust=False).mean().to_numpy()
    return result


def wilder_atr(data: pd.DataFrame, period: int = 14) -> pd.Series:
    """Include the first high-low range in the arithmetic N-bar ATR seed."""
    _period(period)
    previous = data.close.shift().to_numpy()
    ranges = np.nanmax(np.stack((data.high.to_numpy() - data.low.to_numpy(),
                                np.abs(data.high.to_numpy() - previous),
                                np.abs(data.low.to_numpy() - previous))), axis=0)
    return pd.Series(_seeded_wilder(ranges, period), index=data.index, name=f'atr{period}')


def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Seed from N price differences; flat/up-only/down-only are 50/100/0."""
    _period(period)
    result = np.full(len(close), np.nan)
    if len(close) > period:
        differences = np.diff(close.to_numpy(dtype=float))
        gains = _seeded_wilder(np.maximum(differences, 0.), period)
        losses = _seeded_wilder(np.maximum(-differences, 0.), period)
        valid = np.isfinite(gains) & np.isfinite(losses)
        values = np.full(len(gains), np.nan)
        both_zero = valid & (gains == 0) & (losses == 0)
        values[both_zero] = 50.
        no_loss = valid & (gains > 0) & (losses == 0)
        values[no_loss] = 100.
        with_loss = valid & (losses > 0)
        values[with_loss] = 100. - 100. / (1. + gains[with_loss] / losses[with_loss])
        result[1:] = values
    return pd.Series(result, index=close.index, name=f'rsi{period}')


def _candidate_window_features(window: pd.DataFrame) -> dict:
    """Internal snapshot for an already validated, completed H1 window."""
    if len(window) < 2400:
        return {}
    decision = window.index[-1] + HOUR
    local = decision.tz_convert('Europe/London')
    if local.day_of_week >= 5 or not 8 <= local.hour < 14:
        return {}
    if not window.index[-6:].to_series().diff().dropna().eq(HOUR).all():
        return {}
    h4 = window.resample('4h', origin='epoch').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    h4 = h4.loc[h4.index + pd.Timedelta(hours=4) <= decision]
    if len(h4) < 600:
        return {}
    ema20 = window.close.ewm(span=20, adjust=False, min_periods=20).mean()
    ema50 = window.close.ewm(span=50, adjust=False, min_periods=50).mean()
    h4_ema50 = h4.close.ewm(span=50, adjust=False, min_periods=50).mean()
    h4_ema200 = h4.close.ewm(span=200, adjust=False, min_periods=200).mean()
    atr = float(wilder_atr(window).iloc[-1])
    h4_atr = float(wilder_atr(h4).iloc[-1])
    rsi = float(wilder_rsi(window.close).iloc[-1])
    slope = float(h4_ema50.iloc[-1] - h4_ema50.iloc[-6])
    trend_long = bool(h4_ema50.iloc[-1] > h4_ema200.iloc[-1]
                      and h4.close.iloc[-1] > h4_ema50.iloc[-1] and slope > 0)
    trend_short = bool(h4_ema50.iloc[-1] < h4_ema200.iloc[-1]
                       and h4.close.iloc[-1] < h4_ema50.iloc[-1] and slope < 0)
    range_regime = bool(abs(h4_ema50.iloc[-1] - h4_ema200.iloc[-1]) <= 2. * h4_atr
                        and abs(slope) <= .5 * h4_atr)
    last, previous = window.iloc[-1], window.iloc[-2]
    bullish, bearish = bool(last.close > last.open), bool(last.close < last.open)
    prior = window.iloc[-21:-1]
    prior_low, prior_high = float(prior.low.min()), float(prior.high.max())
    mean, std = float(prior.close.mean()), float(prior.close.std(ddof=0))
    lower, upper = mean - 2. * std, mean + 2. * std
    return dict(
        decision_time_utc=decision.isoformat(),
        trend_reclaim_long=bool(trend_long and bullish and last.close > ema20.iloc[-1]
                                and previous.close <= ema20.iloc[-2] and last.close > ema50.iloc[-1]
                                and 50. <= rsi <= 65.),
        trend_reclaim_short=bool(trend_short and bearish and last.close < ema20.iloc[-1]
                                 and previous.close >= ema20.iloc[-2] and last.close < ema50.iloc[-1]
                                 and 35. <= rsi <= 50.),
        trend_sweep_long=bool(trend_long and last.low < prior_low and last.close > prior_low and bullish),
        trend_sweep_short=bool(trend_short and last.high > prior_high and last.close < prior_high and bearish),
        range_reentry_long=bool(range_regime and last.low < lower and last.close >= lower
                                and last.close < mean and bullish and rsi <= 45.),
        range_reentry_short=bool(range_regime and last.high > upper and last.close <= upper
                                 and last.close > mean and bearish and rsi >= 55.),
        trend_long=trend_long, trend_short=trend_short, range_regime=range_regime,
        atr14=atr, low5=float(window.low.iloc[-5:].min()), high5=float(window.high.iloc[-5:].max()),
        spread=int(last.spread), h1_close=float(last.close), h1_ema20=float(ema20.iloc[-1]),
        h1_ema50=float(ema50.iloc[-1]), h1_rsi14=rsi,
        h4_close=float(h4.close.iloc[-1]), h4_ema50=float(h4_ema50.iloc[-1]),
        h4_ema200=float(h4_ema200.iloc[-1]), h4_ema50_slope5=slope, h4_atr14=h4_atr,
        last_completed_h4_open_utc=h4.index[-1].isoformat(), completed_h4_rows=len(h4),
        prior20_low=prior_low, prior20_high=prior_high, prior20_close_mean=mean,
        prior20_close_std_population=std, prior20_lower_band=lower, prior20_upper_band=upper,
        input_window_start_utc=window.index[0].isoformat(), input_window_rows=len(window))


def candidate_window_features(window: pd.DataFrame) -> dict:
    """Return the last completed H1 snapshot, or {} for session/gap/warmup rejection."""
    return _candidate_window_features(validate_bars(window))


def prepare_candidate_signals(data: pd.DataFrame, *, first_entry: pd.Timestamp,
                              window_bars: int = 3000) -> dict[str, dict[int, dict]]:
    """Evaluate each frozen family using prior-only windows and next-H1 entries."""
    data = validate_bars(data)
    if type(window_bars) is not int or window_bars < 2400:
        raise ValueError('At least 2400 closed H1 rows required for H4 warmup.')
    first_entry = pd.Timestamp(first_entry)
    if first_entry.tz is None:
        raise ValueError('Timezone-aware first-entry timestamp required.')
    decisions = data.index + HOUR
    local = decisions.tz_convert('Europe/London')
    gate = ((local.day_of_week < 5) & (local.hour >= 8) & (local.hour < 14)
            & (decisions >= first_entry))
    result = {family: {} for family in FAMILIES}
    for i in np.flatnonzero(gate):
        if i + 1 >= len(data) or data.index[i + 1] != decisions[i]:
            continue
        item = _candidate_window_features(data.iloc[max(0, i - window_bars + 1):i + 1])
        if not item:
            continue
        for family in FAMILIES:
            long, short = item[f'{family}_long'], item[f'{family}_short']
            if long and short:
                raise ValueError(f'Conflicting {family} directions.')
            if long or short:
                result[family][int(i + 1)] = dict(item, strategy_family=family,
                    pullback_long=long, pullback_short=short, breakout_long=False, breakout_short=False,
                    direction_transport_alias='pullback_* carries direction only; strategy_family names the actual entry rule')
    return result
