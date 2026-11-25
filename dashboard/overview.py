"""Home page of saved workspace evidence; never opens a broker connection."""
from __future__ import annotations

from html import escape
import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from dashboard import monitor_control
from dashboard import btc_control
from dashboard.setup_lab import catalog, execution_history
from dashboard.setup_catalog import saved_history
from dashboard.ui import navigate, section_header, style_chart


def saved_summary(root):
    """Read small saved summaries only, preserving unknown/stale distinctions."""
    root=Path(root)
    journal={}
    try:
        value=json.loads((root/'data/processed/trade_journal.json').read_text(encoding='utf-8'))
        if isinstance(value,dict): journal=value
    except (OSError,ValueError): pass
    datasets=[];seen=set()
    for path,info,_ in catalog(root):
        meta=info['metadata']; key=(meta['source'],meta['instrument'],meta['timeframe'])
        if key in seen: continue
        seen.add(key)
        datasets.append(dict(Source=meta['source'],Instrument=meta['instrument'],Timeframe=meta['timeframe'],
            Candles=info.get('rows'),last_open_utc=info.get('last_open_utc'),
            acquired_at_utc=meta.get('acquired_at_utc')))
    current = saved_history(root)
    has_current = not current.empty
    decisions = current.loc[current.technical_signal.eq(True)] if has_current else execution_history(root)
    return dict(journal=journal,datasets=datasets,decisions=decisions,
                decision_profile='five-setup' if has_current else 'legacy A/B')


def overview_view(root):
    section_header('Workspace overview','A clear view of your research',
        'Check monitoring health, read your five executable setups per market, and keep actual DEMO trades separate from research signals.')
    state=monitor_control.snapshot(Path(root))
    btc=btc_control.snapshot(Path(root),check_other=False)
    evidence=saved_summary(root)
    summary=evidence['journal'].get('strategy_summary') or {}
    cols=st.columns(4)
    cols[0].metric('DEMO session','Active' if state['active'] or btc['active'] else 'Unverified' if state['state']=='UNVERIFIED' or btc['state']=='UNVERIFIED' else 'Inactive',
        help='Active requires a verified DEMO account, live monitor, fresh heartbeat and unexpired permission.')
    completed=summary.get('completed')
    cols[1].metric('Completed strategy trades','Unknown' if completed is None else str(completed),
        help='Saved actual strategy journal. Execution tests are excluded.')
    net=summary.get('realized_net_pnl_usd')
    cols[2].metric('Recorded strategy result','Unknown' if net is None else f'${net:+,.2f}',
        help='Saved realized net results, including recorded manual strategy exits. This is not the current account equity.')
    cols[3].metric('Saved research datasets',len(evidence['datasets']),help='Latest snapshots per source, instrument and timeframe.')
    if evidence['journal'].get('generated_at_utc'):
        stamp=pd.to_datetime(evidence['journal']['generated_at_utc'],utc=True,errors='coerce')
        generated=stamp.tz_convert('Asia/Manila').strftime('%d %b %Y, %I:%M %p Manila') if pd.notna(stamp) else 'at an unavailable time'
        st.caption(f'Journal metrics are saved observations, generated {generated}. They are not a live balance or equity feed.')
    st.write('')
    st.button('Open BTC research & DEMO',key='home_btc',on_click=navigate,args=('BTC research & DEMO',),width='stretch')
    cards=[('01 / SETUPS','Understand your five setups','Read EUR/USD and Gold execution rules, inspect their decisions and compare the separate seven research patterns.','Open setups & research','Seven-setup research lab'),
           ('02 / EXECUTION','Understand the latest decision','See the monitored candle, entry checks and the reason an entry was accepted or blocked.','Inspect monitor','Live monitor'),
           ('03 / RESULTS','Review actual DEMO trades','Inspect verified entries, exits and booked results. Research signals are kept separate.','Open trade journal','Actual trade journal')]
    for col,(number,title,copy,label,page) in zip(st.columns(3),cards):
        with col.container(border=True):
            st.markdown(f'<div class="pq-card-icon">{escape(number)}</div><div class="pq-card-title">{escape(title)}</div>'
                        f'<p class="pq-card-copy">{escape(copy)}</p>',unsafe_allow_html=True)
            st.button(label,key=f'home_{page}',on_click=navigate,args=(page,),width='stretch')
    st.write('')
    left,right=st.columns([1.2,1])
    with left:
        with st.container(border=True):
            st.subheader('Why entries were blocked')
            decisions=evidence['decisions']
            profile=evidence['decision_profile']
            if decisions.empty:
                st.caption(f'No technical trigger decisions are saved for the {profile} profile. The monitor records all five checks during an active session; see the setup lab for no-trigger decisions too.')
            else:
                fresh=decisions.loc[decisions.fresh.eq(True)]
                st.caption(f'{len(fresh)} fresh technical candidates recorded across saved {profile} observations. This is accumulated history, not today’s count. Overlapping signals can share one candle.')
                if not fresh.empty:
                    reason_names={
                        'Minimum lot exceeds the 2% budget with reserves; skip this setup.':'Minimum lot exceeds risk budget',
                        'Gold target unavailable: insufficient_1r_target_room':'Insufficient room for target',
                        'Fresh Gold quote leaves less than the frozen 1.2 ATR stop floor.':'Stop distance too small at fresh quote',
                        'Spread plus slippage allowance exceeds 10% of the stop distance; skip this setup.':'Spread and slippage too high'}
                    counts=fresh.reason.fillna('No reason recorded').map(lambda reason:reason_names.get(reason,reason)).value_counts().rename_axis('Reason').reset_index(name='Candidates')
                    fig=px.bar(counts,x='Candidates',y='Reason',orientation='h',color_discrete_sequence=['#f0ba68'])
                    fig.update_layout(showlegend=False,yaxis_title=None,xaxis_title='Recorded candidates')
                    st.plotly_chart(style_chart(fig,280),width='stretch')
                    with st.expander('View recorded decisions'):
                        st.dataframe(decisions.sort_values('decision_time_utc',ascending=False),hide_index=True,width='stretch')
                else: st.caption('Saved candidates were not fresh enough to count as current entry opportunities.')
    with right:
        with st.container(border=True):
            st.subheader('Saved market data')
            st.caption('Independent provider snapshots. Dates below describe saved coverage, not live quotes.')
            if not evidence['datasets']:
                st.caption('No research datasets yet. Open the research lab to refresh MT5 data or import another provider.')
            for dataset in evidence['datasets']:
                stamp=pd.to_datetime(dataset['last_open_utc'],utc=True,errors='coerce')
                clock=stamp.tz_convert('Asia/Manila').strftime('%d %b %Y, %I:%M %p') if pd.notna(stamp) else 'Unknown'
                st.markdown(f"**{dataset['Instrument']} · {dataset['Timeframe']}**")
                st.caption(f"{dataset['Source']} · Last candle opened {clock} Manila")
            st.button('Browse market charts',on_click=navigate,args=('Market data & dates',),width='stretch')
    with st.expander('A quick guide to this workspace'):
        st.markdown('''**Monitoring** watches for a valid, affordable setup during an authorized DEMO session. An active session does not guarantee an entry.

**Five executable setups per market** are eligible under an activated Gold/EURUSD DEMO session. Their readable rules and saved signal/blocked-entry history are in the setup lab’s first tab. More eligible patterns do not guarantee a trade.

**Research signals** are historical rule matches. The separate seven research rules do not send orders or calculate win rates. Weekday and regime trigger counts describe observations, not proven winning conditions.

**Actual trade journal** contains recorded broker fills and realized results. Its timestamps show when that evidence was last saved.

**Historical replay** is the original A-only simulation. Its assumptions and outcomes are separate from both research signals and actual trades.''')
