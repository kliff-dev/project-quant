"""User-activation timing v1: unchanged technical gates, no wall-clock gates.

Static copies of the frozen Gold and EUR technical calculations, explicitly
versioned for the wider eligibility universe. No timestamp rewriting, terminal
access, authorization, order execution, or parameter search occurs here.
"""
from __future__ import annotations
import math
import pandas as pd
from analytics.gold_strategy import M5, complete_m15, confirmed_m15_swings, validate_m5
from analytics.candidate_signals import wilder_atr, wilder_rsi
from analytics.setup_research import validate_bars
from analytics.adaptive_targets import confirmed_h4_swings
HOUR = pd.Timedelta(hours=1)


def gold_features(window: pd.DataFrame) -> dict:
    window = validate_m5(window)
    if len(window) < 1200:
        return {'side': None, 'reason': 'insufficient_1200_m5_warmup'}
    window = window.iloc[-1200:]
    decision = window.index[-1] + M5
    result = dict(side=None, reason='', strategy_family='gold_m15_trend_m5_reclaim',
                  decision_time_utc=decision.isoformat(), input_window_rows=len(window),
                  input_window_start_utc=window.index[0].isoformat())
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



def _eur_candidate_features(window: pd.DataFrame) -> dict:
    """Internal snapshot for an already validated, completed H1 window."""
    window = validate_bars(window)
    if len(window) < 2400:
        return {}
    decision = window.index[-1] + HOUR
    if not window.index[-6:].to_series().diff().dropna().eq(HOUR).all():
        return {}
    h4 = window.resample('4h', origin='epoch').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    h4 = h4.loc[h4.index + pd.Timedelta(hours=4) <= decision]
    if len(h4) < 600:
        return {}
    ema20 = window.close.ewm(span=20, adjust=False, min_periods=20).mean()
    ema50 = window.close.ewm(span=50, adjust=False, min_periods=50).mean()
    h4_ema50 = h4.close.ewm(span=50, adjust=False, min_periods=50).mean()
    h4_ema200 = h4.close.ewm(span=200, adjust=False, min_periods=200).mean()
    atr = float(wilder_atr(window).iloc[-1])
    h4_atr = float(wilder_atr(h4).iloc[-1])
    rsi = float(wilder_rsi(window.close).iloc[-1])
    slope = float(h4_ema50.iloc[-1] - h4_ema50.iloc[-6])
    trend_long = bool(h4_ema50.iloc[-1] > h4_ema200.iloc[-1]
                      and h4.close.iloc[-1] > h4_ema50.iloc[-1] and slope > 0)
    trend_short = bool(h4_ema50.iloc[-1] < h4_ema200.iloc[-1]
                       and h4.close.iloc[-1] < h4_ema50.iloc[-1] and slope < 0)
    range_regime = bool(abs(h4_ema50.iloc[-1] - h4_ema200.iloc[-1]) <= 2. * h4_atr
                        and abs(slope) <= .5 * h4_atr)
    last, previous = window.iloc[-1], window.iloc[-2]
    bullish, bearish = bool(last.close > last.open), bool(last.close < last.open)
    prior = window.iloc[-21:-1]
    prior_low, prior_high = float(prior.low.min()), float(prior.high.max())
    mean, std = float(prior.close.mean()), float(prior.close.std(ddof=0))
    lower, upper = mean - 2. * std, mean + 2. * std
    return dict(
        decision_time_utc=decision.isoformat(),
        trend_reclaim_long=bool(trend_long and bullish and last.close > ema20.iloc[-1]
                                and previous.close <= ema20.iloc[-2] and last.close > ema50.iloc[-1]
                                and 50. <= rsi <= 65.),
        trend_reclaim_short=bool(trend_short and bearish and last.close < ema20.iloc[-1]
                                 and previous.close >= ema20.iloc[-2] and last.close < ema50.iloc[-1]
                                 and 35. <= rsi <= 50.),
        trend_sweep_long=bool(trend_long and last.low < prior_low and last.close > prior_low and bullish),
        trend_sweep_short=bool(trend_short and last.high > prior_high and last.close < prior_high and bearish),
        range_reentry_long=bool(range_regime and last.low < lower and last.close >= lower
                                and last.close < mean and bullish and rsi <= 45.),
        range_reentry_short=bool(range_regime and last.high > upper and last.close <= upper
                                 and last.close > mean and bearish and rsi >= 55.),
        trend_long=trend_long, trend_short=trend_short, range_regime=range_regime,
        atr14=atr, low5=float(window.low.iloc[-5:].min()), high5=float(window.high.iloc[-5:].max()),
        spread=int(last.spread), h1_close=float(last.close), h1_ema20=float(ema20.iloc[-1]),
        h1_ema50=float(ema50.iloc[-1]), h1_rsi14=rsi,
        h4_close=float(h4.close.iloc[-1]), h4_ema50=float(h4_ema50.iloc[-1]),
        h4_ema200=float(h4_ema200.iloc[-1]), h4_ema50_slope5=slope, h4_atr14=h4_atr,
        last_completed_h4_open_utc=h4.index[-1].isoformat(), completed_h4_rows=len(h4),
        prior20_low=prior_low, prior20_high=prior_high, prior20_close_mean=mean,
        prior20_close_std_population=std, prior20_lower_band=lower, prior20_upper_band=upper,
        input_window_start_utc=window.index[0].isoformat(), input_window_rows=len(window))



def eur_features(window: pd.DataFrame) -> dict:
    item = _eur_candidate_features(window)
    if not item:
        return {}
    atr = wilder_atr(window)
    median = float(atr.iloc[-100:].median())
    if not math.isfinite(median) or median <= 0:
        raise ValueError('Positive finite completed-H1 ATR median required.')
    h4 = window.resample('4h', origin='epoch').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
    long, short = item['trend_reclaim_long'], item['trend_reclaim_short']
    return dict(item, strategy_family='trend_reclaim', pullback_long=long,
                pullback_short=short, breakout_long=False, breakout_short=False,
                volatility_ratio=float(atr.iloc[-1]) / median,
                **confirmed_h4_swings(h4, pd.Timestamp(item['decision_time_utc'])))

