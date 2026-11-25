"""Cash-outcome statistics and development-only candidate selection.

These summaries describe completed hypothetical trades, not entry indicators or
predictions. The Wilson interval assumes independent observations and does not
adjust for searching many candidates or changing market regimes.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


WILSON_WARNING = (
    'The 95% Wilson interval is descriptive: trade dependence, regime changes '
    'and strategy selection are not included in its uncertainty.'
)


def _finite(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _timestamps(trade: dict) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    entry, exit_ = trade.get('entry_time_utc'), trade.get('exit_time_utc')
    if entry is None or exit_ is None:
        return None
    start, end = pd.Timestamp(entry), pd.Timestamp(exit_)
    if pd.isna(start) or pd.isna(end) or start.tzinfo is None or end.tzinfo is None:
        raise ValueError('Completed-trade timestamps must be timezone aware.')
    start, end = start.tz_convert('UTC'), end.tz_convert('UTC')
    if end < start:
        raise ValueError('Trade exit precedes its entry.')
    return start, end


def _spans_weekend(start: pd.Timestamp, end: pd.Timestamp) -> bool:
    # Use a half-open holding interval; an exit exactly at Saturday 00:00 UTC
    # does not imply any time spent holding on Saturday.
    if end == start:
        return False
    last_day = (end - pd.Timedelta(nanoseconds=1)).normalize()
    return any(day.day_of_week >= 5 for day in pd.date_range(start.normalize(), last_day, freq='D'))


def research_metrics(run: dict) -> dict:
    """Recompute completed-trade outcomes; open inventory never becomes a win.

    An unavailable count is ``None`` rather than an invented zero. Overnight
    exposure uses the replay's New York rollover units, including triple units;
    it counts trades exposed, not the sum of financing units.
    """
    trades = run['trades']
    values, durations, weekend_flags, rollover_flags = [], [], [], []
    sides = {1: [], -1: []}
    streak = maximum_streak = targets = 0
    for trade in trades:
        value = trade['net_pnl']
        if not _finite(value):
            raise ValueError('Finite numeric net trade PnL required.')
        values.append(float(value))
        streak = streak + 1 if value < 0 else 0
        maximum_streak = max(maximum_streak, streak)
        if trade.get('direction') in sides:
            sides[trade['direction']].append(float(value))
        targets += trade.get('exit_reason') in ('target', 'target_gap')
        stamps = _timestamps(trade)
        weekend_flags.append(None if stamps is None else _spans_weekend(*stamps))
        units = trade.get('rollover_units')
        if units is not None and (not _finite(units) or units < 0):
            raise ValueError('Nonnegative finite rollover units required.')
        rollover_flags.append(None if units is None else units > 0)
        duration = trade.get('holding_hours_upper_bound')
        if duration is None and stamps is not None:
            duration = (stamps[1] - stamps[0]).total_seconds() / 3600
        if duration is not None:
            if not _finite(duration) or duration < 0:
                raise ValueError('Nonnegative finite holding hours required.')
            durations.append(float(duration))

    count = len(values)
    wins = [value for value in values if value > 0]
    losses = [-value for value in values if value < 0]
    lower = upper = None
    if count:
        p, z = len(wins) / count, 1.96
        denominator = 1 + z * z / count
        center = (p + z * z / (2 * count)) / denominator
        half = z * math.sqrt(p * (1 - p) / count + z * z / (4 * count * count)) / denominator
        lower, upper = max(0., 100 * (center - half)), min(100., 100 * (center + half))
    average_win = sum(wins) / len(wins) if wins else None
    average_loss = sum(losses) / len(losses) if losses else None

    def exposure_count(flags):
        return sum(flags) if all(flag is not None for flag in flags) else None

    result = dict(
        completed_trades=count,
        wins=len(wins), losses=len(losses), breakevens=count - len(wins) - len(losses),
        win_rate_percent=100 * len(wins) / count if count else None,
        win_rate_wilson95_lower_percent=lower,
        win_rate_wilson95_upper_percent=upper,
        wilson_interval_warning=WILSON_WARNING,
        average_net_win_usd=average_win,
        average_net_loss_usd_abs=average_loss,
        realized_payoff_ratio=average_win / average_loss if average_win is not None and average_loss is not None else None,
        max_consecutive_net_losses=maximum_streak,
        median_holding_hours_upper_bound=float(np.median(durations)) if durations else None,
        p90_holding_hours_upper_bound=float(np.quantile(durations, .9)) if durations else None,
        maximum_holding_hours_upper_bound=max(durations) if durations else None,
        holding_duration_observations=len(durations),
        overnight_completed_trades=exposure_count(rollover_flags),
        weekend_spanning_completed_trades=exposure_count(weekend_flags),
        target_hit_percent=100 * targets / count if count else None,
    )
    for direction, label in ((1, 'long'), (-1, 'short')):
        values_for_side = sides[direction]
        result[f'{label}_completed_trades'] = len(values_for_side)
        result[f'{label}_win_rate_percent'] = (
            100 * sum(value > 0 for value in values_for_side) / len(values_for_side)
            if values_for_side else None
        )
    return result


def phase_passes(summary: dict, min_trades: int, min_win_rate: float = 45,
                 max_drawdown: float = 15) -> bool:
    """Require sample size, positive historical payoff and controlled drawdown."""
    if type(min_trades) is not int or min_trades < 1:
        raise ValueError('Minimum completed-trade count must be a positive integer.')
    if not _finite(min_win_rate) or not 0 <= min_win_rate <= 100:
        raise ValueError('Win-rate threshold must be between 0 and 100 percent.')
    if not _finite(max_drawdown) or max_drawdown < 0:
        raise ValueError('Drawdown threshold must be nonnegative.')
    fields = ('completed_trades', 'win_rate_percent', 'mean_completed_r', 'final_equity',
              'initial_balance', 'profit_factor', 'max_h1_close_equity_drawdown_percent')
    if any(not _finite(summary.get(field)) for field in fields):
        return False
    return bool(
        summary['completed_trades'] >= min_trades
        and summary['win_rate_percent'] >= min_win_rate
        and summary['mean_completed_r'] > 0
        and summary['final_equity'] > summary['initial_balance']
        and summary['profit_factor'] > 1
        and 0 <= summary['max_h1_close_equity_drawdown_percent'] <= max_drawdown
    )


def select_development(records: list[dict], min_trades: int = 30,
                       min_win_rate: float = 45, max_drawdown: float = 15) -> str | None:
    """Choose from development + its cost stress only; never inspect later data.

    Prefer requested targets of at least 2R. If none qualify, allow the declared
    lower tier (at least 1.5R). Ranking uses Wilson lower bound, then mean R,
    lower drawdown and configuration ID. A winning candidate is never forced.
    """
    # Validate thresholds even for an empty experiment.
    phase_passes({}, min_trades, min_win_rate, max_drawdown)
    eligible = []
    for record in records:
        summary = record['summary']
        if not phase_passes(summary, min_trades, min_win_rate, max_drawdown):
            continue
        stress = record['stress_summary']
        if (not all(_finite(stress.get(key)) for key in ('mean_completed_r', 'final_equity', 'initial_balance'))
                or stress['mean_completed_r'] <= 0 or stress['final_equity'] <= stress['initial_balance']):
            continue
        reward = record['reward_r']
        lower = record['metrics'].get('win_rate_wilson95_lower_percent')
        if not _finite(reward) or reward < 1.5 or not _finite(lower) or not 0 <= lower <= 100:
            continue
        eligible.append(record)
    if not eligible:
        return None
    eligible.sort(key=lambda record: (
        0 if record['reward_r'] >= 2 else 1,
        -record['metrics']['win_rate_wilson95_lower_percent'],
        -record['summary']['mean_completed_r'],
        record['summary']['max_h1_close_equity_drawdown_percent'],
        record['config_id'],
    ))
    return eligible[0]['config_id']
