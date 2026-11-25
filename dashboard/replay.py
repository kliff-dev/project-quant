"""Offline, chronological two-instrument research. Never connects to a broker.

Current activation signal kernels and adaptive target selectors are reused.
Historical USD contract, margin, spread, cost and OHLC fill assumptions remain
models, not a reproduction of historical native-broker admission or ticks.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, ROUND_FLOOR
import hashlib
import math
from pathlib import Path

import pandas as pd

from analytics.activation_signals import gold_features, eur_features
from analytics.account_research import ReplaySettings, money, protective_fill as eur_fill
from analytics.extended_targets import policy_plan, EXIT_POLICY
from analytics.gold_strategy import make_plan as gold_plan, protective_fill as gold_fill, validate_m5
from analytics.setup_research import validate_bars
from dashboard.research_model import INTERVAL, WINDOW

ORDER = ('EURUSDm', 'XAUUSDm')


@dataclass(frozen=True)
class ModelSettings:
    initial_balance: float = 500.
    cost_model: str = 'allowance_debit'
    slippage_multiplier: float = 1.

    def __post_init__(self):
        if (type(self.initial_balance) not in (int, float) or not math.isfinite(self.initial_balance)
                or not 100 <= self.initial_balance <= 100000):
            raise ValueError('Research capital must be $100–$100,000.')
        if self.cost_model not in ('allowance_debit', 'reserve_only'):
            raise ValueError('Unknown modeled cost cashflow choice.')
        if self.slippage_multiplier not in (1, 2):
            raise ValueError('Slippage stress must be 1× or 2×.')


@dataclass(frozen=True)
class GoldModelBudget:
    """Research-only 1%/qualified-minimum-2% inputs; not execution authority."""
    initial_balance: float = 500.
    risk_percent: float = 1.
    max_lots: float = .1
    slippage_price: float = .05
    cost_fraction: float = .05
    charge_cost_allowance: bool = True
    leverage: int = 100


def strong_context(signal: dict, symbol: str) -> bool:
    context = 'm15' if symbol == 'XAUUSDm' else 'h4'
    d = lambda key: Decimal(str(signal[f'{context}_{key}']))
    atr = d('atr14')
    return (abs(d('ema50')-d('ema200')) >= atr
            and abs(d('ema50_slope5')) >= Decimal('.25')*atr
            and Decimal('.75') <= Decimal(str(signal['volatility_ratio'])) <= Decimal('1.5'))


def model_plan(symbol: str, signal: dict, bid: float, basis: float,
               settings: ModelSettings) -> tuple[dict | None, str]:
    """Baseline sizing first; rescue only the minimum lot, with >=2R room."""
    if not math.isfinite(basis) or basis <= 0:
        return None, 'nonpositive_risk_basis'
    if signal.get('spread', 0) <= 0:
        return None, 'spread_filter'
    charge = settings.cost_model == 'allowance_debit'
    if symbol == 'XAUUSDm':
        budget = GoldModelBudget(initial_balance=basis, charge_cost_allowance=charge,
                                 slippage_price=.05*settings.slippage_multiplier)
        build = lambda p: gold_plan(signal, bid, basis, p)
    elif symbol == 'EURUSDm':
        budget = ReplaySettings(initial_balance=basis, charge_cost_allowance=charge,
                                slippage_points=2*int(settings.slippage_multiplier))
        build = lambda p: policy_plan(signal, bid, basis, p, policy=EXIT_POLICY)
    else:
        raise ValueError('Unsupported instrument.')
    plan, reason = build(budget)
    rescue = False
    if plan is None and reason == 'minimum_lot_risk':
        if not strong_context(signal, symbol):
            return None, 'minimum_lot_requires_strong_context'
        plan, reason = build(replace(budget, risk_percent=2., max_lots=.01))
        rescue = True
        if plan and plan['selected_reward_r'] < 2:
            return None, 'minimum_lot_requires_2r_room'
    if plan is None:
        return None, reason
    return dict(plan, minimum_lot_rescue=rescue, actual_planned_risk_percent=plan['planned_risk']/basis*100,
                risk_mode='minimum_lot_rescue_v1', entry_timing='user_activation_v1'), ''


def prepare_current_signals(data, symbol, start, end, progress=None):
    """No session-hour gate. Cheap necessary gate cannot eliminate a reclaim."""
    interval = INTERVAL[symbol]
    minimum = 1200 if symbol == 'XAUUSDm' else 2400
    signals, counts = {}, Counter()
    positions = [i for i in range(minimum-1, len(data)-1)
                 if start <= data.index[i]+interval < end]
    for n, i in enumerate(positions):
        if progress and n % 100 == 0:
            progress((n+1)/max(1, len(positions)), symbol)
        decision = data.index[i]+interval
        if data.index[i+1] != decision:
            counts['no_contiguous_next_open'] += 1
            continue
        last, prior = data.iloc[i], data.iloc[i-1]
        # Reclaim from the previous side of EMA20 necessarily changes close
        # in the candle's direction; this is not an approximate EMA shortcut.
        possible = (last.close > last.open and last.close > prior.close or
                    last.close < last.open and last.close < prior.close)
        if not possible:
            counts['necessary_candle_direction_not_met'] += 1
            continue
        frame = data.iloc[max(0, i-WINDOW[symbol]+1):i+1]
        # Compute this necessary crossing from the exact same bounded, freshly
        # seeded EMA as the kernel. This avoids expensive swing extraction on
        # candles that cannot be an entry, without approximating EMA values.
        ema20 = frame.close.ewm(span=20, adjust=False, min_periods=20).mean()
        crossing = (last.close > ema20.iloc[-1] and prior.close <= ema20.iloc[-2] if last.close > last.open else
                    last.close < ema20.iloc[-1] and prior.close >= ema20.iloc[-2])
        if not crossing:
            counts['ema20_reclaim_not_met'] += 1
            continue
        features = gold_features(frame) if symbol == 'XAUUSDm' else eur_features(frame)
        if symbol == 'XAUUSDm':
            side = features.get('side')
            reason = features.get('reason', 'no_setup')
        else:
            long, short = bool(features.get('trend_reclaim_long')), bool(features.get('trend_reclaim_short'))
            side = ('buy' if long else 'sell') if long != short else None
            reason = 'valid_entry_setup' if side else 'no_unique_trend_reclaim_or_context'
        counts[reason] += 1
        if side:
            signals[i+1] = features
    if progress:
        progress(1., symbol)
    return signals, dict(counts)


def source_fingerprints(root: Path) -> dict:
    """Reproducible logic identity, without importing execution components."""
    paths = ['dashboard/replay.py', 'dashboard/research_model.py', 'analytics/activation_signals.py',
             'analytics/gold_strategy.py', 'analytics/account_research.py', 'analytics/extended_targets.py',
             'analytics/adaptive_targets.py', 'analytics/candidate_signals.py', 'analytics/setup_research.py',
             'config/activation_eurusd_policy.json', 'config/activation_gold_policy.json',
             'config/conditional_risk_spec.json', 'config/activation_timing_spec.json']
    return {p: hashlib.sha256((root/p).read_bytes()).hexdigest() for p in paths}


def replay_portfolio(frames: dict, start, end, settings=None, *, progress=None) -> dict:
    settings = settings or ModelSettings()
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start.tz is None or end.tz is None or end <= start:
        raise ValueError('Increasing timezone-aware replay bounds required.')
    if not frames or any(symbol not in ORDER for symbol in frames):
        raise ValueError('One or both approved instruments required.')
    data = {s: (validate_m5(f) if s == 'XAUUSDm' else validate_bars(f)) for s, f in frames.items()}
    signals, screenings = {}, {}
    for s, frame in data.items():
        signals[s], screenings[s] = prepare_current_signals(frame, s, start, end, progress)
    opens, closes = {}, {}
    for s, frame in data.items():
        for i in range(len(frame)):
            stamp = frame.index[i]
            close = stamp + INTERVAL[s]
            # Only simulate complete bars. Never use a partially observed end bar.
            if start <= stamp and close <= end:
                opens.setdefault(stamp, {})[s] = i
                closes.setdefault(close, {})[s] = i
    if not opens:
        raise ValueError('No complete bars inside the selected interval.')
    balance = settings.initial_balance
    active, quotes, previous_exit = {}, {}, {}
    trades, curve, rejections, decisions = [], [], [], []
    peak, maximum_dd = balance, 0.

    def mark():
        value = balance
        for s, plan in active.items():
            bid, spread = quotes[s]
            exit_quote = bid + (spread if plan['direction'] == -1 else 0) - plan['direction']*plan['slip']
            value += plan['direction']*plan['contract_size']*plan['lots']*(exit_quote-plan['entry_price'])
        return money(value)

    def fill(s, bar, opening_only=False):
        if s == 'XAUUSDm':
            return gold_fill(active[s], bar, opening_only=opening_only)
        return eur_fill(active[s], bar, 'stop_first', opening_only=opening_only)

    def finish(s, result, stamp):
        nonlocal balance
        plan = active.pop(s)
        price, reason, ambiguous = result
        gross = money(plan['direction']*plan['contract_size']*plan['lots']*(price-plan['entry_price']))
        balance = money(balance+gross)
        net = money(gross-plan['entry_fee'])
        trades.append(dict(plan, exit_time_utc=stamp.isoformat(), exit_price=price, exit_reason=reason,
                           ambiguous_bar=ambiguous, gross_pnl=gross, net_pnl=net,
                           net_r=net/plan['planned_risk'], balance_after=balance,
                           holding_hours_upper_bound=(stamp-pd.Timestamp(plan['entry_time_utc'])).total_seconds()/3600))
        previous_exit[s] = stamp

    for stamp in sorted(set(opens) | set(closes)):
        # Release a candle's range only at its close. An H1 exit is not made
        # available to an earlier M5 entry based on an unseen hourly high/low.
        for s, i in closes.get(stamp, {}).items():
            bar = data[s].iloc[i]
            quotes[s] = (float(bar.close), float(bar.spread)*(.001 if s == 'XAUUSDm' else .00001))
            if s in active:
                result = fill(s, bar)
                if result:
                    finish(s, result, stamp)
        occupied = set(active)
        for s, i in opens.get(stamp, {}).items():
            bar = data[s].iloc[i]
            quotes[s] = (float(bar.open), float(bar.spread)*(.001 if s == 'XAUUSDm' else .00001))
            if s in active:
                result = fill(s, bar, True)
                if result:
                    finish(s, result, stamp)
        for s in ORDER:
            if s not in opens.get(stamp, {}):
                continue
            i = opens[stamp][s]
            signal = signals[s].get(i)
            if signal is None:
                continue
            reason = ''
            plan = None
            if s in occupied or s in active:
                reason = 'instrument_position_open'
            elif stamp <= previous_exit.get(s, start-pd.Timedelta(seconds=1)):
                reason = 'signal_not_newer_than_exit'
            else:
                basis = min(balance, mark())
                plan, reason = model_plan(s, signal, float(data[s].iloc[i].open), basis, settings)
                if plan:
                    frozen_risk = sum(p['planned_risk'] for p in active.values())
                    ceiling = float((Decimal(str(basis))*Decimal('.02')).quantize(Decimal('.01'), rounding=ROUND_FLOOR))
                    if Decimal(str(frozen_risk))+Decimal(str(plan['planned_risk'])) > Decimal(str(ceiling)):
                        plan, reason = None, 'combined_2_percent_risk_ceiling'
                    elif sum(p['margin_estimate'] for p in active.values())+plan['margin_estimate'] > mark()-plan['entry_fee']:
                        plan, reason = None, 'combined_illustrative_margin'
            record = dict(symbol=s, decision_time_utc=stamp.isoformat(), status='SIMULATED_ENTRY' if plan else 'REJECTED',
                          reason=reason or 'modeled_plan_admitted', signal=signal, plan=plan)
            decisions.append(record)
            if plan:
                active[s] = dict(plan, symbol=s, entry_time_utc=stamp.isoformat(), signal=signal)
                balance = money(balance-plan['entry_fee'])
            else:
                rejections.append({k: record[k] for k in ('symbol', 'decision_time_utc', 'reason')})
        equity = mark()
        peak = max(peak, equity)
        dd = (peak-equity)/peak*100
        maximum_dd = max(maximum_dd, dd)
        curve.append(dict(time_utc=stamp.isoformat(), balance=balance, equity=equity,
                          drawdown_percent=dd, open_positions=len(active),
                          frozen_planned_risk=sum(p['planned_risk'] for p in active.values())))
    wins = sum(t['net_pnl'] > 0 for t in trades)
    return dict(version='local_research_dashboard_v1', entry_timing='user_activation_v1',
                risk_mode='minimum_lot_rescue_v1', settings=asdict(settings), start_utc=start.isoformat(), end_utc=end.isoformat(),
                initial_balance=settings.initial_balance, ending_balance=balance, ending_equity=curve[-1]['equity'],
                closed_trades=len(trades), win_rate=wins/len(trades) if trades else None,
                net_closed_pnl=money(sum(t['net_pnl'] for t in trades)),
                net_expectancy_r=sum(t['net_r'] for t in trades)/len(trades) if trades else None,
                maximum_close_marked_drawdown_percent=maximum_dd, open_positions=list(active.values()),
                trades=trades, equity_curve=curve, decisions=decisions, rejections=rejections,
                technical_screening_counts=screenings,
                assumptions=['Prior-only bounded activation signals; no fixed trading-hour filter.',
                    '1% baseline; strong-context minimum-lot-only exception up to 2% with at least 2R room; 2% combined cap.',
                    'Fixed USD linear contracts: Gold 100 oz/lot, .001 tick; EUR/USD 100000 units/lot, .00001 tick; .01 minimum/step; .10 cap.',
                    'Recorded bid OHLC and bar spread; next contiguous open plus modeled adverse entry/exit slippage.',
                    'Signal-candle spread is assumed at entry; exit-bar spread estimates SELL ask prices. Actual tick spreads can differ.',
                    'Stop first if SL and TP touch the same candle; intrabar exit time conservatively released at candle close.',
                    '100:1 illustrative margin; actual historical margin, permissions and broker-native admission not available.',
                    '5% of risk budget reserved as additional costs; allowance_debit also charges it once, reserve_only does not.',
                    'No actual historical commission or swap ledger. Zero swap assumed; allowance is not a verified broker fee.',
                    'Partial end bars excluded; open positions marked, not counted as completed trades; drawdown can miss intrabar extremes.',
                    'Portfolio marks use only the latest available completed close or current open, never a future H1 close.',
                    'No scheduled historical activation windows are supplied: entry eligibility is continuously active within the chosen replay range.',
                    'Previously inspected history: descriptive simulation, not untouched out-of-sample evidence or actual DEMO performance.'])
