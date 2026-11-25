"""Offline, single-position account replay. No terminal API or trading imports."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import math

import numpy as np
import pandas as pd

from analytics.setup_research import validate_bars
from scripts.research_holding_period import rollover_units


def money(value: float | Decimal) -> float:
    return float(Decimal(str(value)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class ReplaySettings:
    minimum_stop_atr: float = 1.
    reward_r: float = 2.
    initial_balance: float = 500.
    risk_percent: float = 1.
    max_lots: float = .1
    slippage_points: int = 2
    cost_fraction: float = .05
    financing_per_001lot_unit: float = 0.
    ambiguity: str = 'stop_first'
    leverage: int = 100
    charge_cost_allowance: bool = True

    def __post_init__(self):
        numeric = [self.minimum_stop_atr,self.reward_r,self.initial_balance,self.risk_percent,self.max_lots,self.cost_fraction,self.financing_per_001lot_unit]
        if any(isinstance(v,bool) or not math.isfinite(v) for v in numeric):
            raise ValueError('Finite numeric replay settings required.')
        if min(numeric[:-1])<=0 or self.financing_per_001lot_unit<0 or self.ambiguity not in ('stop_first','target_first'):
            raise ValueError('Invalid research settings.')
        if type(self.leverage) is not int or self.leverage<=0 or type(self.slippage_points) is not int or self.slippage_points<0:
            raise ValueError('Valid leverage/slippage settings required.')
        if type(self.charge_cost_allowance) is not bool:
            raise ValueError('Cost-allowance cashflow choice must be boolean.')

    @property
    def config_id(self) -> str:
        return f'sl{self.minimum_stop_atr:g}atr_tp{self.reward_r:g}r'


def window_signal(window: pd.DataFrame) -> dict:
    """Last completed row, using exactly the live history window and UTC H4 grid."""
    if len(window)<2400:
        return {}
    close=window.close
    decision=window.index[-1]+pd.Timedelta(hours=1)
    local=decision.tz_convert('Europe/London')
    if local.day_of_week>=5 or not 8<=local.hour<14:
        return {}
    if not window.index[-6:].to_series().diff().dropna().eq(pd.Timedelta(hours=1)).all():
        return {}
    ema20=close.ewm(span=20,adjust=False,min_periods=20).mean()
    h4=close.resample('4h',origin='epoch').last().dropna()
    h4=h4.loc[h4.index+pd.Timedelta(hours=4)<=decision]
    if len(h4)<600:
        return {}
    ema50=h4.ewm(span=50,adjust=False,min_periods=50).mean()
    ema200=h4.ewm(span=200,adjust=False,min_periods=200).mean()
    trend_long=bool(ema50.iloc[-1]>ema200.iloc[-1] and h4.iloc[-1]>ema50.iloc[-1] and ema50.iloc[-1]>ema50.iloc[-6])
    trend_short=bool(ema50.iloc[-1]<ema200.iloc[-1] and h4.iloc[-1]<ema50.iloc[-1] and ema50.iloc[-1]<ema50.iloc[-6])
    last=window.iloc[-1]
    previous=window.iloc[-2]
    pullback_long=bool(trend_long and (window.low.iloc[-3:]<=ema20.iloc[-3:]).any() and last.close>ema20.iloc[-1] and last.close>last.open and last.close>previous.high)
    pullback_short=bool(trend_short and (window.high.iloc[-3:]>=ema20.iloc[-3:]).any() and last.close<ema20.iloc[-1] and last.close<last.open and last.close<previous.low)
    breakout_long=bool(trend_long and last.close>window.high.iloc[-21:-1].max())
    breakout_short=bool(trend_short and last.close<window.low.iloc[-21:-1].min())
    if not any((pullback_long,pullback_short,breakout_long,breakout_short)):
        return {}
    values=close.to_numpy()
    prior=np.r_[np.nan,values[:-1]]
    true_range=np.nanmax(np.stack([window.high.to_numpy()-window.low.to_numpy(),np.abs(window.high.to_numpy()-prior),np.abs(window.low.to_numpy()-prior)]),axis=0)
    # Arithmetic 14-bar seed, then the equivalent recursive Wilder average.
    seeded=np.r_[true_range[:14].mean(),true_range[14:]]
    atr=float(pd.Series(seeded).ewm(alpha=1/14,adjust=False).mean().iloc[-1])
    return dict(decision_time_utc=decision.isoformat(),pullback_long=pullback_long,pullback_short=pullback_short,
        breakout_long=breakout_long,breakout_short=breakout_short,atr14=atr,
        low5=float(window.low.iloc[-5:].min()),high5=float(window.high.iloc[-5:].max()),spread=int(last.spread),
        h1_close=float(last.close),h1_ema20=float(ema20.iloc[-1]),h4_ema50=float(ema50.iloc[-1]),h4_ema200=float(ema200.iloc[-1]),
        input_window_start_utc=window.index[0].isoformat(),input_window_rows=len(window))


def prepare_signals(data: pd.DataFrame, *, first_entry: pd.Timestamp, window_bars: int = 3000) -> dict[int,dict]:
    """Necessary price-break gate is a superset of both triggers; no EMA shortcut."""
    data=validate_bars(data)
    if type(window_bars) is not int or window_bars<2400:
        raise ValueError('At least 2400 closed H1 rows required for the production warmup.')
    decisions=data.index+pd.Timedelta(hours=1)
    local=decisions.tz_convert('Europe/London')
    gate=((data.close>data.high.shift())|(data.close<data.low.shift())).to_numpy(copy=True)
    gate&=(local.day_of_week<5)&(local.hour>=8)&(local.hour<14)&(decisions>=first_entry)
    result={}
    for i in np.flatnonzero(gate):
        if i+1>=len(data) or data.index[i+1]!=decisions[i]:
            continue
        item=window_signal(data.iloc[max(0,i-window_bars+1):i+1])
        if item:
            result[int(i+1)]=item
    return result


def make_plan(signal: dict, bid_value: float, balance_value: float, settings: ReplaySettings) -> tuple[dict | None,str]:
    """USD linear contract; dynamic budget, reserves, tick and lot rounding."""
    tick, step, size=Decimal('.00001'),Decimal('.01'),Decimal('100000')
    balance=Decimal(str(balance_value))
    if balance<=0:
        return None,'nonpositive_balance'
    budget=(balance*Decimal(str(settings.risk_percent))/100).quantize(Decimal('.01'),rounding=ROUND_FLOOR)
    fee=(budget*Decimal(str(settings.cost_fraction))).quantize(Decimal('.01'),rounding=ROUND_CEILING)
    if budget<=fee:
        return None,'budget_too_small'
    long=bool(signal['pullback_long'] or signal['breakout_long'])
    short=bool(signal['pullback_short'] or signal['breakout_short'])
    if long==short:
        return None,'conflicting_signal'
    direction=1 if long else -1
    spread=Decimal(str(signal['spread']))*tick
    slip=settings.slippage_points*tick
    if not 0<spread<=Decimal('.0002'):
        return None,'spread_filter'
    entry=Decimal(str(bid_value))+(spread if long else 0)
    atr=Decimal(str(signal['atr14']))
    if atr<=0 or not atr.is_finite():
        raise ValueError('Positive finite signal ATR required.')
    minimum=Decimal(str(settings.minimum_stop_atr))*atr
    stop=min(Decimal(str(signal['low5']))-Decimal('.1')*atr,entry-minimum) if long else max(Decimal(str(signal['high5']))+spread+Decimal('.1')*atr,entry+minimum)
    stop=(stop/tick).to_integral_value(rounding=ROUND_FLOOR if long else ROUND_CEILING)*tick
    distance=direction*(entry-stop)
    if distance<=0 or (spread+2*slip)/distance>Decimal('.1'):
        return None,'cost_filter'
    raw=(budget-fee)/(size*(distance+2*slip))
    lots=min(Decimal(str(settings.max_lots)),(raw/step).to_integral_value(rounding=ROUND_FLOOR)*step)
    if lots<step:
        return None,'minimum_lot_risk'
    risk=size*lots*(distance+2*slip)+fee
    required=Decimal(str(settings.reward_r))*risk
    target_raw=entry+direction*(2*slip+(required+fee)/(size*lots))
    target=(target_raw/tick).to_integral_value(rounding=ROUND_CEILING if long else ROUND_FLOOR)*tick
    margin=size*lots*entry/settings.leverage
    if margin>balance-fee:
        return None,'illustrative_margin'
    return dict(direction=direction,entry_price=float(entry+direction*slip),reference_entry=float(entry),
        stop_loss=float(stop),take_profit=float(target),lots=float(lots),planned_risk=float(risk),risk_budget=float(budget),
        entry_fee=float(fee) if settings.charge_cost_allowance else 0.,cost_reserve=float(fee),
        slip=float(slip),contract_size=float(size),margin_estimate=float(margin),
        trigger='both' if (signal['pullback_long'] or signal['pullback_short']) and (signal['breakout_long'] or signal['breakout_short']) else 'pullback' if signal['pullback_long'] or signal['pullback_short'] else 'breakout'),''


def protective_fill(plan: dict, bar: pd.Series, ambiguity: str, *, opening_only: bool = False) -> tuple[float,str,bool] | None:
    direction=plan['direction']
    shift=float(bar.spread)*.00001 if direction==-1 else 0.
    opening, high, low=float(bar.open)+shift,float(bar.high)+shift,float(bar.low)+shift
    stop,target=plan['stop_loss'],plan['take_profit']
    if (opening<=stop if direction==1 else opening>=stop):
        return opening-direction*plan['slip'],'stop_gap',False
    if (opening>=target if direction==1 else opening<=target):
        return target-direction*plan['slip'],'target_gap',False
    if opening_only:
        return None
    stop_hit=low<=stop if direction==1 else high>=stop
    target_hit=high>=target if direction==1 else low<=target
    if not stop_hit and not target_hit:
        return None
    target_first=target_hit and (not stop_hit or ambiguity=='target_first')
    return (target if target_first else stop)-direction*plan['slip'],'target' if target_first else 'stop',bool(stop_hit and target_hit)


def replay_account(data: pd.DataFrame, signals: dict[int,dict], start: pd.Timestamp, end: pd.Timestamp,
                   settings: ReplaySettings, *, plan_builder=None) -> dict:
    """Compound closed cashflows, retain open marks and one position. No lookahead signals."""
    if plan_builder is not None and not callable(plan_builder):
        raise TypeError('plan_builder must be callable or None.')
    build_plan=make_plan if plan_builder is None else plan_builder
    balance=settings.initial_balance
    active=None
    previous_exit=None
    inventory_blocked=0
    trades,curve,rejections=[],[],[]
    peak=settings.initial_balance
    curve.append(dict(time_utc=start.isoformat(),balance=balance,equity=balance,drawdown_percent=0.,position_open=False))

    def accrue(until):
        nonlocal balance
        if active is None or until<=active['last_cost_time']:
            return
        units=rollover_units(active['last_cost_time'],until)
        cost=money(settings.financing_per_001lot_unit*active['lots']/.01*units)
        balance=money(balance-cost)
        active['financing_cost']=money(active['financing_cost']+cost)
        active['rollover_units']+=units
        active['last_cost_time']=until

    def close(fill,stamp):
        nonlocal balance,active,previous_exit
        price,reason,ambiguous=fill
        gross=money(active['direction']*(price-active['entry_price'])*100000*active['lots'])
        balance=money(balance+gross)
        net=money(gross-active['entry_fee']-active['financing_cost'])
        trade={k:v for k,v in active.items() if k!='last_cost_time'}
        trade.update(status='closed',exit_time_utc=stamp.isoformat(),exit_price=price,exit_reason=reason,
                     ambiguous_exit=ambiguous,gross_pnl=gross,net_pnl=net,realized_r=net/active['planned_risk'],
                     balance_after_exit=balance,holding_hours_upper_bound=(stamp-pd.Timestamp(active['entry_time_utc'])).total_seconds()/3600)
        trades.append(trade)
        previous_exit=stamp
        active=None

    for i in np.flatnonzero((data.index>=start)&(data.index+pd.Timedelta(hours=1)<=end)):
        stamp=data.index[i]
        bar=data.iloc[i]
        if active is not None:
            accrue(stamp)
            gap=protective_fill(active,bar,settings.ambiguity,opening_only=True)
            if gap:
                close(gap,stamp)
        if active is not None and int(i) in signals:
            inventory_blocked+=1
        if active is None and int(i) in signals and (previous_exit is None or stamp>previous_exit):
            plan,reason=build_plan(signals[int(i)],float(bar.open),balance,settings)
            if plan is None:
                rejections.append(dict(time_utc=stamp.isoformat(),reason=reason,balance=balance))
            else:
                balance_before=balance
                balance=money(balance-plan['entry_fee'])
                active=dict(plan,entry_time_utc=stamp.isoformat(),signal=signals[int(i)],balance_before_entry=balance_before,
                            financing_cost=0.,rollover_units=0,last_cost_time=stamp)
        bar_end=stamp+pd.Timedelta(hours=1)
        if active is not None:
            fill=protective_fill(active,bar,settings.ambiguity)
            if fill:
                exit_stamp=stamp if fill[1].endswith('_gap') else bar_end-pd.Timedelta(microseconds=1)
                accrue(exit_stamp)
                close(fill,exit_stamp)
            else:
                accrue(bar_end)
        floating=0.
        if active is not None:
            mark=float(bar.close)+(float(bar.spread)*.00001 if active['direction']==-1 else 0.)
            floating=money(active['direction']*(mark-active['entry_price'])*100000*active['lots'])
        equity=money(balance+floating)
        peak=max(peak,equity)
        drawdown=100*(equity/peak-1)
        curve.append(dict(time_utc=bar_end.isoformat(),balance=balance,equity=equity,drawdown_percent=drawdown,position_open=active is not None))
    if len(curve)==1:
        raise ValueError('No completed H1 bars in the requested account period.')
    open_record=None
    if active is not None:
        open_record={k:v for k,v in active.items() if k!='last_cost_time'}
        open_record.update(status='open_at_data_end',observed_at_utc=curve[-1]['time_utc'],unrealized_price_pnl=money(curve[-1]['equity']-balance))
    values=[t['net_pnl'] for t in trades]
    wins=[v for v in values if v>0]
    losses=[v for v in values if v<0]
    open_cost=0. if open_record is None else open_record['entry_fee']+open_record['financing_cost']
    if not math.isclose(balance,settings.initial_balance+sum(values)-open_cost,abs_tol=1e-7,rel_tol=0):
        raise ValueError('Account cashflows failed reconciliation.')
    summary=dict(config_id=settings.config_id,initial_balance=settings.initial_balance,final_balance=balance,
        final_equity=curve[-1]['equity'],equity_return_percent=100*(curve[-1]['equity']/settings.initial_balance-1),
        completed_trades=len(trades),open_positions=int(active is not None),wins=len(wins),losses=len(losses),
        breakevens=sum(v==0 for v in values),win_rate_percent=100*len(wins)/len(values) if values else None,
        net_completed_trade_pnl=money(sum(values)),mean_completed_r=sum(t['realized_r'] for t in trades)/len(trades) if trades else None,
        expectancy_usd=sum(values)/len(values) if values else None,
        profit_factor=sum(wins)/-sum(losses) if losses else None,
        max_h1_close_equity_drawdown_percent=-min(p['drawdown_percent'] for p in curve),
        total_modeled_entry_allowances=money(sum(t['entry_fee'] for t in trades)+(open_record['entry_fee'] if open_record else 0)),
        total_planning_cost_reserves=money(sum(t['cost_reserve'] for t in trades)+(open_record['cost_reserve'] if open_record else 0)),
        total_financing_charge=money(sum(t['financing_cost'] for t in trades)+(open_record['financing_cost'] if open_record else 0)),
        entry_risk_rejections=len(rejections),inventory_blocked_signals=inventory_blocked,ambiguous_exits=sum(t['ambiguous_exit'] for t in trades),
        first_mark_utc=curve[0]['time_utc'],last_mark_utc=curve[-1]['time_utc'])
    return dict(summary=summary,trades=trades,open_position=open_record,equity_curve=curve,rejections=rejections)


def choose_on_training(results: list[dict], minimum_trades: int = 50) -> str | None:
    eligible=[r['summary'] for r in results if r['summary']['completed_trades']>=minimum_trades and
              r['summary']['mean_completed_r'] is not None and r['summary']['mean_completed_r']>0 and
              r['summary']['final_equity']>r['summary']['initial_balance']]
    if not eligible:
        return None
    eligible.sort(key=lambda r:(-r['mean_completed_r'],r['max_h1_close_equity_drawdown_percent'],r['config_id']))
    return eligible[0]['config_id']
