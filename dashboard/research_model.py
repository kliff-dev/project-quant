"""Read-only view of saved monitor evidence and causal strategy calculations.

No broker, authorization, execution, network or terminal imports. Loading a
dashboard must never start a second MT5 connection or modify monitor evidence.
"""
from __future__ import annotations

import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from analytics.activation_signals import gold_features, eur_features
from analytics.candidate_signals import wilder_atr, wilder_rsi
from analytics.gold_strategy import complete_m15, validate_m5
from analytics.setup_research import validate_bars

SYMBOLS = ('XAUUSDm', 'EURUSDm')
WINDOW = {'XAUUSDm': 1200, 'EURUSDm': 3000}
INTERVAL = {'XAUUSDm': pd.Timedelta(minutes=5), 'EURUSDm': pd.Timedelta(hours=1)}
ARCHIVES = {
    'XAUUSDm': 'data/raw/gold_intraday_2026-11-15/XAUUSDm_M5.csv',
    'EURUSDm': 'data/raw/mt5_six_month_2026-11-14_025036/EURUSDm_H1.csv',
}


def read_json(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Expected a JSON object.')
    return value


def local_path(root: Path, value: str, *, folder: str = 'logs') -> Path:
    root = Path(root).resolve()
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to((root / folder).resolve()):
        raise ValueError(f'Evidence path must remain inside {folder}/.')
    return path


def process_alive(pid: int) -> bool | None:
    """Read liveness without Windows os.kill(pid, 0), which can terminate a PID."""
    if type(pid) is not int or pid <= 0:
        return False
    if os.name == 'nt':
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.GetExitCodeProcess.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            return code.value == 259 if api.GetExitCodeProcess(handle, ctypes.byref(code)) else None
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


def monitor_health(status: dict, *, now=None, alive=None) -> dict:
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    if now.tz is None:
        raise ValueError('Aware current time required.')
    try:
        stamp = pd.Timestamp(status['heartbeat_utc'])
        if stamp.tz is None:
            raise ValueError('Unzoned heartbeat')
        age = (now - stamp).total_seconds()
    except (KeyError, TypeError, ValueError):
        return dict(state='UNKNOWN', age=None, explanation='Heartbeat missing or invalid.')
    alive = process_alive(status.get('pid')) if alive is None else alive
    if age < -5 or age > 45:
        return dict(state='STALE', age=age, explanation='Saved heartbeat is stale or ahead of the PC clock.')
    if alive is not True:
        return dict(state='UNKNOWN' if alive is None else 'STOPPED', age=age,
                    explanation='Monitor process could not be verified.' if alive is None else 'Monitor process is absent.')
    if status.get('status') != 'running':
        return dict(state='STOPPED', age=age, explanation=str(status.get('stop_reason') or status.get('status')))
    try:
        limit = pd.Timestamp(status.get('deadline_utc'))
        if pd.isna(limit) or limit.tz is None:
            raise ValueError('Missing or unzoned deadline')
    except (ValueError, TypeError, OverflowError):
        return dict(state='UNKNOWN', age=age, explanation='Missing or invalid session deadline.')
    if now >= limit:
        return dict(state='EXPIRED', age=age, explanation='Entry authority has expired; monitoring may continue for an open position.')
    return dict(state='RUNNING', age=age, explanation='Fresh heartbeat and a live monitor process.')


def load_bars(path: Path, symbol: str, *, expected_sha=None, decision=None) -> pd.DataFrame:
    if symbol not in SYMBOLS:
        raise ValueError('Unsupported instrument.')
    raw = Path(path).read_bytes()
    if expected_sha and hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError('Saved input checksum mismatch; cannot explain this decision reliably.')
    from io import BytesIO
    frame = pd.read_csv(BytesIO(raw), index_col=0, float_precision='round_trip')
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True))
    frame = validate_m5(frame) if symbol == 'XAUUSDm' else validate_bars(frame)
    if decision is not None:
        stamp = pd.Timestamp(decision)
        if stamp.tz is None or frame.index[-1] + INTERVAL[symbol] != stamp:
            raise ValueError('Saved input does not end at the reported completed-candle decision.')
    return frame


def diagnostics(data: pd.DataFrame, symbol: str, *, profile=None) -> dict:
    """Evaluate the same bounded technical kernel; break its gates into plain rows."""
    gold = symbol == 'XAUUSDm'
    if symbol not in SYMBOLS:
        raise ValueError('Unsupported instrument.')
    frame = (validate_m5(data) if gold else validate_bars(data)).iloc[-WINDOW[symbol]:]
    if profile=='multi_setup_v1':
        from analytics.five_setups import evaluate_setups
        comparison=evaluate_setups(frame,symbol)
        features=comparison['selected'] or next(iter(comparison['setups'].values()))
        rows=[]
        for setup,item in comparison['setups'].items():
            rows.append(dict(Check=setup,BUY='Passed' if item['side']=='buy' else 'Failed',
                SELL='Passed' if item['side']=='sell' else 'Failed',Evidence=item['reason'],Category='Technical setup'))
        rows.append(dict(Check='Portfolio candidate selection',BUY='Passed' if comparison['selected'].get('side')=='buy' else 'Failed',
            SELL='Passed' if comparison['selected'].get('side')=='sell' else 'Failed',Evidence=comparison['selection_reason'],Category='Technical setup'))
        rows.append(dict(Check='Target room, lots, margin and combined risk',BUY='Not evaluated',SELL='Not evaluated',
            Evidence='Native admission is separate; a technical pattern alone cannot authorize an order.',Category='Execution'))
        return dict(symbol=symbol,decision_time_utc=(frame.index[-1]+INTERVAL[symbol]).isoformat(),
            side=comparison['selected'].get('side'),features=features,gates=rows,reason=comparison['selection_reason'],
            frame=frame.iloc[1:],entry_setups=profile,setup_signals=comparison['setup_signals'],
            selected_setup=comparison['selected_setup'],setup_market_context=comparison.get('market_context',{}))
    features = gold_features(frame) if gold else eur_features(frame)
    features = features or {}
    rows = []

    def gate(name, buy, sell, detail, category='Technical'):
        label = lambda value: 'Not evaluated' if value is None else 'Passed' if bool(value) else 'Failed'
        rows.append(dict(Check=name, BUY=label(buy), SELL=label(sell), Evidence=detail, Category=category))

    warm = len(frame) >= (1200 if gold else 2400)
    recent_gap = not frame.index[-6:].to_series().diff().dropna().eq(INTERVAL[symbol]).all()
    gate('Completed-bar warmup and recent continuity', warm and not recent_gap, warm and not recent_gap,
         f'{len(frame)} rows; recent gap: {recent_gap}')
    prefix, context = ('m5', 'm15') if gold else ('h1', 'h4')
    if f'{prefix}_ema20' in features:
        ema = frame.close.ewm(span=20, adjust=False, min_periods=20).mean()
        last, previous = frame.iloc[-1], frame.iloc[-2]
        close, fast, slow, rsi = [features[f'{prefix}_{key}'] for key in ('close', 'ema20', 'ema50', 'rsi14')]
        c50, c200, slope = [features[f'{context}_{key}'] for key in ('ema50', 'ema200', 'ema50_slope5')]
        gate(f'{context.upper()} trend', features['trend_long'], features['trend_short'],
             f'EMA50 {c50:.6g}; EMA200 {c200:.6g}; five-bar EMA50 change {slope:.6g}')
        gate('Previous close starts on pullback side of EMA20', previous.close <= ema.iloc[-2], previous.close >= ema.iloc[-2],
             f'Previous close {previous.close:.6g}; previous EMA20 {ema.iloc[-2]:.6g}')
        gate('Completed candle direction', last.close > last.open, last.close < last.open,
             f'Open {last.open:.6g}; close {last.close:.6g}')
        gate('Close reclaims EMA20 and clears EMA50', close > fast and close > slow, close < fast and close < slow,
             f'Close {close:.6g}; EMA20 {fast:.6g}; EMA50 {slow:.6g}')
        floor, ceiling = (32, 68) if gold else (35, 65)
        gate('RSI14 confirmation', 50 <= rsi <= ceiling, floor <= rsi <= 50,
             f'RSI {rsi:.2f}; BUY 50–{ceiling}; SELL {floor}–50')
        if gold:
            normal = .75 <= features['volatility_ratio'] <= 1.75
            shock = features['latest_bar_range'] <= 2.5 * features['atr14']
            gate('Volatility range', normal, normal, f"ATR / prior 100-bar ATR median: {features['volatility_ratio']:.3f}; allowed 0.75–1.75")
            gate('Shock-candle limit', shock, shock, 'Completed range must be at most 2.5 × ATR14.')
    else:
        gate('Indicator/context availability', None, None, features.get('reason', 'Insufficient H4 context or recent gap.'))
    if gold:
        side = features.get('side')
    else:
        long, short = bool(features.get('trend_reclaim_long')), bool(features.get('trend_reclaim_short'))
        side = ('buy' if long else 'sell') if long != short else None
    gate('Complete technical entry', side == 'buy', side == 'sell',
         'Technical setup only; target room, sizing and execution admission are separate.')
    gate('Target room, lots, margin and combined risk', None, None,
         'Requires an eligible signal and a recorded execution plan; candle checks alone do not verify admission.', 'Execution')
    return dict(symbol=symbol, decision_time_utc=(frame.index[-1]+INTERVAL[symbol]).isoformat(),
                side=side, features=features, gates=rows,
                reason=features.get('reason') or ('valid_entry_setup' if side else 'no_unique_trend_reclaim'),
                frame=frame)


def price_charts(diagnostic: dict) -> tuple[go.Figure, go.Figure]:
    frame, symbol = diagnostic['frame'], diagnostic['symbol']
    gold = symbol == 'XAUUSDm'
    indicators = pd.DataFrame(index=frame.index)
    for period in (20, 50):
        indicators[f'EMA{period}'] = frame.close.ewm(span=period, adjust=False, min_periods=period).mean()
    indicators['RSI14'], indicators['ATR14'] = wilder_rsi(frame.close), wilder_atr(frame)
    tail = frame.iloc[-160:]
    entry = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[.6, .22, .18], vertical_spacing=.04)
    entry.add_trace(go.Candlestick(x=tail.index, open=tail.open, high=tail.high, low=tail.low, close=tail.close, name='Bid candles'), row=1, col=1)
    for period, color in ((20, '#00bfff'), (50, '#ffa500')):
        entry.add_trace(go.Scatter(x=tail.index, y=indicators.loc[tail.index, f'EMA{period}'], name=f'EMA{period}', line_color=color), row=1, col=1)
    entry.add_trace(go.Scatter(x=tail.index, y=indicators.loc[tail.index, 'RSI14'], name='RSI14', line_color='#a98be4'), row=2, col=1)
    for level in ((32, 50, 68) if gold else (35, 50, 65)):
        entry.add_hline(y=level, row=2, col=1, line_dash='dot', line_color='gray')
    entry.add_trace(go.Scatter(x=tail.index, y=indicators.loc[tail.index, 'ATR14'], name='Wilder ATR14', line_color='#38bcb3'), row=3, col=1)
    entry.update_yaxes(range=[0, 100], row=2, col=1)
    entry.update_layout(height=620, title=f"{symbol} {'M5' if gold else 'H1'} — completed entry candles", xaxis_rangeslider_visible=False)
    decision = pd.Timestamp(diagnostic['decision_time_utc'])
    if gold:
        context = complete_m15(frame, decision)
    else:
        context = frame.resample('4h', origin='epoch').agg({'open':'first','high':'max','low':'min','close':'last'}).dropna()
        context = context.loc[context.index + pd.Timedelta(hours=4) <= decision]
        if diagnostic.get('entry_setups')=='multi_setup_v1':
            counts=frame.close.resample('4h',origin='epoch').count()
            context=context.loc[counts.reindex(context.index).eq(4)]
    tail = context.iloc[-160:]
    trend = go.Figure(go.Candlestick(x=tail.index, open=tail.open, high=tail.high, low=tail.low, close=tail.close, name='Context bid candles'))
    for period, color in ((50, '#ffa500'), (200, '#a98be4')):
        values = context.close.ewm(span=period, adjust=False, min_periods=period).mean()
        trend.add_trace(go.Scatter(x=tail.index, y=values.loc[tail.index], name=f'EMA{period}', line_color=color))
    trend.update_layout(height=380, title=f"{'M15' if gold else 'H4'} trend — same bounded calculation history", xaxis_rangeslider_visible=False)
    return entry, trend


def saved_decision(root: Path, decision: dict, symbol: str) -> dict:
    if decision.get('entry_timing') != 'user_activation_v1':
        raise ValueError('This explainer requires the current user_activation_v1 timing profile.')
    if not decision.get('context_sha256') or not decision.get('decision_time_utc'):
        raise ValueError('Decision lacks verifiable snapshot metadata.')
    path = local_path(root, decision['input_file'])
    frame = load_bars(path, symbol, expected_sha=decision['context_sha256'], decision=decision['decision_time_utc'])
    profile=decision.get('entry_setups')
    if profile=='multi_setup_v1':
        code_root=Path(__file__).resolve().parents[1]
        pins=decision.get('setup_source_sha256') or {}
        if set(pins)!={'analytics/five_setups.py','config/five_setup_spec.json'} or any(
            hashlib.sha256((code_root/name).read_bytes()).hexdigest()!=expected for name,expected in pins.items()):
            raise ValueError('App and monitor five-setup source evidence differs; update the installed app.')
    result = diagnostics(frame, symbol,profile=profile)
    result['source_sha256'], result['source'] = decision['context_sha256'], str(path)
    return result
