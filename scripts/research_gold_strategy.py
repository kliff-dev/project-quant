"""Reproduce the frozen Gold pilot against saved broker M5 data, offline."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd
import plotly.graph_objects as go

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from analytics.gold_strategy import (SPEC_SHA256, M5, GoldReplaySettings,
    prepare_signals, replay_account, validate_m5)

DEFAULT_SOURCE=ROOT/'data/raw/gold_intraday_2026-11-15/XAUUSDm_M5.csv'
DEFAULT_OUTPUT=ROOT/'reports/market_analysis/gold_intraday_2026-11-15'
SPEC=ROOT/'config/gold_strategy_spec.json'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dump(path,value):
    path.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def condensed(run):
    return {key:value for key,value in run.items() if key not in ('trades','marks','open_position')} | {
        'open_positions':int(run['open_position'] is not None),
        'target_branches':dict(Counter(str(t['selected_reward_r']) for t in run['trades'])),
        'average_net_winner':(sum(t['net_pnl'] for t in run['trades'] if t['net_pnl']>0)/run['wins']) if run['wins'] else None,
        'average_net_loser':(sum(t['net_pnl'] for t in run['trades'] if t['net_pnl']<0)/run['losses']) if run['losses'] else None}


def audit(run):
    entries=[*run['trades'],*([run['open_position']] if run['open_position'] else [])]
    for trade in entries:
        assert trade['planned_risk'] <= trade['risk_budget']+1e-8
        assert .01 <= trade['lots'] <= .1
        assert trade['contract_size']==100
        assert pd.Timestamp(trade['obstacle_confirmed_at_utc']) <= pd.Timestamp(trade['entry_time_utc'])
        assert trade['target_room_flags'][str(trade['selected_reward_r'])]
        if trade['selected_reward_r']>=3:
            assert trade['strong_context'] and trade['normal_volatility']
        direction=trade['direction']
        reward=100*trade['lots']*(direction*(trade['take_profit']-trade['reference_entry'])-2*trade['slip'])-trade['cost_reserve']
        assert reward+1e-7 >= trade['selected_reward_r']*trade['planned_risk']
    assert abs(run['ending_balance']-(run['initial_balance']+run['net_closed_pnl']-
        (run['open_position']['entry_fee'] if run['open_position'] else 0)))<.011


def factor_groups(trades):
    rows=[]
    for name, extractor in [('target_r',lambda t:str(t['selected_reward_r'])),
        ('weekday',lambda t:t['signal']['weekday']),('month',lambda t:str(t['signal']['month'])),
        ('london_session',lambda t:'07-11' if t['signal']['london_hour']<12 else '12-16'),
        ('volatility',lambda t:'0.75-1.00' if t['volatility_ratio']<=1 else '1.00-1.50' if t['volatility_ratio']<=1.5 else '1.50-1.75'),
        ('trend',lambda t:'strong' if t['strong_context'] else 'ordinary')]:
        groups={}
        for trade in trades: groups.setdefault(extractor(trade),[]).append(trade)
        for label,items in sorted(groups.items()):
            rows.append(dict(factor=name,group=label,trades=len(items),win_rate=sum(t['net_pnl']>0 for t in items)/len(items),
                mean_net_r=sum(t['net_r'] for t in items)/len(items),net_pnl=round(sum(t['net_pnl'] for t in items),2)))
    return rows


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=DEFAULT_SOURCE)
    parser.add_argument('--output',type=Path,default=DEFAULT_OUTPUT)
    args=parser.parse_args(argv)
    if digest(SPEC)!=SPEC_SHA256:
        raise ValueError('Frozen Gold spec hash mismatch; no outcome analysis allowed.')
    source=args.source.resolve();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    frame=pd.read_csv(source)
    frame.index=pd.to_datetime(frame.pop('timestamp_utc'),utc=True)
    frame=validate_m5(frame)
    start,end=frame.index[1200],frame.index[-1]+M5
    span=end-start
    boundaries=[start,(start+span*.6).ceil('5min'),(start+span*.8).ceil('5min'),end]
    phases={'full':(start,end),'development':(boundaries[0],boundaries[1]),
        'validation':(boundaries[1],boundaries[2]),'recent':(boundaries[2],boundaries[3])}
    provenance=dict(created_at_utc=datetime.now(timezone.utc).isoformat(),source=str(source.relative_to(ROOT)),
        source_sha256=digest(source),spec_sha256=SPEC_SHA256,kernel_sha256=digest(ROOT/'analytics/gold_strategy.py'),
        research_script_sha256=digest(Path(__file__)),rows=len(frame),first_open_utc=frame.index[0].isoformat(),
        last_close_utc=end.isoformat(),phase_bounds={k:[a.isoformat(),b.isoformat()] for k,(a,b) in phases.items()},
        phase_rule='60/20/20 calendar time after 1200-M5 warmup; each phase starts flat, independent $500/$1000 balance; full run compounds across all phases',
        outcome_selection='Frozen before this run; no parameter search or results-based retuning',
        safety='Offline only; no MT5/network/orders/credentials imported or used')
    dump(output/'provenance.json',provenance)
    print(f'Preparing causal M5 signals from {len(frame)} bars...',flush=True)
    signals,reasons=prepare_signals(frame)
    dump(output/'signals.json',{str(k):v for k,v in signals.items()})
    dump(output/'signal_rejections.json',reasons)
    print(f'{len(signals)} raw setups; replaying 24 independent scenarios...',flush=True)
    summaries=[];all_trades=[];base_runs={};verification=[]
    for initial in (500.,1000.):
        for cost in ('base','zero_extra_fee','double_cost_stress'):
            settings=GoldReplaySettings(initial_balance=initial)
            if cost=='zero_extra_fee':settings=replace(settings,charge_cost_allowance=False)
            if cost=='double_cost_stress':settings=replace(settings,slippage_price=.1,cost_fraction=.1)
            for phase,(first,last) in phases.items():
                run=replay_account(frame,signals,first,last,settings)
                audit(run)
                scenario=f'{int(initial)}_{cost}_{phase}'
                summaries.append(dict(scenario=scenario,phase=phase,cost_model=cost,settings=asdict(settings),**condensed(run)))
                for trade in run['trades']:
                    all_trades.append(dict(scenario=scenario,**trade))
                if cost=='base' and phase=='full':base_runs[int(initial)]=run
                if run['open_position']:dump(output/f'open_position_{scenario}.json',run['open_position'])
                verification.append(dict(scenario=scenario,audited_entries=run['closed_trades']+int(run['open_position'] is not None),passed=True))
                print(f'{scenario}: {run["closed_trades"]} closed, equity ${run["ending_equity"]:.2f}',flush=True)
    dump(output/'summary.json',dict(provenance=provenance,raw_setups=len(signals),signal_rejections=reasons,runs=summaries))
    dump(output/'trades.json',all_trades)
    flat=[{k:v for k,v in trade.items() if not isinstance(v,(dict,list))} for trade in all_trades]
    pd.DataFrame(flat).to_csv(output/'trades.csv',index=False)
    factors=factor_groups(base_runs[500]['trades'])
    pd.DataFrame(factors,columns=['factor','group','trades','win_rate','mean_net_r','net_pnl']).to_csv(output/'factor_groups.csv',index=False)
    dump(output/'verification.json',dict(scenarios=verification,all_passed=True,
        claims='Internal geometry, risk, causal pivot timing and cash reconciliation assertions; not independent tick-level/live execution validation'))
    chart=go.Figure()
    for initial,run in base_runs.items():
        marks=pd.DataFrame(run['marks'])
        marks.to_csv(output/f'equity_{initial}_base.csv',index=False)
        chart.add_trace(go.Scatter(x=marks.time_utc,y=(marks.equity/initial-1)*100,name=f'${initial} base: equity return %'))
    chart.update_layout(title='Gold M5/M15 frozen pilot — modeled equity returns',yaxis_title='Return (%)',xaxis_title='UTC')
    chart.write_html(output/'equity.html',include_plotlyjs=True)
    lines=['# Gold intraday DEMO pilot: frozen historical research','',
        '**Experimental, not profitability-qualified. No parameters were retuned after seeing these outcomes.**','',
        f'Source: XAUUSDm broker spot-gold CFD bid M5 bars, {len(frame):,} rows; {frame.index[0].isoformat()} through close {end.isoformat()}. This is not COMEX futures or consolidated exchange volume.',
        '', 'One setup: M15 trend / M5 EMA20 reclaim; structure/ATR stop; 1% maximum planned account risk; 1R/2R/3R/4R targets selected before entry from volatility, trend strength and confirmed historical swing room. Stops and targets remain fixed after entry.',
        '', '## Account results','',
        '| Capital | Costs | Phase | Closed | Win rate | Net expectancy R | End equity | Max marked DD | Median holding h |',
        '|---:|---|---|---:|---:|---:|---:|---:|---:|']
    for row in summaries:
        wr='n/a' if row['win_rate'] is None else f'{row["win_rate"]:.1%}'
        expectancy='n/a' if row['net_expectancy_r'] is None else f'{row["net_expectancy_r"]:.3f}'
        hold='n/a' if row['median_holding_hours'] is None else f'{row["median_holding_hours"]:.2f}'
        lines.append(f'| ${row["settings"]["initial_balance"]:.0f} | {row["cost_model"]} | {row["phase"]} | {row["closed_trades"]} | {wr} | {expectancy} | ${row["ending_equity"]:.2f} | {row["maximum_marked_drawdown_percent"]:.2f}% | {hold} |')
    lines += ['', '## Read the results carefully','',
        f'{len(signals)} raw closed-bar setups were identified. Each account replay additionally filters costs, minimum-lot risk, margin and target room, and blocks simultaneous positions. Rejections are in summary.json. Zero trades or negative results remain valid findings.',
        '', 'Each phase starts with an independent flat account; full-period results carry one account across the period. Phase boundaries use calendar time, not outcome-based selections. A position open at a phase end is marked at estimated exit quote, not falsely counted as a completed win/loss.',
        '', 'The factor_groups.csv breakdown records target, weekday, month, session, trend and volatility. It describes this sample and does not authorize new optimized filters. Six months cannot validate annual seasonality; a handful of trades cannot establish conditional win rates.',
        '', '## Costs and limitations','',
        '- Contract: 100 troy ounces per lot; .01 minimum and step; .001 USD tick/point. Forex pip or 100,000-unit formulas are not used.',
        '- Base case uses recorded M5 spread plus $0.05 per ounce adverse slippage on each side and an additional modeled fee allowance of 5% of the trade risk budget. The allowance is not a verified broker commission.',
        '- Zero-extra-fee removes that modeled cash fee but keeps the reserve in sizing/target construction. Stress doubles per-side slippage to $0.10 and reserve/fee to 10%; spread stays the recorded spread.',
        '- Historical financing/swap, actual commission and broker margin changes are unavailable. No swap charge is modeled; positions can remain overnight. Illustrative 100:1 leverage is not verified historical margin. Broker-native margin and account checks must control live DEMO admission.',
        '- M5 OHLC does not reveal intrabar order. A bar touching SL and TP uses stop first; stop gaps exit at adverse open plus slippage, favorable target gaps receive target price less slippage. Marked drawdown uses bar-close equity, so intrabar drawdown may be worse.',
        '- SELL protection/targets use ask estimates from bar spread, which may differ from tick-by-tick ask extrema. Spread at signal close is assumed for entry; actual entry spread can change.',
        '- Fixed New York clock blackouts are a routine announcement-time proxy, not a real event calendar. They cannot identify all FOMC, CPI, payroll, surprise news, holidays or protect existing positions from announcements.',
        '- M15 bars require all three consecutive M5 observations. Unclosed/incomplete bins are excluded; the most recent six M5 candles must be consecutive. Daily/weekend breaks are retained, not filled.',
        '- 3R/4R are available only with predeclared strong-trend, volatility and room conditions; availability does not establish an edge or a short holding time.',
        '', '## Reproduce','', '```powershell', '.\\.venv\\Scripts\\python.exe -B scripts/research_gold_strategy.py', '```','',
        '[Source/spec/kernel hashes and phase bounds](provenance.json) · [All account summaries](summary.json) · [Trades](trades.csv) · [Factor groups](factor_groups.csv) · [Equity chart](equity.html) · [Internal verification](verification.json)','']
    (output/'findings.md').write_text('\n'.join(lines),encoding='utf-8')
    print(f'Wrote offline research to {output}',flush=True)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
