"""Synthetic, offline checks of the fixed candidate rules and causal windows."""

import numpy as np
import pandas as pd
import pytest

from analytics.candidate_signals import (
    FAMILIES, candidate_window_features, prepare_candidate_signals, wilder_atr, wilder_rsi,
)


def candles(close, *, end='2026-11-13T10:00:00Z'):
    close = np.asarray(close, dtype=float)
    opening = np.r_[close[0], close[:-1]]
    return pd.DataFrame(dict(open=opening, high=np.maximum(opening, close) + .00002,
        low=np.minimum(opening, close) - .00002, close=close, spread=8,
        tick_volume=100, real_volume=0),
        index=pd.date_range(end=end, periods=len(close), freq='h'))


def setup(family, side='buy', timestamp_unit='us'):
    n = 3000
    if family == 'range_reentry':
        close = 1.1 + .0001 * np.sin(np.arange(n) * .2)
        mean, std = close[-21:-1].mean(), close[-21:-1].std(ddof=0)
        close[-1] = mean - .5 * std
        frame = candles(close)
        frame.iloc[-1, frame.columns.get_loc('open')] = close[-1] - .00002
        frame.iloc[-1, frame.columns.get_loc('low')] = mean - 2 * std - .00002
    else:
        close = 1.1 + np.arange(n) * .000002 + .00003 * np.sin(np.arange(n) * .2)
        close[-2] = 1.1 + (n - 2) * .000002 - .00003
        close[-1] = 1.1 + (n - 1) * .000002 + .00002
        frame = candles(close)
        if family == 'trend_sweep':
            frame.iloc[-1, frame.columns.get_loc('low')] = frame.low.iloc[-21:-1].min() - .0001
    if side == 'sell':
        original = frame.copy()
        frame['open'] = 2.2 - original.open
        frame['close'] = 2.2 - original.close
        frame['high'] = 2.2 - original.low
        frame['low'] = 2.2 - original.high
    frame.index = frame.index.as_unit(timestamp_unit)
    return frame


def append_bar(frame, *, gap_hours=1):
    last = frame.iloc[-1].copy()
    last['open'] = last['close']
    last['high'] = last['close'] + .00002
    last['low'] = last['close'] - .00002
    later = pd.DataFrame([last], index=pd.DatetimeIndex([frame.index[-1] + pd.Timedelta(hours=gap_hours)]))
    return pd.concat([frame, later])


def test_atr_arithmetic_seed_and_wilder_recurrence_match_hand_calculation():
    frame = candles([10., 11., 10., 13., 13.])
    frame['high'], frame['low'] = frame.close + .5, frame.close - .5
    actual = wilder_atr(frame, period=3)
    seed = (1. + 1.5 + 1.5) / 3
    next_value = (2 * seed + 3.5) / 3
    assert actual.iloc[:2].isna().all()
    assert actual.iloc[2:].to_numpy() == pytest.approx([seed, next_value, (2 * next_value + 1.) / 3], abs=1e-14)


def test_rsi_seeds_from_price_differences_then_smooths():
    close = pd.Series([10., 11., 10., 12., 11., 14.])
    actual = wilder_rsi(close, period=3)
    assert actual.iloc[:3].isna().all()
    gain, loss = 1., 1. / 3
    expected = [100 - 100 / (1 + gain / loss)]
    for difference in [-1., 3.]:
        gain, loss = (2 * gain + max(difference, 0)) / 3, (2 * loss + max(-difference, 0)) / 3
        expected.append(100 - 100 / (1 + gain / loss))
    assert actual.iloc[3:].to_numpy() == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize('values,expected', [([1.] * 20, 50.), (list(range(20)), 100.), (list(range(20, 0, -1)), 0.)])
def test_rsi_flat_and_one_direction_edges(values, expected):
    actual = wilder_rsi(pd.Series(values, dtype=float))
    assert actual.iloc[:14].isna().all()
    assert actual.iloc[14:].eq(expected).all()


@pytest.mark.parametrize('family', FAMILIES)
@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('timestamp_unit', ['us', 'ns'])
def test_three_families_both_directions_and_timestamp_resolutions(family, side, timestamp_unit):
    frame = setup(family, side, timestamp_unit)
    feature = candidate_window_features(frame)
    direction, opposite = ('long', 'short') if side == 'buy' else ('short', 'long')
    assert feature[f'{family}_{direction}']
    assert not feature[f'{family}_{opposite}']
    assert feature['atr14'] > 0 and feature['h4_atr14'] > 0
    assert feature['input_window_rows'] == 3000 and feature['completed_h4_rows'] >= 600
    assert pd.Timestamp(feature['last_completed_h4_open_utc']) + pd.Timedelta(hours=4) <= frame.index[-1] + pd.Timedelta(hours=1)
    assert feature['prior20_low'] == frame.low.iloc[-21:-1].min()
    assert feature['prior20_high'] == frame.high.iloc[-21:-1].max()
    assert feature['prior20_close_mean'] == pytest.approx(frame.close.iloc[-21:-1].mean())
    assert feature['prior20_close_std_population'] == pytest.approx(frame.close.iloc[-21:-1].std(ddof=0))


@pytest.mark.parametrize('family', FAMILIES)
def test_preparation_transports_direction_without_misnaming_family(family):
    frame = append_bar(setup(family))
    signals = prepare_candidate_signals(frame, first_entry=frame.index[-1])
    item = signals[family][len(frame) - 1]
    assert item['strategy_family'] == family
    assert item['pullback_long'] and not item['pullback_short']
    assert not item['breakout_long'] and not item['breakout_short']
    assert 'direction only' in item['direction_transport_alias']
    assert pd.Timestamp(item['decision_time_utc']) == frame.index[-1]
    assert item['input_window_rows'] == 3000


def test_future_prices_and_future_spreads_cannot_change_existing_signals():
    prefix = append_bar(setup('trend_sweep'))
    full = prefix.copy()
    for _ in range(8):
        full = append_bar(full)
    changed = full.copy()
    changed.iloc[len(prefix):, changed.columns.get_indexer(['open', 'high', 'low', 'close'])] += .05
    changed.iloc[len(prefix):, changed.columns.get_loc('spread')] = 99
    start = prefix.index[-1]
    before = prepare_candidate_signals(prefix, first_entry=start)
    after = prepare_candidate_signals(changed, first_entry=start)
    assert before['trend_sweep']
    for family in FAMILIES:
        assert before[family] == {k: v for k, v in after[family].items() if k < len(prefix)}


def test_incomplete_h4_prices_do_not_enter_h4_indicators():
    frame = setup('trend_reclaim')
    before = candidate_window_features(frame)
    altered = frame.copy()
    incomplete = altered.index >= altered.index[-1].floor('4h')
    altered.loc[incomplete, ['open', 'high', 'low', 'close']] += .1
    after = candidate_window_features(altered)
    for key in ['h4_close', 'h4_ema50', 'h4_ema200', 'h4_ema50_slope5', 'h4_atr14', 'completed_h4_rows']:
        assert before[key] == after[key]


def test_signal_bar_extremes_are_excluded_from_prior_bands_and_range():
    frame = setup('range_reentry')
    before = candidate_window_features(frame)
    frame.iloc[-1, frame.columns.get_loc('high')] += .01
    frame.iloc[-1, frame.columns.get_loc('low')] -= .01
    frame.iloc[-1, frame.columns.get_loc('close')] += .001
    after = candidate_window_features(frame)
    for key in ['prior20_low', 'prior20_high', 'prior20_close_mean', 'prior20_close_std_population', 'prior20_lower_band', 'prior20_upper_band']:
        assert before[key] == after[key]


@pytest.mark.parametrize('family', FAMILIES)
def test_directional_candle_confirmation_is_required(family):
    frame = setup(family)
    assert candidate_window_features(frame)[f'{family}_long']
    # A doji retains the other prices/indicators while failing the bullish rule.
    frame.iloc[-1, frame.columns.get_loc('open')] = frame.close.iloc[-1]
    assert not candidate_window_features(frame)[f'{family}_long']


@pytest.mark.parametrize('decision', ['2026-11-13T07:00:00Z', '2026-11-13T14:00:00Z', '2026-11-14T11:00:00Z'])
def test_session_and_weekend_rejection(decision):
    frame = setup('trend_sweep')
    frame.index = pd.date_range(end=pd.Timestamp(decision) - pd.Timedelta(hours=1), periods=len(frame), freq='h')
    assert candidate_window_features(frame) == {}
    extended = append_bar(frame)
    assert all(not entries for entries in prepare_candidate_signals(extended, first_entry=extended.index[-1]).values())


def test_recent_gap_and_missing_next_bar_both_reject_entries():
    frame = setup('trend_sweep')
    recent_gap = frame.drop(frame.index[-3])
    assert candidate_window_features(recent_gap) == {}
    gap_entry = append_bar(frame, gap_hours=2)
    assert all(not entries for entries in prepare_candidate_signals(gap_entry, first_entry=frame.index[-1] + pd.Timedelta(hours=1)).values())
    assert all(not entries for entries in prepare_candidate_signals(frame, first_entry=frame.index[-1]).values())


def test_warmup_rejection_and_invalid_inputs():
    frame = setup('trend_sweep')
    assert candidate_window_features(frame.iloc[-2000:]) == {}
    for period in [0, -1, True, 1.5]:
        with pytest.raises(ValueError):
            wilder_rsi(frame.close, period)
        with pytest.raises(ValueError):
            wilder_atr(frame, period)
    with pytest.raises(ValueError):
        prepare_candidate_signals(frame, first_entry=frame.index[-1], window_bars=2399)
    with pytest.raises(ValueError):
        prepare_candidate_signals(frame, first_entry=pd.Timestamp('2026-11-13'))
    with pytest.raises(ValueError):
        candidate_window_features(frame.iloc[::-1])
