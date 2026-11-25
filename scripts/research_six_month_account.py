"""Replay the original $500 setup and nine predeclared SL/TP choices offline."""

from __future__ import annotations

from dataclasses import asdict,replace
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from analytics.account_research import ReplaySettings,choose_on_training,prepare_signals,replay_account
from analytics.setup_research import validate_bars
from engine.execution_audit import atomic_json

SPEC_FILE='reports/market_analysis/six_month_spec_2026-11-14.json'
SPEC_SHA256='a452435b3727485776ace359d1047b60b892dda139bd00b6b5db9c52ab874ff3'


def flat_csv(rows: list[dict],path: Path) -> None:
    flattened=[{k:json.dumps(v,sort_keys=True) if isinstance(v,(dict,list)) else v for k,v in row.items()} for row in rows]
    pd.DataFrame(flattened).to_csv(path,index=False,float_format='%.10g')


def monthly_rows(run: dict) -> list[dict]:
    frame=pd.DataFrame(run['equity_curve'])
    frame.index=pd.DatetimeIndex(pd.to_datetime(frame.time_utc,utc=True)).tz_convert('Asia/Manila')
    previous=run['summary']['initial_balance']
    rows=[]
    for month,part in frame.groupby(frame.index.strftime('%Y-%m')):
        ending=float(part.equity.iloc[-1])
        rows.append(dict(month_manila=month,starting_equity=previous,ending_equity=ending,
                         equity_change=round(ending-previous,2),return_percent=100*(ending/previous-1)))
        previous=ending
    return rows


def chart(data: pd.DataFrame,baseline: dict,selected: dict | None,path: Path) -> None:
    start=pd.Timestamp(baseline['summary']['first_mark_utc'])
    prices=data.loc[data.index>=start]
    fig=make_subplots(rows=3,cols=1,shared_xaxes=True,vertical_spacing=.06,row_heights=[.45,.35,.2],
        subplot_titles=('EURUSDm and hypothetical baseline entries','Hypothetical account: balance and marked equity','H1-close equity drawdown (%)'))
    fig.add_trace(go.Candlestick(x=prices.index,open=prices.open,high=prices.high,low=prices.low,close=prices.close,name='EURUSDm bid H1'),row=1,col=1)
    entries=[*baseline['trades'],*([baseline['open_position']] if baseline['open_position'] else [])]
    for direction,name,color,symbol in [(1,'BUY entries','#16805d','triangle-up'),(-1,'SELL entries','#bc3f3f','triangle-down')]:
        points=[t for t in entries if t['direction']==direction]
        fig.add_trace(go.Scatter(x=[t['entry_time_utc'] for t in points],y=[t['entry_price'] for t in points],mode='markers',
            marker=dict(color=color,symbol=symbol,size=7),name=name),row=1,col=1)
    equity=pd.DataFrame(baseline['equity_curve'])
    fig.add_trace(go.Scatter(x=equity.time_utc,y=equity.balance,name='Original: balance',line=dict(color='#36658b',dash='dot')),row=2,col=1)
    fig.add_trace(go.Scatter(x=equity.time_utc,y=equity.equity,name='Original: equity',line=dict(color='#16805d')),row=2,col=1)
    fig.add_trace(go.Scatter(x=equity.time_utc,y=equity.drawdown_percent,name='Original drawdown',fill='tozeroy',line=dict(color='#bc3f3f')),row=3,col=1)
    if selected and selected['summary']['config_id']!=baseline['summary']['config_id']:
        candidate=pd.DataFrame(selected['equity_curve'])
        fig.add_trace(go.Scatter(x=candidate.time_utc,y=candidate.equity,name='Earlier-selected: '+selected['summary']['config_id'],line=dict(color='#9467bd')),row=2,col=1)
    fig.add_hline(y=500,line_dash='dash',line_color='#888',row=2,col=1)
    fig.update_layout(title='Six-month $500 research replay — hypothetical, not broker fills',template='plotly_white',height=1000,
        xaxis_rangeslider_visible=False,legend=dict(orientation='h',y=-.08),margin=dict(t=95,b=100))
    fig.update_xaxes(title_text='UTC',row=3,col=1)
    fig.update_yaxes(title_text='USD',row=2,col=1)
    fig.write_html(path,include_plotlyjs=True,full_html=True)


def write_report(output: Path,payload: dict,baseline: dict,chosen: dict | None) -> None:
    summary=baseline['summary']
    original=[r for r in payload['runs'] if r['evaluation']['config_id']=='sl1atr_tp2r' and r['ambiguity']=='stop_first']
    primary=[r for r in payload['runs'] if r['cost_case']=='base' and r['ambiguity']=='stop_first']
    start=pd.Timestamp(summary['first_mark_utc']).tz_convert('Asia/Manila')
    end=pd.Timestamp(summary['last_mark_utc']).tz_convert('Asia/Manila')
    lines=['# Six-month account replay and SL/TP study', '', '## Technical Summary', '',
        f"A hypothetical **500.00 USD** account from **{start} to {end}** using the current baseline entry/SL/TP rules finished with:", '',
        f"- Balance: **{summary['final_balance']:.2f} USD**; marked equity: **{summary['final_equity']:.2f} USD**.",
        f"- Equity change: **{summary['final_equity']-500:+.2f} USD ({summary['equity_return_percent']:+.2f}%)**.",
        f"- Completed trades: **{summary['completed_trades']}**; wins/losses/breakevens: {summary['wins']}/{summary['losses']}/{summary['breakevens']}; win rate: {summary['win_rate_percent']:.1f}%.",
        f"- Open positions at the data boundary: **{summary['open_positions']}**; H1-close equity drawdown: **{summary['max_h1_close_equity_drawdown_percent']:.2f}%**.", '',
        'These are modeled outcomes, not actual historical orders or a forecast. Starting balance resets to 500 USD for each configuration. Risk is recalculated after each trade, one position is allowed, and no final forced liquidation hides an open result.', '',
        '[Interactive price/account chart](equity_curve.html) · [Baseline hypothetical trades](base/stop_first/sl1atr_tp2r_trades.csv) · [Baseline equity marks](base/stop_first/sl1atr_tp2r_equity.csv)', '',
        '## Root Cause', '',
        'SL and TP determine payoff and exit timing; they do not create a profitable entry signal by themselves. Earlier holding-period research used a fixed risk basis and proposed timed exits. This experiment instead compounds a $500 account with the current SL/TP-only holding behavior and the live 3,000-H1 indicator window. Its numbers should not be substituted for those earlier estimates without accounting for these changes.', '',
        '## How It Was Diagnosed', '',
        f"The native read-only data refresh produced {payload['source_manifest']['rows']:,} EURUSDm H1 bars through the latest completed Friday session. Only closed bars enter each signal. The exact UTC H4 aggregation and rolling EMA seeds match the live setup; Wilder ATR uses an equivalent seeded recurrence checked against production.", '',
        'The initial budget is 1% of the flat account balance, rounded down to cents. Lots are rounded down to .01 with a .10 cap; unaffordable minimum lots are skipped. The original stop uses five-bar structure, a .1 ATR buffer and at least 1 ATR distance. Its target solves for 2R under two points of adverse slippage per side and a 5%-of-budget extra cost allowance. That allowance is charged in the model, not represented as an observed historical broker fee.', '',
        '### Monthly original-account path', '', '| Manila month | Starting equity | Ending equity | Change USD |', '| --- | ---: | ---: | ---: |']
    for month in monthly_rows(baseline):
        lines.append(f"| {month['month_manila']} | {month['starting_equity']:.2f} | {month['ending_equity']:.2f} | {month['equity_change']:+.2f} |")
    lines+=['', 'May and November are partial calendar months. These equity changes include floating marks and booked modeled costs; they are not promised monthly returns.', '',
        '### Original rules: financing sensitivity', '', '| Cost assumption | Final balance | Final equity | Win rate | H1-close drawdown |', '| --- | ---: | ---: | ---: | ---: |']
    for row in original:
        s=row['evaluation']
        lines.append(f"| {row['cost_case']} | {s['final_balance']:.2f} | {s['final_equity']:.2f} | {s['win_rate_percent']:.1f}% | {s['max_h1_close_equity_drawdown_percent']:.2f}% |")
    lines+=['', 'Base assumes zero historical swap. Financing stress adds 0.10 USD per 0.01 lot per ordinary weekday rollover, with Wednesday triple units, beyond the modeled per-trade allowance. It is not the account’s actual historical swap schedule. Spread and slippage are reflected in fills and are not deducted twice.', '',
        '### SL/TP calculation experiment', '',
        'The nine choices below were declared before calculating their account outcomes. The ATR figure is a minimum distance, not a replacement for structural protection. Each target is a cost-reserved reward multiple; it is not simply the displayed price-distance ratio. Entries and risk limits are held consistent, but different exits, affordability and balances change which later signals can be taken.', '',
        '| Minimum stop / reward | Earlier mean R | Earlier completed | Six-month equity | Six-month return | Six-month win rate | H1-close drawdown |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for row in primary:
        train,evaluation=row['training'],row['evaluation']
        lines.append(f"| {evaluation['config_id']} | {train['mean_completed_r']:.3f} | {train['completed_trades']} | {evaluation['final_equity']:.2f} | {evaluation['equity_return_percent']:+.2f}% | {evaluation['win_rate_percent']:.1f}% | {evaluation['max_h1_close_equity_drawdown_percent']:.2f}% |")
    selected=payload['selected_on_earlier_training']
    lines+=['', '### Earlier-period selection', '',
        'Selection used only the earlier training run (14 May 2024–14 May 2026 Manila), in base costs/stop-first ordering. A choice needs at least 50 completed trades, positive mean completed R and positive ending equity change; eligible choices rank by mean R, with lower drawdown as tie-break. Recent-six-month outcomes do not select the winner.', '']
    if selected is None:
        lines.append('**No configuration passed the predeclared earlier-period profitability gate.** A setting that looks attractive in the recent six months is therefore exploratory, not a selected deployable strategy.')
    else:
        s=chosen['summary']
        lines.append(f"Earlier data selected **{selected}**. Applied unchanged to the six-month period: balance **{s['final_balance']:.2f} USD**, equity **{s['final_equity']:.2f} USD**, return **{s['equity_return_percent']:+.2f}%**, mean completed R **{s['mean_completed_r']:.3f}**. This is a chronological descriptive evaluation: the later data was already viewed in earlier research and is not a pristine untouched holdout.")
    lines+=['', 'A positive historical result is evidence to investigate, not a promise of future profitability. Choosing the best-looking recent result after inspecting all nine would risk fitting past noise; see [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf). Win rate also needs average payoff and costs; see [CME mathematical expectation](https://www.cmegroup.com/education/courses/trading-psychology/the-mathematics-of-trading-success).', '',
        '## Solution', '',
        'The reproducible account replay, trade lists, monthly path, full grid and sensitivity results are saved here. They support testing profitability without changing the real demo position, live policy, session authorization or execution engine.', '',
        '```powershell', r'.\.venv\Scripts\python.exe -B scripts/research_six_month_account.py', '```', '',
        '[Full results and hashes](summary.json) · [All account summaries](comparison.csv) · [Rolling signal inputs](signals.csv) · [Registered specification](../six_month_spec_2026-11-14.json)', '',
        'Limits:', '',
        '- H1 bid candles and their constant per-bar spread approximate ask extremes and fills. Stop-first and target-first scenarios expose candle ambiguity; detailed per-case files retain any differing outcomes.',
        '- USD cashflows round to cents. Native read-only profit queries verified the account currency precision used by baseline sizing tests.',
        '- Fixed current 100:1 leverage supplies an illustrative entry-margin check; historical leverage, stop-out, permissions, outages and expired monitoring authorizations are not replayed.',
        '- The replay assumes the program is available for every qualifying London entry throughout the period. It starts flat, even if an earlier training position remains open.',
        '- No time exit, daily-loss cap or weekend liquidation was silently added. Gaps may exceed planned stop risk. Open positions are marked, not counted as completed winners.',
        '- H1-close drawdown can miss deeper intrabar drawdown. Intrabar fill time is only bounded by its candle. Funding rates, tick execution and historical announcements remain incomplete.',
        '- The last mark is the latest completed broker bar, not a current live account balance. Every simulated trade remains separate from the actual trade journal.', '',
        '## What I Learned', '',
        '- A fixed-risk opportunity screen cannot answer what a compounded account balance would become.',
        '- SL/TP changes alter wins, payoff sizes, occupied time and the next trades available.',
        '- A training-only selection can legitimately choose nothing; profitability must not be forced.',
        '- A backtest winner still needs stronger execution/cost checks and independent forward observations.', '',
        '## If This Happens Again', '',
        'Register the hypotheses and time split before calculating outcomes, show all tested settings, preserve the original baseline, and keep open-position equity separate from realized balance. Re-run the offline command above against the frozen input; a new parameter search requires a new registered experiment.']
    (output/'findings.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main() -> None:
    spec_path=ROOT/SPEC_FILE
    if hashlib.sha256(spec_path.read_bytes()).hexdigest()!=SPEC_SHA256:
        raise ValueError('Registered account experiment changed; create a new specification instead of silently refitting.')
    spec=json.loads(spec_path.read_text(encoding='utf-8'))
    source_manifest=json.loads((ROOT/spec['input_manifest']).read_text(encoding='utf-8'))
    source=ROOT/source_manifest['file']
    if hashlib.sha256(source.read_bytes()).hexdigest()!=source_manifest['sha256']:
        raise ValueError('Research data checksum mismatch.')
    contract=source_manifest['contract']
    if any(contract[k]!=v for k,v in dict(currency_base='EUR',currency_profit='USD',trade_contract_size=100000.,point=.00001,trade_tick_size=.00001,volume_min=.01,volume_step=.01).items()):
        raise ValueError('Replay requires the registered linear EURUSD/USD contract.')
    frame=pd.read_csv(source)
    frame.index=pd.DatetimeIndex(pd.to_datetime(frame.pop('timestamp_utc'),utc=True))
    frame=validate_bars(frame)
    train_start=pd.Timestamp(spec['training_start_utc'])
    start=pd.Timestamp(spec['evaluation_start_utc'])
    end=frame.index[-1]+pd.Timedelta(hours=1)
    output=ROOT/'reports/market_analysis/six_month_2026-11-14'
    output.mkdir(parents=True,exist_ok=True)
    signals=prepare_signals(frame,first_entry=train_start,window_bars=spec['rolling_closed_h1_bars'])
    flat_csv([dict(entry_bar_index=i,**value) for i,value in signals.items()],output/'signals.csv')
    print(f'Prepared {len(signals)} exact rolling-window entry signals.',flush=True)
    runs=[]
    primary_training=[]
    primary_evaluation={}
    baseline=None
    for ambiguity in spec['ambiguity_cases']:
        for case,charge in [('base',0.),('financing_stress',.1)]:
            for minimum in spec['minimum_stop_atr_candidates']:
                for reward in spec['reward_r_candidates']:
                    settings=ReplaySettings(minimum_stop_atr=minimum,reward_r=reward,initial_balance=spec['initial_balance_usd_each_run'],
                        risk_percent=spec['risk_percent_ceiling'],max_lots=spec['max_lots'],
                        slippage_points=spec['slippage_points_each_side'],cost_fraction=spec['additional_cost_allowance_fraction_of_budget'],
                        financing_per_001lot_unit=charge,ambiguity=ambiguity,leverage=spec['leverage_for_illustrative_margin_check'])
                    training=replay_account(frame,signals,train_start,start,settings)
                    evaluation=replay_account(frame,signals,start,end,settings)
                    path=output/case/ambiguity
                    path.mkdir(parents=True,exist_ok=True)
                    for suffix,rows in [('trades',evaluation['trades']),('equity',evaluation['equity_curve']),('rejections',evaluation['rejections']),('training_trades',training['trades'])]:
                        flat_csv(rows,path/f'{settings.config_id}_{suffix}.csv')
                    atomic_json(path/f'{settings.config_id}_open.json',dict(training=training['open_position'],evaluation=evaluation['open_position']))
                    runs.append(dict(cost_case=case,ambiguity=ambiguity,settings=asdict(settings),training=training['summary'],evaluation=evaluation['summary']))
                    if case=='base' and ambiguity=='stop_first':
                        primary_training.append(training)
                        primary_evaluation[settings.config_id]=evaluation
                        if minimum==1. and reward==2.:
                            baseline=evaluation
                    print(f'{case}/{ambiguity}/{settings.config_id}: training mean R={training["summary"]["mean_completed_r"]:.4f}; six-month equity={evaluation["summary"]["final_equity"]:.2f}',flush=True)
    selected=choose_on_training(primary_training,minimum_trades=50)
    payload=dict(study=spec['study'],generated_at_utc=datetime.now(timezone.utc).isoformat(),spec=spec,spec_sha256=SPEC_SHA256,
        source_manifest=source_manifest,source_manifest_sha256=hashlib.sha256((ROOT/spec['input_manifest']).read_bytes()).hexdigest(),
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),account_code_sha256=hashlib.sha256((ROOT/'analytics/account_research.py').read_bytes()).hexdigest(),
        feature_reference_sha256=hashlib.sha256((ROOT/'analytics/setup_research.py').read_bytes()).hexdigest(),
        prepared_signal_count=len(signals),selected_on_earlier_training=selected,runs=runs)
    atomic_json(output/'summary.json',payload)
    flat_csv([dict(cost_case=r['cost_case'],ambiguity=r['ambiguity'],**r['evaluation']) for r in runs],output/'comparison.csv')
    atomic_json(output/'baseline_account.json',baseline)
    pd.DataFrame(monthly_rows(baseline)).to_csv(output/'baseline_monthly.csv',index=False)
    chosen=primary_evaluation.get(selected)
    chart(frame,baseline,chosen,output/'equity_curve.html')
    write_report(output,payload,baseline,chosen)
    print(json.dumps(dict(baseline=baseline['summary'],selected_on_earlier_training=selected,
        selected_six_month_summary=chosen['summary'] if chosen else None,report=str(output/'findings.md')),indent=2))


if __name__=='__main__':
    main()
