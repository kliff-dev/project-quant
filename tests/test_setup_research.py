"""Offline checks of causal features, risk sizing and historical screening."""

import numpy as np
import pandas as pd
import pytest

from analytics.setup_research import Contract, eligible_lot, opportunity_study, screen_level, signal_features, validate_bars, wilder_atr


@pytest.fixture
def contract():
    return Contract('EURUSDm', 100000, .00001, .00001, .01, .01)


def bars(rows=2700):
    index = pd.date_range('2024-01-01', periods=rows, freq='h', tz='UTC')
    close = 1.05 + np.arange(rows) * .00001 + .0003 * np.sin(np.arange(rows) / 7)
    return pd.DataFrame({'open': close-.00002, 'high': close+.0003, 'low': close-.0003,
                         'close':close,'spread':9,'tick_volume':100,'real_volume':0},index=index)


def test_wilder_arithmetic_seed_and_gap_true_range():
    data = pd.DataFrame({'high':[12.,13.,15.,16.], 'low':[10.,11.,13.,14.], 'close':[11.,12.,14.,15.]})
    actual = wilder_atr(data,3)
    assert actual.iloc[:2].isna().all()
    assert actual.iloc[2] == pytest.approx(7/3)
    assert actual.iloc[3] == pytest.approx((7/3*2+2)/3)


@pytest.mark.parametrize('kind',['duplicate','naive','ohlc','spread','nonfinite','unsorted','offhour'])
def test_reject_bad_historical_inputs(kind):
    data = bars(10)
    if kind == 'duplicate': data.index = pd.DatetimeIndex([data.index[0]] + list(data.index[:-1]))
    if kind == 'naive': data.index = data.index.tz_localize(None)
    if kind == 'ohlc': data.iloc[0,data.columns.get_loc('low')] = 10
    if kind == 'spread': data.iloc[0,data.columns.get_loc('spread')] = -1
    if kind == 'nonfinite': data.iloc[0,data.columns.get_loc('close')] = float('inf')
    if kind == 'unsorted': data = data.iloc[::-1]
    if kind == 'offhour': data.index = data.index + pd.Timedelta(minutes=1)
    with pytest.raises(ValueError): validate_bars(data)


def test_higher_timeframe_and_signals_cannot_see_future():
    data = bars()
    cut = 2538  # Deliberately end inside a four-hour bucket.
    prefix = signal_features(data.iloc[:cut])
    changed = data.copy()
    changed.iloc[cut:,changed.columns.get_indexer(['open','high','low','close'])] += .3
    complete = signal_features(changed).iloc[:cut]
    columns = ['atr14','ema20','trend_long','trend_short','pullback_long','pullback_short','breakout_long','breakout_short','low5','high5']
    pd.testing.assert_frame_equal(prefix[columns], complete[columns])


def test_warmup_requires_600_completed_h4_bars():
    result = signal_features(bars())
    early = result.loc[result.decision_time < pd.Timestamp('2024-04-10',tz='UTC')]
    assert not early.trend_long.any() and not early.trend_short.any()
    assert result.trend_long.any()


def test_minimum_lot_is_rejected_instead_of_rounded_up(contract):
    assert eligible_lot(2.5,400,contract) == 0
    assert eligible_lot(2.5,200,contract) == .01
    assert eligible_lot(5,200,contract) == .01  # Existing project cap stays effective.
    assert eligible_lot(5,200,contract,cap=.1) == .02


def test_lot_lattice_anchored_to_minimum():
    contract = Contract('TEST',1,.01,.01,.03,.02)
    assert eligible_lot(8,100,contract,cap=.1) == .07


@pytest.mark.parametrize('direction',[-1,1])
def test_stop_risk_and_target_use_contract_and_correct_quote_side(contract,direction):
    level = screen_level(contract=contract,direction=direction,next_bid=1.1,spread_price=.00009,
                         atr=.001,low5=1.098,high5=1.102,budget=2.5)
    assert direction*(level['entry']-level['stop']) > 0
    assert direction*(level['target']-level['entry']) >= 2*level['stop_distance']-1e-10
    assert level['planned_stop_loss'] <= 2.5+1e-10
    assert level['minimum_lot_stop_loss'] == pytest.approx(1000*(level['stop_distance']+contract.tick))
    assert level['entry'] == pytest.approx(1.1001 if direction == 1 else 1.09999)


def test_gold_contract_loss_and_affordability():
    gold = Contract('XAUUSDm',100,.001,.001,.01,.01)
    level = screen_level(contract=gold,direction=1,next_bid=2000,spread_price=.16,
                         atr=10,low5=1980,high5=2010,budget=2.5)
    assert level['minimum_lot_stop_loss'] > 20
    assert not level['risk_fits'] and level['selected_lot'] == 0


def test_stress_costs_do_not_increase_affordable_lot(contract):
    args=dict(contract=contract,direction=1,next_bid=1.1,spread_price=.00009,
              atr=.001,low5=1.098,high5=1.102,budget=2.5)
    base=screen_level(**args)
    stress=screen_level(**args,spread_multiplier=2,slippage_ticks=2)
    assert stress['estimated_cost_fraction'] > base['estimated_cost_fraction']
    assert stress['selected_lot'] <= base['selected_lot']


def test_screening_keeps_one_combined_candidate_and_is_not_a_trade_ledger(contract):
    data = bars()
    position = next(i for i in range(2401,len(data)-1) if data.index[i].day_of_week < 5 and data.index[i].hour == 10)
    prior_high = data.high.iloc[position-20:position].max()
    opening = data.close.iloc[position-1]
    close = prior_high + .002
    data.iloc[position,data.columns.get_indexer(['open','high','low','close'])] = [opening,close+.0003,opening-.0003,close]
    result = opportunity_study(data,contract)
    assert not result.empty
    combined=result.loc[result.family=='combined']
    key=['decision_time','scenario','capital','risk_percent']
    assert not combined.duplicated(key).any()
    assert result.loc[result.eligible,'planned_stop_loss'].le(result.loc[result.eligible,'budget']+1e-9).all()
    assert not {'pnl','win_rate','equity','exit_price'}.intersection(result.columns)
