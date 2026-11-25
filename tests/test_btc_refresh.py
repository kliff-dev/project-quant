"""Saved BTC acquisition permits explicit gaps while rejecting corrupt data."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace as R
from unittest.mock import Mock
import json
import shutil

import pandas as pd
import pytest

from engine.gold_connector import PINNED_BROKER,PINNED_IDENTITY
from scripts.refresh_btc_data import refresh
from scripts.research_btc import load_archive

ROOT=Path(__file__).resolve().parents[1]


def source():
    index=pd.date_range('2026-11-01',periods=310,freq='h',tz='UTC').delete(300)
    rows=[dict(time=int(t.timestamp()),open=60000.,high=60100.,low=59900.,close=60050.,spread=640,tick_volume=1,real_volume=0) for t in index]
    info=R(name='BTCUSDm',currency_base='BTC',currency_profit='USD',chart_mode=0,trade_contract_size=1.,
        point=.01,volume_min=.01,volume_max=200.,volume_step=.01,trade_tick_size=.01,digits=2)
    api=R(TIMEFRAME_H1=1,ORDER_TYPE_BUY=0,ORDER_TYPE_SELL=1,symbol_info=lambda s:info,
        copy_rates_range=lambda s,t,start,end:[r for r in rows if start.timestamp()<=r['time']<=end.timestamp()],
        symbol_info_tick=lambda s:R(bid=60000.,ask=60006.4),order_calc_profit=Mock(return_value=1.),
        order_send=Mock(side_effect=AssertionError('Read-only refresh cannot send orders')),
        order_check=Mock(side_effect=AssertionError('Read-only refresh cannot check orders')))
    account=dict(demo_verified=True,trade_mode=0,account_type='DEMO',currency='USD',broker=PINNED_BROKER,server=PINNED_IDENTITY[1])
    connector=R(terminal_api=api,verify_pinned_account=lambda:account,account_identity=PINNED_IDENTITY)
    return rows,api,lambda **kw:nullcontext(connector),index[-1]+pd.Timedelta(hours=1,minutes=30)


def test_bootstrap_and_incremental_refresh_preserve_and_disclose_missing_hours(tmp_path):
    rows,api,factory,now=source()
    first=refresh(tmp_path,connector_factory=factory,now=now)
    assert first['known_missing_h1_candles']==1 and first['new_missing_h1_candles']==1
    assert first['history_truncated_at_start'] and first['quality_warnings']
    second=refresh(tmp_path,connector_factory=factory,now=now)
    assert second['known_missing_h1_candles']==1 and second['new_missing_h1_candles']==0
    assert second['csv_sha256']==first['csv_sha256']
    assert api.order_send.call_count==api.order_check.call_count==0


@pytest.mark.parametrize('change',['duplicate','invalid_price','bad_profit'])
def test_invalid_acquisition_never_publishes_a_dataset(tmp_path,change):
    rows,api,factory,now=source()
    if change=='duplicate': rows.append(rows[-1])
    elif change=='invalid_price': rows[-1]['close']=-1
    else: api.order_calc_profit.return_value=float('nan')
    with pytest.raises(ValueError): refresh(tmp_path,connector_factory=factory,now=now)
    assert not (tmp_path/'data/research/btc/BTCUSDm_H1.csv').exists()


@pytest.mark.parametrize('key,value',[('buy_profit','NaN'),('sell_profit','Infinity'),('volume',True),('price_change_usd','NaN')])
def test_research_rejects_nonfinite_or_boolean_native_profit_evidence(tmp_path,key,value):
    source_path=ROOT/'data/research/btc'
    shutil.copy2(source_path/'BTCUSDm_H1.csv',tmp_path/'BTCUSDm_H1.csv')
    meta=json.loads((source_path/'manifest.json').read_text());meta['profit_calculation_verification'][key]=value
    (tmp_path/'manifest.json').write_text(json.dumps(meta))
    with pytest.raises(ValueError,match='verified USD-linear'): load_archive(tmp_path)
