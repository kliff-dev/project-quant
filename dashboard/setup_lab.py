"""Read-only executable setup guides and a separate seven-pattern research lab."""
from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
import subprocess

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from analytics.setup_lab import SETUPS, VERSION, scan, summarize, read_dataset, register
from dashboard.history import date_bounds
from dashboard.ui import section_header, style_chart
from dashboard.setup_catalog import (PROFILE as EXECUTION_PROFILE, SETUP_IDS,
    SETUP_NAMES, setup_guides, saved_history, condition_counts, history_rows,
    load_research_study, research_rows, pooled_research_mean, research_source_status, STUDY_PATH)


PALETTE = ['#14B8A6', '#38BDF8', '#F5B942', '#5EEAD4', '#3B82F6', '#B6DDF8', '#D6A35E']
REASONS = {
    'warmup_under_250_bars': 'Waiting for enough candle history',
    'recent_missing_candles': 'Recent candle history has a gap',
    'volatility_or_shock_guard': 'Volatility safety check did not pass',
    'new_trigger': 'New research signal',
    'condition_persists_no_new_trigger': 'Condition continues; no new signal',
    'pattern_not_present': 'Waiting for the pattern',
}
GUIDE = {
    'R1': 'A trending market pulls back and then closes across its short moving average again.',
    'R2': 'Price briefly breaks a recent high or low, then closes back inside it with the trend.',
    'R3': 'A trending market closes beyond its previous 20-candle price channel.',
    'R4': 'Price breaks out after a period of unusually narrow trading.',
    'R5': 'In a ranging market, price pushes outside a statistical band and returns inside.',
    'R6': 'Price first closes beyond the London opening-hour range during the following session.',
    'R7': 'Momentum crosses back through the RSI midpoint in the direction of the trend.',
}


@st.cache_data(show_spinner=False)
def cached_scan(manifest: str, checksum: str, clock_hour: str):
    data, info = read_dataset(Path(manifest))
    if info['sha256'] != checksum:
        raise ValueError('Dataset changed; reload the catalog.')
    return scan(data, info['metadata'])


def catalog(root):
    rows = []
    for path in (Path(root) / 'data/research/setup_lab').glob('*.json'):
        try:
            info = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(info, dict) or not isinstance(info.get('metadata'), dict):
                continue
            meta = info['metadata']
            if any(not isinstance(meta.get(key), str) or not meta[key]
                   for key in ('source', 'instrument', 'timeframe')):
                continue
            if not isinstance(info.get('last_open_utc'), str):
                continue
            if not isinstance(meta.get('acquired_at_utc', ''), str):
                continue
            label = f"{meta['source']} · {meta['instrument']} · {meta['timeframe']} · {info['last_open_utc'][:10]}"
            rows.append((path, info, label))
        except (OSError, ValueError, KeyError):
            continue
    return sorted(rows, key=lambda row: row[1]['metadata'].get('acquired_at_utc', ''), reverse=True)


def execution_history(root):
    rows = []
    for path in (Path(root) / 'logs/observations/reclaim_sweep_v1/events').glob('*.json'):
        try:
            item = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(item, dict) or not isinstance(item.get('setup_signals'), dict):
                continue
            for setup, signal in item['setup_signals'].items():
                if isinstance(signal, dict) and signal.get('side') in ('buy', 'sell'):
                    rows.append(dict(decision_time_utc=item['decision_time_utc'], instrument=item['symbol'],
                        setup_id=setup, side=signal['side'], fresh=item.get('signal_fresh_at_observation'),
                        status=item.get('execution_status'), reason=item.get('execution_reason'),
                        executed_setup=item.get('execution_setup_id')))
        except (OSError, ValueError, KeyError):
            continue
    return pd.DataFrame(rows)


def _guide_text(value):
    """Specifications can hold readable text or structured exact thresholds."""
    if isinstance(value, str):
        st.write(value)
    elif isinstance(value, dict):
        labels = {'volatility_ratio_min': 'Minimum ATR ratio',
            'volatility_ratio_max': 'Maximum ATR ratio', 'volatility_definition': 'ATR ratio calculation',
            'shock_atr_max': 'Maximum signal-candle range in ATR',
            'recent_contiguous_bars': 'Required consecutive recent candles',
            'channel_contiguous_bars': 'Required consecutive channel candles',
            'trend_up': 'Uptrend', 'trend_down': 'Downtrend', 'no_fallback': 'No alternate order attempt',
            'no_extra_budget': 'Shared account budget'}
        for key, detail in value.items():
            label = labels.get(key, key.replace('_', ' ').capitalize())
            if isinstance(detail, (str, int, float)):
                st.markdown(f'**{label}:** {detail}')
            else:
                st.markdown(f'**{label}**')
                _guide_text(detail)
    elif isinstance(value, (list, tuple)):
        for detail in value:
            _guide_text(detail)
    elif value is not None:
        st.write(value)


def executable_setup_guide(symbol):
    """Read the versioned technical specification without activating anything."""
    try:
        from analytics.five_setups import load_spec
        specification = load_spec()
        guides = setup_guides(symbol, specification=specification)
        instrument = specification['instruments'][symbol]
    except (ImportError, OSError, ValueError, KeyError, TypeError) as exc:
        st.warning(f'The reviewed five-setup guide is unavailable: {exc}. No execution authority is created by this page.')
        return
    st.caption(f"{symbol} · {instrument['entry_timeframe']} completed entry candles / {instrument['context_timeframe']} slower context · Profile {EXECUTION_PROFILE}")
    st.info('Five setups are eligible during an activated DEMO session. Eligible does not mean a signal is present or an order is guaranteed. Opening this guide does not activate trading.')
    st.markdown('**How an entry is chosen**')
    _guide_text(specification.get('selection'))
    for number, guide in enumerate(guides, 1):
        with st.expander(f"{number}. {guide['name']} · {guide['setup_id']}", expanded=number == 1):
            st.markdown(f"**Purpose:** {guide['purpose']}")
            st.write(guide['explanation'])
            for title, field in [('Market conditions', 'market_conditions'),
                ('Exact completed-candle entry rule', 'rule'), ('Stop loss', 'stop'),
                ('Take profit', 'target'), ('Research basis', 'research_origin'),
                ('Limitations and when to skip', 'limitations')]:
                st.markdown(f'**{title}**')
                _guide_text(guide.get(field, 'See the shared checks below.'))
            st.caption(guide['failure'])
            if guide['setup_id'] == 'range_reentry':
                st.caption('Range setups normally must fit the 1% baseline budget: the existing exception up to 2% requires strong trend conditions, which a measured range usually cannot satisfy. Gold minimum-lot sizing can therefore block this setup.')
    with st.expander('Shared market checks, calendar context and account risk'):
        st.markdown('**Market regime and volatility**')
        _guide_text(instrument.get('regimes'))
        _guide_text(instrument.get('common_guards'))
        st.markdown('**Different weekdays, sessions and months**')
        _guide_text(specification.get('calendar'))
        st.write('The history below records when signals appeared and which market conditions were present. Trigger frequency is not a win rate. We have not established that a weekday or season makes any setup more profitable.')
        st.markdown('**Unchanged account safeguards**')
        st.write('DEMO only. 1% baseline planned risk; only a qualified minimum-lot exception may use up to 2% for one trade. Maximum two EUR/USD and two Gold positions, four total, within an 8% combined planned-risk ceiling. All existing positions count against the ceiling. BTC uses its separate session and policy.')
        st.write('Every entry still needs a fresh quote, affordable volume, an acceptable spread and slippage reserve, valid target room, broker permissions, order_check and verified broker execution. Stops and targets do not guarantee the exact realized loss or reward.')
        st.caption('These are research-informed custom DEMO hypotheses. Indicator references do not prove our combined rules profitable. Each executed trade is documented separately in Actual trade journal.')


def _execution_history_table(actual):
    display = actual.copy().sort_values('decision_time_utc', ascending=False)
    display['decision_time_utc'] = _manila(display.decision_time_utc)
    display['side'] = display.side.fillna('—').str.upper()
    display['raw_side'] = display.raw_side.fillna('—').str.upper()
    st.dataframe(display.head(1000), hide_index=True, width='stretch', column_order=[
        'decision_time_utc', 'setup', 'side', 'raw_side', 'fresh', 'outcome', 'setup_reason',
        'reason', 'regime', 'session', 'weekday_manila', 'selected_setup', 'executed_setup'],
        column_config={'decision_time_utc': st.column_config.DatetimeColumn('Decision · Manila', format='DD MMM YYYY, HH:mm'),
            'setup': 'Setup', 'side': 'New trigger', 'raw_side': 'Underlying condition', 'fresh': 'Fresh candle', 'outcome': 'What happened',
            'setup_reason': 'Technical rule detail', 'reason': 'Execution / selection reason',
            'regime': 'Market regime', 'session': 'Session observation', 'weekday_manila': 'Weekday · Manila',
            'selected_setup': 'Selected pattern', 'executed_setup': 'Execution candidate'})
    st.caption(f'{len(display):,} matching setup evaluations; preview limited to 1,000. An observation is not proof of a broker fill; use Actual trade journal for verified tickets and results.')


def research_evidence_panel(root, symbol):
    """Show actual saved modeled outcomes, rather than infer profit from signals."""
    with st.container(border=True):
        st.markdown('**Historical outcome evidence · $500 model**')
        try:
            study = load_research_study(root)
        except (OSError, ValueError, TypeError) as exc:
            st.warning(f'The saved historical study is unavailable or invalid: {exc}')
            return
        if study is None:
            st.info('Historical study pending or unavailable in this workspace. Technical trigger counts alone do not establish profitability.')
            return
        try:
            source_state, source_details = research_source_status(study)
        except OSError:
            source_state, source_details = 'unverifiable', []
        if source_state == 'mismatch':
            st.warning('Historical reference only: this study uses a different setup kernel or specification revision from the displayed rules. Its outcomes do not validate the current revision; updated research is pending.')
        elif source_state == 'unverifiable':
            st.caption('The study’s setup-source fingerprints cannot be matched to this app revision. Treat these as saved historical evidence, not verified current-rule results.')
        else:
            st.caption('The study’s recorded setup-kernel and specification fingerprints match this app revision.')
        scenarios = (study.get('markets', {}).get(symbol) or {}).get('scenarios') or {}
        available = [key for key in ('500_slip1', '500_slip2') if key in scenarios]
        if not available:
            st.info('The saved study has no $500 modeled outcomes for this market. No win rate or profit estimate is inferred from trigger counts.')
            return
        scenario = st.selectbox('Historical slippage assumption', available,
            format_func=lambda key: 'Normal modeled slippage' if key == '500_slip1' else 'Double modeled slippage — stress case',
            key='five_research_slippage')
        mean, completed = pooled_research_mean(study, scenario)
        if mean is not None and mean < -1e-9:
            st.warning(f'Negative historical aggregate: {mean:+.3f} R per completed independent episode across the recorded market results ({completed} episodes). These modeled results do not establish a profitable strategy.')
        else:
            st.info('This reused-history study does not establish a profitable strategy. Positive rows and small samples are not reliable future winning probabilities.')
        period = study.get('period') or {}
        st.caption(f"Saved {_time_label(study.get('generated_at_utc'))} Manila · Study decisions (UTC): {str(period.get('start', 'Unknown'))[:10]} to {str(period.get('end', 'Unknown'))[:10]} · Descriptive split: {str(period.get('split', 'Unknown'))[:10]}")
        table = research_rows(study, symbol, scenario)
        st.caption(f'{symbol}: technical opportunities are pattern observations; modeled admissions are orders accepted by the offline assumptions. Neither is a native broker fill.')
        columns = {
            'setup': 'Setup', 'technical_opportunities': 'Technical opportunities',
            'modeled_admissions': 'Modeled admissions · $500', 'closed_episodes': 'Completed episodes',
            'win_rate_percent': st.column_config.NumberColumn('Modeled win rate %', format='%.1f'),
            'mean_net_r': st.column_config.NumberColumn('Net mean R · modeled costs', format='%+.3f'),
            'phase': 'Sample phase'}
        st.dataframe(table, hide_index=True, width='stretch', column_order=[
            'setup', 'technical_opportunities', 'modeled_admissions', 'closed_episodes', 'win_rate_percent', 'mean_net_r'],
            column_config=columns)
        st.caption('Blank win rate or mean R means no completed modeled episodes, not a 0% win rate. A 100% result from one or two episodes is not strong evidence. R expresses net outcome relative to the episode’s original planned risk.')
        with st.expander('Earlier versus later descriptive samples and modeling limits'):
            st.dataframe(research_rows(study, symbol, scenario, phases=True), hide_index=True, width='stretch',
                column_order=['setup', 'phase', 'technical_opportunities', 'modeled_admissions',
                    'closed_episodes', 'win_rate_percent', 'mean_net_r'], column_config=columns)
            st.write('Both samples reuse previously examined history. The later descriptive sample is not an untouched holdout. This is not a four-position portfolio backtest: episodes are followed independently, can overlap, and each resets to $500. Their dollar results are not compounded or presented as account equity.')
            st.write('Modeled costs include recorded candle spreads, adverse slippage and a hypothetical cost allowance of 5% of the risk budget. Normal slippage assumes 2 EUR/USD points or $0.05/oz Gold per side; the stress option doubles it. These are assumptions, not verified historical commissions. Actual historical swaps are unknown; separate financing stress in the report is not an actual broker bill.')
            st.write('OHLC candles approximate execution and stop/target order, not real ticks or native order_check outcomes. The model does not reconstruct actual margin, session permission or simultaneous account capacity at every entry. No weekday filter is automatically selected from this table.')
            st.caption(f'Local aggregate research source: {STUDY_PATH.as_posix()}. Use Actual trade journal for verified broker execution and realized cashflows.')


def executable_setups_view(root):
    st.markdown('### Five executable DEMO setups per market')
    st.write('The EUR/USD and Gold profiles each contain five patterns with market-specific thresholds. These are connected to the DEMO execution engine; the separate seven-pattern research lab below does not send orders.')
    symbol = st.selectbox('Executable setup market', ['EURUSDm', 'XAUUSDm'],
        format_func=lambda value: 'EUR/USD · EURUSDm' if value == 'EURUSDm' else 'Gold · XAUUSDm CFD', key='five_market')
    research_evidence_panel(root, symbol)
    guide, decisions, conditions = st.tabs(['Read each setup', 'Signals & execution decisions', 'Market conditions & days'])
    with guide:
        executable_setup_guide(symbol)
    actual = saved_history(root)
    actual = actual.loc[actual.instrument.eq(symbol)]
    with decisions:
        st.markdown('**Five separate rule checks, one recorded decision per completed candle**')
        if actual.empty:
            st.info('No five-setup observations have been saved for this market yet. An active monitor will record every evaluated pattern, including no-trigger and blocked decisions.')
            summary = pd.DataFrame([dict(setup=SETUP_NAMES[key], evaluated_candles=0, technical_triggers=0) for key in SETUP_IDS])
            st.dataframe(summary, hide_index=True, width='stretch')
        else:
            fresh = actual.loc[actual.fresh.eq(True)]
            summary = fresh.groupby('setup_id').agg(evaluated_candles=('decision_time_utc', 'nunique'),
                technical_triggers=('technical_signal', 'sum')).reindex(SETUP_IDS, fill_value=0).reset_index()
            summary.insert(1, 'setup', summary.setup_id.map(SETUP_NAMES))
            st.dataframe(summary, hide_index=True, width='stretch')
            st.caption('Counts use fresh observed candles only. A technical trigger can be blocked or lose priority to another pattern on the same candle; it is not an executed trade.')
            latest = actual.loc[actual.decision_time_utc.eq(actual.decision_time_utc.max())]
            st.markdown(f"**Latest saved checks · {_time_label(latest.iloc[0].decision_time_utc)} Manila**")
            _execution_history_table(latest)
            bounds = pd.to_datetime(actual.decision_time_utc, utc=True).dt.tz_convert('Asia/Manila')
            controls = st.columns(2)
            first = controls[0].date_input('Execution history start (Manila)', value=bounds.min().date(), key='five_first')
            last = controls[1].date_input('Execution history end (Manila)', value=bounds.max().date(), key='five_last')
            try:
                begin, finish = date_bounds(first, last)
                stamps = pd.to_datetime(actual.decision_time_utc, utc=True)
                selected = actual.loc[(stamps >= begin) & (stamps < finish)]
            except ValueError as exc:
                st.warning(str(exc))
                selected = actual.iloc[:0]
            selected_ids = st.multiselect('Executable history setup filters', list(SETUP_IDS), default=list(SETUP_IDS),
                format_func=SETUP_NAMES.get, key='five_setup_filters')
            selected = selected.loc[selected.setup_id.isin(selected_ids)]
            if st.checkbox('Show technical triggers only', value=True, key='five_signals_only'):
                selected = selected.loc[selected.technical_signal.eq(True)]
            _execution_history_table(selected)
            st.download_button('Download five-setup decisions', selected.to_csv(index=False),
                f'{symbol}_five_setup_decisions.csv', 'text/csv', key='five_download')
    with conditions:
        st.write('Compare signals against the number of fresh candles observed in each condition. More triggers on a day may simply reflect more monitoring hours. These tables do not estimate winning probabilities or recommend a weekday.')
        grouping = st.selectbox('Compare recorded context', ['weekday_manila', 'session', 'regime', 'month_manila'],
            format_func=lambda key: {'weekday_manila': 'Weekday · Manila', 'session': 'Market session',
                'regime': 'Market regime', 'month_manila': 'Month · Manila'}[key], key='five_condition')
        grouped = condition_counts(actual, grouping)
        if grouped.empty:
            st.info('No fresh five-setup observations are available for a condition comparison yet.')
        else:
            grouped['setup_id'] = grouped.setup_id.map(SETUP_NAMES)
            st.dataframe(grouped, hide_index=True, width='stretch', column_config={
                'setup_id': 'Setup', 'evaluated_candles': 'Fresh candles observed',
                'technical_triggers': 'Technical triggers',
                'trigger_rate_percent': st.column_config.NumberColumn('Trigger frequency %', format='%.1f')})
        st.caption('London and New York session labels follow their local daylight-saving time. Manila dates are shown separately. Economic-event calendars and news forecasts are not supplied by these labels.')


def _manila(values):
    """Keep sortable local datetime values; label their timezone in the table."""
    return pd.to_datetime(values, utc=True, errors='coerce').dt.tz_convert('Asia/Manila').dt.tz_localize(None)


def _time_label(value):
    stamp = pd.to_datetime(value, utc=True, errors='coerce')
    return 'Unknown' if pd.isna(stamp) else stamp.tz_convert('Asia/Manila').strftime('%d %b %Y, %H:%M')


def _instrument_label(value):
    if value.startswith('XAUUSD'):
        return f'Gold · {value}'
    if value.startswith('EURUSD'):
        return f'EUR/USD · {value}'
    return value


def _data_tools(root):
    with st.expander('Update research data or import another source'):
        st.markdown('**Refresh saved candles**')
        st.caption('Read up to 184 days from the existing DEMO terminal. This only updates research data; it does not place orders or activate trading. Saved charts work offline.')
        script = root / 'scripts/refresh_setup_lab.py'
        python = root / '.venv/Scripts/python.exe'
        available = script.is_file() and python.is_file()
        if st.button('Refresh MT5 research data', disabled=not available):
            try:
                flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                with st.spinner('Updating completed candles and checking the seven setups…'):
                    result = subprocess.run([str(python), '-B', str(script), '--root', str(root), '--provider', 'mt5'],
                        capture_output=True, text=True, timeout=55, creationflags=flags, cwd=root)
                if result.returncode:
                    st.error('Research refresh failed. Check that MT5 is open and signed into your DEMO account.')
                    with st.expander('Refresh error details'):
                        st.code(result.stderr[-2000:] or 'No additional error details were returned.')
                else:
                    st.success('Research data updated. Trading was not activated.')
                    cached_scan.clear()
            except (OSError, subprocess.SubprocessError) as exc:
                st.error(f'Research refresh could not finish: {exc}')
        if not available:
            st.caption('MT5 refresh needs the Project Quant source workspace and its Python environment. You can still import saved data below.')
        st.divider()
        st.markdown('**Import a separate provider**')
        st.caption('Use a Dukascopy-node BID CSV with timestamps in epoch milliseconds. The provider and instrument remain separate from Exness execution prices.')
        upload = st.file_uploader('Import Dukascopy-node BID CSV (epoch-millisecond timestamps)', type=['csv'])
        controls = st.columns(2)
        instrument = controls[0].selectbox('Imported instrument', ['EURUSD', 'XAUUSD'], key='lab_import_instrument')
        timeframe = controls[1].selectbox('Imported timeframe', ['H1', 'M5'], key='lab_import_timeframe')
        if upload is not None and st.button('Save imported research dataset'):
            try:
                frame = pd.read_csv(BytesIO(upload.getvalue()))
                frame.index = pd.DatetimeIndex(pd.to_datetime(pd.to_numeric(frame.pop('timestamp'), errors='raise'), unit='ms', utc=True))
                register(root, frame, dict(source='Dukascopy CSV (user-declared)', instrument=instrument, timeframe=timeframe,
                    price_basis='bid', instrument_type='User-declared Dukascopy OTC forex/metal; not verified Exness execution data',
                    acquired_at_utc=pd.Timestamp.now(tz='UTC').isoformat(), cost_quality='Ask, commissions and swaps unavailable.'))
                st.success('Dataset saved as a separate research source. Its provider declaration has not been independently verified.')
                cached_scan.clear()
            except (OSError, ValueError, KeyError) as exc:
                st.error(f'Could not import this CSV: {exc}')
        with st.expander('Advanced: download command'):
            st.code('.venv\\Scripts\\python.exe -B scripts/refresh_setup_lab.py --provider dukascopy --start 2026-05-22 --end 2026-11-22', language='powershell')
            st.caption('Optional download through the independently maintained dukascopy-node tool requires Node.js and npx. This command does not execute automatically.')


def _setup_guide(info=None):
    st.markdown('Compare how often each pattern occurs before considering it for trading. A signal is a completed-candle observation, not a buy or sell instruction.')
    for key, (name, rule) in SETUPS.items():
        with st.container(border=True):
            st.markdown(f'**{key} · {name}**')
            st.write(GUIDE[key])
            with st.expander(f'{key} exact research rules'):
                st.write(rule)
    with st.expander('Shared checks and data details'):
        st.markdown('All seven rules need 250 candles of history and continuous recent candles. They exclude unusually low or high volatility and large shock candles. They do not assume meaningful forex volume.')
        st.caption('Exact common checks: last six candles continuous; ATR / 100-bar median between 0.5 and 2; candle range at most 3 ATR. Indicators use full-history seeding and completed candles only.')
        if info is not None:
            st.json(info)
        st.caption(f'Rule version: {VERSION}. These are fixed local calculations, without a trained AI model or paid API.')
        st.markdown('References: [indicator definitions](https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/supported-indicators) · [Dukascopy historical data](https://www.dukascopy.com/swiss/english/marketwatch/historical/). These describe tools and data access; they do not validate our custom thresholds or profitability.')


def _signal_chart(path, visible):
    st.markdown('**Explore a signal on the chart**')
    if visible.empty:
        st.info('No signals match these filters. Expand the date range or select another setup.')
        return
    labels = {row.event_id: f'{_time_label(row.decision_time_utc)} · {row.setup_id} · {row.side.upper()}' for row in visible.itertuples()}
    event = st.selectbox('Research signal', visible.event_id.tolist(), format_func=labels.get)
    row = visible.set_index('event_id').loc[event]
    data, _ = read_dataset(path)
    decision = pd.Timestamp(row.decision_time_utc)
    section = data.loc[(data.index >= decision - pd.Timedelta(days=2)) & (data.index < decision)].tail(150)
    if section.empty:
        st.warning('No saved price candles are available around this signal.')
        return
    times = section.index.tz_convert('Asia/Manila')
    figure = go.Figure(go.Candlestick(x=times, open=section.open, high=section.high, low=section.low,
        close=section.close, name='Saved bid candles', increasing_line_color=PALETTE[0], decreasing_line_color=PALETTE[2]))
    figure.add_trace(go.Scatter(x=[times[-1]], y=[row.close], mode='markers',
        name=f'{row.setup_id} {row.side.upper()}', marker=dict(size=13, color=PALETTE[1],
        symbol='triangle-up' if row.side == 'buy' else 'triangle-down', line=dict(width=1, color='#FFFFFF'))))
    figure.update_layout(xaxis_title='Manila time', yaxis_title='Bid price', xaxis_rangeslider_visible=False)
    st.plotly_chart(style_chart(figure, height=420), width='stretch')
    st.caption('The marker identifies the completed signal candle. This is saved bid-price history; execution costs and SL/TP outcomes are not modeled here.')


def research_patterns_view(root):
    root = Path(root)
    st.info('Research only · These seven independent patterns do not place orders. The five executable DEMO setups per market have their own reviewed entry, stop, target and risk rules in the first tab.')
    _data_tools(root)
    datasets = catalog(root)
    if not datasets:
        st.warning('No research data is saved yet. Refresh MT5 research data or import a CSV above to begin.')
        _setup_guide()
        return

    with st.container(border=True):
        st.markdown('**Choose your market data**')
        controls = st.columns([1.2, 1.2, 1])
        providers = list(dict.fromkeys(item[1]['metadata']['source'] for item in datasets))
        provider = controls[0].selectbox('Data provider', providers, key='lab_provider')
        candidates = [item for item in datasets if item[1]['metadata']['source'] == provider]
        instruments = sorted({item[1]['metadata']['instrument'] for item in candidates})
        instrument = controls[1].selectbox('Research instrument', instruments, format_func=_instrument_label, key='lab_instrument')
        candidates = [item for item in candidates if item[1]['metadata']['instrument'] == instrument]
        timeframes = sorted({item[1]['metadata']['timeframe'] for item in candidates})
        timeframe = controls[2].selectbox('Research timeframe', timeframes,
            format_func=lambda value: {'M5': '5-minute candles', 'H1': '1-hour candles'}.get(value, value), key='lab_timeframe')
        candidates = [item for item in candidates if item[1]['metadata']['timeframe'] == timeframe]
        if not st.checkbox('Show older dataset snapshots', value=False):
            candidates = candidates[:1]
        labels = {i: f"{item[1]['first_open_utc'][:10]} to {item[1]['last_open_utc'][:10]} · saved {_time_label(item[1]['metadata'].get('acquired_at_utc'))}"
                  for i, item in enumerate(candidates)}
        chosen = st.selectbox('Research dataset', range(len(candidates)), format_func=labels.get, key='lab_dataset')
        path, info, _ = candidates[chosen]
        st.caption('Each provider is analyzed separately. Gold provider quotes and Exness Gold CFDs are different instruments.')

    try:
        with st.spinner('Reading saved data and checking the seven patterns…'):
            result = cached_scan(str(path), info['sha256'], pd.Timestamp.now(tz='UTC').floor('h').isoformat())
    except (OSError, ValueError, KeyError) as exc:
        st.error(f'Cannot verify research input: {exc}')
        return
    meta = result['metadata']
    dates = pd.to_datetime([result['start_utc'], result['end_utc']], utc=True).tz_convert('Asia/Manila')
    controls = st.columns(2)
    first = controls[0].date_input('Research start date (Manila)', value=dates[0].date(), key=f'lab_first_{path.stem}')
    last = controls[1].date_input('Research end date (Manila)', value=dates[1].date(), key=f'lab_last_{path.stem}')
    try:
        begin, finish = date_bounds(first, last)
    except ValueError as exc:
        st.error(str(exc))
        return
    signals = result['signals']
    stamp = pd.to_datetime(signals.decision_time_utc, utc=True)
    selected = signals.loc[(stamp >= begin) & (stamp < finish)].copy()
    summary = summarize(selected)
    st.caption(f"{meta['instrument_type']} · Saved candles through {_time_label(result['end_utc'])} Manila · Updated {_time_label(meta.get('acquired_at_utc'))} Manila")
    if pd.Timestamp.now(tz='UTC') - pd.Timestamp(result['end_utc']) > 2 * {'M5': pd.Timedelta(minutes=5), 'H1': pd.Timedelta(hours=1)}[meta['timeframe']]:
        st.warning('Saved data is older than the latest expected candles. A market closure may explain this. These historical signals are not current entry instructions.')

    overview, history, guide = st.tabs(['Overview', 'Signal history', 'Setup guide'])
    with overview:
        top = summary.sort_values('triggers', ascending=False, kind='stable').iloc[0]
        cols = st.columns(3)
        cols[0].metric('Selected research triggers', f'{len(selected):,}')
        cols[1].metric('Most frequent setup', top.setup_id if len(selected) else '—',
            help=f'{top.setup}: {int(top.triggers):,} triggers. Frequency does not measure profitability.' if len(selected) else 'No triggers in the selected dates.')
        cols[2].metric('Orders authorized by this lab', 0)
        st.caption(f'{selected.decision_time_utc.nunique():,} unique trigger candles. Several setups can trigger on the same candle; those signals are correlated.')
        with st.container(border=True):
            st.markdown('**Which setups trigger most often?**')
            st.caption('Compare buy and sell observations in the selected dates. Counts measure frequency, not profit.')
            figure = go.Figure()
            for column, name, color in (('buys', 'Buy signals', PALETTE[0]), ('sells', 'Sell signals', PALETTE[2])):
                figure.add_trace(go.Bar(x=summary.setup_id, y=summary[column], name=name,
                    marker_color=color, customdata=summary.setup, hovertemplate='%{x} · %{customdata}<br>%{y} signals<extra>%{fullData.name}</extra>'))
            figure.update_layout(barmode='stack', xaxis_title='Research setup', yaxis_title='Signals')
            st.plotly_chart(style_chart(figure, height=340), width='stretch')
            display = summary.copy()
            display['last_trigger_utc'] = _manila(display.last_trigger_utc)
            st.dataframe(display, hide_index=True, width='stretch', column_config={
                'setup_id': 'ID', 'setup': 'Setup', 'triggers': st.column_config.NumberColumn('Total signals', format='%d'),
                'buys': 'Buy', 'sells': 'Sell',
                'last_trigger_utc': st.column_config.DatetimeColumn('Latest signal · Manila', format='DD MMM YYYY, HH:mm')})
        with st.container(border=True):
            st.markdown('**How does activity change over time?**')
            if not selected.empty:
                weekly = selected.copy()
                weekly['week_manila'] = pd.to_datetime(weekly.decision_time_utc, utc=True).dt.tz_convert('Asia/Manila').dt.strftime('%G-W%V')
                grouped = weekly.groupby(['week_manila', 'setup_id']).size().reset_index(name='triggers')
                figure = px.bar(grouped, x='week_manila', y='triggers', color='setup_id',
                    category_orders={'setup_id': list(SETUPS)}, color_discrete_sequence=PALETTE,
                    labels={'week_manila': 'Week · Manila', 'triggers': 'Signals', 'setup_id': 'Setup'})
                st.plotly_chart(style_chart(figure, height=300), width='stretch')
            else:
                st.info('No signals in this date range. Choose wider dates to compare weekly activity.')
        with st.container(border=True):
            st.markdown('**Latest saved research decision**')
            latest = result['latest'].copy()
            latest['side'] = latest.side.map({'buy': 'BUY', 'sell': 'SELL'}).fillna('No new signal')
            latest['reason'] = latest.reason.map(lambda reason: REASONS.get(reason, reason))
            st.caption(f"As of {_time_label(result['end_utc'])} Manila. This is the latest saved candle, independent of your history date filters.")
            st.dataframe(latest, hide_index=True, width='stretch', column_config={
                'setup_id': 'ID', 'setup': 'Setup', 'side': 'Signal', 'reason': 'What the scanner sees'})

    with history:
        st.markdown('**Explore individual signals**')
        selected_ids = st.multiselect('History setup filters', list(SETUPS), default=list(SETUPS),
            format_func=lambda key: f'{key} · {SETUPS[key][0]}')
        visible = selected.loc[selected.setup_id.isin(selected_ids)].sort_values('decision_time_utc', ascending=False)
        display = visible.copy()
        display['decision_time_utc'] = _manila(display.decision_time_utc)
        display['side'] = display.side.str.upper()
        st.dataframe(display.head(1000), hide_index=True, width='stretch', column_order=[
            'decision_time_utc', 'setup_id', 'setup', 'side', 'close', 'atr14', 'rsi14', 'overlapping_setups'],
            column_config={
                'decision_time_utc': st.column_config.DatetimeColumn('Signal time · Manila', format='DD MMM YYYY, HH:mm'),
                'setup_id': 'ID', 'setup': 'Setup', 'side': 'Direction',
                'close': st.column_config.NumberColumn('Closing bid', format='%.5f'),
                'atr14': st.column_config.NumberColumn('ATR · 14 candles', format='%.5f'),
                'rsi14': st.column_config.NumberColumn('RSI · 14 candles', format='%.1f'),
                'overlapping_setups': 'Setups on this candle'})
        st.caption(f'{len(visible):,} matching signals; showing up to 1,000. Downloads contain every matching row and original UTC timestamps.')
        st.download_button('Download seven-setup signal history', visible.to_csv(index=False), 'seven_setup_signals.csv', 'text/csv')
        _signal_chart(path, visible)
        with st.expander('Legacy native A/B execution decisions — separate history'):
            st.caption('These records describe the earlier two-setup trading policy, before the five-setup profile. They are separate from the seven research rules above and are not a list of verified fills.')
            actual = execution_history(root)
            if actual.empty:
                st.info('No recorded native A/B technical signals in this workspace.')
            else:
                actual = actual.sort_values('decision_time_utc', ascending=False)
                actual['decision_time_utc'] = _manila(actual.decision_time_utc)
                st.dataframe(actual, hide_index=True, width='stretch', column_config={
                    'decision_time_utc': st.column_config.DatetimeColumn('Decision time · Manila', format='DD MMM YYYY, HH:mm'),
                    'instrument': 'Instrument', 'setup_id': 'Native setup', 'side': 'Direction',
                    'fresh': 'Fresh signal', 'status': 'Execution status', 'reason': 'Reason', 'executed_setup': 'Selected native setup'})
            st.caption('Use Actual trade journal for verified broker fills and realized results.')
    with guide:
        _setup_guide(info)
    st.caption('Research frequency is not a win rate. This lab does not calculate SL/TP outcomes or returns after costs. Additional trading rules require a separate cost-aware replay and forward test.')


def research_view(root):
    """Preserve the navigation page while separating executable and research rules."""
    root = Path(root)
    section_header('SETUPS & RESEARCH', 'Your setups, rules and evidence',
        'Read the five executable DEMO patterns for each market, then explore the separate seven-pattern research scanner.')
    active, laboratory = st.tabs(['Five executable DEMO setups', 'Seven research-only patterns'])
    with active:
        executable_setups_view(root)
    with laboratory:
        research_patterns_view(root)
