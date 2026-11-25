"""Offline date/history behavior and safe desktop runtime lifecycle."""
from pathlib import Path
import hashlib
import json
import os
import socket

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from dashboard.history import market_slice, date_bounds, journal_slice, booked_history
from desktop import runtime
from desktop.verification import PAGES, verify_pages
from dashboard.research_model import ARCHIVES
from test_gold_strategy import bars

ROOT=Path(__file__).resolve().parents[1]


def test_manila_inclusive_dates_do_not_drop_evening_utc_rows():
    index=pd.date_range('2026-11-12T15:00Z',periods=27,freq='h')
    frame=pd.DataFrame({'close':range(27)},index=index)
    chosen=market_slice(frame,'2026-11-13','2026-11-13')
    assert chosen.index[0]==pd.Timestamp('2026-11-12T16:00Z')
    assert chosen.index[-1]==pd.Timestamp('2026-11-13T15:00Z')
    assert len(chosen)==24
    assert len(market_slice(frame,'2026-11-13','2026-11-13','UTC'))==18
    with pytest.raises(ValueError): date_bounds('2026-11-14','2026-11-13')


def test_journal_filters_and_booked_curve_do_not_include_open_or_unknown_results():
    trades=[dict(trade_id='one',symbol='EURUSDm',category='strategy',entry_time_utc='2026-11-12T18:00Z',
                 exit_time_utc='2026-11-13T02:00Z',status='closed',realized_net_pnl_usd=3.),
            dict(trade_id='two',symbol='XAUUSDm',category='strategy',entry_time_utc='2026-11-13T02:00Z',
                 exit_time_utc='2026-11-13T03:00Z',status='closed',realized_net_pnl_usd=-2.),
            dict(trade_id='open',symbol='EURUSDm',category='strategy',entry_time_utc='2026-11-13T02:00Z',
                 exit_time_utc=None,status='open_last_verified',realized_net_pnl_usd=None),
            dict(trade_id='bad',symbol='EURUSDm',category='execution_test',entry_time_utc='2026-11-13T02:00Z',
                 exit_time_utc='2026-11-13T03:00Z',status='closed',realized_net_pnl_usd=float('inf'))]
    selected=journal_slice(trades,'2026-11-13','2026-11-13',['EURUSDm','XAUUSDm'],['strategy'])
    assert len(selected)==3
    results=booked_history(selected)
    assert len(results)==2 and list(results.cumulative_booked_pnl)==[3.,1.]
    assert len(booked_history(trades))==2
    assert len(journal_slice(trades,'2026-11-13','2026-11-13',['EURUSDm'],['strategy']))==2


def test_workspace_selection_is_remembered_and_rejects_invalid_folder(tmp_path,monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    folder=tmp_path/'my workspace'
    (folder/'data/raw').mkdir(parents=True)
    assert runtime.save_workspace(folder)==folder.resolve()
    assert runtime.default_workspace()==folder.resolve()
    with pytest.raises(ValueError): runtime.save_workspace(tmp_path/'no dataset')


def test_seed_workspace_preserves_existing_user_data(tmp_path,monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    package=tmp_path/'package'
    seed=package/'seed_workspace/data/raw'
    seed.mkdir(parents=True)
    (seed/'sample.csv').write_text('seed')
    user=tmp_path/'local/ProjectQuant/workspace/data/raw'
    user.mkdir(parents=True)
    (user/'sample.csv').write_text('user revision')
    monkeypatch.setattr(runtime,'resources',lambda:package)
    monkeypatch.setattr(runtime,'code_root',lambda:tmp_path/'missing code')
    monkeypatch.setattr(runtime.Path,'home',lambda:tmp_path/'empty home')
    folder=runtime.default_workspace()
    assert (folder/'data/raw/sample.csv').read_text()=='user revision'


def test_market_history_page_renders_chart_and_date_controls(tmp_path,monkeypatch):
    monkeypatch.setenv('PROJECT_QUANT_ROOT',str(tmp_path))
    folder=tmp_path/'data/raw/gold_intraday_2026-11-15'
    folder.mkdir(parents=True)
    path=folder/'XAUUSDm_M5.csv'
    bars().to_csv(path,index_label='timestamp_utc')
    (folder/'manifest.json').write_text(json.dumps({'csv_sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))
    app=AppTest.from_file(str(ROOT/'dashboard/research_app.py'),default_timeout=30).run()
    app.radio(key='workspace').set_value('Market data & dates').run()
    assert not app.exception and not app.error
    market_dates=[field for field in app.date_input if field.label.startswith('Market ')]
    assert len(app.get('plotly_chart'))==1 and len(market_dates)==2 and len(app.dataframe)==1
    app.selectbox[1].set_value('UTC').run()
    assert not app.exception and not app.error


@pytest.mark.skipif(os.name!='nt',reason='Windows owned process-job lifecycle')
def test_owned_desktop_server_starts_and_stops_without_mt5(tmp_path,monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'local'))
    # Actual script runs over the server protocol catch dynamic imports that
    # a listening socket or health endpoint cannot exercise. All data is local.
    for symbol,relative in ARCHIVES.items():
        path=tmp_path/'workspace'/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        frame=bars()
        if symbol=='EURUSDm':
            frame.index=pd.date_range(end='2026-11-13T09:00Z',periods=len(frame),freq='h')
        frame.to_csv(path,index_label='timestamp_utc')
        key='csv_sha256' if symbol=='XAUUSDm' else 'sha256'
        (path.parent/'manifest.json').write_text(json.dumps({key:hashlib.sha256(path.read_bytes()).hexdigest()}))
    server=runtime.LocalServer(tmp_path/'workspace')
    try:
        url=server.start(timeout=30)
        assert url.startswith('http://127.0.0.1:') and server.process.poll() is None
        process=server.process
        pages=verify_pages(url)
        assert tuple(pages)==PAGES
        assert all(page['exceptions']==0 and page['script_finished'] for page in pages.values())
        assert pages['Market data & dates']['chart_trace_counts']==[3]
    finally:
        server.close()
    assert server.process is None and process.poll() is not None
    with socket.socket() as probe:
        assert probe.connect_ex(('127.0.0.1',server.port)) != 0
