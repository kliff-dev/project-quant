"""Finite, offline audit of the five executable setup definitions.

Only saved broker bars are read. No terminal, order check, order send, credentials,
authorization or execution-state modules are imported. Individual opportunities
are followed independently; their P&L is not a four-position account backtest.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from concurrent.futures import ProcessPoolExecutor, as_completed
import ast
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.account_research import protective_fill as eur_fill
from analytics.gold_strategy import protective_fill as gold_fill
from dashboard.replay import ModelSettings, model_plan
from dashboard.research_model import INTERVAL, WINDOW
from scripts.research_holding_period import rollover_units
from scripts.research_market_behaviour import archive_data

OUT = ROOT / 'reports/strategy_updates/five_setups_2026-11-23'
START = pd.Timestamp('2026-05-19T00:00Z')
SPLIT = pd.Timestamp('2026-09-21T00:00Z')
END = pd.Timestamp('2026-11-14T00:00Z')
SETUPS = ('trend_reclaim', 'trend_sweep', 'channel_breakout',
          'compression_breakout', 'range_reentry')
CODE_PATHS = ('analytics/five_setups.py', 'config/five_setup_spec.json',
              'scripts/research_five_setups.py', 'dashboard/replay.py',
              'analytics/account_research.py', 'analytics/extended_targets.py',
              'analytics/adaptive_targets.py', 'analytics/gold_strategy.py',
              'analytics/candidate_signals.py', 'analytics/setup_research.py',
              'dashboard/research_model.py', 'scripts/research_holding_period.py',
              'scripts/research_market_behaviour.py')


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def preparation_digest(source: str) -> str:
    """Cache the signal preparation, independently of outcome/report changes."""
    functions = {'necessary_gate', 'prepare', 'prepare_partition', 'prepare_parallel',
                 'fast_swings', 'equivalent_research_swings', 'session_label'}
    constants = {'START', 'SPLIT', 'END', 'SETUPS'}
    selected = []
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
            selected.append(ast.dump(node, include_attributes=False))
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets):
            selected.append(ast.dump(node, include_attributes=False))
    return hashlib.sha256('\n'.join(selected).encode()).hexdigest()


def save(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, allow_nan=False, default=str), encoding='utf-8')


def fast_swings(context: pd.DataFrame, decision, *, gold: bool) -> dict:
    """Vector equivalent of the frozen pivot loops, local to offline research.

    Gold's helper rejects missing neighbors; the existing H4 helper does not.
    Preserve that difference exactly instead of silently improving the replay.
    """
    stamp = pd.Timestamp(decision)
    if stamp.tz is None or context.index.tz is None:
        raise ValueError('Aware context and decision timestamps required.')
    interval = pd.Timedelta(minutes=15) if gold else pd.Timedelta(hours=4)
    rows = context.loc[context.index + interval <= stamp].iloc[-120:]
    prefix = 'm15' if gold else 'h4'
    result = {f'{prefix}_swing_highs': [], f'{prefix}_swing_lows': []}
    if len(rows) < 5:
        return result
    indexes = np.arange(2, len(rows)-2)
    contiguous = np.ones(len(indexes), dtype=bool)
    if gold:
        for offset in (-2, -1, 0, 1):
            contiguous &= (rows.index[indexes+offset+1] - rows.index[indexes+offset]) == interval
    for column, high in (('high', True), ('low', False)):
        values = rows[column].to_numpy(dtype=float)
        selected = contiguous.copy()
        for offset in (-2, -1, 1, 2):
            selected &= values[indexes] > values[indexes+offset] if high else values[indexes] < values[indexes+offset]
        result[f'{prefix}_swing_{"highs" if high else "lows"}'] = [
            dict(price=float(values[i]), pivot_open_utc=rows.index[i].isoformat(),
                 confirmed_at_utc=(rows.index[i+2]+interval).isoformat()) for i in indexes[selected]]
    return result


@contextmanager
def equivalent_research_swings(frames):
    """Use a checked speedup in this process, never change executable sources."""
    from analytics import five_setups as kernel
    from analytics.gold_strategy import confirmed_m15_swings
    from analytics.adaptive_targets import confirmed_h4_swings
    checks = 0
    for symbol, frame in frames.items():
        gold = symbol == 'XAUUSDm'
        for last in np.linspace(WINDOW[symbol]-1, len(frame)-1, 8, dtype=int):
            sample = frame.iloc[last-WINDOW[symbol]+2:last+1]
            decision = sample.index[-1] + INTERVAL[symbol]
            context = kernel._context(sample, decision, gold)
            original = confirmed_m15_swings if gold else confirmed_h4_swings
            for rows in (context, context.drop(context.index[-20])):
                if fast_swings(rows, decision, gold=gold) != original(rows, decision):
                    raise ValueError('Vectorized research pivots differ from frozen production helpers.')
                checks += 1
    previous = kernel.confirmed_m15_swings, kernel.confirmed_h4_swings
    kernel.confirmed_m15_swings = lambda context, decision: fast_swings(context, decision, gold=True)
    kernel.confirmed_h4_swings = lambda context, decision: fast_swings(context, decision, gold=False)
    try:
        yield checks
    finally:
        kernel.confirmed_m15_swings, kernel.confirmed_h4_swings = previous


def necessary_gate(data: pd.DataFrame) -> pd.Series:
    """Superset gate only: no approximate EMA, RSI, ATR or regime screening.

    EMA reclaim implies close crosses previous close in the body direction.
    Compression is a subset of channel breakout. Sweep and band reentry have
    exact price-only necessary conditions; RSI/regimes remain in the kernel.
    A tiny band tolerance prevents a rolling floating-point boundary exclusion.
    """
    bullish, bearish = data.close > data.open, data.close < data.open
    reclaim = ((bullish & (data.close > data.close.shift())) |
               (bearish & (data.close < data.close.shift())))
    prior_low = data.low.shift().rolling(20).min()
    prior_high = data.high.shift().rolling(20).max()
    sweep = ((bullish & (data.low < prior_low) & (data.close > prior_low)) |
             (bearish & (data.high > prior_high) & (data.close < prior_high)))
    channel = (data.close > prior_high) | (data.close < prior_low)
    mean = data.close.shift().rolling(20).mean()
    std = data.close.shift().rolling(20).std(ddof=0)
    tolerance = mean.abs() * 1e-10
    lower, upper = mean - 2 * std, mean + 2 * std
    reentry = ((bullish & (data.low <= lower + tolerance) &
                (data.close >= lower - tolerance) & (data.close <= mean + tolerance)) |
               (bearish & (data.high >= upper - tolerance) &
                (data.close <= upper + tolerance) & (data.close >= mean - tolerance)))
    return (reclaim | sweep | channel | reentry).fillna(False)


def prepare(data, symbol: str, *, start=START, end=END, evaluator=None):
    """Recompute every possible candidate from the production bounded window."""
    if evaluator is None:
        from analytics.five_setups import evaluate_setups
        evaluator = evaluate_setups
    if symbol not in WINDOW:
        raise ValueError('Only the two reviewed broker instruments are supported.')
    gate, interval, count = necessary_gate(data), INTERVAL[symbol], WINDOW[symbol]
    decisions = data.index + interval
    eligible = (decisions >= pd.Timestamp(start)) & (decisions < pd.Timestamp(end))
    denominators = Counter()
    phase_denominators = {'earlier_development': Counter(), 'later_descriptive': Counter()}
    records, screenings = [], Counter()
    for i in np.flatnonzero(eligible):
        if i < count - 1:
            screenings['insufficient_exact_window'] += 1
            continue
        stamp = decisions[i]
        if i + 1 >= len(data) or data.index[i + 1] != stamp:
            screenings['no_contiguous_next_open'] += 1
            continue
        denominators[stamp.day_name()] += 1
        phase_denominators['earlier_development' if stamp < SPLIT else 'later_descriptive'][stamp.day_name()] += 1
        if not gate.iloc[i]:
            screenings['necessary_price_condition_not_met'] += 1
            continue
        result = evaluator(data.iloc[i-count+1:i+1], symbol)
        selected = result['selected_setup']
        screenings['exact_kernel_windows'] += 1
        for setup_id in SETUPS:
            signal = result['setups'][setup_id]
            screenings[f'{setup_id}:{signal.get("reason", "unspecified")}'] += 1
            if signal.get('side'):
                if signal.get('decision_time_utc') != stamp.isoformat():
                    raise ValueError('Kernel decision does not match the completed input window.')
                records.append(dict(symbol=symbol, setup_id=setup_id, entry_index=int(i+1),
                    decision_time_utc=stamp.isoformat(), weekday=stamp.day_name(),
                    month=stamp.strftime('%Y-%m'), london_hour=stamp.tz_convert('Europe/London').hour,
                    new_york_hour=stamp.tz_convert('America/New_York').hour,
                    market_regime=signal.get('market_regime', 'unspecified'),
                    session=signal.get('session_context') or signal.get('session_label') or signal.get('session') or
                            session_label(stamp),
                    selected_by_arbitration=selected == setup_id,
                    selection_reason=result['selection_reason'], signal=signal))
        if screenings['exact_kernel_windows'] % 500 == 0:
            print(f'{symbol}: {screenings["exact_kernel_windows"]} exact windows; '
                  f'{len(records)} setup opportunities', flush=True)
    return dict(records=records, screening_counts=dict(screenings),
                eligible_completed_decisions_by_weekday=dict(denominators),
                eligible_completed_decisions_by_phase_weekday={p: dict(v) for p, v in phase_denominators.items()})


def prepare_partition(data, symbol, start, end):
    """Worker-local, checked vector pivot substitution; no shared mutable state."""
    from analytics import five_setups as kernel
    kernel.confirmed_m15_swings = lambda context, decision: fast_swings(context, decision, gold=True)
    kernel.confirmed_h4_swings = lambda context, decision: fast_swings(context, decision, gold=False)
    return prepare(data, symbol, start=start, end=end)


def prepare_parallel(frames):
    """Disjoint chronological partitions; no outcome-dependent sampling."""
    merged = {symbol: dict(records=[], screening_counts=Counter(),
        eligible_completed_decisions_by_weekday=Counter(),
        eligible_completed_decisions_by_phase_weekday={p: Counter() for p in ('earlier_development', 'later_descriptive')})
        for symbol in frames}
    boundaries = pd.date_range(START, END, periods=7)
    with ProcessPoolExecutor(max_workers=4) as pool:
        tasks = {pool.submit(prepare_partition, data, symbol, start, end): symbol
            for symbol, data in frames.items() for start, end in zip(boundaries[:-1], boundaries[1:])}
        for task in as_completed(tasks):
            symbol, result = tasks[task], task.result()
            take = merged[symbol]
            take['records'].extend(result['records'])
            for key in ('screening_counts', 'eligible_completed_decisions_by_weekday'):
                take[key].update(result[key])
            for phase, counts in result['eligible_completed_decisions_by_phase_weekday'].items():
                take['eligible_completed_decisions_by_phase_weekday'][phase].update(counts)
            print(f'Completed chronological partition for {symbol}', flush=True)
    for take in merged.values():
        take['records'].sort(key=lambda r: (r['entry_index'], SETUPS.index(r['setup_id'])))
    return merged


def session_label(stamp) -> str:
    stamp = pd.Timestamp(stamp)
    london, new_york = stamp.tz_convert('Europe/London').hour, stamp.tz_convert('America/New_York').hour
    l, n = 8 <= london < 17, 8 <= new_york < 17
    return 'London/New York overlap' if l and n else 'London only' if l else 'New York only' if n else 'Other hours'


def episode(data, symbol: str, entry_index: int, plan: dict, phase_end) -> dict:
    """Existing stop-first bid/Ask approximation; no information after phase end."""
    interval = INTERVAL[symbol]
    stop = int(data.index.searchsorted(pd.Timestamp(phase_end) - interval, side='right'))
    if type(entry_index) is not int or not 0 <= entry_index < stop:
        raise ValueError('Entry must have a complete bar inside its phase.')
    result, exit_index = None, None
    fill = gold_fill if symbol == 'XAUUSDm' else lambda p, b: eur_fill(p, b, 'stop_first')
    for j in range(entry_index, stop):
        result = fill(plan, data.iloc[j])
        if result is not None:
            exit_index = j
            break
    if result is None:
        return dict(status='open_at_phase_end', exit_time_utc=None, net_pnl=None,
                    net_r=None, ambiguous=False, holding_hours_upper_bound=None,
                    assumed_financing_stress=None, net_r_financing_stress=None)
    price, reason, ambiguous = result
    entered, exited = data.index[entry_index], data.index[exit_index] + interval
    gross = plan['direction'] * plan['contract_size'] * plan['lots'] * (price - plan['entry_price'])
    net = gross - plan['entry_fee']
    # Sensitivity only: not a claim about this account's actual historical swap.
    financing = rollover_units(entered, exited) * .10 * plan['lots'] / .01
    return dict(status='closed', exit_reason=reason, exit_price=price,
                exit_time_utc=exited.isoformat(), net_pnl=net, net_r=net/plan['planned_risk'],
                ambiguous=bool(ambiguous), holding_hours_upper_bound=(exited-entered).total_seconds()/3600,
                assumed_financing_stress=financing,
                net_r_financing_stress=(net-financing)/plan['planned_risk'])


def aligned_model_plan(symbol, signal, bid, capital, settings):
    """Mirror the new native directional cap without altering observed features."""
    aligned = bool(signal.get('trend_long' if signal['side'] == 'buy' else 'trend_short'))
    plan, reason = model_plan(symbol, signal, bid, capital, settings)
    if plan is None:
        if not aligned and reason in ('minimum_lot_requires_strong_context', 'minimum_lot_requires_2r_room', 'minimum_lot_risk'):
            reason = 'minimum_lot_requires_aligned_context'
        return None, reason
    if plan['minimum_lot_rescue'] and not aligned:
        return None, 'minimum_lot_requires_aligned_context'
    plan = dict(plan, direction_aligned_context=aligned)
    if not aligned and plan['selected_reward_r'] > 2:
        # The common model planner has already verified 2R obstacle room.
        # Reconstruct its exact rounded 2R geometry from unchanged common money
        # values; never fabricate a different indicator value to force a branch.
        d = Decimal(plan['direction'])
        tick = Decimal('.001') if symbol == 'XAUUSDm' else Decimal('.00001')
        risk, fee, size, lots, slip, reference = [Decimal(str(plan[k])) for k in
            ('planned_risk', 'cost_reserve', 'contract_size', 'lots', 'slip', 'reference_entry')]
        target = reference + d*(2*slip+(2*risk+fee)/(size*lots))
        target = (target/tick).to_integral_value(rounding=ROUND_CEILING if d == 1 else ROUND_FLOOR)*tick
        plan.update(take_profit=float(target), selected_reward_r=2,
                    branch_reason='2r_cap_without_direction_aligned_context',
                    absolute_strength_context=plan['strong_context'], strong_context=False)
    return plan, ''


def evaluate_opportunities(data, symbol: str, records, capital: float, *, slippage_multiplier=1):
    """Fixed-capital independent episodes; no compounding or account-return claim."""
    settings = ModelSettings(initial_balance=capital, slippage_multiplier=slippage_multiplier)
    rows = []
    for record in records:
        i, stamp = record['entry_index'], pd.Timestamp(record['decision_time_utc'])
        phase = 'earlier_development' if stamp < SPLIT else 'later_descriptive'
        # Use only the recorded signal-bar spread, as the current offline planner
        # does. Later Ask extremes use each later bar's spread. Neither is a tick.
        signal = record['signal']
        # The older offline USD planner uses legacy booleans only to transport
        # direction; its stop, cost, sizing and adaptive target rules are common.
        # Keep the actual family in record/setup_id rather than relabeling it.
        if symbol == 'EURUSDm' and record['setup_id'] == 'range_reentry':
            signal = dict(signal, pullback_long=signal['side'] == 'buy',
                          pullback_short=signal['side'] == 'sell')
        plan, reason = aligned_model_plan(symbol, signal, float(data.iloc[i].open), capital, settings)
        row = {k: v for k, v in record.items() if k != 'signal'}
        row.update(capital=capital, phase=phase, slippage_multiplier=slippage_multiplier,
                   admitted=bool(plan), rejection_reason=reason or None)
        if plan:
            row.update(lots=plan['lots'], planned_risk=plan['planned_risk'],
                       planned_risk_percent=plan['actual_planned_risk_percent'],
                       selected_reward_r=plan['selected_reward_r'],
                       minimum_lot_rescue=plan['minimum_lot_rescue'],
                       entry_price=plan['entry_price'], stop_loss=plan['stop_loss'],
                       take_profit=plan['take_profit'],
                       **episode(data, symbol, i, plan, SPLIT if stamp < SPLIT else END))
        rows.append(row)
    return rows


def summary(rows) -> dict:
    admitted = [r for r in rows if r['admitted']]
    closed = [r for r in admitted if r['status'] == 'closed']
    gains = sum(max(0., r['net_pnl']) for r in closed)
    losses = -sum(min(0., r['net_pnl']) for r in closed)
    return dict(technical_opportunities=len(rows), modeled_admissions=len(admitted), closed_episodes=len(closed),
        open_at_phase_end=len(admitted)-len(closed), wins=sum(r['net_pnl'] > 0 for r in closed),
        win_rate=sum(r['net_pnl'] > 0 for r in closed)/len(closed) if closed else None,
        mean_net_r=float(np.mean([r['net_r'] for r in closed])) if closed else None,
        profit_factor=gains/losses if losses else None,
        mean_net_r_financing_stress=float(np.mean([r['net_r_financing_stress'] for r in closed])) if closed else None,
        median_holding_hours_upper_bound=float(np.median([r['holding_hours_upper_bound'] for r in closed])) if closed else None,
        ambiguous_exits=sum(r['ambiguous'] for r in closed),
        arbitration_selected=sum(r['selected_by_arbitration'] for r in rows),
        qualified_minimum_lot_rescues=sum(r['minimum_lot_rescue'] for r in admitted),
        rejections=dict(Counter(r['rejection_reason'] for r in rows if not r['admitted'])))


def grouped(rows, dimension: str):
    return {str(value): summary([r for r in rows if r[dimension] == value])
            for value in sorted({r[dimension] for r in rows}, key=str)}


def eur_range_diagnostics(data):
    """Explain zero observed range triggers, without changing any rule/outcome."""
    from analytics import five_setups as kernel
    counts, common = Counter(), Counter()
    for i in range(WINDOW['EURUSDm']-1, len(data)-1):
        decision = data.index[i] + INTERVAL['EURUSDm']
        if not START <= decision < END or data.index[i+1] != decision:
            continue
        counts['eligible_completed_decisions'] += 1
        f = kernel._snapshot(data.iloc[i-WINDOW['EURUSDm']+2:i+1], 'EURUSDm')
        if f.get('reason'):
            common[f['reason']] += 1
            continue
        counts['common_market_checks_passed'] += 1
        ratio = abs(f['h4_ema50']-f['h4_ema200'])/f['h4_atr14']
        slope = abs(f['h4_ema50_slope5'])/f['h4_atr14']
        registered_range = ratio <= 2 and slope <= .5
        initial_strict_range = ratio <= .75 and slope <= .2
        price_long = (f['pattern_window_contiguous'] and f['last_close'] > f['last_open'] and
            f['last_low'] < f['prior20_lower_band'] <= f['last_close'] < f['prior20_close_mean'])
        price_short = (f['pattern_window_contiguous'] and f['last_close'] < f['last_open'] and
            f['last_high'] > f['prior20_upper_band'] >= f['last_close'] > f['prior20_close_mean'])
        pattern = price_long or price_short
        with_rsi = price_long and f['h1_rsi14'] <= 45 or price_short and f['h1_rsi14'] >= 55
        for name, value in (
            ('current_registered_flat_h4_context', f['range_regime']),
            ('previous_registered_research_h4_context', registered_range),
            ('initial_strict_h4_context', initial_strict_range),
            ('price_band_reentry_ignoring_context_and_rsi', pattern),
            ('price_band_reentry_with_rsi_ignoring_context', with_rsi),
            ('current_flat_context_and_price_pattern_before_rsi', f['range_regime'] and pattern),
            ('current_flat_context_price_pattern_and_rsi', f['range_regime'] and with_rsi),
            ('previous_registered_context_price_pattern_and_rsi', registered_range and with_rsi),
            ('initial_strict_context_price_pattern_and_rsi', initial_strict_range and with_rsi)):
            counts[name] += int(value)
    return dict(counts=dict(counts), common_rejections=dict(common),
        current_rule='H4 abs(EMA50-EMA200)<=2ATR and abs(EMA50 five-bar slope)<=.5ATR',
        previous_registered_research_rule='H4 abs(EMA50-EMA200)<=2ATR and abs(five-bar slope)<=.5ATR',
        initial_strict_rule='H4 abs(EMA50-EMA200)<=.75ATR and abs(five-bar slope)<=.2ATR',
        interpretation='Fixed-rule count decomposition only. No weekday/threshold/exit tuning and no outcome-based selection.')


def render(payload):
    lines = ['# Five setups per market: reproducible historical audit', '',
        '## Technical Summary', '',
        'The production five-setup kernel was recomputed on saved broker bars. Every valid setup is audited independently, including simultaneous signals; the executable engine still arbitrates to one new order per symbol/candle and permits only two open positions per symbol. **These episodes are not an account equity curve or proof of profitability.**', '',
        'The fixed split is earlier development (19 May–20 September 2026 UTC) and a later descriptive interval (21 September–13 November 2026 UTC). Prior research already inspected this history, so the latter is not an untouched holdout. No thresholds, weekdays or sessions were selected using these results.', '',
        '[Machine-readable methods and results](research_summary.json) · [Individual opportunities and outcomes](research_episodes.csv) · [Frozen kernel candidates](research_signals.json)', '',
        '## Root Cause', '',
        'Previously, seven research families were visible but only reclaim/sweep could submit protected DEMO orders. More raw signals also encounter the same spread, target-room, broker-minimum and risk barriers. The [earlier A/B shared-$500 comparison](../../market_analysis/two_setups_2026-11-18/report.md) closed 63 modeled trades with -$74.38 net P&L and a 0.687 profit factor; it did not establish a profitable combined strategy. Its results remain unchanged. The new definitions are DEMO hypotheses, not five proven edges.', '',
        '## How It Was Diagnosed', '',
        'A vectorized necessary-condition gate excludes only candles that cannot contain any of the five patterns. Every remaining candidate is recomputed from the exact causal 3,000-H1 EUR/USD or 1,200-M5 Gold production window, using completed H4/M15 context. Saved source hashes identify the calculations. A missing consecutive next open prevents a modeled entry.', '',
        'The existing offline adaptive planner checks recorded spread, round-trip adverse slippage, a 5% risk-budget cost allowance, minimum .01 lot/step, .1 lot cap, the structural ATR stop, and confirmed target room. Baseline risk is 1%; the 2% exception permits only the minimum lot under strong direction-aligned context and at-least-2R room. Targets remain 1–4R, with a 2R cap when the setup direction lacks aligned context. A second scenario doubles slippage.', '',
        '### $500 independent episodes, normal modeled slippage', '',
        '| Market / setup / interval | Technical | Model admits | Closed | Net wins | Mean net R | Financing stress R |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    def num(value, places=3):
        return 'n/a' if value is None else f'{value:.{places}f}'
    for symbol, result in payload['markets'].items():
        for setup in SETUPS:
            for phase in ('earlier_development', 'later_descriptive'):
                row = result['scenarios']['500_slip1']['by_setup_phase'][setup][phase]
                win = 'n/a' if row['win_rate'] is None else f'{100*row["win_rate"]:.1f}%'
                lines.append(f'| {symbol} / {setup} / {phase} | {row["technical_opportunities"]} | {row["modeled_admissions"]} | {row["closed_episodes"]} | {win} | {num(row["mean_net_r"])} | {num(row["mean_net_r_financing_stress"])} |')
    lines += ['', 'A net win includes modeled spread/slippage and the allowance debit. Small counts and overlapping entries make these rates unstable; no future probability is estimated. Dollar P&L is deliberately not summed into a hypothetical account return because episodes can overlap and share price moves.', '',
        '### Coverage and provenance checks', '',
        'EUR/USD uses the earlier registered range definition: H4 EMA50/200 separation <=2 H4 ATR and absolute five-bar EMA50 slope <=0.5 H4 ATR. Gold keeps its separate <=0.75 M15 ATR / <=0.2 M15 ATR definition. The initial implementation applied the stricter Gold-like thresholds to EUR/USD; they produced no EUR flat-regime observations in this archive. This was corrected to the existing EUR research specification before activation. The [initial strict definition and results are preserved](research_initial_strict.zip); thresholds were not selected by profit or weekday performance.', '',
        '### Market conditions and different days', '',
        'Trend reclaim, sweep and channel breakout require directional context; compression breakout requires prior compression and rejects an opposing context trend; range reentry requires flat, tightly separated context averages. Volatility and shock-candle vetoes remain. The app displays which condition was present and why each setup waited.', '',
        'The JSON contains per-setup weekday, month, session and regime counts/outcomes, plus weekday denominators. Weekdays refer to UTC candle-decision dates; London/New York sessions use their local DST-aware times. These are descriptive monitoring dimensions, not claims that Tuesday or a particular session has a better future win rate. Session labels do not impose a new clock schedule. No calendar feed means policy news, holidays, surprise releases and changing macro conditions are not explicitly modeled.', '',
        '## Solution', '',
        'This audit separates technical triggers, modeled admissions and modeled outcomes. The executable kernel and native broker planner are tested separately by the implementation checks. Historical model admission does not verify historical permissions, native P&L, actual fills, margin availability or an 8%-risk four-position account path.', '',
        'Reproduce without internet:', '', '```powershell',
        r'.\.venv\Scripts\python.exe -B scripts/research_five_setups.py', '```', '',
        '### Data and costs limitations', '',
        '- EURUSDm is broker OTC EUR/USD; XAUUSDm is a broker Gold CFD, not an exchange futures contract. Archives end 13 November, so this study does not describe today’s intraday market.',
        '- The MT5 archives are bid OHLC bars in UTC with bar spread. Tick volume is unused. Ask extremes, intrabar spread variation, feed latency and historical fill paths are unavailable.',
        '- Baseline fees debit a hypothetical 5% of the applicable risk budget; this is a conservative research assumption, not a verified historical commission schedule. Actual broker swaps are not reconstructed.',
        '- The separate financing sensitivity assumes $0.10 per .01 lot per ordinary New York 17:00 rollover, with Wednesday triple units. It is illustrative and can understate Gold financing.',
        '- When one bar touches both SL and TP, stop-first is used. Gaps can exceed reserved risk. Positions still open at the phase boundary are excluded from closed-outcome statistics.',
        '- Fixed $500/$1,000 basis is reset for every episode. No compounding, portfolio drawdown, correlation diversification, broker stop-out, outages or native order permission is reproduced.',
        '- Previously inspected data, five hypotheses per market and subgroup comparisons create selection bias. No losing result is hidden or optimized away. Fresh forward DEMO evidence is still needed.', '',
        '## What I Learned', '',
        '- Five setup definitions do not imply five executable trades: conditions and execution affordability remain separate gates.',
        '- Different weekdays and sessions need enough observations and future confirmation before becoming filters.',
        '- Planned 1–4R targets are geometry rules, not evidence that targets will be reached.',
        '- Higher aggregate risk changes exposure, not the average quality of a setup.', '',
        '## If This Happens Again', '',
        'Start with the per-setup reason in the app. Compare raw trigger, selected setup, native planning rejection and actual deal record. Use research_episodes.csv to inspect costs and sizing; do not interpret research signals as missed executable orders.', '',
        '### Primary-source context', '',
        '- [MetaQuotes: bar fields, UTC and terminal-history limits](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesrange_py) supports the data handling; a bar archive is not a tick-by-tick execution record.',
        '- [World Gold Council: gold return drivers](https://www.gold.org/goldhub/research/gold-outlook-2026) discusses economic growth, risk, opportunity cost and momentum. Our price-based regimes are a limited proxy, not that macro attribution model.',
        '- [IMF: FX depth and liquidity vary across market time zones](https://www.elibrary.imf.org/view/journals/007/2015/041/article-A001-en.xml) supports recording sessions; it does not validate a particular weekday/setup rule.',
        '- [Bailey et al.: the probability of backtest overfitting](https://escholarship.org/uc/item/4w1110bb) explains why selecting attractive historical variants needs independent validation.', '']
    (OUT / 'research_findings.md').write_text('\n'.join(lines), encoding='utf-8')


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    fingerprints = {p: digest(ROOT / p) for p in CODE_PATHS}
    frames, archives = archive_data()
    cache_path = OUT / 'research_signals.json'
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding='utf-8'))
    else:
        cache = {}
    kernel_identity = {k: v for k, v in fingerprints.items()
                       if k not in ('dashboard/replay.py', 'scripts/research_five_setups.py')}
    prepared_sha = preparation_digest(Path(__file__).read_text(encoding='utf-8'))
    cached_identity = {k: v for k, v in cache.get('kernel_identity', {}).items()
                       if k not in ('dashboard/replay.py', 'scripts/research_five_setups.py')}
    cached_prepared_sha = cache.get('preparation_functions_sha256')
    original_script_sha = cache.get('scan_script_sha256') or cache.get('kernel_identity', {}).get('scripts/research_five_setups.py')
    # One initial scan preceded the direction-aligned native policy clarification.
    # Reuse it only after verifying its preserved source and identical signal
    # preparation AST. Admissions and all outcomes are recalculated below.
    preserved = OUT / 'research_scan_source.py'
    if not cached_prepared_sha and original_script_sha and preserved.is_file() and digest(preserved) == original_script_sha:
        cached_prepared_sha = preparation_digest(preserved.read_text(encoding='utf-8'))
    if cached_identity != kernel_identity or cached_prepared_sha != prepared_sha or cache.get('archives') != archives:
        cache = dict(archives=archives, markets={})
        original_script_sha = fingerprints['scripts/research_five_setups.py']
    cache.update(kernel_identity=kernel_identity, preparation_functions_sha256=prepared_sha,
                 scan_script_sha256=original_script_sha, archives=archives)
    with equivalent_research_swings(frames) as checks:
        missing = {s: f for s, f in frames.items() if s not in cache['markets']}
        if missing:
            cache['markets'].update(prepare_parallel(missing))
        save(cache_path, cache)
    payload = dict(generated_at_utc=datetime.now(timezone.utc).isoformat(), profile='multi_setup_v1',
        kind='independent_cost_aware_opportunities_not_portfolio_backtest',
        period=dict(start=START.isoformat(), split=SPLIT.isoformat(), end=END.isoformat()),
        sources_sha256=fingerprints, archives=archives,
        scan_script_sha256=cache['scan_script_sha256'], preparation_functions_sha256=prepared_sha,
        native_directional_constraints='Minimum-lot rescue requires direction-aligned trend; targets above2R require aligned context.',
        vectorized_research_pivot_equivalence_checks=checks, markets={})
    prior_path = ROOT / 'reports/market_analysis/two_setups_2026-11-18/comparison.json'
    prior = json.loads(prior_path.read_text(encoding='utf-8'))['scenarios']['combined_500']
    payload['prior_two_setup_evidence'] = dict(path=str(prior_path.relative_to(ROOT)), sha256=digest(prior_path),
        **{key: prior[key] for key in ('closed_trades', 'net_closed_pnl', 'profit_factor', 'net_expectancy_r')})
    all_rows = []
    for symbol, data in frames.items():
        recorded = cache['markets'][symbol]
        market = dict(technical_counts=dict(Counter(r['setup_id'] for r in recorded['records'])),
            screening_counts=recorded['screening_counts'],
            eligible_completed_decisions_by_weekday=recorded['eligible_completed_decisions_by_weekday'],
            eligible_completed_decisions_by_phase_weekday=recorded['eligible_completed_decisions_by_phase_weekday'],
            scenarios={})
        if symbol == 'EURUSDm':
            print('Diagnosing EUR range context/pattern overlap without tuning', flush=True)
            market['range_condition_diagnostics'] = eur_range_diagnostics(data)
        for capital in (500., 1000.):
            for slip in (1, 2):
                name = f'{int(capital)}_slip{slip}'
                rows = evaluate_opportunities(data, symbol, recorded['records'], capital, slippage_multiplier=slip)
                all_rows.extend(rows)
                by_setup_phase = {setup: {phase: summary([r for r in rows if r['setup_id'] == setup and r['phase'] == phase])
                    for phase in ('earlier_development', 'later_descriptive')} for setup in SETUPS}
                market['scenarios'][name] = dict(overall=summary(rows), by_setup_phase=by_setup_phase,
                    by_setup={setup: dict(overall=summary([r for r in rows if r['setup_id'] == setup]),
                        **{f'by_{dimension}': grouped([r for r in rows if r['setup_id'] == setup], dimension)
                           for dimension in ('weekday', 'month', 'session', 'market_regime')}) for setup in SETUPS})
                for setup in SETUPS:
                    rates = {}
                    for weekday, denominator in recorded['eligible_completed_decisions_by_weekday'].items():
                        take = [r for r in rows if r['setup_id'] == setup and r['weekday'] == weekday]
                        rates[weekday] = dict(eligible_completed_decisions=denominator,
                            technical_opportunities=len(take), modeled_admissions=sum(r['admitted'] for r in take),
                            technical_trigger_percent=100*len(take)/denominator,
                            modeled_admission_percent=100*sum(r['admitted'] for r in take)/denominator)
                    market['scenarios'][name]['by_setup'][setup]['weekday_frequency'] = rates
                print(f'{symbol} {name}: {market["scenarios"][name]["overall"]}', flush=True)
        payload['markets'][symbol] = market
    if fingerprints != {p: digest(ROOT / p) for p in CODE_PATHS}:
        raise ValueError('Research source changed during computation; rerun before publishing results.')
    pd.DataFrame(all_rows).to_csv(OUT / 'research_episodes.csv', index=False)
    save(OUT / 'research_summary.json', payload)
    render(payload)


if __name__ == '__main__':
    main()
