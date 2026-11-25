"""Offline A/B and shared-account comparison; no broker or order imports."""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analytics.two_setups import evaluate_setups, SETUPS
from dashboard.research_model import INTERVAL, WINDOW
import dashboard.replay as replay
from scripts.research_market_behaviour import archive_data, cached_research, portfolio_summary, save_json, START, END

OUT = ROOT/'reports/market_analysis/two_setups_2026-11-18'
BASE = ROOT/'reports/market_analysis/market_behaviour_2026-11-16'


def prepare_sweep_signals(data, symbol):
    """Prior-range prefilter is necessary, never an approximate indicator gate."""
    minimum = 1200 if symbol == 'XAUUSDm' else 2400
    low, high = data.low.shift().rolling(20).min(), data.high.shift().rolling(20).max()
    possible = ((data.low < low) & (data.close > low) & (data.close > data.open) |
                (data.high > high) & (data.close < high) & (data.close < data.open))
    signals, counts = {}, Counter()
    for n, i in enumerate(j for j in range(minimum-1, len(data)-1)
                          if possible.iloc[j] and START <= data.index[j]+INTERVAL[symbol] < END):
        if data.index[i+1] != data.index[i]+INTERVAL[symbol]:
            counts['no_contiguous_next_open'] += 1
            continue
        item = evaluate_setups(data.iloc[max(0,i-WINDOW[symbol]+1):i+1], symbol)['setups']['trend_sweep']
        counts[item.get('reason', 'no_setup')] += 1
        if item.get('side'):
            signals[i+1] = item
        if n % 200 == 0:
            print(f'{symbol} sweep windows: {n}; signals: {len(signals)}', flush=True)
    return signals, dict(counts)


def labeled_replay(frames, signals, screenings, capital, *, slip=1):
    with cached_research(signals, screenings):
        result = replay.replay_portfolio(frames, START, END,
            replay.ModelSettings(initial_balance=capital, slippage_multiplier=slip))
    # Retain attribution in every account-path trade and decision.
    for group in ('trades', 'decisions', 'rejections', 'open_positions'):
        items = result.get(group, [])
        if isinstance(items, dict):
            items = items.values()
        for row in items:
            symbol = row.get('symbol')
            stamp = row.get('entry_time_utc') or row.get('decision_time_utc')
            if symbol in frames and stamp:
                pos = frames[symbol].index.get_indexer([pd.Timestamp(stamp)])[0]
                if pos in signals[symbol]:
                    row['setup_id'] = signals[symbol][pos].get('setup_id', SETUPS[0])
                    row['trigger'] = 'sweep' if row['setup_id']=='trend_sweep' else 'pullback'
    return result


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    frames, archives = archive_data()
    baseline = {s: {int(k): dict(v, setup_id='trend_reclaim') for k,v in items.items()}
                for s,items in json.loads((BASE/'technical_signals.json').read_text()).items()}
    sweep, screenings = {}, {}
    overlaps = Counter()
    for symbol, frame in frames.items():
        sweep[symbol], screenings[symbol] = prepare_sweep_signals(frame, symbol)
        overlaps[symbol] = len(set(baseline[symbol]) & set(sweep[symbol]))
    combined = {s: dict(sweep[s], **{}) for s in frames}
    for symbol in combined:
        # A has priority for overlaps, matching the native arbitration.
        for index in set(sweep[symbol]) & set(baseline[symbol]):
            from analytics.two_setups import side_of
            if side_of(sweep[symbol][index],symbol)!=side_of(baseline[symbol][index],symbol):
                raise ValueError('Unexpected conflicting baseline/sweep contexts; stop the comparison.')
        combined[symbol].update(baseline[symbol])
    save_json(OUT/'signals.json', {'A':baseline,'B':sweep,'combined':combined})
    summaries = {}
    for label, signals in [('A', baseline), ('B', sweep), ('combined', combined)]:
        for capital in (500.,1000.):
            name = f'{label}_{int(capital)}'
            print('Replaying '+name, flush=True)
            result = labeled_replay(frames, signals, screenings, capital)
            save_json(OUT/(name+'.json'), result)
            summaries[name] = portfolio_summary(result)
            summaries[name]['by_setup'] = {
                setup: dict(closed_trades=sum(t.get('setup_id')==setup for t in result['trades']),
                            net_pnl=round(sum(t['net_pnl'] for t in result['trades'] if t.get('setup_id')==setup),2))
                for setup in SETUPS}
    result = labeled_replay(frames, combined, screenings, 500., slip=2)
    save_json(OUT/'combined_500_slippage2.json',result)
    summaries['combined_500_slippage2'] = portfolio_summary(result)
    evidence = dict(profile='reclaim_sweep_v1', generated_at_utc=datetime.now(timezone.utc).isoformat(),
        period=dict(start=START.isoformat(), end=END.isoformat()), archives=archives,
        spec_sha256=hashlib.sha256((ROOT/'config/two_setup_spec.json').read_bytes()).hexdigest(),
        kernel_sha256=hashlib.sha256((ROOT/'analytics/two_setups.py').read_bytes()).hexdigest(),
        raw_signals={s:dict(A=len(baseline[s]),B=len(sweep[s]),overlap=overlaps[s],
                           combined=len(combined[s])) for s in frames}, scenarios=summaries,
        limitations=['Previously examined history; descriptive development comparison, not untouched validation.',
            'Recorded bar spread, adverse slippage, 5% risk-budget modeled cost allowance; zero historical swaps.',
            'OHLC stop-first ambiguity and illustrative historical margin/Ask extrema; no actual fills.',
            'A priority on overlap; shared 2% risk and one position per instrument. Independent A/B accounts are hypothetical.',
            'No selection, threshold search or tuning based on results. DEMO exploration is not profitability qualification.'])
    save_json(OUT/'comparison.json',evidence)
    render_report(evidence)
    print(json.dumps(evidence,indent=2),flush=True)


def render_report(evidence):
    summaries=evidence['scenarios']
    lines=['# Two-setup historical DEMO research','',
        'A: existing trend/EMA20 reclaim. B: trend-aligned prior-20-bar sweep/rejection. Definitions frozen before this comparison.',
        '', '| Scenario | Closed trades | Win rate | Ending equity | Net P&L | Profit factor | Max close-marked drawdown |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for name,row in summaries.items():
        pf='undefined' if row['profit_factor'] is None else f"{row['profit_factor']:.2f}"
        lines.append(f"| {name} | {row['closed_trades']} | {100*row['win_rate']:.1f}% | {row['ending_equity']:.2f} | {row['net_closed_pnl']:.2f} | {pf} | {row['maximum_close_marked_drawdown_percent']:.2f}% |")
    lines += ['', '## Technical opportunity counts','']
    for s,row in evidence['raw_signals'].items():lines.append(f'- {s}: A {row["A"]}, B {row["B"]}, overlaps {row["overlap"]}, unique combined {row["combined"]}.')
    lines += ['', '## Limits','', *['- '+x for x in evidence['limitations']], '',
              'Raw signals are not executable trades. Added signals can still fail affordability, costs, target room or shared-position limits.',
              'Compare setup-level results together with opportunity/rejection records; the shared account has selection and occupancy effects.', '']
    lines += ['', '[Interactive equity comparison](comparison.html).', '']
    (OUT/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    import plotly.graph_objects as go
    chart=go.Figure()
    for name in ('A_500','B_500','combined_500'):
        result=json.loads((OUT/(name+'.json')).read_text())
        curve=result['equity_curve']
        chart.add_trace(go.Scatter(x=[row['time_utc'] for row in curve],
            y=[row['equity'] for row in curve],name=name,mode='lines'))
    chart.update_layout(title='Hypothetical $500 accounts: A, B and shared A/B',
        xaxis_title='UTC',yaxis_title='Modeled equity USD',template='plotly_white')
    chart.write_html(OUT/'comparison.html',include_plotlyjs=True)


if __name__ == '__main__':
    if '--render-only' in sys.argv:
        render_report(json.loads((OUT/'comparison.json').read_text()))
    else:main()
