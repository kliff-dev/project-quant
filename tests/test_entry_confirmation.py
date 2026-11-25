"""Confirmation timing, expiry, evidence and gap checks with offline candles."""
from copy import deepcopy

import pandas as pd
import pytest

from analytics import entry_confirmation as module


def setup(monkeypatch,symbol='XAUUSDm',side='buy'):
    gold=symbol=='XAUUSDm'
    n=module.ROWS[symbol]
    frame=pd.DataFrame(dict(open=[100.]*n,high=[101.]*n,low=[99.5]*n,
        close=[100.5]*n,spread=[20]*n,tick_volume=[10]*n,real_volume=[0]*n),
        index=pd.date_range(end='2026-11-13T10:00Z',periods=n,freq='5min' if gold else 'h'))
    frame.iloc[-1,frame.columns.get_loc('close')]=101.1
    frame.iloc[-1,frame.columns.get_loc('high')]=101.2
    if side=='sell':
        old=frame.copy()
        for c in ['open','close']:
            frame[c]=200-old[c]
        frame['high'],frame['low']=200-old.low,200-old.high
    prefix='m5' if gold else 'h1'
    seed_time=frame.index[-2]
    current=dict(atr14=1.,volatility_ratio=1.,latest_bar_range=1.7,
        trend_long=side=='buy',trend_short=side=='sell',
        **{f'{prefix}_rsi14':55. if side=='buy' else 45.,
           f'{prefix}_ema20':100.,f'{prefix}_ema50':100.})
    def kernel(data):
        value=deepcopy(current)
        previous=data.index[-1]==seed_time
        if gold:
            value['side']=side if previous else None
        else:
            value.update(trend_reclaim_long=previous and side=='buy',trend_reclaim_short=previous and side=='sell')
        return value
    monkeypatch.setattr(module,'gold_features' if gold else 'eur_features',kernel)
    return frame,current


@pytest.mark.parametrize('symbol',['XAUUSDm','EURUSDm'])
@pytest.mark.parametrize('side',['buy','sell'])
def test_confirmation_uses_next_closed_bar_and_expires_after_it(monkeypatch,symbol,side):
    frame,_=setup(monkeypatch,symbol,side)
    result=module.confirmed_features(frame,symbol)
    assert result['side']==side and result['entry_confirmation_mode']==module.MODE
    evidence=result['entry_confirmation']
    interval=pd.Timedelta(minutes=5 if symbol=='XAUUSDm' else 60)
    assert pd.Timestamp(evidence['seed_decision_time_utc'])+interval==pd.Timestamp(result['decision_time_utc'])
    assert result['input_window_rows']==len(frame)
    later=frame.iloc[[-1]].copy()
    later.index+=interval
    assert module.confirmed_features(pd.concat([frame,later]),symbol)['side'] is None
    assert module.confirmed_features(pd.concat([frame,later]).iloc[:-1],symbol)==result


@pytest.mark.parametrize('symbol',['XAUUSDm','EURUSDm'])
def test_touch_without_close_beyond_is_not_confirmation(monkeypatch,symbol):
    frame,_=setup(monkeypatch,symbol)
    frame.iloc[-1,frame.columns.get_loc('close')]=frame.high.iloc[-2]
    result=module.confirmed_features(frame,symbol)
    assert result['side'] is None and result['reason']=='confirmation_failed_or_expired'


@pytest.mark.parametrize('change',['trend','rsi','ema','shock','volatility'])
def test_confirmation_cannot_bypass_current_eligibility(monkeypatch,change):
    frame,current=setup(monkeypatch)
    key,value={'trend':('trend_long',False),'rsi':('m5_rsi14',75.),
        'ema':('m5_ema20',200.),'shock':('latest_bar_range',3.),'volatility':('volatility_ratio',2.)}[change]
    current[key]=value
    assert module.confirmed_features(frame,'XAUUSDm')['side'] is None


def test_gap_and_short_window_are_rejected(monkeypatch):
    frame,_=setup(monkeypatch)
    assert module.confirmed_features(frame.iloc[1:],'XAUUSDm')['reason']=='confirmation_warmup_missing'
    broken=frame.copy()
    broken.index=broken.index[:-2].append(broken.index[-2:]+pd.Timedelta(minutes=5))
    assert module.confirmed_features(broken,'XAUUSDm')['reason']=='confirmation_recent_gap'
