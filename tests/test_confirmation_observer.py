"""A forward researcher has no execution capability or authority."""
import json
from pathlib import Path
from types import SimpleNamespace as Record
from unittest.mock import Mock

import pandas as pd
import pytest

from engine import confirmation_observer as observer
from test_entry_confirmation import setup


def rig(tmp_path,monkeypatch,symbol='XAUUSDm'):
    frame,_=setup(monkeypatch,symbol)
    for name in ['config/entry_confirmation_spec.json','analytics/entry_confirmation.py','analytics/activation_signals.py']:
        path=tmp_path/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('immutable source')
    interval=pd.Timedelta(minutes=5 if symbol=='XAUUSDm' else 60)
    candle=(frame.index[-1]+interval).isoformat()
    forming=frame.iloc[[-1]].copy()
    forming.index+=interval
    forming[['open','high','low','close']]+=500
    full=pd.concat([frame,forming])
    rates=full.assign(time=full.index.as_unit('s').astype('int64')).to_records(index=False)
    methods={
        'copy_rates_from_pos':Mock(return_value=rates),
        'symbol_info_tick':Mock(return_value=Record(bid=100.,ask=100.1,time_msc=int(pd.Timestamp(candle).timestamp()*1000))),
        'symbol_info':Mock(return_value=Record(name=symbol,chart_mode=0,trade_contract_size=100.,point=.001,
            trade_tick_size=.001,volume_min=.01,volume_step=.01)),
        'account_info':Mock(return_value=Record(balance=500.,equity=500.,currency='USD'))}
    api=Record(TIMEFRAME_M5=5,TIMEFRAME_H1=60,**methods)
    account=dict(demo_verified=True,trade_mode=0,account_type='DEMO',currency='USD')
    connector=Record(terminal_api=api,verify_pinned_account=Mock(return_value=account),account_identity=('test-demo','test-server'))
    watch=observer.ConfirmationObserver(tmp_path,connector)
    watch.sources=observer.source_evidence(tmp_path)
    return watch,api,connector,candle,frame


@pytest.mark.parametrize('symbol',['XAUUSDm','EURUSDm'])
def test_snapshot_is_closed_reproducible_once_per_bar_and_restart(tmp_path,monkeypatch,symbol):
    watch,api,connector,candle,frame=rig(tmp_path,monkeypatch,symbol)
    record=watch.observe(symbol,candle,position_open=True)
    assert record['status']=='observed' and record['execution_authority'] is False
    saved=pd.read_csv(tmp_path/record['input_file'],index_col=0)
    assert len(saved)==len(frame) and saved.close.iloc[-1]==frame.close.iloc[-1]
    assert record['confirmed_reclaim']['side']=='buy'
    assert record['position_open_when_observed'] is True
    assert record['funds']['balance']==500.
    assert watch.observe(symbol,candle)==record
    restarted=observer.ConfirmationObserver(tmp_path,connector)
    assert restarted.observe(symbol,candle)==record
    assert api.copy_rates_from_pos.call_count==1
    assert not hasattr(api,'order_send') and not hasattr(api,'order_check')
    events=list(watch.folder.glob('events/*.json'))
    assert len(events)==1 and json.loads(events[0].read_text())['execution_authority'] is False


def test_unverified_account_or_history_does_not_create_signal_event(tmp_path,monkeypatch):
    watch,api,connector,candle,_=rig(tmp_path,monkeypatch)
    connector.verify_pinned_account.side_effect=ValueError('not pinned demo')
    with pytest.raises(ValueError,match='not pinned demo'):
        watch.observe('XAUUSDm',candle)
    assert not list(watch.folder.glob('events/*.json'))
    api.copy_rates_from_pos.assert_not_called()


def test_source_change_blocks_before_reading_market_data(tmp_path,monkeypatch):
    watch,api,_,candle,_=rig(tmp_path,monkeypatch)
    (tmp_path/'config/entry_confirmation_spec.json').write_text('changed')
    with pytest.raises(ValueError,match='source changed'):
        watch.observe('XAUUSDm',candle)
    api.copy_rates_from_pos.assert_not_called()


def test_history_behind_requested_close_is_not_mislabeled(tmp_path,monkeypatch):
    watch,api,_,candle,_=rig(tmp_path,monkeypatch)
    api.copy_rates_from_pos.return_value=api.copy_rates_from_pos.return_value[:-2]
    with pytest.raises(ValueError,match='does not end'):
        watch.observe('XAUUSDm',candle)
    assert not list(watch.folder.glob('events/*.json'))
