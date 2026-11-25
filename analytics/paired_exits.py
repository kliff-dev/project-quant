"""Isolated opportunity outcomes for exit comparisons, never a portfolio.

Every opportunity starts with the same fixed research balance and ignores other
positions. Overlapping outcomes may be paired to compare exit rules; summing
them does not establish an executable account's returns or risk.
"""

from __future__ import annotations

from collections.abc import Callable
import operator

import numpy as np
import pandas as pd

from analytics.account_research import ReplaySettings, make_plan, money, protective_fill
from analytics.setup_research import validate_bars
from scripts.research_holding_period import rollover_units


HOUR = pd.Timedelta(hours=1)


def replay_opportunity(data: pd.DataFrame, entry_index: int, signal: dict,
                       end: pd.Timestamp, settings: ReplaySettings, *,
                       plan_builder: Callable = make_plan) -> dict:
    """Replay one fixed-balance opportunity, with no overlap/position gating.

    The builder has the same ``(signal, bid, balance, settings)`` signature as
    ``make_plan`` and returns ``(plan_or_none, rejection_reason)``. All market
    inputs and decision timestamps must be aware; they are normalized to UTC.
    Only fully completed H1 bars through ``end`` are observable. A position
    still open there is marked, never forced closed or counted as a winner.
    """
    data = validate_bars(data)
    if isinstance(entry_index, (bool, np.bool_)):
        raise ValueError('A valid integer entry index is required.')
    try:
        entry_index = operator.index(entry_index)
    except TypeError as exc:
        raise ValueError('A valid integer entry index is required.') from exc
    if not 0 <= entry_index < len(data):
        raise ValueError('Entry index is outside the data.')
    if not callable(plan_builder):
        raise ValueError('A callable plan builder is required.')
    end = pd.Timestamp(end)
    decision = pd.Timestamp(signal.get('decision_time_utc'))
    if pd.isna(end) or end.tz is None:
        raise ValueError('A timezone-aware observation end is required.')
    if pd.isna(decision) or decision.tz is None:
        raise ValueError('A timezone-aware decision timestamp is required.')
    end, decision = end.tz_convert('UTC'), decision.tz_convert('UTC')
    entry_time = data.index[entry_index]
    if decision != entry_time:
        raise ValueError('The signal decision must equal the entry bar open.')
    if entry_time + HOUR > end:
        return dict(trade=None, rejection='no_completed_entry_bar')

    plan, reason = plan_builder(signal, float(data.open.iloc[entry_index]),
                                settings.initial_balance, settings)
    if plan is None:
        return dict(trade=None, rejection=reason)
    balance = money(settings.initial_balance - plan['entry_fee'])
    active = dict(plan, entry_time_utc=entry_time.isoformat(), signal=signal,
                  balance_before_entry=settings.initial_balance,
                  financing_cost=0., rollover_units=0)
    last_cost_time = entry_time
    last_bar = None
    last_observed = None

    def accrue(until: pd.Timestamp) -> None:
        nonlocal balance, last_cost_time
        if until <= last_cost_time:
            return
        units = rollover_units(last_cost_time, until)
        cost = money(settings.financing_per_001lot_unit * active['lots'] / .01 * units)
        balance = money(balance - cost)
        active['financing_cost'] = money(active['financing_cost'] + cost)
        active['rollover_units'] += units
        last_cost_time = until

    eligible = np.flatnonzero((np.arange(len(data)) >= entry_index)
                              & (data.index + HOUR <= end))
    for i in eligible:
        stamp, bar = data.index[i], data.iloc[i]
        # Match the account replay: financing through the next opening is
        # booked before testing an opening gap, including missing-market gaps.
        accrue(stamp)
        fill = protective_fill(active, bar, settings.ambiguity, opening_only=True)
        if fill is None:
            fill = protective_fill(active, bar, settings.ambiguity)
        if fill is not None:
            price, exit_reason, ambiguous = fill
            exit_time = stamp if exit_reason.endswith('_gap') else stamp + HOUR - pd.Timedelta(microseconds=1)
            accrue(exit_time)
            gross = money(active['direction'] * (price - active['entry_price']) * 100000 * active['lots'])
            net = money(gross - active['entry_fee'] - active['financing_cost'])
            trade = dict(active, status='closed', exit_time_utc=exit_time.isoformat(),
                         exit_price=price, exit_reason=exit_reason,
                         ambiguous_exit=ambiguous, gross_pnl=gross, net_pnl=net,
                         realized_r=net / active['planned_risk'],
                         balance_after_exit=money(balance + gross),
                         holding_hours_upper_bound=(exit_time - entry_time).total_seconds() / 3600)
            return dict(trade=trade, rejection=None)
        last_observed = stamp + HOUR
        accrue(last_observed)
        last_bar = bar

    # There is at least one completed entry bar by the guard above.
    mark = float(last_bar.close) + (float(last_bar.spread) * .00001 if active['direction'] == -1 else 0.)
    floating = money(active['direction'] * (mark - active['entry_price']) * 100000 * active['lots'])
    trade = dict(active, status='open_at_data_end', observed_at_utc=last_observed.isoformat(),
                 unrealized_price_pnl=floating,
                 net_marked_pnl=money(floating - active['entry_fee'] - active['financing_cost']))
    return dict(trade=trade, rejection=None)
