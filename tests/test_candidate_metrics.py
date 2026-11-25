"""Tests of outcome uncertainty and development-only candidate selection."""

from copy import deepcopy

import pytest

from analytics.candidate_metrics import phase_passes, research_metrics, select_development


def trade(value, **extra):
    return dict(net_pnl=value, direction=1, holding_hours_upper_bound=2.,
                rollover_units=0, exit_reason='target' if value > 0 else 'stop', **extra)


def candidate(name='a', reward=2., **updates):
    summary = dict(completed_trades=40, win_rate_percent=50., mean_completed_r=.2,
                   initial_balance=500., final_equity=540., profit_factor=1.5,
                   max_h1_close_equity_drawdown_percent=8.)
    summary.update(updates)
    return dict(config_id=name, reward_r=reward, summary=summary,
                metrics=dict(win_rate_wilson95_lower_percent=35.),
                stress_summary=dict(mean_completed_r=.1, initial_balance=500., final_equity=510.))


def test_wilson_known_balanced_sample_and_breakeven_denominator():
    rows = [trade(1.) for _ in range(50)] + [trade(-1.) for _ in range(40)] + [trade(0.) for _ in range(10)]
    metrics = research_metrics(dict(trades=rows, open_position=trade(999.)))
    assert metrics['completed_trades'] == 100
    assert metrics['win_rate_percent'] == 50
    assert metrics['win_rate_wilson95_lower_percent'] == pytest.approx(40.38298, abs=.00001)
    assert metrics['win_rate_wilson95_upper_percent'] == pytest.approx(59.61702, abs=.00001)
    assert metrics['breakevens'] == 10
    assert 'selection' in metrics['wilson_interval_warning']


@pytest.mark.parametrize('value, expected_bound', [(1., 'upper'), (-1., 'lower')])
def test_wilson_small_extreme_sample_stays_bounded(value, expected_bound):
    metrics = research_metrics(dict(trades=[trade(value)]))
    assert 0 <= metrics['win_rate_wilson95_lower_percent'] <= metrics['win_rate_wilson95_upper_percent'] <= 100
    assert metrics[f'win_rate_wilson95_{expected_bound}_percent'] == pytest.approx(100 if value > 0 else 0)
    if value > 0:
        assert metrics['win_rate_wilson95_lower_percent'] < 21
    else:
        assert metrics['win_rate_wilson95_upper_percent'] > 79


def test_empty_run_reports_unknown_ratios_and_zero_counts():
    metrics = research_metrics(dict(trades=[]))
    for key in ('win_rate_percent', 'win_rate_wilson95_lower_percent', 'win_rate_wilson95_upper_percent',
                'realized_payoff_ratio', 'median_holding_hours_upper_bound', 'target_hit_percent'):
        assert metrics[key] is None
    assert metrics['completed_trades'] == metrics['max_consecutive_net_losses'] == 0
    assert metrics['overnight_completed_trades'] == metrics['weekend_spanning_completed_trades'] == 0


def test_realized_payoff_is_cash_outcome_not_requested_reward():
    rows = [trade(10.), trade(-5.), trade(2.), trade(-5.)]
    for row in rows:
        row['requested_reward_r'] = 2.
    metrics = research_metrics(dict(trades=rows))
    assert metrics['average_net_win_usd'] == 6
    assert metrics['average_net_loss_usd_abs'] == 5
    assert metrics['realized_payoff_ratio'] == 1.2


def test_losing_streak_breakeven_reset_and_duration_percentiles():
    rows = [trade(value) for value in (-1., -1., 0., -1., -1., -1., 1.)]
    for i, row in enumerate(rows):
        row['holding_hours_upper_bound'] = float(i + 1)
    metrics = research_metrics(dict(trades=rows))
    assert metrics['max_consecutive_net_losses'] == 3
    assert metrics['median_holding_hours_upper_bound'] == 4
    assert metrics['p90_holding_hours_upper_bound'] == pytest.approx(6.4)
    assert metrics['maximum_holding_hours_upper_bound'] == 7


def test_side_outcomes_target_gaps_and_exposure_counts():
    rows = [trade(8.), trade(-4.), trade(0.)]
    rows[0].update(direction=-1, rollover_units=3, exit_reason='target_gap',
                   entry_time_utc='2026-11-13T20:00:00Z', exit_time_utc='2026-11-16T10:00:00Z')
    rows[1].update(entry_time_utc='2026-11-13T20:00:00Z', exit_time_utc='2026-11-14T00:00:00Z')
    rows[2].update(entry_time_utc='2026-11-13T20:00:00Z', exit_time_utc='2026-11-13T23:00:00Z')
    metrics = research_metrics(dict(trades=rows))
    assert metrics['overnight_completed_trades'] == 1  # Triple units are one exposed trade.
    assert metrics['weekend_spanning_completed_trades'] == 1
    assert metrics['long_completed_trades'] == 2 and metrics['long_win_rate_percent'] == 0
    assert metrics['short_completed_trades'] == 1 and metrics['short_win_rate_percent'] == 100
    assert metrics['target_hit_percent'] == pytest.approx(100 / 3)


def test_missing_exposure_data_is_not_fabricated_and_duration_can_be_derived():
    row = dict(net_pnl=1., direction=1, entry_time_utc='2026-11-13T10:00:00Z',
               exit_time_utc='2026-11-13T13:00:00Z')
    metrics = research_metrics(dict(trades=[row]))
    assert metrics['overnight_completed_trades'] is None
    assert metrics['maximum_holding_hours_upper_bound'] == 3
    row.pop('exit_time_utc')
    metrics = research_metrics(dict(trades=[row]))
    assert metrics['weekend_spanning_completed_trades'] is None


@pytest.mark.parametrize('field, value', [('net_pnl', float('nan')), ('holding_hours_upper_bound', -1.),
                                         ('rollover_units', -1)])
def test_invalid_cash_or_exposure_values_fail(field, value):
    row = trade(1.)
    row[field] = value
    with pytest.raises(ValueError):
        research_metrics(dict(trades=[row]))


def test_unaware_or_reversed_timestamps_are_rejected():
    row = trade(1.)
    row.update(entry_time_utc='2026-11-13T10:00:00', exit_time_utc='2026-11-13T11:00:00Z')
    with pytest.raises(ValueError):
        research_metrics(dict(trades=[row]))
    row.update(entry_time_utc='2026-11-13T12:00:00Z')
    with pytest.raises(ValueError):
        research_metrics(dict(trades=[row]))


@pytest.mark.parametrize('updates', [dict(completed_trades=29), dict(win_rate_percent=44.99),
    dict(mean_completed_r=0.), dict(final_equity=500.), dict(profit_factor=1.),
    dict(max_h1_close_equity_drawdown_percent=15.01), dict(win_rate_percent=float('nan'))])
def test_phase_gates_reject_individually(updates):
    assert not phase_passes(candidate(**updates)['summary'], 30)
    assert select_development([candidate(**updates)]) is None


def test_phase_inclusive_boundaries_and_no_forced_winner():
    record = candidate(completed_trades=30, win_rate_percent=45., max_h1_close_equity_drawdown_percent=15.)
    assert phase_passes(record['summary'], 30)
    assert select_development([record]) == 'a'
    assert select_development([]) is None
    record['stress_summary']['mean_completed_r'] = 0.
    assert select_development([record]) is None
    record['stress_summary'].update(mean_completed_r=.1, final_equity=500.)
    assert select_development([record]) is None


def test_two_r_tier_has_priority_and_lower_tier_is_fallback_only():
    primary, fallback = candidate('two', reward=2.), candidate('onehalf', reward=1.5)
    fallback['metrics']['win_rate_wilson95_lower_percent'] = 70.
    assert select_development([fallback, primary]) == 'two'
    primary['summary']['completed_trades'] = 5
    assert select_development([fallback, primary]) == 'onehalf'
    fallback['reward_r'] = 1.
    assert select_development([fallback, primary]) is None


def test_ranking_wilson_then_mean_r_then_drawdown_then_id():
    a, b = candidate('a'), candidate('b')
    b['metrics']['win_rate_wilson95_lower_percent'] = 36.
    assert select_development([a, b]) == 'b'
    b['metrics']['win_rate_wilson95_lower_percent'] = 35.
    b['summary']['mean_completed_r'] = .3
    assert select_development([a, b]) == 'b'
    a['summary']['mean_completed_r'] = .3
    b['summary']['max_h1_close_equity_drawdown_percent'] = 7.
    assert select_development([a, b]) == 'b'
    a['summary']['max_h1_close_equity_drawdown_percent'] = 7.
    assert select_development([b, a]) == 'a'


def test_selector_cannot_read_later_period_and_does_not_mutate_records():
    class Forbidden:
        def __getitem__(self, key):
            raise AssertionError('Later outcomes must not be used to select development winner.')
    record = candidate()
    original = deepcopy(record)
    record['later_period'] = Forbidden()
    assert select_development([record]) == 'a'
    record.pop('later_period')
    assert record == original


@pytest.mark.parametrize('arguments', [dict(min_trades=0), dict(min_win_rate=101), dict(max_drawdown=-1)])
def test_selection_thresholds_are_validated_even_with_no_records(arguments):
    with pytest.raises(ValueError):
        select_development([], **arguments)
