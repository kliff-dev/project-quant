"""Date-filtered chart and journal views; local data only."""
from __future__ import annotations

import math
import pandas as pd
import plotly.graph_objects as go


def date_bounds(first, last, zone='Asia/Manila'):
    begin = pd.Timestamp(first).tz_localize(zone)
    finish = pd.Timestamp(last).tz_localize(zone)+pd.DateOffset(days=1)
    if finish <= begin:
        raise ValueError('End date must be on or after the start date.')
    return begin, finish


def market_slice(frame, first, last, zone='Asia/Manila'):
    begin, finish = date_bounds(first,last,zone)
    return frame.loc[(frame.index >= begin) & (frame.index < finish)].copy()


def market_chart(frame, selected, symbol, zone='Asia/Manila'):
    visible = selected.iloc[-5000:]
    dates = visible.index.tz_convert(zone)
    figure = go.Figure(go.Candlestick(x=dates,open=visible.open,high=visible.high,low=visible.low,close=visible.close,name='Historical bid candles'))
    for period, color in ((20,'#00bfff'),(50,'#ffa500')):
        values = frame.close.ewm(span=period,adjust=False,min_periods=period).mean()
        figure.add_trace(go.Scatter(x=dates,y=values.loc[visible.index],name=f'EMA{period}',line_color=color))
    figure.update_layout(height=540,title=f'{symbol} saved market history',xaxis_title=zone,yaxis_title='Quote price',xaxis_rangeslider_visible=False)
    return figure


def journal_slice(trades, first, last, symbols, categories):
    begin, finish = date_bounds(first,last,'Asia/Manila')
    rows = []
    for trade in trades:
        value = trade.get('entry_time_utc')
        stamp = pd.to_datetime(value,utc=True,errors='coerce')
        if pd.isna(stamp) or not begin <= stamp < finish:
            continue
        if trade.get('symbol') in symbols and trade.get('category') in categories:
            rows.append(trade)
    return rows


def booked_history(trades):
    """Recorded completed-trade cash results, never an account-equity curve."""
    records = []
    for trade in trades:
        stamp = pd.to_datetime(trade.get('exit_time_utc'),utc=True,errors='coerce')
        pnl = trade.get('realized_net_pnl_usd')
        if trade.get('status')=='closed' and pd.notna(stamp) and type(pnl) in (int,float) and math.isfinite(pnl):
            records.append(dict(exit_time_utc=stamp,net_pnl=pnl,symbol=trade.get('symbol'),trade_id=trade.get('trade_id')))
    result = pd.DataFrame(records,columns=['exit_time_utc','net_pnl','symbol','trade_id'])
    if not result.empty:
        result = result.sort_values(['exit_time_utc','trade_id']).reset_index(drop=True)
    result['cumulative_booked_pnl'] = result.net_pnl.cumsum()
    return result
