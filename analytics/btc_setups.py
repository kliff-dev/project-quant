"""Four frozen, paper-inspired BTC H1 hypotheses; no terminal or order access.

Published evidence supports investigating these rule families, not this exact
intraday implementation, its symmetric shorts, or its structural 2R exits.
See docs/BTC_RESEARCH_SOURCES.md for the evidence and implementation boundary.
Input timestamps are timezone-aware UTC-aligned H1 candle OPEN times.
"""
from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from analytics.candidate_signals import wilder_atr

VERSION = 'btc_research_v1'
HOUR = pd.Timedelta(hours=1)
WARMUP = 300
REWARD_R = 2.0
SETUPS = {
    'B1': ('Moving-average crossover',
           'SMA20 crosses above SMA100 for BUY or below for SELL, on completed H1 closes.'),
    'B2': ('Trading-range breakout',
           'Close newly exceeds the prior 48 H1 highs for BUY or prior 48 lows for SELL; the signal candle is excluded.'),
    'B3': ('One-percent price filter',
           'Close newly exceeds 1.01 times the prior 24-close maximum for BUY, or falls below 0.99 times the prior 24-close minimum for SELL.'),
    'B4': ('Weekly time-series momentum',
           'Exact 168-hour close return crosses above zero with positive exact 12-hour return for BUY; both reverse for SELL.'),
}
SIGNAL_COLUMNS = [
    'setup_id', 'name', 'side', 'signal_open_utc', 'decision_time_utc',
    'close', 'atr14', 'stop_price', 'stop_distance', 'reward_r',
    'eligible_for_selection', 'selected_setup', 'selection_reason',
]


def arbitrate(setup_signals: dict, setup_ids: Iterable[str] | None = None) -> tuple[str | None, str]:
    """One deterministic choice; conflicting enabled directions fail closed.

    Priority is B1, B2, B3, B4 regardless of caller ordering. Admission rejection
    of the returned candidate must never cause a fallback to another setup.
    """
    enabled = tuple(SETUPS) if setup_ids is None else tuple(setup_ids)
    if not enabled or len(set(enabled)) != len(enabled) or set(enabled) - set(SETUPS):
        raise ValueError('Choose a nonempty unique subset of B1, B2, B3, B4.')
    sides = {}
    for key in SETUPS:
        if key not in enabled:
            continue
        value = setup_signals.get(key, {})
        side = value.get('side') if isinstance(value, dict) else value
        if side not in (None, 'buy', 'sell'):
            raise ValueError('Setup direction must be buy, sell or None.')
        if side:
            sides[key] = side
    if len(set(sides.values())) > 1:
        return None, 'conflicting_setup_directions'
    if not sides:
        return None, 'no_technical_setup'
    return next(iter(sides)), ('same_direction_priority_B1_to_B4' if len(sides) > 1 else 'unique_setup')


def _completed(frame: pd.DataFrame, as_of=None) -> tuple[pd.DataFrame, pd.Timestamp]:
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError('Timezone-aware H1 candle OPEN timestamps are required.')
    if frame.index.hasnans or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError('Timestamps must be sorted, unique and nonmissing.')
    if not {'open', 'high', 'low', 'close'}.issubset(frame):
        raise ValueError('OHLC columns are required.')
    data = frame.copy()
    data.index = data.index.tz_convert('UTC')
    if np.any(data.index.as_unit('ns').asi8 % HOUR.value):
        raise ValueError('Candle OPEN times must align with UTC H1 boundaries.')
    now = pd.Timestamp(as_of if as_of is not None else pd.Timestamp.now(tz='UTC'))
    if now.tz is None:
        raise ValueError('A timezone-aware as_of timestamp is required.')
    now = now.tz_convert('UTC')
    data = data.loc[data.index + HOUR <= now].copy()
    # Validate only information available at this decision. A forming/future
    # candle cannot change an earlier historical decision or its validation.
    prices = data[['open', 'high', 'low', 'close']].to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise ValueError('Completed OHLC prices must be positive and finite.')
    if ((data.high < data[['open', 'close', 'low']].max(axis=1)) |
            (data.low > data[['open', 'close', 'high']].min(axis=1))).any():
        raise ValueError('Completed OHLC price geometry is invalid.')
    return data, now


def _continuous(data: pd.DataFrame, count: int) -> pd.Series:
    """Exactly count consecutive H1 candles including the decision candle."""
    differences = data.index.to_series().diff().eq(HOUR)
    return differences.rolling(count - 1, min_periods=count - 1).sum().eq(count - 1)


def _exact_return(close: pd.Series, hours: int) -> pd.Series:
    # Reindex by clock time, never by row count across provider maintenance gaps.
    anchor = close.reindex(close.index - hours * HOUR).to_numpy(dtype=float)
    return pd.Series(close.to_numpy(dtype=float) / anchor - 1., index=close.index)


def _new(condition: pd.Series) -> pd.Series:
    condition = condition.fillna(False)
    return condition & ~condition.shift(fill_value=False)


def _latest_frame(records: list[dict], columns: list[str]) -> pd.DataFrame:
    result = pd.DataFrame(records, columns=columns)
    # Pandas 3 infers a nullable string dtype from mixed directions/None and
    # otherwise returns float NaN for a missing direction in to_dict(). Keep
    # the public contract explicitly buy/sell/None for broker-side arbitration.
    result['side'] = pd.Series([row['side'] for row in records], dtype=object)
    return result


def scan(frame: pd.DataFrame, as_of=None) -> dict:
    """Causally scan all completed candles, retaining all four raw triggers.

    All setups require 300 observed bars, six recent uninterrupted candles and
    a positive Wilder ATR14. Breakout/filter windows additionally require 50/26
    continuous bars (including their previous-condition comparison). B4 uses
    exact 168-hour and 12-hour anchors, not 168/12 observed rows. No candles are
    filled, no provider is substituted, and no parameter is fitted to outcomes.
    """
    data, now = _completed(frame, as_of)
    latest_columns = ['setup_id', 'name', 'side', 'reason', 'guard_passed']
    if data.empty:
        return dict(version=VERSION, signals=pd.DataFrame(columns=SIGNAL_COLUMNS),
                    latest=_latest_frame([dict(setup_id=key, name=value[0], side=None,
                        reason='no_completed_h1_candle', guard_passed=False) for key, value in SETUPS.items()],
                        latest_columns), guards={'completed_bars': 0, 'common': False},
                    completed_bars=0, eligible_bars=0, as_of_utc=now.isoformat(),
                    end_utc=None, stale=True)

    close = data.close.astype(float)
    fast, slow = close.rolling(20).mean(), close.rolling(100).mean()
    atr = wilder_atr(data)
    warm = pd.Series(np.arange(len(data)) >= WARMUP - 1, index=data.index)
    recent = _continuous(data, 6)
    active_quote = data.high.gt(data.low)
    common = warm & recent & atr.gt(0) & active_quote
    prior_high, prior_low = data.high.rolling(48).max().shift(), data.low.rolling(48).min().shift()
    upper, lower = close.rolling(24).max().shift() * 1.01, close.rolling(24).min().shift() * .99
    week, halfday = _exact_return(close, 168), _exact_return(close, 12)
    week_available = week.notna() & week.shift().notna() & halfday.notna()
    own_guards = {'B1': pd.Series(True, index=data.index),
                  'B2': _continuous(data, 50), 'B3': _continuous(data, 26),
                  'B4': week_available}
    pairs = {
        'B1': ((fast > slow) & (fast.shift() <= slow.shift()),
               (fast < slow) & (fast.shift() >= slow.shift())),
        'B2': (_new(close > prior_high), _new(close < prior_low)),
        'B3': (_new(close > upper), _new(close < lower)),
        'B4': ((week > 0) & (week.shift() <= 0) & (halfday > 0),
               (week < 0) & (week.shift() >= 0) & (halfday < 0)),
    }
    triggers, latest = {}, []
    for key, (buy, sell) in pairs.items():
        guard = common & own_guards[key]
        buy, sell = buy.fillna(False) & guard, sell.fillna(False) & guard
        trigger = pd.Series(np.where(buy & ~sell, 1, np.where(sell & ~buy, -1, 0)), index=data.index)
        triggers[key] = trigger
        reason = ('warmup_under_300_bars' if not warm.iloc[-1] else
                  'recent_missing_h1_candles' if not recent.iloc[-1] else
                  'nonpositive_atr_or_flat_latest_candle' if not common.iloc[-1] else
                  'missing_exact_168h_or_12h_anchor' if key == 'B4' and not own_guards[key].iloc[-1] else
                  'missing_candles_in_rule_window' if not own_guards[key].iloc[-1] else
                  'new_closed_h1_trigger' if trigger.iloc[-1] else 'pattern_not_present')
        latest.append(dict(setup_id=key, name=SETUPS[key][0],
                           side={1: 'buy', -1: 'sell', 0: None}[int(trigger.iloc[-1])],
                           reason=reason, guard_passed=bool(guard.iloc[-1])))

    # These levels are frozen using the completed decision candle; admission
    # recomputes actual monetary risk from the executable bid/ask and lot size.
    stop_low = data.low.rolling(5).min() - .2 * atr
    stop_high = data.high.rolling(5).max() + .2 * atr
    records = []
    event_rows = pd.concat(triggers, axis=1)
    for stamp, row in event_rows.loc[event_rows.ne(0).any(axis=1)].iterrows():
        sides = {key: {1: 'buy', -1: 'sell', 0: None}[int(row[key])] for key in SETUPS}
        selected, selection = arbitrate(sides)
        price, volatility = float(close.loc[stamp]), float(atr.loc[stamp])
        for key, side in sides.items():
            if side is None:
                continue
            stop = min(float(stop_low.loc[stamp]), price - 1.5 * volatility) if side == 'buy' else max(
                float(stop_high.loc[stamp]), price + 1.5 * volatility)
            records.append(dict(setup_id=key, name=SETUPS[key][0], side=side,
                signal_open_utc=stamp.isoformat(), decision_time_utc=(stamp + HOUR).isoformat(),
                close=price, atr14=volatility, stop_price=stop, stop_distance=abs(price - stop),
                reward_r=REWARD_R, eligible_for_selection=key == selected,
                selected_setup=selected, selection_reason=selection))
    signals = pd.DataFrame(records, columns=SIGNAL_COLUMNS)
    if not signals.empty:
        signals = signals.sort_values(['decision_time_utc', 'setup_id']).reset_index(drop=True)
    decision = data.index[-1] + HOUR
    return dict(version=VERSION, signals=signals, latest=_latest_frame(latest, latest_columns),
                guards=dict(completed_bars=len(data), warmup=bool(warm.iloc[-1]),
                            recent_six_contiguous=bool(recent.iloc[-1]), common=bool(common.iloc[-1]),
                            per_setup={key: bool(value.iloc[-1]) for key, value in own_guards.items()}),
                completed_bars=len(data), eligible_bars=int(common.sum()),
                start_utc=data.index[0].isoformat(), end_utc=decision.isoformat(),
                as_of_utc=now.isoformat(), stale=bool(now - decision > 2 * HOUR))


def evaluate(frame: pd.DataFrame, as_of=None, setup_ids: Iterable[str] | None = None) -> dict:
    """Latest closed-bar decision for an explicitly enabled subset.

    This is a technical candidate, never permission to trade. A stale latest
    bar (over two hours old) yields no candidate even if it historically fired.
    """
    enabled = tuple(SETUPS) if setup_ids is None else tuple(setup_ids)
    arbitrate({}, enabled)  # Validate even when input has insufficient warmup.
    result = scan(frame, as_of=as_of)
    setup_signals = {row['setup_id']: dict(side=None if pd.isna(row['side']) else row['side'], reason=row['reason'])
                     for row in result['latest'].to_dict('records')}
    selected, reason = arbitrate(setup_signals, enabled)
    technical_selected = selected
    if result['stale']:
        selected, reason = None, ('no_completed_h1_candle' if not result['completed_bars']
                                  else 'stale_completed_h1_candle')
    record = {}
    if selected:
        rows = result['signals']
        match = rows.loc[rows.setup_id.eq(selected) & rows.decision_time_utc.eq(result['end_utc'])]
        if len(match) != 1:
            raise ValueError('Latest setup must reproduce exactly one historical signal.')
        record = match.iloc[0].to_dict()
    return dict(version=VERSION, selected_setup=selected, technical_selected_setup=technical_selected,
                enabled_setups=list(enabled), side=record.get('side'),
                close=record.get('close'), atr14=record.get('atr14'),
                stop_price=record.get('stop_price'), stop_distance=record.get('stop_distance'),
                reward_r=REWARD_R, decision_time_utc=result['end_utc'],
                setup_signals=setup_signals, reason=reason,
                guards=result['guards'], stale=result['stale'])
