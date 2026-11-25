"""Offline, causal signal and lot-feasibility analysis. No trading API imports.

This module screens individual historical opportunities, not sequential trades.
It does not simulate exits, an equity curve, margin or strategy profitability.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Contract:
    symbol: str
    size: float
    point: float
    tick: float
    minimum_lot: float
    lot_step: float

    def __post_init__(self) -> None:
        for name in ('size', 'point', 'tick', 'minimum_lot', 'lot_step'):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be positive and finite.')


def validate_bars(data: pd.DataFrame) -> pd.DataFrame:
    """Require sorted, unique, aware UTC H1 bars and valid broker spread fields."""
    if not isinstance(data, pd.DataFrame) or data.empty:
        raise ValueError('Nonempty H1 bars required.')
    if not isinstance(data.index, pd.DatetimeIndex) or data.index.tz is None:
        raise ValueError('Timezone-aware timestamps required.')
    if data.index.hasnans or data.index.has_duplicates or not data.index.is_monotonic_increasing:
        raise ValueError('Timestamps must be valid, unique and increasing.')
    result = data.copy()
    result.index = result.index.tz_convert('UTC')
    if not result.index.equals(result.index.floor('h')):
        raise ValueError('Expected H1 open timestamps on hour boundaries.')
    required = ['open', 'high', 'low', 'close', 'spread', 'tick_volume', 'real_volume']
    if not set(required).issubset(result.columns):
        raise ValueError('Missing OHLC, spread or volume columns.')
    for column in required:
        if pd.api.types.is_bool_dtype(result[column]):
            raise ValueError('Boolean market values are invalid.')
        result[column] = pd.to_numeric(result[column], errors='raise')
    if not np.isfinite(result[required].to_numpy(dtype=float)).all():
        raise ValueError('Market values must be finite.')
    if (result[['open', 'high', 'low', 'close']] <= 0).any().any():
        raise ValueError('Prices must be positive.')
    if (result[['spread', 'tick_volume', 'real_volume']] < 0).any().any():
        raise ValueError('Spread and volumes cannot be negative.')
    if (result.high < result[['open', 'close', 'low']].max(axis=1)).any() or (result.low > result[['open', 'close', 'high']].min(axis=1)).any():
        raise ValueError('Inconsistent OHLC.')
    return result


def wilder_atr(data: pd.DataFrame, period: int = 14) -> pd.Series:
    """True range, arithmetic seed of N bars, then Wilder's recursive average."""
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError('ATR period must be a positive integer.')
    previous = data.close.shift()
    tr = pd.concat([data.high - data.low, (data.high - previous).abs(), (data.low - previous).abs()], axis=1).max(axis=1)
    values = np.full(len(data), np.nan)
    if len(data) >= period:
        values[period - 1] = float(tr.iloc[:period].mean())
        for i in range(period, len(data)):
            values[i] = (values[i - 1] * (period - 1) + float(tr.iloc[i])) / period
    return pd.Series(values, index=data.index, name='atr14')


def signal_features(data: pd.DataFrame) -> pd.DataFrame:
    """Use closed H1 bars and H4 context released only at its close timestamp."""
    result = validate_bars(data)
    result['decision_time'] = result.index + pd.Timedelta(hours=1)
    result['atr14'] = wilder_atr(result)
    # Recursive EMA seeded with first observed close; eligibility waits 600 H4 bars.
    result['ema20'] = result.close.ewm(span=20, adjust=False, min_periods=20).mean()
    h4 = result.resample('4h', origin='epoch').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    h4['available_at'] = h4.index + pd.Timedelta(hours=4)
    h4 = h4.loc[h4.available_at <= result.decision_time.iloc[-1]].copy()
    h4['ema50'] = h4.close.ewm(span=50, adjust=False, min_periods=50).mean()
    h4['ema200'] = h4.close.ewm(span=200, adjust=False, min_periods=200).mean()
    h4['slope5'] = h4.ema50 - h4.ema50.shift(5)
    warm = np.arange(len(h4)) >= 599
    h4['trend_long'] = warm & (h4.ema50 > h4.ema200) & (h4.close > h4.ema50) & (h4.slope5 > 0)
    h4['trend_short'] = warm & (h4.ema50 < h4.ema200) & (h4.close < h4.ema50) & (h4.slope5 < 0)
    if h4.empty:
        result['trend_long'] = False
        result['trend_short'] = False
    else:
        aligned = pd.merge_asof(pd.DataFrame({'decision_time': result.decision_time.to_numpy()}),
                                h4[['available_at', 'trend_long', 'trend_short']].reset_index(drop=True),
                                left_on='decision_time', right_on='available_at', direction='backward')
        for column in ('trend_long', 'trend_short'):
            result[column] = aligned[column].eq(True).to_numpy()
    touch_low = (result.low <= result.ema20).rolling(3, min_periods=3).max().eq(1)
    touch_high = (result.high >= result.ema20).rolling(3, min_periods=3).max().eq(1)
    result['pullback_long'] = result.trend_long & touch_low & (result.close > result.ema20) & (result.close > result.open) & (result.close > result.high.shift())
    result['pullback_short'] = result.trend_short & touch_high & (result.close < result.ema20) & (result.close < result.open) & (result.close < result.low.shift())
    result['breakout_long'] = result.trend_long & (result.close > result.high.shift().rolling(20, min_periods=20).max())
    result['breakout_short'] = result.trend_short & (result.close < result.low.shift().rolling(20, min_periods=20).min())
    result['low5'] = result.low.rolling(5, min_periods=5).min()
    result['high5'] = result.high.rolling(5, min_periods=5).max()
    result['next_open'] = result.open.shift(-1)
    next_time = pd.Series(result.index, index=result.index).shift(-1)
    result['next_is_consecutive'] = next_time.eq(result.decision_time)
    result['recent_is_consecutive'] = pd.Series(result.index, index=result.index).diff().eq(pd.Timedelta(hours=1)).rolling(5, min_periods=5).sum().eq(5)
    london = result.decision_time.dt.tz_convert('Europe/London')
    result['session'] = (london.dt.day_of_week < 5) & (london.dt.hour >= 8) & (london.dt.hour < 14)
    return result


def eligible_lot(budget: float, loss_per_lot: float, contract: Contract, cap: float = .01) -> float:
    """Round DOWN from the minimum lot on its lattice, never force a minimum."""
    for value in (budget, loss_per_lot, cap):
        if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            raise ValueError('Sizing inputs must be positive and finite.')
    minimum, step = Decimal(str(contract.minimum_lot)), Decimal(str(contract.lot_step))
    limit = min(Decimal(str(budget)) / Decimal(str(loss_per_lot)), Decimal(str(cap)))
    if limit < minimum:
        return 0.0
    size = minimum + ((limit - minimum) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if size * Decimal(str(loss_per_lot)) > Decimal(str(budget)):
        raise ValueError('Rounded lot exceeds risk budget.')
    return float(size)


def screen_level(*, contract: Contract, direction: int, next_bid: float, spread_price: float,
                 atr: float, low5: float, high5: float, budget: float,
                 spread_multiplier: float = 1, slippage_ticks: int = 1) -> dict:
    """Estimate an entry/stop/target without sending or simulating any order."""
    if direction not in (-1, 1):
        raise ValueError('Direction must be +1 or -1.')
    if not all(math.isfinite(x) for x in (next_bid, spread_price, atr, low5, high5, budget, spread_multiplier)):
        raise ValueError('Finite inputs required.')
    if min(next_bid, atr, low5, high5, budget, spread_multiplier) <= 0 or spread_price <= 0:
        raise ValueError('Positive prices, ATR, budget and observed spread required.')
    if isinstance(slippage_ticks, bool) or not isinstance(slippage_ticks, int) or slippage_ticks < 0:
        raise ValueError('Slippage must be a nonnegative integer tick count.')
    spread, slip = spread_price * spread_multiplier, slippage_ticks * contract.tick
    entry = next_bid + spread + slip if direction == 1 else next_bid - slip
    raw_stop = min(low5 - .1 * atr, entry - atr) if direction == 1 else max(high5 + spread + .1 * atr, entry + atr)
    tick = Decimal(str(contract.tick))
    rounding = ROUND_FLOOR if direction == 1 else ROUND_CEILING
    stop = float((Decimal(str(raw_stop)) / tick).to_integral_value(rounding=rounding) * tick)
    if min(stop, entry) <= 0:
        raise ValueError('Positive modeled entry and stop required.')
    distance = direction * (entry - stop)
    if distance <= 0:
        raise ValueError('Adverse stop required.')
    # Entry includes adverse slippage; reserve another slip for an eventual SL exit.
    loss_per_lot = contract.size * (distance + slip)
    lots = eligible_lot(budget, loss_per_lot, contract)
    cost_ratio = (spread + 2 * slip) / distance
    target_rounding = ROUND_CEILING if direction == 1 else ROUND_FLOOR
    raw_target = Decimal(str(entry)) + direction * Decimal('2') * abs(Decimal(str(entry)) - Decimal(str(stop)))
    target = float((raw_target / tick).to_integral_value(rounding=target_rounding) * tick)
    return dict(entry=entry, stop=stop, target=target, stop_distance=distance,
                estimated_spread=spread, slippage_per_side=slip,
                estimated_cost_fraction=cost_ratio, minimum_lot_stop_loss=contract.minimum_lot * loss_per_lot,
                selected_lot=lots, planned_stop_loss=lots * loss_per_lot,
                risk_fits=lots > 0, cost_fits=cost_ratio <= .1)


def opportunity_study(data: pd.DataFrame, contract: Contract) -> pd.DataFrame:
    """Compare predeclared entry families; opportunities can overlap in time."""
    features = signal_features(data)
    records = []
    for row in features.itertuples():
        if not row.session:
            continue
        for family in ('pullback', 'breakout', 'combined'):
            long_signal = row.pullback_long if family == 'pullback' else row.breakout_long if family == 'breakout' else row.pullback_long or row.breakout_long
            short_signal = row.pullback_short if family == 'pullback' else row.breakout_short if family == 'breakout' else row.pullback_short or row.breakout_short
            if not long_signal and not short_signal:
                continue
            if long_signal and short_signal:
                raise ValueError('Conflicting trend directions.')
            direction = 1 if long_signal else -1
            for scenario, multiplier, slips in (('base', 1, 1), ('stress', 2, 2)):
                for capital in (500, 1000):
                    for risk_percent in (.5, 1.0):
                        budget = capital * risk_percent / 100
                        record = dict(symbol=contract.symbol, decision_time=row.decision_time.isoformat(), family=family,
                                      direction='BUY' if direction == 1 else 'SELL', scenario=scenario,
                                      capital=capital, risk_percent=risk_percent, budget=budget,
                                      period='development' if row.decision_time < pd.Timestamp('2025-11-13', tz='UTC') else 'later_1' if row.decision_time < pd.Timestamp('2026-05-13', tz='UTC') else 'later_2')
                        if not row.next_is_consecutive or not row.recent_is_consecutive:
                            record.update(eligible=False, rejection='nonconsecutive_bars')
                        else:
                            levels = screen_level(contract=contract, direction=direction, next_bid=row.next_open,
                                                  spread_price=row.spread * contract.point, atr=row.atr14,
                                                  low5=row.low5, high5=row.high5, budget=budget,
                                                  spread_multiplier=multiplier, slippage_ticks=slips)
                            record.update(levels)
                            record.update(eligible=levels['risk_fits'] and levels['cost_fits'],
                                          rejection='minimum_lot_risk' if not levels['risk_fits'] else 'cost_fraction' if not levels['cost_fits'] else '')
                        records.append(record)
    return pd.DataFrame(records)
