"""Offline research causality, costs, minimum lots and episode attribution."""
import numpy as np
import pandas as pd
import pytest

import scripts.research_five_setups as study
from dashboard.replay import ModelSettings, model_plan
from analytics.gold_strategy import confirmed_m15_swings
from analytics.adaptive_targets import confirmed_h4_swings
from test_five_setups import fixture_frame


def gold_signal(*, atr=2., strong=True):
    return dict(side='buy', atr14=atr, low3=3500.-atr, high3=3501., spread=100,
        decision_time_utc='2026-11-13T10:00:00+00:00', volatility_ratio=1.,
        m15_ema50=3510. if strong else 3501., m15_ema200=3500.,
        m15_ema50_slope5=1. if strong else .1, m15_atr14=3.,
        m15_swing_highs=[dict(price=3600., pivot_open_utc='2026-11-13T08:00:00Z',
            confirmed_at_utc='2026-11-13T08:45:00Z')], m15_swing_lows=[])


def data_at_entry():
    index = pd.date_range('2026-11-13T10:00Z', periods=3, freq='5min')
    return pd.DataFrame(dict(open=3500., high=3500.5, low=3499.5,
        close=3500., spread=100, tick_volume=1, real_volume=0), index=index)


def test_minimum_lot_is_not_forced_or_sized_toward_two_percent():
    settings = ModelSettings(initial_balance=500.)
    baseline, reason = model_plan('XAUUSDm', gold_signal(), 3500., 500., settings)
    assert not reason and baseline['actual_planned_risk_percent'] <= 1
    rescued, reason = model_plan('XAUUSDm', gold_signal(atr=6.), 3500., 500., settings)
    assert not reason and rescued['minimum_lot_rescue']
    assert rescued['lots'] == .01 and 1 < rescued['actual_planned_risk_percent'] < 2
    assert model_plan('XAUUSDm', gold_signal(atr=6., strong=False), 3500., 500., settings)[1] == 'minimum_lot_requires_strong_context'
    assert model_plan('XAUUSDm', gold_signal(atr=12.), 3500., 500., settings)[1] == 'minimum_lot_risk'


def test_episode_stop_first_and_all_reserved_costs_reconcile():
    plan, _ = model_plan('XAUUSDm', gold_signal(), 3500., 500., ModelSettings())
    data = data_at_entry()
    data.loc[data.index[0], 'low'] = plan['stop_loss'] - 1
    data.loc[data.index[0], 'high'] = plan['take_profit'] + 1
    result = study.episode(data, 'XAUUSDm', 0, plan, data.index[-1]+pd.Timedelta(minutes=5))
    assert result['status'] == 'closed' and result['exit_reason'] == 'stop'
    assert result['ambiguous'] is True
    assert result['net_pnl'] == pytest.approx(-plan['planned_risk'])
    assert result['net_r'] == pytest.approx(-1.)


def test_episode_does_not_use_outcomes_after_temporal_phase_boundary():
    plan, _ = model_plan('XAUUSDm', gold_signal(), 3500., 500., ModelSettings())
    data = data_at_entry()
    data.loc[data.index[1], 'high'] = plan['take_profit']+10
    end = data.index[1]
    assert study.episode(data, 'XAUUSDm', 0, plan, end)['status'] == 'open_at_phase_end'
    assert study.episode(data, 'XAUUSDm', 0, plan, end+pd.Timedelta(minutes=5))['exit_reason'] == 'target'


def test_prepare_uses_exact_completed_window_and_consecutive_next_open():
    index = pd.date_range('2026-01-01T00:00Z', periods=3003, freq='h')
    close = 1.1 + np.arange(3003)*.00001
    data = pd.DataFrame(dict(open=close-.000005, close=close, high=close+.00001,
        low=close-.00001, spread=8, tick_volume=1, real_volume=0), index=index)
    observed = []
    def evaluator(window, symbol):
        observed.append(window.copy())
        stamp = (window.index[-1]+pd.Timedelta(hours=1)).isoformat()
        setups = {key: dict(side='buy' if key == 'trend_reclaim' else None,
            decision_time_utc=stamp, reason='test') for key in study.SETUPS}
        return dict(selected_setup='trend_reclaim', selection_reason='unique_setup', setups=setups)
    result = study.prepare(data, 'EURUSDm', start=index[2999], end=index[-1]+pd.Timedelta(hours=2), evaluator=evaluator)
    assert len(observed) == 3 and len(result['records']) == 3
    assert all(len(window) == 3000 for window in observed)
    for window, record in zip(observed, result['records']):
        assert window.index[-1] < pd.Timestamp(record['decision_time_utc'])
        assert data.index[record['entry_index']] == pd.Timestamp(record['decision_time_utc'])
    broken = data.drop(index[-2])
    result = study.prepare(broken, 'EURUSDm', start=index[2999], end=index[-1]+pd.Timedelta(hours=2), evaluator=evaluator)
    assert result['screening_counts']['no_contiguous_next_open'] >= 1


def test_closed_summary_excludes_open_episodes_and_keeps_selection_attribution():
    common = dict(admitted=True, selected_by_arbitration=False, minimum_lot_rescue=False)
    rows = [dict(common, status='closed', net_pnl=2., net_r=2., net_r_financing_stress=1.9,
                 holding_hours_upper_bound=1., ambiguous=False),
            dict(common, status='closed', net_pnl=-1., net_r=-1., net_r_financing_stress=-1.2,
                 holding_hours_upper_bound=2., ambiguous=True),
            dict(common, status='open_at_phase_end'),
            dict(admitted=False, rejection_reason='cost_filter', selected_by_arbitration=True)]
    result = study.summary(rows)
    assert result['technical_opportunities'] == 4 and result['closed_episodes'] == 2
    assert result['win_rate'] == .5 and result['mean_net_r'] == .5
    assert result['open_at_phase_end'] == 1 and result['arbitration_selected'] == 1
    assert result['rejections'] == {'cost_filter': 1}
    assert not any('balance' in key or 'equity' in key for key in result)


@pytest.mark.parametrize('gold', [True, False])
@pytest.mark.parametrize('gap', [True, False])
def test_vector_pivots_equal_original_frozen_loops_including_gap_behavior(gold, gap):
    interval = '15min' if gold else '4h'
    index = pd.date_range('2026-11-01T00:00Z', periods=120, freq=interval)
    rng = np.random.default_rng(43)
    close = 100 + rng.normal(0, 1, 120)
    context = pd.DataFrame(dict(high=close+1, low=close-1), index=index)
    if gap:
        context = context.drop(index[90])
    decision = index[-1] + pd.Timedelta(interval)
    original = confirmed_m15_swings if gold else confirmed_h4_swings
    assert study.fast_swings(context, decision, gold=gold) == original(context, decision)


@pytest.mark.parametrize('symbol', ['EURUSDm', 'XAUUSDm'])
@pytest.mark.parametrize('setup_id', study.SETUPS)
@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_price_only_prefilter_does_not_omit_any_reviewed_synthetic_setup(symbol, setup_id, side):
    # These price fixtures independently reproduce the production pattern in
    # test_five_setups; the optimization must retain all twenty cases.
    assert bool(study.necessary_gate(fixture_frame(symbol, setup_id, side)).iloc[-1])


def test_mixed_context_cannot_use_native_minimum_lot_rescue():
    signal = dict(gold_signal(atr=6.), trend_long=False, trend_short=False)
    assert model_plan('XAUUSDm', signal, 3500., 500., ModelSettings())[0]['minimum_lot_rescue']
    assert study.aligned_model_plan('XAUUSDm', signal, 3500., 500., ModelSettings()) == (None, 'minimum_lot_requires_aligned_context')


def test_mixed_context_two_r_cap_preserves_stop_size_and_reserved_risk():
    signal = dict(gold_signal(), trend_long=False, trend_short=False)
    original, _ = model_plan('XAUUSDm', signal, 3500., 500., ModelSettings())
    capped, reason = study.aligned_model_plan('XAUUSDm', signal, 3500., 500., ModelSettings())
    assert not reason and original['selected_reward_r'] == 4 and capped['selected_reward_r'] == 2
    assert all(capped[key] == original[key] for key in ('lots', 'stop_loss', 'planned_risk', 'entry_price'))
    reserved_gain = (capped['take_profit']-capped['reference_entry']-2*capped['slip'])*100*capped['lots']-capped['cost_reserve']
    assert reserved_gain >= 2*capped['planned_risk']-1e-9
    assert reserved_gain < 2*capped['planned_risk']+.001*100*capped['lots']+1e-9


def test_eur_range_planner_transport_keeps_real_family_and_valid_direction():
    frame = pd.DataFrame(dict(open=1.1, close=1.1, high=1.101, low=1.099, spread=8),
        index=pd.date_range('2026-11-13T10:00Z', periods=3, freq='h'))
    signal = dict(side='buy', atr14=.002, low5=1.099, high5=1.102, spread=8,
        decision_time_utc=frame.index[0].isoformat(), volatility_ratio=1.,
        pullback_long=False, pullback_short=False, breakout_long=False, breakout_short=False,
        range_reentry_long=True, range_reentry_short=False, trend_long=False, trend_short=False,
        h4_ema50=1.1001, h4_ema200=1.1, h4_ema50_slope5=.00001, h4_atr14=.001,
        h4_swing_highs=[dict(price=1.2, pivot_open_utc='2026-11-12T00:00Z', confirmed_at_utc='2026-11-12T12:00Z')],
        h4_swing_lows=[])
    record = dict(symbol='EURUSDm', setup_id='range_reentry', entry_index=0,
        decision_time_utc=frame.index[0].isoformat(), signal=signal)
    result = study.evaluate_opportunities(frame, 'EURUSDm', [record], 500.)[0]
    assert result['admitted'] and result['setup_id'] == 'range_reentry'
    assert result['selected_reward_r'] <= 2 and result['planned_risk_percent'] <= 1
    assert signal['pullback_long'] is False  # Saved evidence is not relabeled.


def test_signal_cache_fingerprint_tracks_signal_changes_but_not_outcome_changes():
    original = 'START = 1\ndef prepare():\n    return 3\ndef episode():\n    return 4\n'
    assert study.preparation_digest(original) == study.preparation_digest(original.replace('return 4', 'return 5'))
    assert study.preparation_digest(original) != study.preparation_digest(original.replace('return 3', 'return 5'))
    assert study.preparation_digest(original) != study.preparation_digest(original.replace('START = 1', 'START = 2'))
