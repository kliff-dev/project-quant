"""Local research workspace: saved monitor evidence, causal replay and journal.

Start: python -m streamlit run dashboard/research_app.py --server.address 127.0.0.1
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(os.environ.get('PROJECT_QUANT_ROOT', Path(__file__).resolve().parents[1])).resolve()
CODE_ROOT = Path(os.environ.get('PROJECT_QUANT_CODE_ROOT', Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from dashboard.research_model import (ARCHIVES, INTERVAL, SYMBOLS, WINDOW, diagnostics,
    load_bars, local_path, monitor_health, price_charts, read_json, saved_decision)
from dashboard.replay import ModelSettings, replay_portfolio, source_fingerprints
from dashboard.history import market_slice, market_chart, journal_slice, booked_history
from dashboard.trading_controls import trading_controls
from dashboard.btc_workspace import btc_controls, btc_view
from dashboard.setup_lab import research_view, executable_setup_guide, _execution_history_table
from dashboard.setup_catalog import PROFILE as FIVE_PROFILE, history_rows
from dashboard.overview import overview_view
from dashboard.navigation import PAGES
from dashboard.ui import apply_theme, brand, context_bar, section_header, style_chart


def local_time(value):
    try:
        stamp = pd.Timestamp(value)
        return stamp.tz_convert('Asia/Manila').strftime('%d %b %Y %I:%M:%S %p Manila') if stamp.tz else 'Unknown timezone'
    except (ValueError, TypeError):
        return 'Unavailable'


@st.cache_data(show_spinner=False, max_entries=64)
def cached_decision(root, decision_json):
    item = json.loads(decision_json)
    expected_kernel = item.get('entry_timing_kernel_sha256')
    if expected_kernel and hashlib.sha256((CODE_ROOT/'analytics/activation_signals.py').read_bytes()).hexdigest() != expected_kernel:
        raise ValueError('The app and monitor use different strategy-kernel versions. Update the app before interpreting these gates.')
    return saved_decision(Path(root), item, item['symbol'])


@st.cache_data(show_spinner=False, max_entries=4)
def archive(root, symbol, fingerprint):
    return load_bars(Path(root)/ARCHIVES[symbol], symbol, expected_sha=fingerprint)


def archive_identity(symbol):
    path = ROOT/ARCHIVES[symbol]
    metadata = read_json(path.parent/'manifest.json')
    expected = metadata['csv_sha256'] if symbol == 'XAUUSDm' else metadata['sha256']
    return path, expected


def render_diagnostic(item):
    st.caption(f"Completed candle decision: {local_time(item['decision_time_utc'])}")
    st.write(f"Reproduced technical setup: **{item['side'].upper() if item['side'] else 'WAIT'}**")
    entry, trend = price_charts(item)
    charts = st.tabs(['Trend context', 'Entry detail', 'Entry checklist'])
    with charts[0]: st.plotly_chart(style_chart(trend), width='stretch')
    with charts[1]: st.plotly_chart(style_chart(entry), width='stretch')
    with charts[2]:
        st.dataframe(pd.DataFrame(item['gates']), hide_index=True, width='stretch')
        st.caption('Passed technical checks still need target room, affordable lots and broker permission. No readiness percentage is estimated.')


def recent_events(status):
    path = ROOT/'logs/monitor/events.jsonl'
    if not path.is_file():
        return []
    run_ids = set()
    activation = status.get('activation_id')
    if activation:
        for symbol in SYMBOLS:
            try:
                child = read_json(local_path(ROOT, f'logs/activations/{activation}/{symbol}_status.json'))
                if child.get('run_id'):
                    run_ids.add(child['run_id'])
            except (OSError, ValueError):
                pass
    if not run_ids:
        return []
    with path.open('rb') as stream:
        stream.seek(0, 2)
        start = max(0, stream.tell()-2_000_000)
        stream.seek(start)
        if start:
            stream.readline()
        lines = stream.read().decode('utf-8', errors='replace').splitlines()
    rows = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue  # A currently appended partial line is not a completed event.
        if event.get('run_id') not in run_ids or event.get('event') != 'candle_evaluated':
            continue
        item = event.get('decision') or {}
        rows.append(dict(Symbol=item.get('symbol'), Decision=local_time(item.get('decision_time_utc')),
                         Status=item.get('status'), Reason=item.get('reason')))
    return rows[-500:]


@st.fragment(run_every='10s')
def live_view(symbol):
    try:
        status = read_json(ROOT/'logs/monitor/status.json')
        health = monitor_health(status)
    except (OSError, ValueError, KeyError) as exc:
        st.warning(f'No readable monitor status: {exc}')
        return
    cols = st.columns(4)
    cols[0].metric('Monitor', health['state'])
    cols[1].metric('Open positions', status.get('open_position_count', 'Unknown'))
    cols[2].metric('Verified entries this activation', status.get('orders_sent', 'Unknown'))
    cols[3].metric('Heartbeat age', f"{health['age']:.0f}s" if health['age'] is not None else 'Unknown')
    if health['state'] != 'RUNNING':
        st.warning(health['explanation']+' The evidence below is a saved snapshot, not current trading status.')
    st.caption(f"Heartbeat: {local_time(status.get('heartbeat_utc'))} · Entry deadline: {local_time(status.get('deadline_utc'))}")
    policy = status.get('policy') or {}
    st.caption(f"Saved policy: {policy.get('baseline_risk_percent', '?')}% baseline / {policy.get('risk_percent', '?')}% qualified ceiling; {status.get('maximum_combined_risk_percent', '?')}% combined · Maximum {status.get('maximum_positions_per_symbol', '?')} positions per market")
    decision = ((status.get('instruments') or {}).get(symbol) or {}).get('latest_decision') or {}
    comparison = status.get('strategy_comparison') or {}
    five_profile = comparison.get('mode') == FIVE_PROFILE or decision.get('entry_setups') == FIVE_PROFILE
    if five_profile:
        st.caption('Five executable patterns for this market · A trigger still needs an affordable protected order and available account risk.')
        latest = ((comparison.get('symbols') or {}).get(symbol) or {}).get('latest') or {}
        if not latest and decision.get('setup_signals'):
            latest = dict(decision, profile=FIVE_PROFILE, execution_status=decision.get('status'),
                execution_reason=decision.get('reason'), execution_setup_id=decision.get('setup_id'))
        rows = history_rows(latest)
        if rows:
            with st.expander('Latest checks across all five setups', expanded=True):
                _execution_history_table(pd.DataFrame(rows))
        with st.expander('Read the five setup rules for this market'):
            executable_setup_guide(symbol)
    st.subheader(f"{symbol}: {decision.get('status', 'No decision')}")
    st.write(decision.get('reason', 'No evaluated candle is recorded.'))
    if decision.get('input_file'):
        try:
            item = cached_decision(str(ROOT), json.dumps(decision, sort_keys=True))
            render_diagnostic(item)
            st.caption(f"Input checksum verified: {item['source_sha256'][:16]}… · Calculations use this decision's frozen closed-bar window.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            st.error(f'Cannot reproduce this saved decision: {exc}')
    events = recent_events(status)
    if events:
        with st.expander('Recent decisions and reasons in this activation'):
            table = pd.DataFrame(events)
            st.dataframe(table.iloc[::-1], hide_index=True, width='stretch')
            st.dataframe(table.groupby(['Symbol','Status','Reason'], dropna=False).size().rename('Count').reset_index(), hide_index=True)
            st.caption('Up to 500 evaluated-candle events from the last 2 MB of the local log; polling heartbeats are not counted as setups.')
    with st.expander('Saved monitor decision details'):
        st.json(decision)


def historical_view():
    section_header('Offline simulation', 'Historical replay', 'Explore the original A-only model with explicit cost assumptions. These are simulated results, separate from actual trades.')
    st.caption('Offline simulation of the original A-only activation signals and a modeled two-market account. This replay does not model the five-setup profile, earlier A/B monitor or seven-pattern research lab. No MT5 connection or orders.')
    frames, identity = {}, {}
    try:
        for symbol in SYMBOLS:
            path, expected = archive_identity(symbol)
            frames[symbol] = archive(str(ROOT), symbol, expected)
            identity[symbol] = dict(path=str(path.relative_to(ROOT)), sha256=expected, rows=len(frames[symbol]),
                                    first_open_utc=frames[symbol].index[0].isoformat(),
                                    last_close_utc=(frames[symbol].index[-1]+INTERVAL[symbol]).isoformat())
    except (OSError, ValueError, KeyError) as exc:
        st.warning(f'Saved broker archives unavailable or invalid: {exc}')
        return
    first = max(frame.index[min(WINDOW[s], len(frame)-1)] for s, frame in frames.items())
    last = min(frame.index[-1]+INTERVAL[s] for s, frame in frames.items())
    st.caption(f'Common replay coverage after warmup: {first.isoformat()} through {last.isoformat()}. Dates below are UTC.')
    with st.form('replay_inputs'):
        cols = st.columns(3)
        capital = cols[0].number_input('Starting USD capital', min_value=100., max_value=100000., value=500., step=100.)
        start = cols[1].date_input('Start date UTC', value=max(first.date(), last.date()-timedelta(days=14)), min_value=first.date(), max_value=last.date())
        end_date = cols[2].date_input('End date UTC (inclusive)', value=last.date(), min_value=first.date(), max_value=last.date())
        cost = st.selectbox('Additional modeled costs', ['allowance_debit', 'reserve_only'],
                            format_func=lambda x: 'Debit the modeled allowance once (not a verified broker fee)' if x == 'allowance_debit' else 'Reserve for sizing only; no additional fee debit; zero swap')
        stress = st.selectbox('Adverse slippage', [1, 2], format_func=lambda x: f'{x}× base allowance')
        submitted = st.form_submit_button('Run offline replay', type='primary')
    st.caption('The first run may take a minute for a few weeks; six months can take several minutes. The independent trading monitor keeps running.')
    example = ROOT/'reports/research_dashboard/first_replay_2026-11-16.json'
    if example.is_file() and st.button('Load saved two-week example replay'):
        try:
            saved = read_json(example)
            provenance = saved.get('provenance') or {}
            if provenance.get('logic_sha256') != source_fingerprints(CODE_ROOT):
                raise ValueError('Example logic fingerprints differ from the current research model; run a new replay.')
            if any((provenance.get('sources', {}).get(s) or {}).get('sha256') != identity[s]['sha256'] for s in SYMBOLS):
                raise ValueError('Example source fingerprints differ from the loaded archives.')
            st.session_state['portfolio_replay'] = saved
        except (OSError, ValueError, KeyError) as exc:
            st.error(f'Cannot load example replay: {exc}')
    if submitted:
        begin = max(first, pd.Timestamp(start, tz='UTC'))
        finish = min(last, pd.Timestamp(end_date, tz='UTC')+pd.Timedelta(days=1))
        try:
            bar = st.progress(0., text='Replaying completed candles…')
            def progress(fraction, symbol):
                index = list(frames).index(symbol)
                bar.progress(min(.95, (index+fraction)/len(frames)*.95), text=f'Calculating {symbol} closed-bar decisions…')
            run = replay_portfolio(frames, begin, finish, ModelSettings(capital, cost, stress), progress=progress)
            run['provenance'] = dict(sources=identity, logic_sha256=source_fingerprints(CODE_ROOT),
                                     created_at_utc=pd.Timestamp.now(tz='UTC').isoformat())
            st.session_state['portfolio_replay'] = run
            bar.progress(1., text='Replay completed')
        except (ValueError, KeyError, OSError) as exc:
            st.error(f'Replay failed: {exc}')
    run = st.session_state.get('portfolio_replay')
    if run:
        st.warning('MODELED HISTORICAL RESULTS — previously inspected history; not actual DEMO returns or untouched validation.')
        st.caption(f"Displayed run: {run['start_utc']} → {run['end_utc']} · Initial ${run['initial_balance']:,.2f} · {run['settings']['cost_model']} · {run['settings']['slippage_multiplier']}× slippage")
        cols = st.columns(4)
        cols[0].metric('Modeled ending equity', f"${run['ending_equity']:,.2f}")
        cols[1].metric('Closed simulated trades', run['closed_trades'])
        cols[2].metric('Net expectancy', f"{run['net_expectancy_r']:.3f}R" if run['net_expectancy_r'] is not None else 'Not available')
        cols[3].metric('Close-marked drawdown', f"{run['maximum_close_marked_drawdown_percent']:.2f}%")
        wins = f"{run['win_rate']:.1%}" if run['win_rate'] is not None else 'Not available'
        st.caption(f"Closed-trade win rate: {wins} · Net closed P&L: ${run['net_closed_pnl']:,.2f}. These describe this sample; trade count and costs matter.")
        curve = pd.DataFrame(run['equity_curve'])
        chart = go.Figure(go.Scatter(x=curve.time_utc, y=curve.equity, name='Modeled equity'))
        chart.update_layout(height=320, xaxis_title='UTC', yaxis_title='USD')
        st.plotly_chart(style_chart(chart), width='stretch')
        st.write(f"Open simulated positions at boundary: **{len(run['open_positions'])}**. These are marked and excluded from completed-trade statistics.")
        tabs = st.tabs(['Trades', 'Rejected opportunities', 'Decision replay', 'Assumptions and export'])
        with tabs[0]:
            flat = [{k:v for k,v in trade.items() if not isinstance(v, (dict, list))} for trade in run['trades']]
            st.dataframe(pd.DataFrame(flat), hide_index=True, width='stretch')
            st.download_button('Download simulated trades CSV', pd.DataFrame(flat).to_csv(index=False), 'simulated_trades.csv', 'text/csv')
        with tabs[1]:
            rows = pd.DataFrame(run['rejections'])
            st.dataframe(rows, hide_index=True, width='stretch')
            if not rows.empty:
                st.dataframe(rows.groupby(['symbol','reason']).size().rename('Count').reset_index(), hide_index=True)
            st.json(run['technical_screening_counts'])
        with tabs[2]:
            opportunities = run['decisions']
            if opportunities:
                choice = st.selectbox('Simulated opportunity', range(len(opportunities)),
                    format_func=lambda i: f"{opportunities[i]['decision_time_utc']} · {opportunities[i]['symbol']} · {opportunities[i]['status']}")
                item = opportunities[choice]
                st.write(item['reason'])
                selected = frames[item['symbol']]
                selected = selected.loc[selected.index+INTERVAL[item['symbol']] <= pd.Timestamp(item['decision_time_utc'])]
                render_diagnostic(diagnostics(selected, item['symbol']))
                if item['plan']:
                    st.json(item['plan'])
            else:
                st.info('No technical opportunities were found in this interval. Use the candle inspector below to examine WAIT decisions.')
        with tabs[3]:
            for assumption in run['assumptions']:
                st.write('• '+assumption)
            st.json(run['provenance'])
            st.download_button('Download complete replay JSON', json.dumps(run, indent=2, allow_nan=False), 'research_replay.json', 'application/json')
    with st.expander('Inspect any historical completed candle'):
        symbol = st.selectbox('Inspector instrument', SYMBOLS, key='inspect_symbol')
        frame = frames[symbol]
        first_index, last_index = min(WINDOW[symbol]-1, len(frame)-1), len(frame)-1
        if first_index == last_index:
            index = last_index
            st.caption(f'Only one candle is available at the required warmup boundary: index {index}.')
        else:
            index = st.slider('Closed candle index', min_value=first_index, max_value=last_index,
                              value=last_index, key=f'inspect_index_{symbol}')
        st.caption('Only this candle and earlier input are supplied to the calculations; subsequent archive candles are excluded.')
        if st.button('Explain selected candle'):
            render_diagnostic(diagnostics(frame.iloc[:index+1], symbol))


def journal_view():
    section_header('Recorded broker results', 'Actual DEMO trade journal', 'Review recorded entries, exits and net results. Research triggers and simulations are excluded.')
    path = ROOT/'data/processed/trade_journal.json'
    if not path.is_file():
        st.info('No saved journal is available.')
        return
    try:
        journal = read_json(path)
    except (OSError, ValueError) as exc:
        st.error(f'Cannot read saved journal: {exc}')
        return
    st.caption(f"Saved journal generated: {local_time(journal.get('generated_at_utc'))}. This page does not refresh broker history or execute trades.")
    st.caption(journal.get('source_scope', 'Project execution records only.'))
    summary = journal.get('strategy_summary') or {}
    cols = st.columns(3)
    cols[0].metric('Completed actual strategy trades', summary.get('completed', 'Unknown'))
    cols[1].metric('Realized strategy net P&L', f"${summary.get('realized_net_pnl_usd', 0):.2f}")
    cols[2].metric('Verified broker SL/TP exits', (journal.get('strategy_rule_exit_summary') or {}).get('completed', 'Unknown'))
    assessment = journal.get('strategy_assessment')
    if assessment:
        st.info(str(assessment))
    trades = journal.get('trades', [])
    dated = [pd.Timestamp(t['entry_time_utc']).tz_convert('Asia/Manila').date() for t in trades if t.get('entry_time_utc')]
    if not dated:
        st.info('The saved journal contains no dated entries yet.')
        return
    st.subheader('Filter actual history by entry date')
    controls = st.columns(2)
    first = controls[0].date_input('First entry date (Manila)',value=min(dated),key='journal_first')
    last = controls[1].date_input('Last entry date (Manila)',value=max(dated),key='journal_last')
    names = sorted({t.get('symbol') for t in trades if t.get('symbol')})
    categories = sorted({t.get('category') for t in trades if t.get('category')})
    selected_symbols = st.multiselect('History instruments',names,default=names)
    selected_categories = st.multiselect('Trade categories',categories,default=['strategy'] if 'strategy' in categories else categories)
    try:
        selected = journal_slice(trades,first,last,selected_symbols,selected_categories)
    except ValueError as exc:
        st.error(str(exc))
        return
    history = booked_history(selected)
    if not history.empty:
        pnl = go.Figure(go.Bar(x=history.exit_time_utc.dt.tz_convert('Asia/Manila'),y=history.net_pnl,
                               name='Recorded net trade P&L',marker_color=['#32b98d' if v>=0 else '#ed6268' for v in history.net_pnl]))
        pnl.add_trace(go.Scatter(x=history.exit_time_utc.dt.tz_convert('Asia/Manila'),y=history.cumulative_booked_pnl,
                                name='Cumulative selected-trade P&L',mode='lines+markers'))
        pnl.update_layout(height=330,yaxis_title='Booked USD',xaxis_title='Exit time (Manila)')
        st.plotly_chart(style_chart(pnl),width='stretch')
        st.caption('This graph shows booked results from the selected completed trades. It is not the full account equity or drawdown curve; deposits, open trades and unrecorded broker activity are excluded.')
    st.caption(f'{len(selected)} entries match the filters; {len(history)} have recorded closed cash results. Summary metrics above refer to the entire saved journal.')
    columns = ['trade_id','symbol','setup_id','entry_setups','category','status','side','volume_lots','entry_time_manila','exit_time_manila',
               'entry_price','exit_price','initial_stop_loss','initial_take_profit','realized_net_pnl_usd','exit_reason']
    table = pd.DataFrame([{k:t.get(k) for k in columns} for t in selected],columns=columns)
    st.dataframe(table, hide_index=True, width='stretch')
    st.download_button('Download actual journal CSV', table.to_csv(index=False), 'actual_demo_journal.csv', 'text/csv')
    st.download_button('Download saved actual journal JSON', json.dumps(journal, indent=2), 'actual_demo_journal.json', 'application/json')
    st.caption('Execution tests and manual exits remain classified separately in the saved journal. Actual results are never combined with hypothetical replay trades.')


def market_data_view():
    section_header('Saved market history', 'Market data & dates', 'Browse archived candles, compare moving averages and export a date range. These charts are saved data, not live quotes.')
    symbol = st.selectbox('Market instrument',SYMBOLS,format_func=lambda s: 'Gold — XAUUSDm CFD, M5' if s=='XAUUSDm' else 'EUR/USD — EURUSDm, H1')
    zone = st.selectbox('Chart dates and timezone',['Asia/Manila','UTC'])
    try:
        path, expected = archive_identity(symbol)
        frame = archive(str(ROOT),symbol,expected)
    except (OSError,ValueError,KeyError) as exc:
        st.warning(f'Saved historical dataset unavailable: {exc}')
        return
    dates = frame.index.tz_convert(zone)
    minimum,maximum = dates[0].date(),dates[-1].date()
    controls = st.columns(2)
    first = controls[0].date_input('Market start date',value=max(minimum,maximum-timedelta(days=14)),min_value=minimum,max_value=maximum,key=f'market_first_{symbol}_{zone}')
    last = controls[1].date_input('Market end date (inclusive)',value=maximum,min_value=minimum,max_value=maximum,key=f'market_last_{symbol}_{zone}')
    try:
        selected = market_slice(frame,first,last,zone)
    except ValueError as exc:
        st.error(str(exc))
        return
    st.caption(f'Saved broker bid history · {len(frame):,} archived candles · {minimum} to {maximum} · checksum {expected[:16]}…')
    if selected.empty:
        st.info('No saved candles in this date range; market closures or archive gaps may account for the empty interval.')
        return
    st.plotly_chart(style_chart(market_chart(frame,selected,symbol,zone)),width='stretch')
    if len(selected)>5000:
        st.caption('The chart displays the latest 5,000 matching candles for responsiveness. The table download contains the full selected range.')
    st.caption('These historical-chart EMAs use the full archive as warmup; the live strategy panel uses its own bounded history. This page shows saved data, not a live price feed. Volume fields are broker tick/real-volume fields, not consolidated market volume.')
    output = selected.copy()
    output.insert(0,'timestamp_display',output.index.tz_convert(zone).astype(str))
    st.dataframe(output.tail(500),width='stretch')
    st.caption(f'{len(selected):,} matching candles; the table preview shows at most the last 500.')
    st.download_button('Download selected market data CSV',output.to_csv(index_label='timestamp_utc'),f'{symbol}_{first}_{last}.csv','text/csv')


def main():
    st.set_page_config(page_title='Project Quant | Research Workspace', page_icon='📊', layout='wide',initial_sidebar_state='expanded')
    apply_theme()
    with st.sidebar:
        brand()
        page=st.radio('Workspace',PAGES,key='workspace',label_visibility='collapsed')
        st.divider()
        st.caption('EUR/USD & Gold · DEMO only')
        st.caption('Five executable setups each for EUR/USD and Gold; seven separate research patterns. BTC has its own controls. Opening the app never starts trading.')
        with st.expander('Workspace details'):
            st.caption(f'Data folder: {ROOT}')
            if os.environ.get('PROJECT_QUANT_DESKTOP')=='1':
                st.caption('File → Open workspace changes this folder.')
    context_bar()
    controls=st.columns(2)
    with controls[0]: trading_controls(ROOT)
    with controls[1]: btc_controls(ROOT)
    st.write('')
    if page == 'Overview':
        overview_view(ROOT)
    elif page == 'Live monitor':
        section_header('DEMO execution monitor','Follow the latest decision','Check monitoring health and the saved entry evidence. A technical signal still needs to pass risk, cost and broker checks.')
        symbol = st.selectbox('Instrument', SYMBOLS, format_func=lambda s: 'Gold — XAUUSDm CFD' if s == 'XAUUSDm' else 'EUR/USD — EURUSDm')
        live_view(symbol)
    elif page == 'Seven-setup research lab':
        research_view(ROOT)
    elif page == 'BTC research & DEMO':
        btc_view(ROOT)
    elif page == 'Historical replay':
        historical_view()
    elif page == 'Market data & dates':
        market_data_view()
    elif page == 'Actual trade journal':
        journal_view()
    else:
        st.markdown('''Read the slower **trend chart first**, then the **entry chart**. Blue is EMA20, orange EMA50 and purple EMA200. RSI confirms momentum; ATR describes volatility used in stop placement.

**Passed / Failed / Not evaluated** refers to the named check. A complete technical setup still needs target room, affordable lot size, fresh prices, broker permissions and available combined risk before the monitor can enter.

The live page recomputes the exact bounded technical calculations from the checksum-verified completed-bar snapshot. Its chart can therefore differ from an MT5 display seeded from more history. A stale heartbeat is explicitly flagged; a historical candle timestamp is never presented as a fresh quote.

**Five executable setups per market** are described under **Seven-setup research lab → Five executable DEMO setups**. Their market conditions and exact rules come from the versioned execution specification. The seven independent research patterns remain separate and do not send orders. Signal frequency by weekday, session or regime is a monitoring observation, not an estimated win rate.

Historical replay remains the original A-only simulation and a separate, clearly labeled account model. It does not model the five-setup profile. Spread, slippage, fees, margin and OHLC exits are assumptions. The model does not reproduce native historical broker admission or prove profitability. Longer targets and higher ceilings do not themselves establish better returns.

Use **Activate DEMO trading** to start the existing DEMO monitor until the displayed Manila end time. The green light requires a fresh heartbeat, a live process, a verified DEMO account and unexpired entry authority. Red means inactive or unverified, including startup and stopping. Opening the app never activates trading. **Stop new entries** revokes entry authority without closing positions. Closing this app leaves the independent monitor running until its own deadline.

The existing daily SMA research app remains available as `dashboard/app.py`.''')


if __name__ == '__main__':
    main()
