"""Synthetic parity checks for paired opportunities; no real-data P&L tests."""

import pandas as pd
import pytest

from analytics.account_research import ReplaySettings, make_plan, replay_account
from analytics.paired_exits import replay_opportunity


def quiet(**changes):
    return dict(dict(open=1.1, high=1.1002, low=1.0998, close=1.1,
                     spread=8, tick_volume=100, real_volume=0), **changes)


def frame(rows, start='2026-11-13T10:00:00Z'):
    return pd.DataFrame(rows, index=pd.date_range(start, periods=len(rows), freq='h'))


def signal(stamp, side='buy'):
    return dict(decision_time_utc=stamp.isoformat(), pullback_long=side == 'buy',
                pullback_short=side == 'sell', breakout_long=False, breakout_short=False,
                atr14=.0015, low5=1.0999, high5=1.1001, spread=8)


def assert_matches_account(data, raw, settings, *, entry_index=0, end=None,
                           plan_builder=make_plan):
    end = end if end is not None else data.index[-1] + pd.Timedelta(hours=1)
    isolated = replay_opportunity(data, entry_index, raw, end, settings,
                                  plan_builder=plan_builder)
    account = replay_account(data, {entry_index: raw}, data.index[entry_index], end,
                             settings, plan_builder=plan_builder)
    if isolated['rejection'] is not None:
        assert isolated['trade'] is None
        assert account['rejections'][0]['reason'] == isolated['rejection']
    elif isolated['trade']['status'] == 'closed':
        assert isolated['trade'] == account['trades'][0]
        assert account['open_position'] is None
    else:
        expected = dict(account['open_position'])
        expected['net_marked_pnl'] = pytest.approx(account['summary']['final_equity'] - settings.initial_balance)
        assert isolated['trade'] == expected
        assert not account['trades']
        assert account['summary']['win_rate_percent'] is None
    return isolated, account


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('outcome', ['target', 'stop', 'target_gap', 'stop_gap', 'ambiguous', 'open'])
@pytest.mark.parametrize('ambiguity', ['stop_first', 'target_first'])
def test_isolated_outcome_matches_single_signal_account(side, outcome, ambiguity):
    up = side == 'buy'
    move_up = up if outcome.startswith('target') else not up
    if outcome == 'ambiguous':
        final = quiet(high=1.12, low=1.08)
    elif outcome == 'open':
        final = quiet(close=1.1001)
    elif outcome.endswith('_gap'):
        level = 1.12 if move_up else 1.08
        final = quiet(open=level, high=level + .0002, low=level - .0002, close=level)
    else:
        final = quiet(high=1.12) if move_up else quiet(low=1.08)
    data = frame([quiet(), final])
    isolated, _ = assert_matches_account(data, signal(data.index[0], side),
                                         ReplaySettings(ambiguity=ambiguity))
    trade = isolated['trade']
    if outcome == 'open':
        assert trade['status'] == 'open_at_data_end'
        assert 'net_pnl' not in trade and 'realized_r' not in trade
    elif outcome.endswith('_gap'):
        assert trade['exit_time_utc'] == data.index[1].isoformat()
        assert trade['exit_reason'] == outcome
    else:
        assert pd.Timestamp(trade['exit_time_utc']) == data.index[1] + pd.Timedelta(hours=1) - pd.Timedelta(microseconds=1)
        assert trade['ambiguous_exit'] == (outcome == 'ambiguous')


@pytest.mark.parametrize('side', ['buy', 'sell'])
@pytest.mark.parametrize('closed', [False, True])
def test_carry_rounding_and_wednesday_triple_match_account(side, closed):
    rows = [quiet(), quiet(), quiet()]
    if closed:
        rows[-1] = quiet(high=1.12) if side == 'buy' else quiet(low=1.08)
    data = frame(rows, start='2026-11-11T21:00:00Z')
    settings = ReplaySettings(financing_per_001lot_unit=.013)
    isolated, _ = assert_matches_account(data, signal(data.index[0], side), settings)
    trade = isolated['trade']
    assert trade['rollover_units'] == 3
    assert trade['financing_cost'] > 0


def test_weekend_gap_and_carry_match_account_without_double_booking():
    data = frame([quiet(), quiet(), quiet(high=1.12)], start='2026-11-13T20:00:00Z')
    data.index = pd.DatetimeIndex(['2026-11-13T20:00:00Z', '2026-11-13T21:00:00Z', '2026-11-16T08:00:00Z'])
    assert_matches_account(data, signal(data.index[0]), ReplaySettings(financing_per_001lot_unit=.03))


def test_builder_skip_preserves_rejection_and_never_creates_trade():
    data = frame([quiet()])
    calls = []
    def skip(raw, bid, balance, settings):
        calls.append((raw, bid, balance, settings))
        return None, 'insufficient_room'
    isolated, _ = assert_matches_account(data, signal(data.index[0]), ReplaySettings(), plan_builder=skip)
    assert isolated == dict(trade=None, rejection='insufficient_room')
    assert all(call[2] == 500. for call in calls)


def test_minimum_lot_rejection_matches_account():
    data = frame([quiet()])
    isolated, _ = assert_matches_account(data, signal(data.index[0]), ReplaySettings(initial_balance=50.))
    assert isolated['rejection'] == 'minimum_lot_risk'


def test_fixed_initial_balance_and_same_geometry_across_reward_choices():
    data = frame([quiet(), quiet(high=1.12)])
    raw = signal(data.index[0])
    plans = [make_plan(raw, 1.1, 500., ReplaySettings(reward_r=r))[0] for r in (1., 2., 3.)]
    for field in ('stop_loss', 'lots', 'planned_risk', 'risk_budget', 'cost_reserve', 'entry_fee', 'entry_price'):
        assert len({plan[field] for plan in plans}) == 1
    assert len({plan['take_profit'] for plan in plans}) == 3
    assert_matches_account(data, raw, ReplaySettings(reward_r=1.))


def test_entry_isolated_from_earlier_rows_and_uses_custom_plan_metadata():
    data = frame([quiet(high=1.12), quiet(), quiet(high=1.12)])
    raw = signal(data.index[1])
    def tagged(raw, bid, balance, settings):
        plan, reason = make_plan(raw, bid, balance, settings)
        return dict(plan, policy_id='synthetic_tag'), reason
    isolated, _ = assert_matches_account(data, raw, ReplaySettings(), entry_index=1, plan_builder=tagged)
    assert isolated['trade']['policy_id'] == 'synthetic_tag'
    assert isolated['trade']['entry_time_utc'] == data.index[1].isoformat()


def test_positive_open_mark_is_not_a_completed_win():
    data = frame([quiet(close=1.1002)])
    isolated, account = assert_matches_account(data, signal(data.index[0]),
                                                ReplaySettings(charge_cost_allowance=False))
    assert isolated['trade']['net_marked_pnl'] > 0
    assert account['summary']['completed_trades'] == 0
    assert account['summary']['wins'] == 0


@pytest.mark.parametrize('kind', ['data_naive', 'end_naive', 'signal_naive', 'wrong_decision', 'missing_decision', 'index_bool', 'index_float', 'index_outside'])
def test_invalid_timestamps_and_entry_are_rejected(kind):
    data = frame([quiet()])
    raw, end, index = signal(data.index[0]), data.index[0] + pd.Timedelta(hours=1), 0
    if kind == 'data_naive': data.index = data.index.tz_localize(None)
    elif kind == 'end_naive': end = end.tz_localize(None)
    elif kind == 'signal_naive': raw['decision_time_utc'] = '2026-11-13T10:00:00'
    elif kind == 'wrong_decision': raw['decision_time_utc'] = (data.index[0] - pd.Timedelta(hours=1)).isoformat()
    elif kind == 'missing_decision': del raw['decision_time_utc']
    elif kind == 'index_bool': index = True
    elif kind == 'index_float': index = 0.
    elif kind == 'index_outside': index = 1
    with pytest.raises(ValueError):
        replay_opportunity(data, index, raw, end, ReplaySettings())


def test_incomplete_entry_bar_is_rejected_without_calling_builder():
    data = frame([quiet()])
    def forbidden(*args):
        pytest.fail('A plan was built using an unobservable entry bar.')
    result = replay_opportunity(data, 0, signal(data.index[0]),
                                data.index[0] + pd.Timedelta(minutes=30), ReplaySettings(),
                                plan_builder=forbidden)
    assert result == dict(trade=None, rejection='no_completed_entry_bar')


def test_aware_non_utc_timestamps_normalize_to_utc():
    data = frame([quiet(high=1.12)])
    data.index = data.index.tz_convert('Asia/Manila')
    raw = signal(data.index[0])
    result = replay_opportunity(data, 0, raw, data.index[0] + pd.Timedelta(hours=1), ReplaySettings())
    assert result['trade']['entry_time_utc'] == data.index[0].tz_convert('UTC').isoformat()
