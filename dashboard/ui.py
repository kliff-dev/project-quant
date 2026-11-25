"""Shared local-only appearance and small presentation helpers."""
from __future__ import annotations

from html import escape

import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

COLORS = ['#35d3b4', '#75a7ff', '#f0ba68', '#b39cff', '#f3819a', '#8cc9d5', '#c3d47b']
THEME_CONFIG = '''[global]
developmentMode = false
[theme]
base = "dark"
primaryColor = "#35d3b4"
backgroundColor = "#0b111b"
secondaryBackgroundColor = "#131e2d"
textColor = "#e6edf7"
font = "sans serif"
'''

CSS = '''
<style>
:root {color-scheme:dark; --pq-bg:#0b111b; --pq-panel:#111c2a; --pq-line:#263449;
 --pq-ink:#e6edf7; --pq-muted:#a8b7cd; --pq-accent:#35d3b4;}
html, body, [data-testid="stApp"] {font-family:"Segoe UI",Arial,sans-serif;}
[data-testid="stAppViewContainer"] {background:#0b111b;}
[data-testid="stHeader"] {background:rgba(11,17,27,.96); border-bottom:1px solid #1b2a3c;}
.block-container {max-width:1480px; padding-top:4rem; padding-bottom:3rem;}
[data-testid="stSidebar"] {background:#101a28; border-right:1px solid #263449;}
[data-testid="stSidebar"] .block-container {padding-top:1.4rem;}
[data-testid="stSidebarUserContent"] {padding-top:2rem;}
h1,h2,h3 {letter-spacing:-.025em; color:#eff5ff;}
h1 {font-size:2rem !important; line-height:1.2 !important;}
h2 {font-size:1.5rem !important;}
h3 {font-size:1.15rem !important;}
[data-testid="stCaptionContainer"] {color:#a8b7cd; line-height:1.5;}
[data-testid="stVerticalBlockBorderWrapper"] > div {border-radius:14px;}
[data-testid="stMetric"] {padding:16px 18px; background:#111d2c; border:1px solid #263449; border-radius:12px;}
[data-testid="stMetricLabel"] {color:#a8b7cd; font-size:.85rem;}
[data-testid="stMetricValue"] {font-size:1.8rem; color:#eff5ff; font-variant-numeric:tabular-nums;}
[data-testid="stButton"] button,[data-testid="stDownloadButton"] button {border-radius:9px; min-height:42px; font-weight:600;}
[data-testid="stButton"] button[kind="primary"] {color:#071713;}
button:focus-visible, a:focus-visible, input:focus-visible {outline:2px solid #8fdacb !important; outline-offset:3px;}
[data-testid="stTabs"] [role="tablist"] {gap:24px; border-bottom:1px solid #263449;}
[data-testid="stTabs"] [role="tab"] {font-size:.94rem; padding-bottom:14px;}
[data-testid="stExpander"] {border-color:#263449; border-radius:12px;}
[data-testid="stAlert"] {border-radius:10px;}
[data-testid="stSidebar"] [role="radiogroup"] {gap:5px;}
[data-testid="stSidebar"] [role="radiogroup"] label {padding:10px 11px; border-radius:8px; margin:0;}
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) {background:#1d3441;}
.pq-brand {display:flex; gap:12px; align-items:center; margin:0 0 30px;}
.pq-mark {width:38px; height:38px; border:1px solid #35d3b4; border-radius:10px;
 display:flex; align-items:center; justify-content:center; color:#35d3b4; font-weight:700; letter-spacing:-1px;}
.pq-brand strong {font-size:1.05rem; display:block; letter-spacing:-.02em;}
.pq-brand span {font-size:.73rem; color:#9badc5; display:block; margin-top:2px;}
.pq-eyebrow {font-size:.72rem; text-transform:uppercase; letter-spacing:.15em; font-weight:700; color:#68d8c2; margin:0 0 7px;}
.pq-section h2 {margin:0 0 6px; padding:0; font-size:1.65rem !important;}
.pq-section p {margin:0 0 20px; color:#a8b7cd; max-width:920px; line-height:1.55;}
.pq-context {display:flex; align-items:center; justify-content:space-between; gap:14px; border-bottom:1px solid #263449; padding-bottom:18px; margin-bottom:18px;}
.pq-context strong {font-size:1rem; letter-spacing:-.02em;}
.pq-context span {font-size:.75rem; color:#a8b7cd;}
.pq-badge {display:inline-block; color:#9fdfd0 !important; background:#12312f; border:1px solid #275249; border-radius:5px; padding:3px 8px; letter-spacing:.07em; font-size:.68rem !important; font-weight:700;}
.pq-card-icon {font-size:.75rem; color:#68d8c2; font-weight:700; text-transform:uppercase; letter-spacing:.1em;}
.pq-card-title {font-size:1.08rem; color:#eff5ff; font-weight:650; margin:8px 0;}
.pq-card-copy {color:#a8b7cd; font-size:.91rem; line-height:1.55; min-height:66px;}
.pq-note {color:#a8b7cd; font-size:.85rem; line-height:1.5;}
@media (max-width:850px) {
 .block-container {padding:4rem 1rem 2rem;}
 [data-testid="stMetricValue"] {font-size:1.35rem;}
 [data-testid="stMetric"] {padding:12px;}
 .pq-context {align-items:flex-start; flex-direction:column;}
 .pq-section h2 {font-size:1.4rem !important;}
 .pq-card-copy {min-height:0;}
}
</style>
'''


def apply_theme():
    st.markdown(CSS, unsafe_allow_html=True)
    pio.templates['project_quant'] = go.layout.Template(layout=dict(
        paper_bgcolor='#0b111b', plot_bgcolor='#0b111b',
        font=dict(family='Segoe UI, Arial, sans-serif',color='#c6d3e5',size=12),
        colorway=COLORS,
        xaxis=dict(gridcolor='#233045',zerolinecolor='#233045',linecolor='#263449'),
        yaxis=dict(gridcolor='#233045',zerolinecolor='#233045',linecolor='#263449'),
        legend=dict(bgcolor='rgba(0,0,0,0)',orientation='h',y=1.12,x=0),
        margin=dict(l=24,r=20,t=65,b=40),hovermode='x unified',
        hoverlabel=dict(bgcolor='#18283b',font_color='#e6edf7')))
    pio.templates.default='project_quant'


def style_chart(figure, height=None):
    figure.update_layout(template='project_quant')
    if height is not None: figure.update_layout(height=height)
    return figure


def section_header(eyebrow, title, description=''):
    st.markdown(f'<div class="pq-section"><div class="pq-eyebrow">{escape(eyebrow)}</div>'
                f'<h2>{escape(title)}</h2><p>{escape(description)}</p></div>',unsafe_allow_html=True)


def brand():
    st.markdown('<div class="pq-brand"><div class="pq-mark">PQ</div><div>'
                '<strong>Project Quant</strong><span>LOCAL RESEARCH WORKSPACE</span></div></div>',unsafe_allow_html=True)


def context_bar():
    st.markdown('<div class="pq-context"><strong>Project Quant <span>/ Research &amp; DEMO monitoring</span></strong>'
                '<span><b class="pq-badge">DEMO ONLY</b> &nbsp; All calculations run locally</span></div>',unsafe_allow_html=True)


def navigate(page):
    st.session_state['workspace']=page
