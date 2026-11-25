"""Offline evidence for the independent research scanner and app page."""
from pathlib import Path
import json

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal
from streamlit.testing.v1 import AppTest

from analytics.setup_lab import SETUPS, scan, validate, register, read_dataset, summarize
from dashboard.setup_lab import catalog, execution_history
from scripts.refresh_setup_lab import import_duka

ROOT=Path(__file__).resolve().parents[1]
META=dict(source='sample provider',instrument='XAUUSD',timeframe='M5',price_basis='bid',instrument_type='Synthetic test quotes')


def sample(periods=2200, freq='5min'):
    rng=np.random.default_rng(74)
    index=pd.date_range('2026-06-01T00:00Z',periods=periods,freq=freq)
    close=2000+np.cumsum(rng.normal(.01,.6,periods))
    opening=np.r_[close[0]-.1,close[:-1]]
    return pd.DataFrame(dict(open=opening,high=np.maximum(opening,close)+.2,
        low=np.minimum(opening,close)-.2,close=close,spread=20),index=index)


def test_prefix_causality_and_stable_event_ids():
    data=sample()
    full=scan(data,META,as_of=data.index[-1]+pd.Timedelta(minutes=5))
    for n in (500,1100,1900):
        end=data.index[n-1]+pd.Timedelta(minutes=5)
        prefix=scan(data.iloc[:n],META,as_of=end)
        expected=full['signals'].loc[pd.to_datetime(full['signals'].decision_time_utc,utc=True)<=end].reset_index(drop=True)
        assert_frame_equal(prefix['signals'],expected)
        # Supplying the future input with a cutoff must give identical output.
        assert_frame_equal(scan(data,META,as_of=end)['signals'],prefix['signals'])
    assert full['signals'].event_id.is_unique
    assert not full['signals'].execution_authority.any()
    assert sum(summarize(full['signals']).triggers)==len(full['signals'])


@pytest.mark.parametrize('change', ['duplicate','unsorted','naive','invalid_price','geometry','unaligned'])
def test_bad_input_is_rejected(change):
    data=sample()
    if change=='duplicate': data.index=pd.DatetimeIndex([data.index[0]]+list(data.index[:-1]))
    elif change=='unsorted': data=data.iloc[::-1]
    elif change=='naive': data.index=data.index.tz_localize(None)
    elif change=='invalid_price': data.iloc[20,data.columns.get_loc('close')]=np.inf
    elif change=='geometry': data.iloc[20,data.columns.get_loc('low')]=10000
    else: data.index=data.index+pd.Timedelta(seconds=1)
    with pytest.raises(ValueError): validate(data,'M5')


def test_pandas_microsecond_timestamp_resolution_is_not_mistaken_for_misalignment():
    data=sample();data.index=data.index.as_unit('us')
    assert len(validate(data,'M5'))==len(data)


def test_missing_and_flat_candles_block_the_next_five_decisions():
    data=sample()
    missing=data.index[1000]
    changed=data.drop(missing)
    result=scan(changed,META,as_of=data.index[-1]+pd.Timedelta(minutes=5))
    signals=pd.to_datetime(result['signals'].decision_time_utc,utc=True)
    assert not ((signals>missing)&(signals<=missing+pd.Timedelta(minutes=30))).any()
    flat=data.copy();flat.loc[missing,['open','high','low','close']]=data.loc[missing,'close']
    filtered=scan(flat,META,as_of=data.index[-1]+pd.Timedelta(minutes=5))
    assert filtered['excluded_flat_bars']==1
    assert_frame_equal(filtered['signals'],result['signals'])


def test_incomplete_bar_excluded_and_stale_is_explicit():
    data=sample();end=data.index[-1]
    result=scan(data,META,as_of=end)
    assert result['completed_bars']==len(data)-1
    assert result['end_utc']==end.isoformat()
    assert scan(data,META,as_of=end+pd.Timedelta(days=2))['stale']
    assert len(result['latest'])==7


def test_dataset_roundtrip_provenance_dedup_and_tampering(tmp_path):
    data=sample()
    p=register(tmp_path,data,dict(META,acquired_at_utc='first'))
    assert register(tmp_path,data,dict(META,acquired_at_utc='second'))==p
    loaded,info=read_dataset(p)
    np.testing.assert_allclose(loaded.close,data.close)
    other=register(tmp_path,data,dict(META,source='other feed'))
    assert other!=p and len(catalog(tmp_path))==2
    csv=p.parent/info['csv'];csv.write_bytes(csv.read_bytes()+b'bad')
    with pytest.raises(ValueError,match='checksum'): read_dataset(p)


def test_opening_range_requires_complete_london_hour_and_one_break_per_direction():
    data=sample(5000)
    result=scan(data,META,as_of=data.index[-1]+pd.Timedelta(minutes=5))
    rows=result['signals'].loc[lambda d:d.setup_id.eq('R6')].copy()
    assert not rows.empty
    local=pd.to_datetime(rows.decision_time_utc,utc=True).dt.tz_convert('Europe/London')
    assert local.dt.hour.between(9,12).all()
    rows['date']=local.dt.date
    assert rows.groupby(['date','side']).size().max()==1
    # Drop one opening bar on each day: opening-range signals must disappear.
    local_index=data.index.tz_convert('Europe/London')
    incomplete=data.loc[~((local_index.hour==8)&(local_index.minute==15))]
    bad=scan(incomplete,META,as_of=data.index[-1]+pd.Timedelta(minutes=5))
    assert not bad['signals'].setup_id.eq('R6').any()


def test_import_requires_timestamp_and_preserves_source_and_missing_costs(tmp_path):
    d=sample(500); raw=d.copy();raw.insert(0,'timestamp',d.index.as_unit('ms').asi8)
    csv=tmp_path/'duka.csv';raw.to_csv(csv,index=False)
    path=import_duka(tmp_path,csv,'XAUUSD','M5')
    _,info=read_dataset(path)
    assert info['metadata']['source']=='Dukascopy'
    assert 'unavailable' in info['metadata']['cost_quality']


def test_empty_history_and_warmup():
    d=sample(100);r=scan(d,META,as_of=d.index[-1]+pd.Timedelta(minutes=5))
    assert r['signals'].empty and len(summarize(r['signals']))==7
    assert set(r['latest'].reason)=={'warmup_under_250_bars'}
    assert len(execution_history(Path('missing')))==0


def test_research_page_renders_filters_counts_charts_and_signal_history(tmp_path,monkeypatch):
    monkeypatch.setenv('PROJECT_QUANT_ROOT',str(tmp_path))
    d=sample(5000);register(tmp_path,d,META)
    app=AppTest.from_file(str(ROOT/'dashboard/research_app.py'),default_timeout=45).run()
    app.radio(key='workspace').set_value('Seven-setup research lab').run()
    assert not app.exception and not app.error
    assert len(app.get('plotly_chart'))>=2
    assert any(metric.label=='Orders authorized by this lab' and metric.value=='0' for metric in app.metric)
    assert any(table.value.shape[0]==7 for table in app.dataframe)
    for field in app.date_input:
        if field.label.startswith('Research '): field.set_value(pd.Timestamp('2026-06-10').date())
    app.run()
    assert not app.exception and not app.error
