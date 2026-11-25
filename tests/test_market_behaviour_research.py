"""Path-ordering checks for the research study; synthetic offline quotes only."""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('market_behaviour_study',ROOT/'scripts/research_market_behaviour.py')
study=importlib.util.module_from_spec(spec)
spec.loader.exec_module(study)


def sample():
    data=pd.DataFrame([
        dict(open=100.,high=101.,low=99.,close=100.,spread=200),
        dict(open=100.,high=103.,low=95.,close=101.,spread=200),
        dict(open=101.,high=101.,low=95.,close=96.,spread=200),
    ],index=pd.date_range('2026-06-01',periods=3,freq='5min',tz='UTC'))
    signal=dict(atr14=1.,m15_ema50_slope5=-.3,m15_atr14=1.,m5_ema20=100.,m5_ema50=100.1,volatility_ratio=1.)
    plan=dict(direction=-1,reference_entry=100.,entry_price=99.95,stop_loss=102.,take_profit=96.,
        lots=.01,planned_risk=2.35,actual_planned_risk_percent=.235,entry_fee=.25,
        slip=.05,contract_size=100.,selected_reward_r=2)
    return data,signal,plan


def test_stop_first_ambiguity_and_excursion_excludes_exit_candle():
    data,signal,plan=sample()
    result=study.episode(data,'XAUUSDm',0,signal,plan)
    assert result['status']=='stop'
    assert result['ambiguous'] is True
    assert result['mfe_before_exit_bar_atr']==pytest.approx(.8)
    assert result['mae_before_exit_bar_atr']==pytest.approx(1.2)
    assert result['net_pnl']==pytest.approx(-2.35)


def test_short_barrier_checks_use_ask():
    data,signal,plan=sample()
    data.loc[:,'high']=101.9
    data.loc[:,'low']=99.
    result=study.episode(data,'XAUUSDm',0,signal,plan)
    assert result['status']=='stop'  # Bid 101.9 is below SL; Ask 102.1 crosses it.


def test_recovery_cannot_use_the_stop_candle_itself():
    data,signal,plan=sample()
    data=data.iloc[:2]
    result=study.episode(data,'XAUUSDm',0,signal,plan)
    assert result['target_after_stop'] is False
    assert result['recovery_horizon_complete'] is False
    data,signal,plan=sample()
    assert study.episode(data,'XAUUSDm',0,signal,plan)['target_after_stop'] is True


def test_archive_end_is_censored_instead_of_a_closed_trade():
    data,signal,plan=sample()
    result=study.episode(data.iloc[:1],'XAUUSDm',0,signal,plan)
    assert result['status']=='open_at_archive_end'
    assert result['net_pnl'] is None


def test_research_ablation_restores_function_references_after_failure():
    before_prepare=study.replay.prepare_current_signals
    before_strong=study.replay.strong_context
    with pytest.raises(RuntimeError):
        with study.cached_research({}, {}, True):
            assert study.replay.strong_context is not before_strong
            raise RuntimeError('deliberate test failure')
    assert study.replay.prepare_current_signals is before_prepare
    assert study.replay.strong_context is before_strong


def test_win_interval_keeps_small_samples_uncertain():
    assert study.wilson(0,0) is None
    low,high=study.wilson(1,1)
    assert low<.21 and high==pytest.approx(1.)
