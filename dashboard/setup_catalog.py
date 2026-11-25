"""Readable, read-only guides and saved evidence for the executable setup profile.

No function here connects to MT5, creates authority or sends an order. Exact rule
text comes from the same versioned specification as the technical kernel.
"""
from __future__ import annotations

import json
import math
import hashlib
from pathlib import Path

import pandas as pd


PROFILE = 'multi_setup_v1'
SETUP_IDS = ('trend_reclaim', 'trend_sweep', 'channel_breakout',
             'compression_breakout', 'range_reentry')
SETUP_NAMES = {
    'trend_reclaim': 'Trend EMA reclaim',
    'trend_sweep': 'Trend sweep and recovery',
    'channel_breakout': 'Trend channel breakout',
    'compression_breakout': 'Compression breakout',
    'range_reentry': 'Range band re-entry',
}
PLAIN_GUIDES = {
    'trend_reclaim': ('Join an established trend after a pullback finishes.',
        'A moving average smooths recent prices. This setup waits for a completed candle to reclaim the short average in the slower trend direction.',
        'It can fail when a trend is ending or price keeps crossing the average in a choppy market.'),
    'trend_sweep': ('Trade a failed break against an established trend.',
        'Price briefly trades beyond a previous channel edge, then closes back inside it. The recovery must agree with the slower trend.',
        'A wick alone is insufficient. A genuine reversal can keep moving against the original trend.'),
    'channel_breakout': ('Join a trend when price closes beyond its recent range.',
        'The channel uses earlier candles only. A matching-direction close must break its edge without already being too far beyond it.',
        'Breakouts can fail and reverse. The chase limit avoids some late entries but does not identify every false breakout.'),
    'compression_breakout': ('Look for expansion after an unusually narrow range.',
        'Bands around recent closing prices describe how compressed trading has become. A completed candle must then break the prior price channel.',
        'Compression does not predict the direction or strength of the next move. The slower market context must not strongly oppose the break.'),
    'range_reentry': ('Look for a return toward the middle when the instrument’s range rule permits it.',
        'EUR/USD uses its registered low-trend/range definition; Gold uses a stricter flat-context definition. Price must pierce an earlier statistical band and close back inside, with momentum supporting a return toward the mean.',
        'A range can become a trend. This setup is excluded when the slower context no longer meets the range definition.'),
}


def setup_guides(symbol: str, *, specification: dict | None = None) -> list[dict]:
    """Return five guides in the reviewed selection order, using exact spec text."""
    if specification is None:
        from analytics.five_setups import load_spec
        specification = load_spec()
    if specification.get('profile') != PROFILE:
        raise ValueError('The five-setup specification has an unexpected profile.')
    instrument = specification['instruments'][symbol]
    rules = instrument['setups']
    if set(rules) != set(SETUP_IDS):
        raise ValueError('The specification must define exactly five executable setups.')
    result = []
    for setup_id in SETUP_IDS:
        purpose, explanation, failure = PLAIN_GUIDES[setup_id]
        rule = rules[setup_id]
        result.append(dict(rule, setup_id=setup_id, name=rule.get('name', SETUP_NAMES[setup_id]),
            purpose=purpose, explanation=explanation, failure=failure,
            symbol=symbol, entry_timeframe=instrument['entry_timeframe'],
            context_timeframe=instrument['context_timeframe']))
    return result


HISTORY_COLUMNS = ['decision_time_utc', 'instrument', 'setup_id', 'setup', 'side', 'raw_side',
    'technical_signal', 'fresh', 'status', 'reason', 'setup_reason', 'selected_setup',
    'executed_setup', 'outcome', 'weekday_manila', 'month_manila', 'session',
    'regime', 'volatility_ratio', 'source_file']


def _context_value(context: dict, *keys, default=None):
    return next((context[key] for key in keys if context.get(key) is not None), default)


def history_rows(item: dict, *, source_file: str = '') -> list[dict]:
    """One row per evaluated setup, distinguishing signals from broker execution.

    ENTERED/position_open applies only to the selected execution setup. Other
    overlapping signals are not labeled as additional fills.
    """
    if item.get('profile', item.get('entry_setups', PROFILE)) != PROFILE:
        return []
    signals = item.get('setup_signals') or {}
    if not isinstance(signals, dict):
        return []
    # This is one scalar timestamp, not a column requiring pandas' generic
    # format inference. Preserve the old UTC semantics without repeating that
    # expensive inference for every observation on each dashboard refresh.
    try:
        stamp = pd.Timestamp(item.get('decision_time_utc'))
    except (ValueError, TypeError, OverflowError):
        return []
    if pd.isna(stamp) or item.get('symbol') not in ('EURUSDm', 'XAUUSDm'):
        return []
    stamp = stamp.tz_localize('UTC') if stamp.tz is None else stamp.tz_convert('UTC')
    local = stamp.tz_convert('Asia/Manila')
    context = item.get('setup_market_context') or item.get('market_context') or {}
    if not isinstance(context, dict):
        context = {}
    status = str(item.get('execution_status') or 'not_evaluated')
    executed_setup = item.get('execution_setup_id')
    selected = item.get('selected_setup')
    rows = []
    for setup_id in SETUP_IDS:
        signal = signals.get(setup_id)
        if not isinstance(signal, dict):
            continue
        side = signal.get('side') if signal.get('side') in ('buy', 'sell') else None
        raw_side = signal.get('raw_side') if signal.get('raw_side') in ('buy', 'sell') else None
        if not side and raw_side:
            outcome = 'Condition persists — no new trigger'
        elif not side:
            outcome = 'No technical trigger'
        elif item.get('signal_fresh_at_observation') is not True:
            outcome = 'Historical / not fresh'
        elif executed_setup == setup_id and status.lower() in ('entered', 'position_open', 'executed', 'entry_verified', 'executed_verified'):
            outcome = 'Entry recorded — check trade journal'
        elif selected and selected != setup_id:
            outcome = 'Overlapping signal — not selected'
        elif status.lower() in ('wait', 'blocked', 'rejected', 'error'):
            outcome = 'Technical trigger — execution blocked'
        else:
            outcome = 'Technical trigger — ' + status.replace('_', ' ')
        rows.append(dict(decision_time_utc=stamp.isoformat(), instrument=item['symbol'],
            setup_id=setup_id, setup=SETUP_NAMES[setup_id], side=side, raw_side=raw_side,
            technical_signal=bool(side), fresh=item.get('signal_fresh_at_observation'),
            status=status, reason=item.get('execution_reason') or item.get('selection_reason'),
            setup_reason=signal.get('reason'), selected_setup=selected,
            executed_setup=executed_setup, outcome=outcome,
            weekday_manila=local.day_name(), month_manila=local.strftime('%Y-%m'),
            session=_context_value(context, 'session', 'session_label', 'session_context', default='Not recorded'),
            regime=_context_value(context, 'regime', 'market_regime', default='Not recorded'),
            volatility_ratio=_context_value(context, 'volatility_ratio', 'atr_ratio'),
            source_file=source_file))
    return rows


def saved_history(root) -> pd.DataFrame:
    """Read immutable observation files; malformed/partial JSON never grants a signal."""
    rows = []
    folder = Path(root) / 'logs/observations' / PROFILE / 'events'
    for path in sorted(folder.glob('*.json')):
        try:
            item = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(item, dict):
                rows.extend(history_rows(item, source_file=path.name))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return pd.DataFrame(rows, columns=HISTORY_COLUMNS).drop_duplicates(
        subset=['instrument', 'decision_time_utc', 'setup_id'], keep='last').reset_index(drop=True)


def condition_counts(history: pd.DataFrame, group: str = 'weekday_manila') -> pd.DataFrame:
    """Exposure-aware counts, never a win rate or a ranking of future profitability."""
    if group not in ('weekday_manila', 'month_manila', 'session', 'regime'):
        raise ValueError('Unsupported condition grouping.')
    columns = [group, 'setup_id', 'evaluated_candles', 'technical_triggers', 'trigger_rate_percent']
    if history.empty:
        return pd.DataFrame(columns=columns)
    fresh = history.loc[history.fresh.eq(True)]
    if fresh.empty:
        return pd.DataFrame(columns=columns)
    grouped = fresh.groupby([group, 'setup_id'], dropna=False).agg(
        evaluated_candles=('decision_time_utc', 'nunique'),
        technical_triggers=('technical_signal', 'sum')).reset_index()
    grouped['trigger_rate_percent'] = 100 * grouped.technical_triggers / grouped.evaluated_candles
    return grouped[columns]


STUDY_PATH = Path('reports/strategy_updates/five_setups_2026-11-23/research_summary.json')
STUDY_KIND = 'independent_cost_aware_opportunities_not_portfolio_backtest'
PHASES = {'earlier_development': 'Earlier development', 'later_descriptive': 'Later descriptive'}


def load_research_study(root) -> dict | None:
    """Read local aggregate research only; it is neither authority nor trade history."""
    path = Path(root) / STUDY_PATH
    if not path.is_file():
        return None
    item = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(item, dict) or item.get('profile') != PROFILE or
            item.get('kind') != STUDY_KIND or not isinstance(item.get('markets'), dict)):
        raise ValueError('Saved research does not identify the five-setup independent-episode model.')
    return item


def _research_number(value, *, count=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if count and (value < 0 or int(value) != value):
        return None
    return int(value) if count else float(value)


def research_rows(study: dict, symbol: str, scenario='500_slip1', *, phases=False) -> pd.DataFrame:
    """Preserve absent/zero-completion results as unknown, rather than 0% wins."""
    modeled = study.get('markets', {}).get(symbol, {}).get('scenarios', {}).get(scenario, {})
    rows = []
    for setup_id in SETUP_IDS:
        choices = PHASES.items() if phases else [(None, None)]
        for phase, label in choices:
            values = (modeled.get('by_setup_phase', {}).get(setup_id, {}).get(phase, {}) if phase else
                      modeled.get('by_setup', {}).get(setup_id, {}).get('overall', {}))
            closed = _research_number(values.get('closed_episodes'), count=True)
            win_rate = _research_number(values.get('win_rate')) if closed else None
            mean = _research_number(values.get('mean_net_r')) if closed else None
            rows.append(dict(setup_id=setup_id, setup=SETUP_NAMES[setup_id], phase=label,
                technical_opportunities=_research_number(values.get('technical_opportunities'), count=True),
                modeled_admissions=_research_number(values.get('modeled_admissions'), count=True),
                closed_episodes=closed, win_rate_percent=100*win_rate if win_rate is not None and 0 <= win_rate <= 1 else None,
                mean_net_r=mean))
    return pd.DataFrame(rows)


def pooled_research_mean(study: dict, scenario='500_slip1') -> tuple[float | None, int]:
    """Episode-count-weighted descriptive R; deliberately not account return."""
    total, count = 0., 0
    for symbol in ('EURUSDm', 'XAUUSDm'):
        overall = study.get('markets', {}).get(symbol, {}).get('scenarios', {}).get(scenario, {}).get('overall', {})
        closed = _research_number(overall.get('closed_episodes'), count=True)
        mean = _research_number(overall.get('mean_net_r'))
        if closed and mean is not None:
            total += closed * mean
            count += closed
    return (total/count if count else None), count


def research_source_status(study: dict) -> tuple[str, list[str]]:
    """Compare displayed kernel/spec revision with the study's recorded sources."""
    from analytics.five_setups import SPEC_PATH
    source_root = Path(SPEC_PATH).parent.parent
    recorded = study.get('sources_sha256') or {}
    mismatched, unknown = [], []
    for name in ('analytics/five_setups.py', 'config/five_setup_spec.json'):
        path = source_root / name
        expected = recorded.get(name) if isinstance(recorded, dict) else None
        if not isinstance(expected, str) or not path.is_file():
            unknown.append(name)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            mismatched.append(name)
    return ('mismatch', mismatched) if mismatched else ('unverifiable', unknown) if unknown else ('matches', [])
