"""Offline checks for the optional account-replay planning callback."""

from dataclasses import replace

import pandas as pd
import pytest

from analytics.account_research import ReplaySettings, make_plan, replay_account


def signal():
    return dict(pullback_long=True, pullback_short=False,
                breakout_long=False, breakout_short=False,
                atr14=.0015, low5=1.0999, high5=1.1001, spread=8)


def bars(highs):
    return pd.DataFrame(
        [dict(open=1.1, high=high, low=1.0998, close=1.1, spread=8)
         for high in highs],
        index=pd.date_range('2026-11-13T10:00:00Z', periods=len(highs), freq='h'),
    )


def test_explicit_none_preserves_default_replay_result():
    frame=bars([1.1002, 1.12, 1.1002])
    inputs=(frame, {0:signal(), 1:signal(), 2:signal()}, frame.index[0],
            frame.index[-1]+pd.Timedelta(hours=1), ReplaySettings())
    expected=replay_account(*inputs)
    actual=replay_account(*inputs, plan_builder=None)
    assert actual==expected
    assert len(actual['trades'])==1
    assert actual['open_position'] is not None


def test_custom_planner_changes_reward_preserves_metadata_and_inventory_gate():
    frame=bars([1.1002, 1.1002, 1.12])
    settings=ReplaySettings()
    seen=[]

    def build_plan(raw, bid, balance, passed_settings):
        seen.append((raw, bid, balance, passed_settings))
        plan, reason=make_plan(raw, bid, balance, replace(passed_settings, reward_r=3.))
        assert plan is not None
        plan.update(target_branch='three_r', target_reward_r=3.,
                    planner_evidence={'source':'completed_signal'})
        return plan, reason

    raw=signal()
    run=replay_account(frame, {0:raw, 1:signal(), 2:signal()}, frame.index[0],
                       frame.index[-1]+pd.Timedelta(hours=1), settings,
                       plan_builder=build_plan)
    baseline, _=make_plan(raw, 1.1, 500., settings)
    assert seen==[(raw, 1.1, 500., settings)]
    assert run['summary']['inventory_blocked_signals']==2
    assert run['summary']['completed_trades']==1
    trade=run['trades'][0]
    assert trade['take_profit']>baseline['take_profit']
    assert trade['target_branch']=='three_r'
    assert trade['target_reward_r']==3.
    assert trade['planner_evidence']=={'source':'completed_signal'}
    assert trade['realized_r']>=3.


def test_custom_planner_rejection_is_recorded_without_opening_position():
    frame=bars([1.1002])

    def reject(raw, bid, balance, settings):
        return None, 'adaptive_room_insufficient'

    run=replay_account(frame, {0:signal()}, frame.index[0],
                       frame.index[0]+pd.Timedelta(hours=1), ReplaySettings(),
                       plan_builder=reject)
    assert run['rejections']==[dict(time_utc=frame.index[0].isoformat(),
                                    reason='adaptive_room_insufficient', balance=500.)]
    assert run['summary']['entry_risk_rejections']==1
    assert run['summary']['final_balance']==500.
    assert not run['trades'] and run['open_position'] is None


def test_non_callable_planner_is_rejected():
    frame=bars([1.1002])
    with pytest.raises(TypeError, match='plan_builder must be callable or None'):
        replay_account(frame, {}, frame.index[0],
                       frame.index[0]+pd.Timedelta(hours=1), ReplaySettings(),
                       plan_builder={'reward_r':3.})
