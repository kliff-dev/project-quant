"""Synthetic prices exercise every new hypothesis; all tests run offline."""
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from analytics import five_setups as kernel
from analytics.candidate_signals import wilder_atr, wilder_rsi


def fixture_frame(symbol, setup_id, side='buy'):
    """Actual OHLC that uniquely selects each of the five patterns, both ways."""
    n = kernel.INPUT_ROWS[symbol]
    j = np.arange(n)
    if setup_id in ('range_reentry', 'compression_breakout'):
        close = 1.1 + .0001*np.sin(j*.2)
    else:
        close = 1.1 + j*.000002 + .00003*np.sin(j*.2)
    if setup_id == 'trend_reclaim':
        close[-2] = 1.1 + (n-2)*.000002 - .00003
        close[-1] = 1.1 + (n-1)*.000002 + .00002
    if setup_id == 'compression_breakout':
        close[-60:] = close[-61] + .000002*np.sin(np.arange(60)*.8)
    opening = np.r_[close[0], close[:-1]]
    high, low = np.maximum(opening, close)+.00005, np.minimum(opening, close)-.00005
    if setup_id == 'trend_sweep':
        low[-1] = low[-21:-1].min()-.000005
        opening[-1] = close[-1]-.00001
    if setup_id in ('channel_breakout', 'compression_breakout'):
        close[-1] = high[-21:-1].max()+.00001
        opening[-1] = close[-2]
        high[-1] = close[-1]+.000005
        low[-1] = min(low[-1], opening[-1]-.000005)
    if setup_id == 'range_reentry':
        mean, std = close[-21:-1].mean(), close[-21:-1].std(ddof=0)
        close[-1] = mean-.5*std
        opening[-1] = close[-1]-.00002
        high[-1] = max(high[-1], close[-1])
        low[-1] = mean-2*std-.000005
    if side == 'sell':
        opening, close, high, low = 2.2-opening, 2.2-close, 2.2-low, 2.2-high
    if symbol == 'XAUUSDm':
        opening, close, high, low = [3500+(x-1.1)*2000 for x in (opening, close, high, low)]
    freq = '5min' if symbol == 'XAUUSDm' else 'h'
    end = '2026-11-13 09:55Z' if symbol == 'XAUUSDm' else '2026-11-13 09:00Z'
    return pd.DataFrame(dict(open=opening, high=high, low=low, close=close,
                            spread=8, tick_volume=100, real_volume=0),
                        index=pd.date_range(end=end, periods=n, freq=freq))


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
@pytest.mark.parametrize('setup_id', kernel.SETUPS)
@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_actual_prices_select_all_ten_patterns_both_directions(symbol, setup_id, side):
    frame = fixture_frame(symbol, setup_id, side)
    original = frame.copy(deep=True)
    result = kernel.evaluate_setups(frame, symbol)
    assert result['profile'] == 'multi_setup_v1'
    assert result['selected_setup'] == setup_id
    assert tuple(result['setups']) == kernel.SETUPS
    selected = result['selected']
    assert selected['side'] == selected['raw_side'] == side
    assert selected['setup_id'] == selected['strategy_family'] == setup_id
    assert selected['reason'] == 'valid_new_setup'
    assert selected['input_window_rows'] == len(frame)
    assert selected['indicator_seed_rows'] == len(frame)-1
    assert selected['atr14'] == pytest.approx(wilder_atr(frame.iloc[1:]).iloc[-1])
    base = 'm5' if symbol == 'XAUUSDm' else 'h1'
    context = 'm15' if symbol == 'XAUUSDm' else 'h4'
    assert selected[f'{base}_rsi14'] == pytest.approx(wilder_rsi(frame.close.iloc[1:]).iloc[-1])
    assert isinstance(selected[f'{context}_swing_highs'], list)
    assert isinstance(selected[f'{context}_swing_lows'], list)
    if setup_id == 'range_reentry':
        assert selected['market_regime'] == 'range'
        assert not selected['pullback_long'] and not selected['pullback_short']
    elif setup_id in ('channel_breakout', 'compression_breakout'):
        assert selected[f'breakout_{"long" if side == "buy" else "short"}']
        assert not selected['pullback_long'] and not selected['pullback_short']
    else:
        assert selected[f'pullback_{"long" if side == "buy" else "short"}']
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
def test_indicator_window_is_bounded_and_future_candles_do_not_rewrite_past(symbol):
    frame = fixture_frame(symbol, 'channel_breakout')
    expected = kernel.evaluate_setups(frame, symbol)
    interval = frame.index[-1]-frame.index[-2]
    old = frame.iloc[:2].copy()
    old.index = pd.date_range(end=frame.index[0]-interval, periods=2, freq=interval)
    old[['open', 'high', 'low', 'close']] += 500
    assert kernel.evaluate_setups(pd.concat([old, frame]), symbol) == expected
    future = frame.iloc[-1:].copy()
    future.index += interval
    future[['open', 'high', 'low', 'close']] += 1000
    all_data = pd.concat([frame, future])
    assert kernel.evaluate_setups(all_data.loc[all_data.index <= frame.index[-1]], symbol) == expected


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
def test_continuing_channel_condition_does_not_generate_another_entry(symbol):
    frame = fixture_frame(symbol, 'channel_breakout')
    assert kernel.evaluate_setups(frame, symbol)['selected_setup'] == 'channel_breakout'
    previous = kernel._snapshot(frame.iloc[1:], symbol)
    next_row = frame.iloc[-1:].copy()
    next_row.index += frame.index[-1]-frame.index[-2]
    opening = float(frame.close.iloc[-1])
    close = float(frame.high.iloc[-1] + .1*previous['atr14'])
    next_row.loc[:, ['open', 'close', 'high', 'low']] = [opening, close, close+.01*previous['atr14'], opening-.01*previous['atr14']]
    result = kernel.evaluate_setups(pd.concat([frame, next_row]), symbol)
    channel = result['setups']['channel_breakout']
    assert channel['raw_side'] == channel['prior_condition_side'] == 'buy'
    assert channel['side'] is None
    assert channel['reason'] == 'condition_persists_no_new_trigger'


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
def test_warmup_gap_flat_quote_and_weekend_block_all_setups(symbol):
    frame = fixture_frame(symbol, 'trend_reclaim')
    assert kernel.evaluate_setups(frame.iloc[1:], symbol)['selected_setup'] is None
    interval = frame.index[-1]-frame.index[-2]
    old = frame.iloc[:1].copy()
    old.index -= interval
    gapped = pd.concat([old, frame.drop(frame.index[-3])])
    result = kernel.evaluate_setups(gapped, symbol)
    assert result['selected_setup'] is None
    assert {item['reason'] for item in result['setups'].values()} == {'recent_missing_candles'}
    flat = frame.copy()
    flat.iloc[-2, flat.columns.get_indexer(['open', 'high', 'low', 'close'])] = frame.close.iloc[-2]
    assert {v['reason'] for v in kernel.evaluate_setups(flat, symbol)['setups'].values()} == {'recent_flat_quote_candle'}
    weekend = frame.copy()
    weekend.index += pd.Timedelta(days=1)
    result = kernel.evaluate_setups(weekend, symbol)
    assert result['selected_setup'] is None
    assert result['market_context']['weekday'] == 'Saturday'


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
@pytest.mark.parametrize('fault', ['negative', 'geometry', 'nan', 'boolean', 'duplicate', 'naive'])
def test_invalid_market_inputs_are_rejected(symbol, fault):
    frame = fixture_frame(symbol, 'trend_reclaim')
    if fault == 'negative':
        frame.iloc[-1, frame.columns.get_loc('close')] = -1
    elif fault == 'geometry':
        frame.iloc[-1, frame.columns.get_loc('high')] = frame.low.iloc[-1]-1
    elif fault == 'nan':
        frame.iloc[-1, frame.columns.get_loc('spread')] = np.nan
    elif fault == 'boolean':
        frame['spread'] = True
    elif fault == 'duplicate':
        frame.index = pd.DatetimeIndex([*frame.index[:-1], frame.index[-2]])
    else:
        frame.index = frame.index.tz_localize(None)
    with pytest.raises(ValueError):
        kernel.evaluate_setups(frame, symbol)


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
def test_shock_or_excess_volatility_blocks_every_pattern(symbol):
    frame = fixture_frame(symbol, 'trend_sweep')
    frame.iloc[-1, frame.columns.get_loc('high')] += 100
    result = kernel.evaluate_setups(frame, symbol)
    assert result['selected_setup'] is None
    assert all(item['reason'] in ('shock_candle', 'volatility_outside_075_175', 'volatility_outside_050_200')
               for item in result['setups'].values())


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
def test_range_is_not_an_alias_for_a_trend_or_breakout(symbol):
    frame = fixture_frame(symbol, 'range_reentry')
    result = kernel.evaluate_setups(frame, symbol)
    assert result['selected_setup'] == 'range_reentry'
    for key in ('trend_reclaim', 'trend_sweep', 'channel_breakout'):
        assert result['setups'][key]['side'] is None
    f = result['selected']
    assert f['range_regime'] and not f['trend_long'] and not f['trend_short']


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
def test_squeeze_uses_prior_candles_and_not_breakout_width(symbol):
    frame = fixture_frame(symbol, 'compression_breakout')
    result = kernel.evaluate_setups(frame, symbol)
    f = result['selected']
    close = frame.close.iloc[1:]
    widths = 4*close.rolling(20).std(ddof=0)/close.rolling(20).mean()
    assert f['prior20_bandwidth'] == pytest.approx(widths.iloc[-2])
    assert f['compression_threshold'] == pytest.approx(widths.iloc[:-2].iloc[-120:].quantile(.2))
    assert f['compression']


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
@pytest.mark.parametrize('setup_id', ['channel_breakout', 'compression_breakout'])
def test_channel_requires_strict_escape_and_does_not_chase_extension(symbol, setup_id):
    frame = fixture_frame(symbol, setup_id)
    channel_high = frame.high.iloc[-21:-1].max()
    boundary = frame.copy()
    boundary.iloc[-1, boundary.columns.get_loc('close')] = channel_high
    assert kernel.evaluate_setups(boundary, symbol)['setups'][setup_id]['side'] is None
    extended = frame.copy()
    # Excessively distant close is either a chase or a shared shock/volatility
    # rejection. None can authorize this channel hypothesis.
    extended.iloc[-1, extended.columns.get_loc('close')] += 10
    extended.iloc[-1, extended.columns.get_loc('high')] += 10
    assert kernel.evaluate_setups(extended, symbol)['setups'][setup_id]['side'] is None


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
def test_channel_lookback_cannot_bridge_a_missing_candle(symbol):
    frame = fixture_frame(symbol, 'channel_breakout')
    old = frame.iloc[:1].copy()
    old.index -= frame.index[-1]-frame.index[-2]
    gapped = pd.concat([old, frame.drop(frame.index[-15])])
    item = kernel.evaluate_setups(gapped, symbol)['setups']['channel_breakout']
    assert not item['pattern_window_contiguous']
    assert item['side'] is None


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
def test_common_low_volatility_ratio_blocks_every_setup(symbol):
    frame = fixture_frame(symbol, 'trend_reclaim')
    # Long history of broad ordinary candles, followed by twenty narrow bars.
    # The latest ATR falls relative to its trailing median without inventing
    # a missing candle or relying on a future quote.
    distance = 1. if symbol == 'XAUUSDm' else .001
    frame.loc[frame.index[:-20], 'high'] += distance
    frame.loc[frame.index[:-20], 'low'] -= distance
    result = kernel.evaluate_setups(frame, symbol)
    assert result['selected_setup'] is None
    expected = 'volatility_outside_075_175' if symbol == 'XAUUSDm' else 'volatility_outside_050_200'
    assert {item['reason'] for item in result['setups'].values()} == {expected}


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
def test_selected_target_pivots_match_independent_closed_context(symbol):
    frame = fixture_frame(symbol, 'channel_breakout')
    result = kernel.evaluate_setups(frame, symbol)
    decision = pd.Timestamp(result['selected']['decision_time_utc'])
    gold = symbol == 'XAUUSDm'
    context = kernel._context(frame.iloc[1:], decision, gold)
    expected = (kernel.confirmed_m15_swings if gold else kernel.confirmed_h4_swings)(context, decision)
    for key, values in expected.items():
        assert result['selected'][key] == values
        for pivot in values:
            assert pd.Timestamp(pivot['confirmed_at_utc']) <= decision


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
def test_wait_does_not_extract_target_swings_and_selected_does_once(symbol, monkeypatch):
    name = 'confirmed_m15_swings' if symbol == 'XAUUSDm' else 'confirmed_h4_swings'
    original = getattr(kernel, name)
    calls = []
    def count(context, decision):
        calls.append(decision)
        return original(context, decision)
    monkeypatch.setattr(kernel, name, count)
    ready = fixture_frame(symbol, 'channel_breakout')
    assert kernel.evaluate_setups(ready, symbol)['selected_setup']
    assert len(calls) == 1
    calls.clear()
    waiting = ready.copy()
    waiting.iloc[-1, waiting.columns.get_indexer(['open', 'high', 'low', 'close'])] = ready.close.iloc[-1]
    assert kernel.evaluate_setups(waiting, symbol)['selected_setup'] is None
    assert calls == []


def test_arbitration_uses_frozen_priority_and_rejects_persistent_opposition():
    same = {'channel_breakout': {'side': 'buy', 'raw_side': 'buy'},
            'compression_breakout': {'side': 'buy', 'raw_side': 'buy'}}
    assert kernel.arbitrate(same) == ('channel_breakout', 'same_direction_overlap_declared_priority')
    opposite = deepcopy(same)
    opposite['range_reentry'] = {'side': None, 'raw_side': 'sell'}
    assert kernel.arbitrate(opposite) == (None, 'conflicting_setup_directions')
    with pytest.raises(ValueError):
        kernel.arbitrate({'trend_reclaim': {'side': 'unknown'}})


@pytest.mark.parametrize('symbol', kernel.INPUT_ROWS)
def test_context_requires_every_component_and_releases_at_close(symbol):
    frame = fixture_frame(symbol, 'channel_breakout')
    interval = frame.index[-1]-frame.index[-2]
    decision = frame.index[-1]+interval
    gold = symbol == 'XAUUSDm'
    full = kernel._context(frame, decision, gold)
    context_interval = pd.Timedelta(minutes=15) if gold else pd.Timedelta(hours=4)
    last_open = full.index[-1]
    assert full.index[-1]+context_interval <= decision
    assert last_open not in kernel._context(frame, last_open+context_interval-pd.Timedelta(seconds=1), gold).index
    missing = frame.drop(frame.loc[(frame.index >= last_open) & (frame.index < last_open+context_interval)].index[0])
    assert last_open not in kernel._context(missing, decision, gold).index


def test_calendar_records_dst_aware_context_without_invented_performance():
    summer = kernel._calendar(pd.Timestamp('2026-07-14T12:00:00Z'))
    winter = kernel._calendar(pd.Timestamp('2026-11-23T12:00:00Z'))
    assert (summer['london_hour'], summer['new_york_hour']) == (13, 8)
    assert (winter['london_hour'], winter['new_york_hour']) == (12, 7)
    assert summer['session_context'] == 'london_new_york_overlap'
    assert winter['session_context'] == 'london'
    assert summer['calendar_performance_estimate'] is None


@pytest.mark.parametrize('gold,gap_limit,slope_limit', [(False, 2., .5), (True, .75, .2)])
@pytest.mark.parametrize('direction', [1, -1])
def test_instrument_specific_regime_boundaries_are_inclusive_and_exclusive(
        gold, gap_limit, slope_limit, direction):
    params = dict(gold=gold, ema50=100.+direction*gap_limit, ema200=100.,
                  last_close=100.+direction*(gap_limit+1.), slope=direction*slope_limit, atr=1.)
    # Even a directional EMA arrangement cannot borrow trend classification
    # at the low-trend boundary; consequently risk/target alignment is absent.
    assert kernel._classify_regime(**params) == (True, False, False)
    for changed in ('ema50', 'slope'):
        outside = dict(params)
        outside[changed] += direction*1e-8
        ranging, up, down = kernel._classify_regime(**outside)
        assert not ranging
        assert (up, down) == (direction == 1, direction == -1)


@pytest.mark.parametrize('direction', [1, -1])
def test_eur_restores_prior_h4_low_trend_coverage_without_altering_gold(direction):
    # This lies between the new Gold limits and the original EUR H4 limits.
    # It intentionally has a directional price arrangement: exclusive regime
    # precedence prevents interpreting it as an aligned high-risk trend.
    shared = dict(ema50=100.+direction*1.5, ema200=100.,
                  last_close=100.+direction*3., slope=direction*.4, atr=1.)
    assert kernel._classify_regime(gold=False, **shared) == (True, False, False)
    assert kernel._classify_regime(gold=True, **shared) == (False, direction == 1, direction == -1)


def test_spec_describes_exact_five_ids_each_instrument_and_pure_contract():
    spec = kernel.load_spec()
    assert spec['profile'] == kernel.PROFILE
    assert tuple(spec['priority']) == kernel.SETUPS
    for symbol, count in kernel.INPUT_ROWS.items():
        assert spec['instruments'][symbol]['input_rows'] == count
        assert tuple(spec['instruments'][symbol]['setups']) == kernel.SETUPS
    assert spec['instruments']['XAUUSDm']['common_guards']['shock_atr_max'] == 2.5
    provenance = spec['instruments']['EURUSDm']['setups']['range_reentry']['research_origin']
    assert 'candidate_strategy_spec_2026-11-14.json' in provenance
    assert '2.0 ATR' in provenance and '0.50 ATR' in provenance
    with pytest.raises(ValueError, match='exact approved'):
        kernel.evaluate_setups(fixture_frame('EURUSDm', 'trend_reclaim'), 'EURUSD')
