"""Offline BTC H1 causality, exact clock anchors and frozen-rule contracts."""
import numpy as np
import pandas as pd
import pytest

from analytics.btc_setups import HOUR, SETUPS, VERSION, arbitrate, evaluate, scan


def bars(count=400, change=200.):
    index = pd.date_range('2026-01-01', periods=count, freq='h', tz='UTC')
    close = np.full(count, 10000., dtype=float)
    close[-1] += change
    opening = np.r_[close[0], close[:-1]]
    return pd.DataFrame(dict(open=opening, high=np.maximum(opening, close) + 2.,
                             low=np.minimum(opening, close) - 2., close=close), index=index)


def final_time(frame):
    return frame.index[-1] + HOUR


@pytest.mark.parametrize('change,side', [(200., 'buy'), (-200., 'sell')])
def test_all_four_families_trigger_from_completed_prices(change, side):
    frame = bars(change=change)
    result = scan(frame, as_of=final_time(frame))
    assert result['version'] == VERSION
    assert result['latest'].set_index('setup_id').side.to_dict() == dict.fromkeys(SETUPS, side)
    assert result['guards']['common']
    selected = evaluate(frame, as_of=final_time(frame))
    assert selected['selected_setup'] == 'B1'
    assert selected['side'] == side
    assert selected['reason'] == 'same_direction_priority_B1_to_B4'
    rows = result['signals'].loc[lambda x: x.decision_time_utc.eq(final_time(frame).isoformat())]
    assert rows.setup_id.tolist() == list(SETUPS)
    assert rows.eligible_for_selection.tolist() == [True, False, False, False]
    assert rows.reward_r.eq(2.).all()
    assert rows.stop_distance.ge(1.5 * rows.atr14).all()
    assert rows.stop_price.lt(rows.close).all() if side == 'buy' else rows.stop_price.gt(rows.close).all()


def test_structure_and_atr_stop_use_last_five_completed_candles():
    frame = bars()
    frame.loc[frame.index[-4], 'low'] = 9700.
    result = evaluate(frame, as_of=final_time(frame), setup_ids=('B3',))
    expected = min(frame.low.iloc[-5:].min() - .2 * result['atr14'], frame.close.iloc[-1] - 1.5 * result['atr14'])
    assert result['stop_price'] == pytest.approx(expected)
    assert result['stop_distance'] == pytest.approx(result['close'] - expected)


def test_partial_and_future_candles_cannot_change_past_decision():
    frame = bars()
    expected = evaluate(frame, as_of=final_time(frame))
    future = frame.iloc[[-1]].copy()
    future.index += HOUR
    future[['open', 'high', 'low', 'close']] = np.nan
    appended = pd.concat([frame, future])
    assert evaluate(appended, as_of=final_time(frame)) == expected
    actual = scan(appended, as_of=final_time(frame))
    pd.testing.assert_frame_equal(actual['signals'], scan(frame, as_of=final_time(frame))['signals'])
    before_close = evaluate(frame, as_of=final_time(frame) - pd.Timedelta(seconds=1))
    assert before_close['decision_time_utc'] == frame.index[-1].isoformat()
    assert before_close['side'] is None


def test_appending_actual_future_prices_preserves_all_prior_signals():
    frame = bars()
    extra = pd.concat([frame.iloc[[-1]]] * 30)
    extra.index = pd.date_range(final_time(frame), periods=30, freq='h')
    extra['open'], extra['close'], extra['high'], extra['low'] = 10200., 10210., 10212., 10198.
    past = scan(frame, as_of=final_time(frame))['signals']
    full = scan(pd.concat([frame, extra]), as_of=final_time(extra))['signals']
    full = full.loc[full.decision_time_utc.le(final_time(frame).isoformat())].reset_index(drop=True)
    pd.testing.assert_frame_equal(past, full)


def test_299_bars_and_recent_gap_block_all_setups():
    short = bars(299)
    result = scan(short, as_of=final_time(short))
    assert result['signals'].empty
    assert result['latest'].reason.eq('warmup_under_300_bars').all()
    frame = bars().drop(bars().index[-3])
    result = evaluate(frame, as_of=final_time(frame))
    assert result['side'] is None
    assert not result['guards']['recent_six_contiguous']


def test_old_maintenance_gap_does_not_block_all_300_warmup_bars():
    frame = bars().drop(bars().index[-100])
    result = evaluate(frame, as_of=final_time(frame))
    assert result['guards']['warmup']
    assert result['guards']['recent_six_contiguous']
    assert result['side'] == 'buy'


def test_breakout_windows_need_their_own_continuity():
    frame = bars().drop(bars().index[-30])
    latest = scan(frame, as_of=final_time(frame))['latest'].set_index('setup_id')
    assert latest.loc['B1', 'side'] == 'buy'
    assert latest.loc['B2', 'side'] is None
    assert latest.loc['B2', 'reason'] == 'missing_candles_in_rule_window'
    assert latest.loc['B3', 'side'] == 'buy'
    frame = bars().drop(bars().index[-20])
    latest = scan(frame, as_of=final_time(frame))['latest'].set_index('setup_id')
    assert latest.loc['B3', 'reason'] == 'missing_candles_in_rule_window'


def test_weekly_momentum_requires_exact_168_hour_anchor_not_168_rows():
    frame = bars()
    anchor = frame.index[-1] - 168 * HOUR
    frame = frame.drop(anchor)
    latest = scan(frame, as_of=final_time(frame))['latest'].set_index('setup_id')
    assert latest.loc['B1', 'side'] == 'buy'
    assert latest.loc['B4', 'side'] is None
    assert latest.loc['B4', 'reason'] == 'missing_exact_168h_or_12h_anchor'


def test_weekly_momentum_also_requires_matching_12_hour_return():
    frame = bars()
    anchor = frame.index[-1] - 12 * HOUR
    frame.loc[anchor, ['open', 'close', 'high', 'low']] = [10300., 10300., 10302., 10298.]
    latest = scan(frame, as_of=final_time(frame))['latest'].set_index('setup_id')
    assert latest.loc['B4', 'side'] is None


@pytest.mark.parametrize('setup,change', [('B2', 2.), ('B3', 100.)])
def test_strict_breakout_or_filter_threshold_is_required(setup, change):
    frame = bars(change=change)
    latest = scan(frame, as_of=final_time(frame))['latest'].set_index('setup_id')
    assert latest.loc[setup, 'side'] is None


def test_crossovers_and_breakouts_do_not_refire_each_persistent_bar():
    frame = bars()
    extra = frame.iloc[[-1]].copy()
    extra.index += HOUR
    extra[['open', 'close', 'high', 'low']] = [10200., 10200., 10202., 10198.]
    latest = scan(pd.concat([frame, extra]), as_of=final_time(extra))['latest']
    assert latest.side.isna().all()


def test_arbitration_subset_conflicts_priority_and_validation():
    sides = {'B1': {'side': 'buy'}, 'B2': {'side': 'sell'}, 'B3': {'side': 'buy'}}
    assert arbitrate(sides) == (None, 'conflicting_setup_directions')
    assert arbitrate(sides, ('B3', 'B1')) == ('B1', 'same_direction_priority_B1_to_B4')
    assert arbitrate(sides, ('B2',)) == ('B2', 'unique_setup')
    for invalid in [(), ('B5',), ('B1', 'B1')]:
        with pytest.raises(ValueError, match='subset'):
            arbitrate(sides, invalid)
    with pytest.raises(ValueError, match='direction'):
        arbitrate({'B1': 'hold'})
    frame = bars()
    assert evaluate(frame, as_of=final_time(frame), setup_ids=('B4',))['selected_setup'] == 'B4'


def test_stale_bars_show_historical_trigger_but_cannot_be_latest_candidate():
    frame = bars()
    result = evaluate(frame, as_of=final_time(frame) + 3 * HOUR)
    assert result['technical_selected_setup'] == 'B1'
    assert result['selected_setup'] is None
    assert result['side'] is None
    assert result['reason'] == 'stale_completed_h1_candle'


def test_no_completed_candle_is_a_safe_empty_decision():
    frame = bars(1)
    result = evaluate(frame, as_of=frame.index[0])
    assert result['selected_setup'] is None
    assert result['reason'] == 'no_completed_h1_candle'
    assert result['guards']['completed_bars'] == 0


@pytest.mark.parametrize('mutation', ['duplicate', 'naive', 'unsorted', 'off_hour', 'negative', 'geometry'])
def test_invalid_completed_data_is_not_silently_repaired(mutation):
    frame = bars()
    if mutation == 'duplicate':
        frame.index = frame.index[:-1].append(frame.index[-2:-1])
    elif mutation == 'naive':
        frame.index = frame.index.tz_localize(None)
    elif mutation == 'unsorted':
        frame = frame.iloc[::-1]
    elif mutation == 'off_hour':
        frame.index += pd.Timedelta(minutes=1)
    elif mutation == 'negative':
        frame.iloc[-1, frame.columns.get_loc('low')] = -1.
    else:
        frame.iloc[-1, frame.columns.get_loc('high')] = 9000.
    with pytest.raises(ValueError):
        scan(frame, as_of=pd.Timestamp('2027-01-01', tz='UTC'))


def test_naive_decision_time_is_rejected():
    with pytest.raises(ValueError, match='timezone-aware as_of'):
        scan(bars(), as_of='2026-12-01')
