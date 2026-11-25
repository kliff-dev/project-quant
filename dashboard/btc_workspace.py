"""Saved BTC evidence and separate, explicit DEMO controls. No broker on load."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from analytics.btc_setups import SETUPS, scan
from dashboard import btc_control as control
from dashboard import monitor_control as common
from dashboard.ui import section_header, style_chart

SOURCES = [
    ('Technical rules / breakouts (2020)', 'https://dirkgerritsen.nl/uploads/gerritsen_et_al_2020_bitcoin_trading_rules.pdf'),
    ('Technical rules with costs (2024)', 'https://doi.org/10.1016/j.iref.2024.05.003'),
    ('Cryptocurrency momentum', 'https://www.nber.org/papers/w24877'),
]


@st.fragment(run_every='5s')
def btc_controls(root):
    root=Path(root); state=control.snapshot(root); status=state['status']
    with st.container(border=True):
        st.caption('BITCOIN · SEPARATE DEMO SESSION')
        if state['active']: st.markdown('🟢 **ACTIVE — BTC DEMO monitoring**')
        elif state['state']=='UNVERIFIED': st.markdown('🔴 **STATUS UNVERIFIED — BTC DEMO monitoring**')
        else: st.markdown('🔴 **INACTIVE — BTC DEMO monitoring**')
        st.write(state['reason'])
        activate=stop=False
        with st.expander('Manage BTC DEMO session'):
            st.caption('Experimental BTCUSDm CFD rules. A signal still needs affordable lots and all broker checks. Opening this app never starts trading.')
            st.warning('The combined local historical test lost money at both $500 and $1,000. These are experimental DEMO hypotheses, not proven profitable setups. Review Historical cost tests before activating.')
            end=common.default_end(); cols=st.columns(2)
            day=cols[0].date_input('BTC end date (Manila)',value=end.date(),key='btc_end_date',disabled=not state['can_start'])
            clock=cols[1].time_input('BTC end time (Manila)',value=end.time().replace(tzinfo=None),step=900,key='btc_end_time',disabled=not state['can_start'])
            selected=st.multiselect('BTC setups to monitor',list(SETUPS),default=list(SETUPS),format_func=lambda x:f'{x} · {SETUPS[x][0]}',key='btc_enabled_setups',disabled=not state['can_start'])
            cols=st.columns(2)
            activate=cols[0].button('Activate BTC DEMO',key='activate_btc',type='primary',width='stretch',disabled=not state['can_start'] or not selected)
            stop=cols[1].button('Stop BTC entries',key='stop_btc',width='stretch',disabled=not state['can_stop'])
            st.caption('1% planned account risk · 2R requested target · Maximum one BTC position · No failed-order retries. Fees and adverse fills can change the realized loss or reward.')
            st.caption('BTC and Gold/EURUSD sessions run separately. Stop the other session and close its positions before starting BTC. Stop or expiry leaves existing SL/TP in place.')
            if status.get('deadline_utc'): st.caption(f"Saved BTC entry deadline UTC: {status['deadline_utc']}")
            if not state['capability']['available']: st.info(state['capability']['reason'])
    if activate:
        try:
            control.request_start(root,datetime.combine(day,clock,common.MANILA),selected)
            st.info('BTC requested. Green requires a verified DEMO account and a fresh running heartbeat.');st.rerun()
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc: st.error(f'BTC activation rejected: {exc}')
    if stop:
        try:
            control.request_stop(root,status.get('run_id'));st.info('BTC stop requested; existing positions retain SL/TP.');st.rerun()
        except (OSError,ValueError,RuntimeError) as exc: st.error(f'BTC stop rejected: {exc}')


@st.cache_data(show_spinner=False,max_entries=4)
def saved_data(root,checksum,clock_hour):
    path=Path(root)/'data/research/btc/BTCUSDm_H1.csv'
    if hashlib.sha256(path.read_bytes()).hexdigest()!=checksum: raise ValueError('BTC data checksum mismatch. Refresh or restore the matching manifest.')
    frame=pd.read_csv(path,index_col=0);frame.index=pd.DatetimeIndex(pd.to_datetime(frame.index,utc=True))
    return frame,scan(frame,as_of=pd.Timestamp(clock_hour))


def actual_history(root):
    rows=[]
    for path in (Path(root)/'logs/btc_monitor/attempts').glob('*.json'):
        try:
            record=json.loads(path.read_text(encoding='utf-8')); plan=record.get('plan') or {};fill=record.get('fill') or {};closed=record.get('closed') or {}
            rows.append(dict(Setup=(plan.get('intent') or {}).get('selected_setup'),Status=record.get('status'),
                Decision=(plan.get('intent') or {}).get('decision_time_utc'),Lots=(plan.get('request') or {}).get('volume'),
                Entry=fill.get('price'),Position=fill.get('position_ticket'),Net_USD=closed.get('net_pnl_usd'),Evidence=path.name))
        except (OSError,ValueError,TypeError): continue
    return pd.DataFrame(rows)


def refresh_data(root):
    root=Path(root).resolve();python=root/'.venv/Scripts/python.exe';script=root/'scripts/refresh_btc_data.py'
    if not python.is_file() or not script.is_file(): raise ValueError('The configured Windows workspace Python and refresh script are required.')
    with common.external_dll_search():
        result=subprocess.run([str(python),'-B',str(script),'--root',str(root)],cwd=root,env=common.clean_environment(),
            stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode: raise RuntimeError((result.stderr or result.stdout)[-2000:])
    saved_data.clear()


def btc_view(root):
    root=Path(root)
    section_header('Bitcoin research & DEMO','Four hypotheses. Visible evidence.',
        'Research-backed families with frozen hourly adaptations. Published results do not prove these CFD setups profitable.')
    st.caption('Exact instrument: Exness BTCUSDm · Bitcoin/USD CFD · H1 bid candles · Local deterministic rules, without a trained AI model.')
    if st.button('Refresh saved BTC data',key='refresh_btc_data',help='Read-only MT5 history download. This does not start trading.'):
        try:
            with st.spinner('Retrieving completed BTC candles from MT5…'): refresh_data(root)
            st.success('BTC saved history updated. Saved replays remain tied to their original data checksum.')
        except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc: st.error(f'BTC data refresh failed: {exc}')
    tabs=st.tabs(['Signals & chart','Historical cost tests','Research & rules','Actual BTC DEMO history'])
    with tabs[0]:
        try:
            meta=json.loads((root/'data/research/btc/manifest.json').read_text(encoding='utf-8'))
            frame,result=saved_data(str(root),meta['csv_sha256'],pd.Timestamp.now(tz='UTC').floor('h').isoformat())
            cols=st.columns(3);cols[0].metric('Saved H1 candles',f'{len(frame):,}');cols[1].metric('Historical raw signals',f"{len(result['signals']):,}")
            cols[2].metric('Latest data','Stale' if result['stale'] else 'Recent')
            st.caption(f"Saved coverage UTC: {meta['first_open_utc']} → {meta['last_close_utc']}. Raw signals are not orders or wins. Volume is broker tick activity.")
            st.dataframe(result['latest'],hide_index=True,width='stretch')
            counts=result['signals'].groupby('setup_id').size().reindex(list(SETUPS),fill_value=0).rename('Raw signals').reset_index()
            st.plotly_chart(style_chart(px.bar(counts,x='setup_id',y='Raw signals',color_discrete_sequence=['#14b8a6']),250),width='stretch')
            dates=st.columns(2)
            first=dates[0].date_input('BTC chart start UTC',value=max(frame.index[0].date(),(frame.index[-1]-pd.Timedelta(days=14)).date()),min_value=frame.index[0].date(),max_value=frame.index[-1].date(),key='btc_chart_first')
            last=dates[1].date_input('BTC chart end UTC',value=frame.index[-1].date(),min_value=frame.index[0].date(),max_value=frame.index[-1].date(),key='btc_chart_last')
            chosen=st.multiselect('Show BTC signal families',list(SETUPS),default=list(SETUPS),key='btc_chart_setups')
            view=frame.loc[(frame.index.date>=first)&(frame.index.date<=last)].tail(3000)
            signals=result['signals']; signals=signals.loc[signals.setup_id.isin(chosen)&(pd.to_datetime(signals.signal_open_utc,utc=True).dt.date>=first)&(pd.to_datetime(signals.signal_open_utc,utc=True).dt.date<=last)]
            if not view.empty:
                fig=go.Figure(go.Candlestick(x=view.index,open=view.open,high=view.high,low=view.low,close=view.close,name='BTCUSDm bid'))
                for window,color in [(20,'#38bdf8'),(100,'#f5b942')]: fig.add_scatter(x=view.index,y=frame.close.rolling(window).mean().reindex(view.index),name=f'SMA{window}',line_color=color)
                for key in chosen:
                    stamps=pd.to_datetime(signals.signal_open_utc,utc=True)
                    points=signals.loc[signals.setup_id.eq(key)&stamps.between(view.index[0],view.index[-1])]
                    fig.add_scatter(x=points.signal_open_utc,y=points.close,mode='markers',name=key,marker=dict(size=8,symbol='diamond'))
                fig.update_layout(xaxis_rangeslider_visible=False,yaxis_title='USD / BTC');st.plotly_chart(style_chart(fig,440),width='stretch')
            st.dataframe(signals.iloc[::-1].head(300),hide_index=True,width='stretch')
            st.download_button('Download BTC signal history',signals.to_csv(index=False),'btc_signals.csv','text/csv')
            st.caption('Markers are signal-candle OPEN times; decisions become available one hour later. Chart displays at most 3,000 candles; export retains all matching signals.')
        except (OSError,ValueError,KeyError,TypeError) as exc: st.warning(f'No verified BTC archive available: {exc}')
    with tabs[1]:
        st.warning('Research simulation, not verified all-cost broker profit. Historical financing/commission are unverified; the baseline assumes zero for those costs. Spread and adverse slippage are included.')
        try:
            path=sorted((root/'reports/market_analysis').glob('btc_*/run.json'))[-1]
            run=json.loads(path.read_text(encoding='utf-8'))
            cols=st.columns(3)
            period=cols[0].selectbox('BTC test period',list(dict.fromkeys(x['period'] for x in run['scenarios'])))
            capital=cols[1].selectbox('BTC test capital',sorted(set(x['initial_balance'] for x in run['scenarios'])))
            cost=cols[2].selectbox('BTC cost scenario',list(dict.fromkeys(x['cost_case'] for x in run['scenarios'] if x['period']==period and x['initial_balance']==capital)))
            rows=[]
            rejection_rows=[]
            for item in run['scenarios']:
                if (item['period'],item['initial_balance'],item['cost_case'])!=(period,capital,cost): continue
                s=item['summary'];rows.append(dict(Setup=item['setup_id'],Closed=s['closed_trades'],Win_rate=s.get('win_rate'),
                    Net_USD=s.get('closed_net_pnl_usd'),Expectancy_R=s.get('expectancy_r'),Close_mark_drawdown=s.get('max_drawdown_close_mark_fraction')))
                for reason,count in s.get('rejections',{}).items(): rejection_rows.append(dict(Setup=item['setup_id'],Reason=reason,Count=count))
            st.dataframe(pd.DataFrame(rows),hide_index=True,width='stretch')
            st.caption('Independent setup tests overlap in time and cannot be added together. Combined uses one position and deterministic arbitration. Tiny samples and zero admitted trades do not demonstrate an edge.')
            with st.expander('Why historical entries were rejected'):
                st.dataframe(pd.DataFrame(rejection_rows),hide_index=True,width='stretch')
                st.caption('B3 had no affordable entries at either tested capital. Its signal history is still tracked; the engine never shrinks its stop or raises risk to force an order.')
            for item in run.get('limitations',[]): st.caption(item)
            st.caption(f"Frozen replay data SHA256: {run['source']['csv_sha256']}. Refreshing history does not rerun these tests.")
            st.download_button('Download BTC replay summary',path.read_text(encoding='utf-8'),'btc_replay_summary.json','application/json')
        except (OSError,ValueError,KeyError,IndexError,TypeError): st.info('No saved cost replay is available in this workspace yet.')
    with tabs[2]:
        for key,(name,rule) in SETUPS.items():
            with st.container(border=True): st.markdown(f'**{key} · {name}**');st.write(rule)
        st.write('All four use completed H1 bars, Wilder ATR14, a structural stop outside the previous five candles with a 0.2 ATR buffer, and at least 1.5 ATR distance. The requested target is 2× the entry-to-stop distance. A fresh quote may widen the stop outward; lots are recomputed and rounded down to the broker step.')
        st.write('The 1% budget uses the smaller of balance and equity, with a 5% budget reserve and $2/BTC adverse slippage allowance per side. Minimum lots exceeding that budget are rejected. Opposing signals skip; same-direction signals use B1 → B4 priority, without falling back after a risk rejection.')
        st.caption('The papers test different parameters, horizons, markets and exits. Some use long/cash rather than CFD shorts. Our exact H1 rules, symmetric shorts and 2R stops are additional hypotheses, not paper replications.')
        for title,url in SOURCES: st.markdown(f'[{title}]({url})')
    with tabs[3]:
        history=actual_history(root)
        if history.empty: st.info('No BTC DEMO attempts have been recorded. Building or opening this page does not authorize an order.')
        else:
            st.dataframe(history,hide_index=True,width='stretch');st.download_button('Download actual BTC attempt history',history.to_csv(index=False),'btc_demo_history.csv','text/csv')
        st.caption('Only the BTC engine’s durable attempt ledger is shown here. Filled and closed statuses require broker order/deal/position evidence; reserved or uncertain attempts are not counted as successful trades.')
