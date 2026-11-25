"""Seven deterministic research hypotheses. No terminal or order access.

This deliberately separate, single-timeframe laboratory does not reproduce or
authorize the native bounded, multi-timeframe A/B execution policies.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path

import numpy as np
import pandas as pd

from analytics.candidate_signals import wilder_atr, wilder_rsi

VERSION = 'seven_lab_v1'
INTERVALS = {'M5': pd.Timedelta(minutes=5), 'H1': pd.Timedelta(hours=1)}
SETUPS = {
    'R1': ('Trend EMA reclaim', 'Trend: EMA50 > EMA200 and EMA50 rising over 5 bars; bullish close crosses above EMA20 and is above EMA50, RSI 50–68. SELL reverses prices and uses RSI 32–50.'),
    'R2': ('Trend liquidity sweep', 'Same trend; low breaks the previous 20-bar low then bullish close recovers it. SELL reverses the high/low test.'),
    'R3': ('Donchian breakout', 'Same trend; close crosses beyond the previous 20-bar high/low. The signal candle is excluded from the channel.'),
    'R4': ('Compression breakout', 'Previous 20-close band width is at or below the previous 120-width 20th percentile; close breaks the previous 20-bar high/low with matching candle direction.'),
    'R5': ('Range band re-entry', 'Range: EMA50–EMA200 separation < 1 ATR and absolute 5-bar EMA50 change < 0.2 ATR; price pierces then closes back inside the previous 20-close 2-standard-deviation band, RSI ≤45 for BUY / ≥55 for SELL.'),
    'R6': ('London opening-range breakout', 'First completed close crossing the 08:00–09:00 Europe/London range during 09:00–12:00, once per direction per local date. The opening hour must contain every expected candle. DST follows London.'),
    'R7': ('Trend RSI momentum return', 'Same trend; RSI crosses 50 and price closes beyond EMA20 and EMA50 in the trend direction.'),
}
SIGNAL_COLUMNS = ['event_id', 'setup_id', 'setup', 'side', 'decision_time_utc',
                  'source', 'instrument', 'timeframe', 'close', 'atr14', 'rsi14',
                  'spread_points', 'overlapping_setups', 'execution_authority']


def validate(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if timeframe not in INTERVALS:
        raise ValueError('Only M5 and H1 research datasets are supported.')
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is None:
        raise ValueError('Timezone-aware candle OPEN timestamps are required.')
    if frame.empty or frame.index.hasnans or not frame.index.is_monotonic_increasing or frame.index.has_duplicates:
        raise ValueError('Nonempty, sorted, unique timestamps are required; duplicates are not silently removed.')
    data = frame.copy()
    data.index = data.index.tz_convert('UTC')
    if np.any(data.index.as_unit('ns').asi8 % INTERVALS[timeframe].value):
        raise ValueError('Candle timestamps must align with the requested UTC timeframe.')
    required = ['open', 'high', 'low', 'close']
    if not set(required).issubset(data):
        raise ValueError('OHLC columns are required.')
    values = data[required].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError('Prices must be positive finite numbers.')
    if ((data.high < data[required].max(axis=1)) | (data.low > data[required].min(axis=1))).any():
        raise ValueError('Invalid OHLC price geometry.')
    return data


def scan(frame: pd.DataFrame, metadata: dict, *, as_of=None) -> dict:
    """Vectorized causal scan. All windows end at a completed decision candle.

    A trigger is a transition into a directional condition, not each persistent
    matching candle. Missing recent bars block signals; missing sessions are not
    filled with invented prices. Full-history indicator seeding is versioned.
    """
    for key in ('source', 'instrument', 'timeframe', 'price_basis'):
        if not metadata.get(key):
            raise ValueError(f'Dataset metadata requires {key}.')
    if metadata['price_basis'] != 'bid':
        raise ValueError('This laboratory requires explicitly identified bid candles.')
    tf = metadata['timeframe']
    data = validate(frame, tf)
    interval = INTERVALS[tf]
    now = pd.Timestamp(as_of if as_of is not None else pd.Timestamp.now(tz='UTC'))
    if now.tz is None:
        raise ValueError('Timezone-aware as_of required.')
    data = data.loc[data.index + interval <= now].copy()
    if data.empty:
        raise ValueError('No completed candles are available.')
    # Some feeds publish carried-forward, zero-range placeholders over closures.
    # Preserve the source file but exclude these bars from indicators and reset
    # the recent-continuity gate across them. Do not mistake them for liquidity.
    flat = data.high.eq(data.low)
    excluded_flat_bars = int(flat.sum())
    data = data.loc[~flat]
    if data.empty:
        raise ValueError('No non-flat completed quote candles are available.')
    close, previous = data.close, data.close.shift()
    ema20 = close.ewm(span=20, adjust=False, min_periods=20).mean()
    ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean()
    ema200 = close.ewm(span=200, adjust=False, min_periods=200).mean()
    atr, rsi = wilder_atr(data), wilder_rsi(close)
    slope = ema50 - ema50.shift(5)
    trend_up = (ema50 > ema200) & (slope > 0) & (close > ema50)
    trend_down = (ema50 < ema200) & (slope < 0) & (close < ema50)
    bullish, bearish = close > data.open, close < data.open
    low = data.low.rolling(20).min().shift()
    high = data.high.rolling(20).max().shift()
    mean = close.rolling(20).mean().shift()
    std = close.rolling(20).std(ddof=0).shift()
    lower, upper = mean - 2 * std, mean + 2 * std
    width = 4 * std / mean
    squeeze = width <= width.shift().rolling(120).quantile(.2)
    ranging = ((ema50 - ema200).abs() < atr) & (slope.abs() < .2 * atr)
    pairs = {
        'R1': (trend_up & bullish & (previous <= ema20.shift()) & (close > ema20) & rsi.between(50,68),
               trend_down & bearish & (previous >= ema20.shift()) & (close < ema20) & rsi.between(32,50)),
        'R2': (trend_up & bullish & (data.low < low) & (close > low),
               trend_down & bearish & (data.high > high) & (close < high)),
        'R3': (trend_up & (close > high) & (previous <= high.shift()),
               trend_down & (close < low) & (previous >= low.shift())),
        'R4': (squeeze & bullish & (close > high), squeeze & bearish & (close < low)),
        'R5': (ranging & bullish & (data.low < lower) & (close >= lower) & (close < mean) & (rsi <= 45),
               ranging & bearish & (data.high > upper) & (close <= upper) & (close > mean) & (rsi >= 55)),
        'R7': (trend_up & (rsi.shift() < 50) & (rsi >= 50) & (close > ema20),
               trend_down & (rsi.shift() > 50) & (rsi <= 50) & (close < ema20)),
    }
    # The completed opening range is propagated only within the same local day.
    local = data.index.tz_convert('Europe/London')
    day = pd.Series(local.date, index=data.index)
    opening = local.hour == 8
    count = pd.Series(opening.astype(int), index=data.index).groupby(day).cumsum()
    range_high = data.high.where(opening).groupby(day).cummax().groupby(day).ffill()
    range_low = data.low.where(opening).groupby(day).cummin().groupby(day).ffill()
    session = (local.hour >= 9) & (local.hour < 12) & (count == int(pd.Timedelta(hours=1) / interval))
    pairs['R6'] = (session & (close > range_high) & (previous <= range_high),
                   session & (close < range_low) & (previous >= range_low))
    contiguous = data.index.to_series().diff().eq(interval).rolling(5).sum().eq(5)
    ratio = atr / atr.rolling(100).median()
    warm = pd.Series(np.arange(len(data)) >= 249, index=data.index)
    guard = warm & contiguous & ratio.between(.5,2) & ((data.high-data.low) <= 3*atr) & (atr > 0)
    triggers, latest = {}, []
    for key in SETUPS:
        buy, sell = (condition.fillna(False) & guard for condition in pairs[key])
        direction = pd.Series(np.where(buy & ~sell, 1, np.where(sell & ~buy, -1, 0)), index=data.index)
        trigger = direction.where(direction.ne(direction.shift()), 0)
        if key == 'R6':
            for side in (1,-1):
                eligible = trigger.eq(side)
                trigger = trigger.mask(eligible & (eligible.groupby(day).cumsum() > 1), 0)
        triggers[key] = trigger
        reason = ('warmup_under_250_bars' if not warm.iloc[-1] else
                  'recent_missing_candles' if not contiguous.iloc[-1] else
                  'volatility_or_shock_guard' if not guard.iloc[-1] else
                  'new_trigger' if trigger.iloc[-1] else
                  'condition_persists_no_new_trigger' if direction.iloc[-1] else 'pattern_not_present')
        latest.append(dict(setup_id=key, setup=SETUPS[key][0],
                           side={1:'buy',-1:'sell',0:None}[int(trigger.iloc[-1])], reason=reason))
    overlap = sum(trigger.ne(0).astype(int) for trigger in triggers.values())
    records = []
    for key, trigger in triggers.items():
        for stamp, value in trigger[trigger.ne(0)].items():
            decision = (stamp + interval).isoformat()
            side = 'buy' if value == 1 else 'sell'
            identity = [VERSION, metadata['source'], metadata['instrument'], tf, key, decision, side]
            records.append(dict(event_id=hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24],
                setup_id=key, setup=SETUPS[key][0], side=side, decision_time_utc=decision,
                source=metadata['source'], instrument=metadata['instrument'], timeframe=tf,
                close=float(close.loc[stamp]), atr14=float(atr.loc[stamp]), rsi14=float(rsi.loc[stamp]),
                spread_points=None if 'spread' not in data else float(data.loc[stamp,'spread']),
                overlapping_setups=int(overlap.loc[stamp]), execution_authority=False))
    signals = pd.DataFrame(records, columns=SIGNAL_COLUMNS).sort_values(['decision_time_utc','setup_id']).reset_index(drop=True)
    gaps = data.index.to_series().diff().gt(interval)
    latest_decision = data.index[-1] + interval
    return dict(version=VERSION, metadata=dict(metadata), signals=signals, latest=pd.DataFrame(latest),
                completed_bars=len(data), excluded_flat_bars=excluded_flat_bars,
                eligible_bars=int(guard.sum()), gap_intervals=int(gaps.sum()),
                start_utc=data.index[0].isoformat(), end_utc=latest_decision.isoformat(),
                as_of_utc=now.isoformat(), stale=bool(now-latest_decision > 2*interval),
                execution_authority=False)


def summarize(signals: pd.DataFrame) -> pd.DataFrame:
    rows=[]
    for key,(name,_) in SETUPS.items():
        selected=signals.loc[signals.setup_id.eq(key)]
        rows.append(dict(setup_id=key,setup=name,triggers=len(selected),
                         buys=int(selected.side.eq('buy').sum()),sells=int(selected.side.eq('sell').sum()),
                         last_trigger_utc=None if selected.empty else selected.decision_time_utc.max()))
    return pd.DataFrame(rows)


def register(root: Path, frame: pd.DataFrame, metadata: dict) -> Path:
    """Content-addressed dataset and manifest; never combine providers."""
    data = validate(frame,metadata['timeframe'])
    # Validate provenance even when there is insufficient indicator warmup.
    for name in ('source','instrument','timeframe','price_basis','instrument_type'):
        if not isinstance(metadata.get(name),str) or not metadata[name].strip():
            raise ValueError(f'Missing provenance field: {name}')
    raw=data.to_csv(index_label='timestamp_utc',lineterminator='\n').encode()
    digest=hashlib.sha256(raw).hexdigest()
    stable_meta={key:value for key,value in metadata.items() if key not in ('acquired_at_utc','requested_start_utc')}
    identity=hashlib.sha256(json.dumps(stable_meta,sort_keys=True).encode()+raw).hexdigest()
    folder=Path(root)/'data/research/setup_lab'
    folder.mkdir(parents=True,exist_ok=True)
    csv=folder/f'{identity}.csv'
    manifest=folder/f'{identity}.json'
    if not csv.exists(): csv.write_bytes(raw)
    payload=dict(metadata=metadata,csv=csv.name,sha256=digest,rows=len(data),
                 first_open_utc=data.index[0].isoformat(),last_open_utc=data.index[-1].isoformat())
    if not manifest.exists(): manifest.write_text(json.dumps(payload,indent=2)+'\n',encoding='utf-8')
    return manifest


def read_dataset(manifest: Path) -> tuple[pd.DataFrame,dict]:
    info=json.loads(Path(manifest).read_text(encoding='utf-8'))
    path=Path(manifest).parent/info['csv']
    if path.resolve().parent != Path(manifest).parent.resolve():
        raise ValueError('Dataset file must remain beside its manifest.')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=info['sha256']:
        raise ValueError('Research dataset checksum mismatch.')
    data=pd.read_csv(BytesIO(raw),index_col=0,float_precision='round_trip')
    data.index=pd.DatetimeIndex(pd.to_datetime(data.index,utc=True))
    return validate(data,info['metadata']['timeframe']),info
