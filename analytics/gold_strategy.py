"""Frozen, causal M5/M15 Gold research kernel. No terminal or network imports.

All bars are broker bid bars. A bar's spread is an imperfect historical ask
proxy; volume is not used as consolidated spot-market volume.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import math

import numpy as np
import pandas as pd

from analytics.candidate_signals import wilder_atr, wilder_rsi

M5 = pd.Timedelta(minutes=5)
M15 = pd.Timedelta(minutes=15)
SPEC_SHA256 = '1eea23dd683776ff69cd44f7937592a2b5411fe5d9882fb873aa49d49a564bfd'


def validate_m5(data: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(data, pd.DataFrame) or data.empty:
        raise ValueError('Nonempty M5 bars required.')
    if not isinstance(data.index, pd.DatetimeIndex) or data.index.tz is None:
        raise ValueError('Timezone-aware M5 timestamps required.')
    if data.index.hasnans or data.index.has_duplicates or not data.index.is_monotonic_increasing:
        raise ValueError('Valid unique increasing timestamps required.')
    frame = data.copy()
    frame.index = frame.index.tz_convert('UTC')
    if not frame.index.equals(frame.index.floor('5min')):
        raise ValueError('M5 open timestamps must fall on five-minute boundaries.')
    columns = ['open', 'high', 'low', 'close', 'spread', 'tick_volume', 'real_volume']
    if not set(columns).issubset(frame.columns):
        raise ValueError('OHLC, spread and volume columns required.')
    for column in columns:
        if pd.api.types.is_bool_dtype(frame[column]):
            raise ValueError('Boolean market values are invalid.')
        frame[column] = pd.to_numeric(frame[column], errors='raise')
    if not np.isfinite(frame[columns].to_numpy(dtype=float)).all():
        raise ValueError('Finite market values required.')
    if (frame[['open', 'high', 'low', 'close']] <= 0).any().any():
        raise ValueError('Positive prices required.')
    if (frame[['spread', 'tick_volume', 'real_volume']] < 0).any().any():
        raise ValueError('Nonnegative spread and volume required.')
    if (frame.high < frame[['open', 'low', 'close']].max(axis=1)).any() or (frame.low > frame[['open', 'high', 'close']].min(axis=1)).any():
        raise ValueError('Inconsistent OHLC.')
    return frame


def complete_m15(data: pd.DataFrame, decision: pd.Timestamp) -> pd.DataFrame:
    """Never turn one/two M5 observations into a complete context candle."""
    decision = pd.Timestamp(decision)
    if decision.tz is None:
        raise ValueError('Timezone-aware decision required.')
    resampled = data.resample('15min', origin='epoch').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'})
    counts = data.close.resample('15min', origin='epoch').count()
    return resampled.loc[(counts == 3) & (resampled.index + M15 <= decision)].dropna()


def session_status(decision: pd.Timestamp) -> str:
    decision = pd.Timestamp(decision)
    if decision.tz is None:
        raise ValueError('Timezone-aware decision required.')
    london, ny = decision.tz_convert('Europe/London'), decision.tz_convert('America/New_York')
    if london.day_of_week >= 5 or not 7 <= london.hour < 17:
        return 'outside_london_session'
    minute = ny.hour * 60 + ny.minute
    if any(start <= minute < end for start, end in [(495, 540), (585, 615), (825, 870)]):
        return 'routine_us_clock_blackout_not_event_calendar'
    return ''


def confirmed_m15_swings(context: pd.DataFrame, decision: pd.Timestamp) -> dict:
    """Two strict left/right neighbors; rightmost candle must have closed."""
    decision = pd.Timestamp(decision)
    if decision.tz is None:
        raise ValueError('Timezone-aware decision required.')
    rows = context.loc[context.index + M15 <= decision].iloc[-120:]
    result = {'m15_swing_highs': [], 'm15_swing_lows': []}
    for i in range(2, len(rows) - 2):
        # Missing M15 bars must not supply artificial neighboring candles.
        if not rows.index[i-2:i+3].to_series().diff().dropna().eq(M15).all():
            continue
        for column, key, high in [('high', 'm15_swing_highs', True), ('low', 'm15_swing_lows', False)]:
            price = float(rows[column].iloc[i])
            neighbors = rows[column].iloc[[i-2, i-1, i+1, i+2]]
            if bool((neighbors < price).all() if high else (neighbors > price).all()):
                result[key].append(dict(price=price, pivot_open_utc=rows.index[i].isoformat(),
                                        confirmed_at_utc=(rows.index[i+2]+M15).isoformat()))
    return result


def _window_features(window: pd.DataFrame) -> dict:
    if len(window) < 1200:
        return {'side': None, 'reason': 'insufficient_1200_m5_warmup'}
    window = window.iloc[-1200:]
    decision = window.index[-1] + M5
    result = dict(side=None, reason='', strategy_family='gold_m15_trend_m5_reclaim',
                  decision_time_utc=decision.isoformat(), input_window_rows=len(window),
                  input_window_start_utc=window.index[0].isoformat())
    rejection = session_status(decision)
    if rejection:
        return dict(result, reason=rejection)
    if not window.index[-6:].to_series().diff().dropna().eq(M5).all():
        return dict(result, reason='recent_m5_gap')
    context = complete_m15(window, decision)
    if len(context) < 250:
        return dict(result, reason='insufficient_250_complete_m15_warmup')
    ema20 = window.close.ewm(span=20, adjust=False, min_periods=20).mean()
    ema50 = window.close.ewm(span=50, adjust=False, min_periods=50).mean()
    context50 = context.close.ewm(span=50, adjust=False, min_periods=50).mean()
    context200 = context.close.ewm(span=200, adjust=False, min_periods=200).mean()
    atr_series = wilder_atr(window)
    atr, median = float(atr_series.iloc[-1]), float(atr_series.iloc[-100:].median())
    m15_atr, rsi = float(wilder_atr(context).iloc[-1]), float(wilder_rsi(window.close).iloc[-1])
    if min(atr, median, m15_atr) <= 0:
        return dict(result, reason='nonpositive_atr')
    ratio, slope = atr / median, float(context50.iloc[-1] - context50.iloc[-6])
    last, previous = window.iloc[-1], window.iloc[-2]
    trend_long = bool(context50.iloc[-1] > context200.iloc[-1] and context.close.iloc[-1] > context50.iloc[-1] and slope > 0)
    trend_short = bool(context50.iloc[-1] < context200.iloc[-1] and context.close.iloc[-1] < context50.iloc[-1] and slope < 0)
    long = bool(trend_long and last.close > last.open and previous.close <= ema20.iloc[-2]
                and last.close > ema20.iloc[-1] and last.close > ema50.iloc[-1] and 50 <= rsi <= 68)
    short = bool(trend_short and last.close < last.open and previous.close >= ema20.iloc[-2]
                 and last.close < ema20.iloc[-1] and last.close < ema50.iloc[-1] and 32 <= rsi <= 50)
    result.update(atr14=atr, low3=float(window.low.iloc[-3:].min()), high3=float(window.high.iloc[-3:].max()),
        spread=float(last.spread), volatility_ratio=ratio, m5_close=float(last.close), m5_ema20=float(ema20.iloc[-1]),
        m5_ema50=float(ema50.iloc[-1]), m5_rsi14=rsi, m15_close=float(context.close.iloc[-1]),
        m15_ema50=float(context50.iloc[-1]), m15_ema200=float(context200.iloc[-1]),
        m15_ema50_slope5=slope, m15_atr14=m15_atr, trend_long=trend_long, trend_short=trend_short,
        latest_bar_range=float(last.high-last.low), completed_m15_rows=len(context),
        last_completed_m15_open_utc=context.index[-1].isoformat(),
        london_hour=decision.tz_convert('Europe/London').hour, weekday=decision.day_name(), month=decision.month)
    if not .75 <= ratio <= 1.75:
        return dict(result, reason='volatility_outside_075_175')
    if last.high-last.low > 2.5*atr:
        return dict(result, reason='shock_candle')
    if not (long or short):
        return dict(result, reason='no_m5_reclaim_in_m15_trend')
    result.update(confirmed_m15_swings(context, decision))
    return dict(result, side='buy' if long else 'sell', reason='valid_entry_setup')


def window_features(window: pd.DataFrame) -> dict:
    """Reproducible bounded 1200-bar closed-data signal snapshot."""
    return _window_features(validate_m5(window))


@dataclass(frozen=True)
class GoldReplaySettings:
    initial_balance: float = 500.
    risk_percent: float = 1.
    max_lots: float = .1
    slippage_price: float = .05
    cost_fraction: float = .05
    charge_cost_allowance: bool = True
    leverage: int = 100

    def __post_init__(self):
        values = [self.initial_balance, self.risk_percent, self.max_lots, self.slippage_price, self.cost_fraction]
        if any(isinstance(v, bool) or not math.isfinite(v) for v in values):
            raise ValueError('Finite numeric Gold settings required.')
        if self.initial_balance <= 0 or not 0 < self.risk_percent <= 1 or not .01 <= self.max_lots <= .1 or min(self.slippage_price, self.cost_fraction) < 0:
            raise ValueError('Invalid Gold settings or risk above DEMO ceiling.')
        if type(self.leverage) is not int or self.leverage <= 0 or type(self.charge_cost_allowance) is not bool:
            raise ValueError('Valid leverage and cashflow choice required.')


def _d(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Finite values required.')
    return result


def select_target(signal: dict, plans: dict[int, dict], spread_price: float) -> tuple[int | None, dict, str]:
    """Shared native/research branch selector using actual rounded TP prices."""
    if set(plans) != {1, 2, 3, 4}:
        raise ValueError('All four native rounded target plans required.')
    side = signal.get('side')
    if side not in ('buy', 'sell'):
        return None, {}, 'missing_direction'
    direction = 1 if side == 'buy' else -1
    reference = _d(plans[1]['reference_entry'])
    decision = pd.Timestamp(signal['decision_time_utc'])
    if decision.tz is None:
        raise ValueError('Timezone-aware decision required.')
    candidates = []
    for pivot in signal.get('m15_swing_highs' if direction == 1 else 'm15_swing_lows', []):
        opened, confirmed = pd.Timestamp(pivot['pivot_open_utc']), pd.Timestamp(pivot['confirmed_at_utc'])
        if opened.tz is None or confirmed.tz is None:
            raise ValueError('Timezone-aware pivot timestamps required.')
        if confirmed > decision or opened + 3*M15 != confirmed:
            continue
        quote = _d(pivot['price']) + (_d(spread_price) if direction == -1 else 0)
        if direction*(quote-reference) > 0:
            candidates.append((pivot, quote))
    if not candidates:
        return None, {}, 'missing_confirmed_obstacle'
    pivot, quote = min(candidates, key=lambda item: direction*item[1])
    buffer = Decimal('.2')*_d(signal['atr14'])
    boundary = quote-direction*buffer
    room = {str(r): direction*(_d(plans[r]['take_profit'])-boundary) <= 0 for r in (1, 2, 3, 4)}
    strong = abs(_d(signal['m15_ema50'])-_d(signal['m15_ema200'])) >= _d(signal['m15_atr14']) and abs(_d(signal['m15_ema50_slope5'])) >= Decimal('.25')*_d(signal['m15_atr14'])
    normal = Decimal('.75') <= _d(signal['volatility_ratio']) <= Decimal('1.5')
    if not room['1']:
        return None, {}, 'insufficient_1r_target_room'
    selected = next(r for r in (4, 3, 2, 1) if room[str(r)] and (r <= 2 or strong and normal))
    evidence = dict(exit_policy='gold_adaptive_1_2_3_4', selected_reward_r=selected,
        branch_reason=f'{selected}r_room_' + ('strong_trend_normal_volatility' if selected >= 3 else 'available_room_or_context_cap'),
        obstacle_bid_price=float(pivot['price']), obstacle_exit_quote=float(quote),
        obstacle_pivot_open_utc=pivot['pivot_open_utc'], obstacle_confirmed_at_utc=pivot['confirmed_at_utc'],
        obstacle_buffer_price=float(buffer), strong_context=bool(strong), normal_volatility=bool(normal),
        volatility_ratio=float(signal['volatility_ratio']), target_room_flags=room)
    return selected, evidence, ''


def make_plan(signal: dict, bid: float, balance: float, settings: GoldReplaySettings | None = None) -> tuple[dict | None, str]:
    settings = settings or GoldReplaySettings()
    if signal.get('side') not in ('buy', 'sell'):
        return None, 'missing_direction'
    direction = 1 if signal['side'] == 'buy' else -1
    funds, tick, step, size = _d(balance), Decimal('.001'), Decimal('.01'), Decimal('100')
    if funds <= 0:
        return None, 'nonpositive_balance'
    spread, slip, atr = _d(signal['spread'])*tick, _d(settings.slippage_price), _d(signal['atr14'])
    if spread < 0 or spread > 1:
        return None, 'spread_filter'
    if not Decimal('.75') <= _d(signal['volatility_ratio']) <= Decimal('1.75'):
        return None, 'volatility_filter'
    if atr <= 0:
        raise ValueError('Positive ATR required.')
    entry = _d(bid)+(spread if direction == 1 else 0)
    stop = min(_d(signal['low3'])-Decimal('.2')*atr, entry-Decimal('1.2')*atr) if direction == 1 else max(_d(signal['high3'])+spread+Decimal('.2')*atr, entry+Decimal('1.2')*atr)
    stop = (stop/tick).to_integral_value(rounding=ROUND_FLOOR if direction == 1 else ROUND_CEILING)*tick
    distance = direction*(entry-stop)
    if distance <= 0 or (spread+2*slip)/distance > Decimal('.1'):
        return None, 'cost_filter'
    budget = (funds*_d(settings.risk_percent)/100).quantize(Decimal('.01'), rounding=ROUND_FLOOR)
    fee = (budget*_d(settings.cost_fraction)).quantize(Decimal('.01'), rounding=ROUND_CEILING)
    if budget <= fee:
        return None, 'budget_too_small'
    raw = (budget-fee)/(size*(distance+2*slip))
    lots = min(_d(settings.max_lots), (raw/step).to_integral_value(rounding=ROUND_FLOOR)*step)
    if lots < step:
        return None, 'minimum_lot_risk'
    risk = size*lots*(distance+2*slip)+fee
    margin = size*lots*entry/settings.leverage
    if margin > funds-fee:
        return None, 'illustrative_margin'
    plans = {}
    for reward in (1, 2, 3, 4):
        target_raw = entry+direction*(2*slip+(reward*risk+fee)/(size*lots))
        target = (target_raw/tick).to_integral_value(rounding=ROUND_CEILING if direction == 1 else ROUND_FLOOR)*tick
        plans[reward] = dict(direction=direction, reference_entry=float(entry), entry_price=float(entry+direction*slip),
            stop_loss=float(stop), take_profit=float(target), lots=float(lots), planned_risk=float(risk), risk_budget=float(budget),
            entry_fee=float(fee) if settings.charge_cost_allowance else 0., cost_reserve=float(fee), slip=float(slip),
            contract_size=float(size), margin_estimate=float(margin))
    selected, evidence, reason = select_target(signal, plans, float(spread))
    return (dict(plans[selected], **evidence), '') if selected is not None else (None, reason)


def prepare_signals(data: pd.DataFrame) -> tuple[dict[int, dict], dict[str, int]]:
    """Next-open signals; cheap necessary candle-direction gate avoids no-op work."""
    data = validate_m5(data)
    decisions = data.index+M5
    local = decisions.tz_convert('Europe/London')
    necessary = (((data.close > data.open) & (data.close > data.close.shift())) |
                 ((data.close < data.open) & (data.close < data.close.shift())))
    gate = necessary & (local.day_of_week < 5) & (local.hour >= 7) & (local.hour < 17)
    signals, reasons = {}, {}
    for i in np.flatnonzero(gate):
        if i < 1199 or i+1 >= len(data) or data.index[i+1] != decisions[i]:
            continue
        item = _window_features(data.iloc[i-1199:i+1])
        reason = item['reason']
        reasons[reason] = reasons.get(reason, 0)+1
        if item.get('side'):
            signals[int(i+1)] = item
    return signals, reasons


def protective_fill(plan: dict, bar: pd.Series, *, opening_only: bool = False) -> tuple | None:
    direction = plan['direction']
    shift = float(bar.spread)*.001 if direction == -1 else 0.
    opening, high, low = (float(bar[name])+shift for name in ('open', 'high', 'low'))
    stop, target, slip = plan['stop_loss'], plan['take_profit'], plan['slip']
    if direction*(opening-stop) <= 0:
        return opening-direction*slip, 'stop_gap', False
    if direction*(opening-target) >= 0:
        return target-direction*slip, 'target_gap', False
    if opening_only:
        return None
    stop_hit = low <= stop if direction == 1 else high >= stop
    target_hit = high >= target if direction == 1 else low <= target
    if stop_hit:
        return stop-direction*slip, 'stop', bool(target_hit)
    if target_hit:
        return target-direction*slip, 'target', False
    return None


def replay_account(data: pd.DataFrame, signals: dict[int, dict], start: pd.Timestamp,
                   end: pd.Timestamp, settings: GoldReplaySettings) -> dict:
    """One position, conservative bar ordering, mark open holdings at period end."""
    data = validate_m5(data)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    if start.tz is None or end.tz is None or end <= start:
        raise ValueError('Increasing timezone-aware replay bounds required.')
    balance, position, trades, marks, rejected = settings.initial_balance, None, [], [], {}
    peak, max_dd, skipped_occupied, ambiguities = balance, 0., 0, 0
    def cash(value):
        return float(_d(value).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
    def finish(bar, opened, fill):
        nonlocal balance, position, ambiguities
        price, reason, ambiguous = fill
        gross = position['direction']*position['contract_size']*position['lots']*(price-position['entry_price'])
        net = cash(gross-position['entry_fee'])
        balance = cash(balance+gross)
        record = dict(position, exit_price=price, exit_time_utc=opened.isoformat(), exit_reason=reason,
                      gross_pnl=cash(gross), net_pnl=net, net_r=net/position['planned_risk'],
                      holding_hours=(opened-pd.Timestamp(position['entry_time_utc'])).total_seconds()/3600,
                      ambiguous_bar=bool(ambiguous), balance_after=balance)
        trades.append(record)
        ambiguities += int(ambiguous)
        position = None
    for i in np.flatnonzero((data.index >= start) & (data.index < end)):
        bar, opened = data.iloc[i], data.index[i]
        occupied_at_open = position is not None
        if position:
            fill = protective_fill(position, bar, opening_only=True)
            if fill:
                finish(bar, opened, fill)
        signal = signals.get(int(i))
        # A position existing at this open blocks a fresh entry even if it gaps out.
        if signal and occupied_at_open:
            skipped_occupied += 1
        elif signal:
            plan, reason = make_plan(signal, float(bar.open), balance, settings)
            if plan is None:
                rejected[reason] = rejected.get(reason, 0)+1
            else:
                position = dict(plan, entry_time_utc=opened.isoformat(), signal=signal)
                balance = cash(balance-plan['entry_fee'])
        if position:
            fill = protective_fill(position, bar)
            if fill:
                finish(bar, opened+M5, fill)
        mark = balance
        if position:
            exit_quote = float(bar.close)+(float(bar.spread)*.001 if position['direction'] == -1 else 0.)
            exit_quote -= position['direction']*position['slip']
            mark += position['direction']*100*position['lots']*(exit_quote-position['entry_price'])
        peak = max(peak, mark)
        max_dd = max(max_dd, (peak-mark)/peak*100 if peak > 0 else 0.)
        marks.append(dict(time_utc=(opened+M5).isoformat(), balance=balance, equity=cash(mark)))
    wins = sum(t['net_pnl'] > 0 for t in trades)
    losses = sum(t['net_pnl'] < 0 for t in trades)
    return dict(initial_balance=settings.initial_balance, ending_balance=balance,
        ending_equity=marks[-1]['equity'] if marks else balance, closed_trades=len(trades), wins=wins, losses=losses,
        win_rate=wins/len(trades) if trades else None, net_expectancy_r=float(np.mean([t['net_r'] for t in trades])) if trades else None,
        net_closed_pnl=cash(sum(t['net_pnl'] for t in trades)), maximum_marked_drawdown_percent=max_dd,
        median_holding_hours=float(np.median([t['holding_hours'] for t in trades])) if trades else None,
        ambiguous_bars=ambiguities, skipped_occupied=skipped_occupied, rejection_counts=rejected,
        open_position=position, trades=trades, marks=marks)
