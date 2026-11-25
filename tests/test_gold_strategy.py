"""Offline Gold contract, causal context, adaptive geometry and replay tests."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from analytics.candidate_signals import wilder_atr, wilder_rsi
from analytics.gold_strategy import (SPEC_SHA256, GoldReplaySettings, complete_m15,
    confirmed_m15_swings, make_plan, prepare_signals, protective_fill, replay_account, select_target,
    session_status, validate_m5, window_features)


def bars(side='buy'):
    index = pd.date_range(end='2026-11-13 09:55Z', periods=1200, freq='5min')
    close = 3500+np.arange(1200)*.003+.05*np.sin(np.arange(1200)*.35)
    close[-1] = close[-2]+.02
    opening = np.r_[close[0]-.002, close[:-1]]
    if side == 'sell':
        close, opening = 7000-close, 7000-opening
    return pd.DataFrame(dict(open=opening, high=np.maximum(opening, close)+.01,
        low=np.minimum(opening, close)-.01, close=close, spread=20,
        tick_volume=1, real_volume=0), index=index)


def signal(side='buy', ratio=1., strong=True):
    return dict(side=side, atr14=2., low3=3499., high3=3501., spread=100,
        decision_time_utc='2026-11-13T10:00:00Z', volatility_ratio=ratio,
        m15_ema50=3510. if strong else 3501., m15_ema200=3500.,
        m15_ema50_slope5=1. if strong else .1, m15_atr14=3.,
        m15_swing_highs=[dict(price=3600., pivot_open_utc='2026-11-13T08:00:00Z',confirmed_at_utc='2026-11-13T08:45:00Z')],
        m15_swing_lows=[dict(price=3400., pivot_open_utc='2026-11-13T08:00:00Z',confirmed_at_utc='2026-11-13T08:45:00Z')])


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_completed_reclaim_and_indicators_match_independent_series(side):
    frame = bars(side)
    item = window_features(frame)
    assert item['side'] == side and item['reason'] == 'valid_entry_setup'
    assert item['atr14'] == pytest.approx(wilder_atr(frame).iloc[-1])
    assert item['m5_rsi14'] == pytest.approx(wilder_rsi(frame.close).iloc[-1])
    assert item['m5_ema20'] == pytest.approx(frame.close.ewm(span=20,adjust=False).mean().iloc[-1])
    assert item['completed_m15_rows'] == 400
    assert pd.Timestamp(item['last_completed_m15_open_utc'])+pd.Timedelta(minutes=15) <= pd.Timestamp(item['decision_time_utc'])


def test_bounded_window_and_future_bars_cannot_change_past_snapshot():
    frame = bars()
    first = window_features(frame)
    prior = frame.iloc[:10].copy()
    prior.index = frame.index[0]-pd.to_timedelta(np.arange(10,0,-1)*5,unit='min')
    prior[['open','high','low','close']] += 1000
    assert window_features(pd.concat([prior,frame])) == first
    future = frame.iloc[-3:].copy()
    future.index = pd.date_range(frame.index[-1]+pd.Timedelta(minutes=5),periods=3,freq='5min')
    future[['open','high','low','close']] += 900
    extended = pd.concat([frame,future])
    assert window_features(extended.loc[extended.index < pd.Timestamp(first['decision_time_utc'])]) == first


@pytest.mark.parametrize('side',['buy','sell'])
def test_next_open_signal_ignores_entry_candle_future_prices(side):
    frame=bars(side)
    future=frame.iloc[-1:].copy()
    future.index += pd.Timedelta(minutes=5)
    future[['open','high','low','close']] += 1000
    signals,_=prepare_signals(pd.concat([frame,future]))
    assert signals[1200] == window_features(frame)
    future[['open','high','low','close']] -= 2000
    other,_=prepare_signals(pd.concat([frame,future]))
    assert other[1200] == signals[1200]


def test_incomplete_and_unclosed_m15_are_excluded():
    frame = bars()
    decision = frame.index[-1]+pd.Timedelta(minutes=5)
    context = complete_m15(frame,decision)
    assert len(context) == 400
    assert len(complete_m15(frame.drop(frame.index[-2]),decision)) == 399
    assert len(complete_m15(frame,decision-pd.Timedelta(minutes=1))) == 399


def test_recent_gap_and_insufficient_warmup_reject():
    frame = bars()
    assert window_features(frame.iloc[:-1])['reason'] == 'insufficient_1200_m5_warmup'
    replacement = frame.iloc[:1].copy()
    replacement.index -= pd.Timedelta(minutes=5)
    broken = pd.concat([replacement,frame.drop(frame.index[-3])])
    assert window_features(broken)['reason'] == 'recent_m5_gap'


@pytest.mark.parametrize('mutation', ['duplicate','unsorted','naive','off_grid','nan','negative','bad_ohlc','boolean'])
def test_invalid_input_rejects(mutation):
    frame = bars()
    if mutation == 'duplicate': frame = pd.concat([frame,frame.iloc[-1:]])
    if mutation == 'unsorted': frame = frame.iloc[::-1]
    if mutation == 'naive': frame.index = frame.index.tz_localize(None)
    if mutation == 'off_grid': frame.index += pd.Timedelta(seconds=1)
    if mutation == 'nan': frame.loc[frame.index[-1],'close'] = np.nan
    if mutation == 'negative': frame.loc[frame.index[-1],'spread'] = -1
    if mutation == 'bad_ohlc': frame.loc[frame.index[-1],'high'] = 1
    if mutation == 'boolean': frame['spread'] = True
    with pytest.raises(ValueError): validate_m5(frame)


@pytest.mark.parametrize('time,reason', [
    ('2026-11-13T07:00Z',''), ('2026-11-13T16:55Z',''),
    ('2026-11-13T17:00Z','outside_london_session'),
    ('2026-11-14T10:00Z','outside_london_session'),
    ('2026-11-13T13:15Z','routine_us_clock_blackout_not_event_calendar'),
    ('2026-11-13T14:00Z',''),
    ('2026-07-10T12:15Z','routine_us_clock_blackout_not_event_calendar'),
    ('2026-07-10T13:00Z',''),
    ('2026-11-13T14:45Z','routine_us_clock_blackout_not_event_calendar'),
    ('2026-11-13T15:15Z','')])
def test_sessions_clock_proxy_boundaries_and_dst(time,reason):
    assert session_status(pd.Timestamp(time)) == reason


def test_pivot_only_observable_after_both_right_candles_closed():
    index = pd.date_range('2026-11-13T07:00Z',periods=5,freq='15min')
    context = pd.DataFrame(dict(high=[2,3,5,3,2],low=[1,.8,.5,.8,1]),index=index)
    assert not confirmed_m15_swings(context,index[-1])['m15_swing_highs']
    pivots = confirmed_m15_swings(context,index[-1]+pd.Timedelta(minutes=15))
    assert pivots['m15_swing_highs'][0]['price'] == 5
    assert pivots['m15_swing_lows'][0]['price'] == .5
    assert not confirmed_m15_swings(context.drop(index[1]),index[-1]+pd.Timedelta(minutes=15))['m15_swing_highs']


@pytest.mark.parametrize('side',['buy','sell'])
def test_gold_contract_lots_and_reserved_4r_are_not_forex_contract(side):
    plan, reason = make_plan(signal(side),3500.,500.)
    assert not reason and plan['selected_reward_r'] == 4
    assert plan['contract_size'] == 100 and plan['lots'] == .01
    assert plan['risk_budget'] == 5 and plan['planned_risk'] <= 5
    assert abs(plan['reference_entry']-plan['stop_loss']) >= 2.4
    loss = 100*plan['lots']*(abs(plan['reference_entry']-plan['stop_loss'])+2*plan['slip'])+plan['cost_reserve']
    gain = 100*plan['lots']*(abs(plan['take_profit']-plan['reference_entry'])-2*plan['slip'])-plan['cost_reserve']
    assert loss == pytest.approx(plan['planned_risk']) and gain >= 4*loss-1e-8


@pytest.mark.parametrize('side',['buy','sell'])
@pytest.mark.parametrize('r',[1,2,3,4])
def test_all_target_branches_exact_rounded_room(side,r):
    raw = signal(side)
    baseline,_ = make_plan(raw,3500.,500.)
    direction = baseline['direction']
    plans={}
    for value in (1,2,3,4):
        distance = 2*baseline['slip']+(value*baseline['planned_risk']+baseline['cost_reserve'])/(100*baseline['lots'])
        plans[value]=dict(reference_entry=baseline['reference_entry'],take_profit=baseline['reference_entry']+direction*distance)
    price = plans[r]['take_profit']+direction*.4-(.1 if direction==-1 else 0)
    key='m15_swing_highs' if direction==1 else 'm15_swing_lows'
    raw[key][0]['price']=price
    selected,evidence,reason=select_target(raw,plans,.1)
    assert not reason and selected==r
    assert evidence['target_room_flags']=={str(value):value<=r for value in (1,2,3,4)}


@pytest.mark.parametrize('ratio,expected',[(.75,4),(1.5,4),(1.50001,2),(1.75,2),(.74999,None),(1.75001,None)])
def test_volatility_adaptation_limits_without_changing_account_risk(ratio,expected):
    plan, reason = make_plan(signal(ratio=ratio),3500.,500.)
    assert (plan['selected_reward_r'] if plan else None)==expected
    if plan: assert plan['risk_budget']==5
    else: assert reason=='volatility_filter'


def test_weak_trend_caps_at_2r_and_missing_room_skips():
    assert make_plan(signal(strong=False),3500.,500.)[0]['selected_reward_r']==2
    raw=signal();raw['m15_swing_highs']=[]
    assert make_plan(raw,3500.,500.)==(None,'missing_confirmed_obstacle')


def test_minimum_lot_does_not_force_tighter_stop_or_excess_risk():
    raw=signal();raw.update(atr14=10.,low3=3490.)
    assert make_plan(raw,3500.,500.)==(None,'minimum_lot_risk')
    assert raw['low3']==3490.


def test_cost_filter_and_spread_cap_are_price_units():
    raw=signal();raw['spread']=1001
    assert make_plan(raw,3500.,500.)==(None,'spread_filter')
    raw['spread']=500
    assert make_plan(raw,3500.,500.)==(None,'cost_filter')


@pytest.mark.parametrize('side',['buy','sell'])
def test_stop_first_ambiguity_and_gap_fill(side):
    plan,_=make_plan(signal(side),3500.,500.)
    bid_open=3500.
    bar=pd.Series(dict(open=bid_open,high=max(plan['stop_loss'],plan['take_profit'])+1,
        low=min(plan['stop_loss'],plan['take_profit'])-1,close=bid_open,spread=100))
    price,reason,ambiguous=protective_fill(plan,bar)
    assert reason=='stop' and ambiguous
    assert price==pytest.approx(plan['stop_loss']-plan['direction']*plan['slip'])
    bar['open']=plan['stop_loss']-plan['direction']*2-(.1 if side=='sell' else 0)
    price,reason,_=protective_fill(plan,bar)
    assert reason=='stop_gap'
    assert plan['direction']*(price-plan['stop_loss'])<0


def test_one_position_cost_accounting_and_open_mark():
    frame=bars().iloc[-3:].copy()
    frame[['open','close']]=3500.
    frame['high']=3500.2;frame['low']=3499.8;frame['spread']=100
    signals={0:signal(),1:signal()}
    run=replay_account(frame,signals,frame.index[0],frame.index[-1]+pd.Timedelta(minutes=5),GoldReplaySettings())
    assert run['closed_trades']==0 and run['open_position']
    assert run['skipped_occupied']==1 and run['ending_balance']==499.75
    assert run['ending_equity']<run['ending_balance']


def test_closed_replay_net_pnl_reconciles_cash_and_no_automatic_reentry():
    frame=bars().iloc[-3:].copy()
    frame[['open','close']]=3500.;frame['high']=3500.2;frame['low']=3499.8;frame['spread']=100
    frame.loc[frame.index[1],'low']=3490.
    run=replay_account(frame,{0:signal(),1:signal()},frame.index[0],frame.index[-1]+pd.Timedelta(minutes=5),GoldReplaySettings())
    assert run['closed_trades']==1 and run['skipped_occupied']==1
    assert run['ending_balance']==pytest.approx(500+run['net_closed_pnl'])
    assert not run['open_position'] and run['trades'][0]['net_pnl']<0


def test_zero_extra_fee_changes_cashflow_but_preserves_planning_reserve():
    a,_=make_plan(signal(),3500.,500.)
    b,_=make_plan(signal(),3500.,500.,GoldReplaySettings(charge_cost_allowance=False))
    assert a['planned_risk']==b['planned_risk'] and a['take_profit']==b['take_profit']
    assert a['entry_fee']==.25 and b['entry_fee']==0


def test_frozen_spec_hash_and_no_outcome_fitted_rules():
    path=Path(__file__).resolve().parents[1]/'config/gold_strategy_spec.json'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==SPEC_SHA256
    assert json.loads(path.read_text())['risk_percent']==1


@pytest.mark.parametrize('kwargs',[{'risk_percent':2},{'max_lots':1},{'slippage_price':-1},{'initial_balance':False},{'leverage':True}])
def test_invalid_replay_settings_reject(kwargs):
    with pytest.raises(ValueError): GoldReplaySettings(**kwargs)
