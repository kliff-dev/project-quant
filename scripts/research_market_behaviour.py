"""Reproducible, offline market-behaviour study. Never imports a broker connector.

Run from the repository: .venv\\Scripts\\python.exe -B scripts/research_market_behaviour.py
The isolated process patches only research function references for a controlled
ablation. No source files, live configuration, or execution state are changed.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analytics.candidate_signals import wilder_atr
from analytics.gold_strategy import protective_fill as gold_fill
from analytics.account_research import protective_fill as eur_fill
from dashboard.research_model import ARCHIVES, INTERVAL, load_bars
import dashboard.replay as replay

OUTPUT = ROOT / 'reports/market_analysis/market_behaviour_2026-11-16'
START, END = pd.Timestamp('2026-05-19T00:00Z'), pd.Timestamp('2026-11-14T00:00Z')


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False, default=str), encoding='utf-8')


def wilson(wins, n):
    """Descriptive binomial interval; NOT independence-adjusted for our episodes."""
    if not n:
        return None
    z, p = 1.959963984540054, wins/n
    centre = (p+z*z/(2*n))/(1+z*z/n)
    half = z*np.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
    return [max(0., float(centre-half)), min(1., float(centre+half))]


def archive_data():
    frames, evidence = {}, {}
    for symbol, relative in ARCHIVES.items():
        path = ROOT/relative
        manifest = json.loads((path.parent/'manifest.json').read_text(encoding='utf-8'))
        expected = manifest.get('csv_sha256') or manifest['sha256']
        frames[symbol] = load_bars(path, symbol, expected_sha=expected)
        data = frames[symbol]
        evidence[symbol] = dict(path=relative, sha256=expected, hash_verified=True,
            rows=len(data), first_open_utc=data.index[0].isoformat(),
            last_close_utc=(data.index[-1]+INTERVAL[symbol]).isoformat(),
            duplicate_timestamps=int(data.index.duplicated().sum()),
            gaps=int((data.index.to_series().diff() > INTERVAL[symbol]).sum()))
    return frames, evidence


def describe(frames):
    evidence, hour_rows, daily_frames = {}, [], {}
    for symbol, whole in frames.items():
        data = whole.loc[(whole.index >= START) & (whole.index+INTERVAL[symbol] <= END)]
        atr = wilder_atr(whole).reindex(data.index)
        point = .001 if symbol == 'XAUUSDm' else .00001
        spread_ratio = data.spread*point/atr
        daily = data.resample('1D').agg({'open':'first','high':'max','low':'min','close':'last'})
        daily['bars'] = data.close.resample('1D').count()
        # Exclude incomplete boundary days and partial holiday/session days.
        minimum = 240 if symbol == 'XAUUSDm' else 20
        daily = daily.loc[(daily.bars >= minimum) & (daily.index > data.index[0].normalize())]
        daily['range_price'] = daily.high-daily.low
        daily['range_percent'] = daily.range_price/daily.open*100
        daily_frames[symbol] = daily
        daily.to_csv(OUTPUT/f'{symbol}_daily_ranges.csv')
        evidence[symbol] = dict(rows_in_study=len(data),
            daily_range_days=len(daily), median_daily_range_price=float(daily.range_price.median()),
            p90_daily_range_price=float(daily.range_price.quantile(.9)),
            median_daily_range_percent=float(daily.range_percent.median()),
            median_entry_atr_price=float(atr.median()),
            p10_entry_atr_price=float(atr.quantile(.1)), p90_entry_atr_price=float(atr.quantile(.9)),
            median_spread_price=float((data.spread*point).median()),
            median_spread_fraction_atr=float(spread_ratio.median()),
            p95_spread_fraction_atr=float(spread_ratio.quantile(.95)),
            daily_completeness_rule=f'>={minimum} bars; first UTC day excluded')
        hours = data.index.tz_convert('Europe/London').hour
        for hour in range(24):
            take = data.loc[hours == hour]
            if len(take):
                hour_rows.append(dict(symbol=symbol, london_hour=hour, bars=len(take),
                    median_candle_range=float((take.high-take.low).median()),
                    p90_candle_range=float((take.high-take.low).quantile(.9)),
                    median_spread_price=float((take.spread*point).median())))
    # Align actual full Gold hours and EUR/USD hours; never bridge closures.
    gold = frames['XAUUSDm']
    count = gold.close.resample('1h').count()
    gh = gold.close.resample('1h').last().where(count == 12)
    eh = frames['EURUSDm'].close
    prices = pd.concat([gh.rename('gold'), eh.rename('eurusd')], axis=1)
    returns = np.log(prices/prices.shift()).dropna()
    returns = returns.loc[(returns.index >= START) & (returns.index+pd.Timedelta(hours=1) <= END)]
    rows = []
    for month, group in returns.groupby(returns.index.strftime('%Y-%m')):
        rows.append(dict(month=month, aligned_hourly_returns=len(group),
            correlation=float(group.gold.corr(group.eurusd))))
    evidence['cross_instrument'] = dict(aligned_hourly_returns=len(returns),
        correlation=float(returns.gold.corr(returns.eurusd)), monthly=rows,
        caveat='Contemporaneous correlation, not a predictive signal or causal USD decomposition.')
    pd.DataFrame(hour_rows).to_csv(OUTPUT/'london_hour_descriptives.csv', index=False)
    pd.DataFrame(rows).to_csv(OUTPUT/'monthly_correlation.csv', index=False)
    return evidence, hour_rows, daily_frames


@contextmanager
def cached_research(signals, screenings, omit_rescue_slope=False):
    original_prepare, original_strong = replay.prepare_current_signals, replay.strong_context
    replay.prepare_current_signals = lambda data, symbol, start, end, progress=None: (signals[symbol], screenings[symbol])
    if omit_rescue_slope:
        def context(signal, symbol):
            prefix = 'm15' if symbol == 'XAUUSDm' else 'h4'
            return (abs(signal[f'{prefix}_ema50']-signal[f'{prefix}_ema200']) >= signal[f'{prefix}_atr14']
                    and .75 <= signal['volatility_ratio'] <= 1.5)
        replay.strong_context = context
    try:
        yield
    finally:
        replay.prepare_current_signals, replay.strong_context = original_prepare, original_strong


def portfolio_summary(result):
    trades = result['trades']
    result_keys = ['initial_balance','ending_balance','ending_equity','closed_trades',
        'win_rate','net_closed_pnl','net_expectancy_r','maximum_close_marked_drawdown_percent']
    summary = {k:result[k] for k in result_keys}
    summary.update(open_positions=len(result['open_positions']),
        ambiguous_exits=sum(t['ambiguous_bar'] for t in trades),
        holding_hours_median_upper_bound=float(np.median([t['holding_hours_upper_bound'] for t in trades])) if trades else None,
        rejections=dict(Counter(r['reason'] for r in result['rejections'])))
    gains = sum(max(0, t['net_pnl']) for t in trades)
    losses = -sum(min(0, t['net_pnl']) for t in trades)
    summary['profit_factor'] = gains/losses if losses else None
    summary['naive_binomial_win_interval_95'] = wilson(sum(t['net_pnl']>0 for t in trades),len(trades))
    summary['by_symbol'] = {}
    summary['by_month'] = {}
    for field, key, values in [('symbol','by_symbol',sorted({t['symbol'] for t in trades})),
                               ('entry_time_utc','by_month', sorted({t['entry_time_utc'][:7] for t in trades}))]:
        for value in values:
            take = [t for t in trades if (t[field][:7] if key == 'by_month' else t[field]) == value]
            summary[key][value] = dict(closed_trades=len(take), wins=sum(t['net_pnl']>0 for t in take),
                                      net_pnl=round(sum(t['net_pnl'] for t in take),2))
    return summary


def episode(data, symbol, position, signal, plan):
    """Follow the existing fill model; keep excursions before terminal bar only."""
    direction, entry = plan['direction'], plan['reference_entry']
    interval = INTERVAL[symbol]
    horizon = pd.Timedelta(hours=24 if symbol == 'XAUUSDm' else 120)
    deadline = data.index[position]+horizon
    end = int(data.index.searchsorted(deadline, side='left'))
    unit = .001 if symbol == 'XAUUSDm' else .00001
    finish = gold_fill if symbol == 'XAUUSDm' else lambda p,b: eur_fill(p,b,'stop_first')
    mfe, mae, result, exit_pos = 0., 0., None, None
    for j in range(position, len(data)):
        bar = data.iloc[j]
        result = finish(plan,bar)
        if result:
            exit_pos = j
            break
        shift = float(bar.spread)*unit if direction == -1 else 0.
        favourable = direction*((float(bar.high) if direction == 1 else float(bar.low))+shift-entry)
        adverse = -direction*((float(bar.low) if direction == 1 else float(bar.high))+shift-entry)
        mfe, mae = max(mfe,favourable), max(mae,adverse)
    context = 'm15' if symbol == 'XAUUSDm' else 'h4'
    prefix = 'm5' if symbol == 'XAUUSDm' else 'h1'
    slope = abs(signal[f'{context}_ema50_slope5'])/signal[f'{context}_atr14']
    separation = abs(signal[f'{prefix}_ema20']-signal[f'{prefix}_ema50'])/signal['atr14']
    row = dict(symbol=symbol, entry_time_utc=data.index[position].isoformat(), side='buy' if direction==1 else 'sell',
        selected_reward_r=plan['selected_reward_r'], lots=plan['lots'], planned_risk=plan['planned_risk'],
        risk_percent=plan['actual_planned_risk_percent'], entry=plan['entry_price'],
        stop_loss=plan['stop_loss'], take_profit=plan['take_profit'],
        stop_distance_atr=abs(entry-plan['stop_loss'])/signal['atr14'],
        target_distance_atr=abs(entry-plan['take_profit'])/signal['atr14'],
        context_slope_atr=slope, entry_ema_separation_atr=separation,
        volatility_ratio=signal['volatility_ratio'],
        slope_band='<0.25' if slope<.25 else '0.25–0.50' if slope<.5 else '>=0.50',
        volatility_band='<1' if signal['volatility_ratio']<1 else '1–1.5' if signal['volatility_ratio']<=1.5 else '>1.5',
        compression_band='<0.1ATR' if separation<.1 else '>=0.1ATR',
        london_hour=data.index[position].tz_convert('Europe/London').hour,
        mfe_before_exit_bar_atr=mfe/signal['atr14'], mae_before_exit_bar_atr=mae/signal['atr14'],
        recovery_horizon_hours=horizon.total_seconds()/3600,
        recovery_horizon_complete=bool(data.index[-1]+interval>=deadline))
    if result is None:
        return dict(row,status='open_at_archive_end', net_r=None, net_pnl=None,
                    target_after_stop=None, exit_time_utc=None, ambiguous=False)
    price, reason, ambiguous = result
    net = direction*plan['contract_size']*plan['lots']*(price-plan['entry_price'])-plan['entry_fee']
    recovery = None
    if reason.startswith('stop'):
        later = data.iloc[exit_pos+1:end]
        if len(later):
            exit_side = later.low+later.spread*unit if direction == -1 else later.high
            recovery = bool((exit_side<=plan['take_profit']).any() if direction==-1 else (exit_side>=plan['take_profit']).any())
        else:
            recovery = False
    return dict(row, status=reason, net_r=net/plan['planned_risk'],net_pnl=net,
        exit_time_utc=(data.index[exit_pos]+interval).isoformat(),
        holding_hours_upper_bound=(data.index[exit_pos]+interval-data.index[position]).total_seconds()/3600,
        ambiguous=bool(ambiguous),target_after_stop=recovery)


def episode_groups(rows):
    result = []
    df = pd.DataFrame(rows)
    for symbol, group in df.groupby('symbol'):
        complete = group.loc[group.net_r.notna()]
        for dimension in ['all','slope_band','volatility_band','compression_band','selected_reward_r']:
            parts = [('all',complete)] if dimension=='all' else complete.groupby(dimension)
            for label, take in parts:
                stopped = take.loc[take.status.str.startswith('stop') & take.recovery_horizon_complete]
                result.append(dict(symbol=symbol,dimension=dimension,group=str(label),closed_episodes=len(take),
                    win_rate=float((take.net_pnl>0).mean()),mean_net_r=float(take.net_r.mean()),
                    stop_count=int(take.status.str.startswith('stop').sum()),
                    mean_stop_atr=float(take.stop_distance_atr.mean()),
                    mean_target_atr=float(take.target_distance_atr.mean()),
                    median_mfe_before_exit_bar_atr=float(take.mfe_before_exit_bar_atr.median()),
                    stopped_with_complete_recovery_horizon=len(stopped),
                    later_target_touches=int(stopped.target_after_stop.sum()),
                    ambiguous_exits=int(take.ambiguous.sum())))
    return result


def charts(portfolios, hour_rows, daily_frames, groups):
    figure=make_subplots(rows=2,cols=1,subplot_titles=('Modeled shared-account equity; prior-inspected history', 'Observed daily ranges in percent; completed UTC days'))
    for name,result in portfolios.items():
        curve=pd.DataFrame(result['equity_curve'])
        figure.add_trace(go.Scatter(x=curve.time_utc,y=(curve.equity/result['initial_balance']-1)*100,name=name),row=1,col=1)
    for symbol,frame in daily_frames.items():
        figure.add_trace(go.Scatter(x=frame.index,y=frame.range_percent,name=symbol+' daily range %'),row=2,col=1)
    figure.update_yaxes(title_text='Return %',row=1,col=1)
    figure.update_yaxes(title_text='High-low / open %',row=2,col=1)
    figure.update_layout(title='Gold and EUR/USD — descriptive study, not a validated strategy',height=850,template='plotly_dark')
    figure.write_html(OUTPUT/'market_behaviour_charts.html',include_plotlyjs=True)


def main():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    before=replay.source_fingerprints(ROOT)
    frames,archive=archive_data()
    description,hour_rows,daily_frames=describe(frames)
    signals,screenings={},{}
    for symbol,data in frames.items():
        print('Preparing exact signals: '+symbol,flush=True)
        signals[symbol],screenings[symbol]=replay.prepare_current_signals(data,symbol,START,END)
        print(f'{symbol}: {len(signals[symbol])} technical candidates',flush=True)
    save_json(OUTPUT/'technical_signals.json',{s:{str(k):v for k,v in x.items()} for s,x in signals.items()})
    portfolios={}
    for name,capital,omit in [('current_500',500.,False),('omit_rescue_slope_500',500.,True),('current_1000',1000.,False)]:
        print('Replaying '+name,flush=True)
        with cached_research(signals,screenings,omit):
            result=replay.replay_portfolio(frames,START,END,replay.ModelSettings(initial_balance=capital))
        save_json(OUTPUT/(name+'.json'),result)
        portfolios[name]=result
    episodes,rejected=[],{}
    for symbol,data in frames.items():
        print('Following independent diagnostic episodes: '+symbol,flush=True)
        reasons=Counter()
        for position,signal in signals[symbol].items():
            plan,reason=replay.model_plan(symbol,signal,float(data.iloc[position].open),1000.,replay.ModelSettings(initial_balance=1000.))
            if plan is None:
                reasons[reason]+=1
                continue
            episodes.append(episode(data,symbol,position,signal,plan))
        rejected[symbol]=dict(reasons)
    groups=episode_groups(episodes)
    pd.DataFrame(episodes).to_csv(OUTPUT/'independent_candidate_paths.csv',index=False)
    pd.DataFrame(groups).to_csv(OUTPUT/'candidate_group_diagnostics.csv',index=False)
    charts(portfolios,hour_rows,daily_frames,groups)
    after=replay.source_fingerprints(ROOT)
    assert before==after,'Research altered source identity'
    summary=dict(generated_utc=datetime.now(timezone.utc).isoformat(),study_start_utc=START.isoformat(),study_end_utc=END.isoformat(),
        archives=archive,description=description,screenings=screenings,
        technical_candidates={s:len(v) for s,v in signals.items()},
        portfolio_scenarios={k:portfolio_summary(v) for k,v in portfolios.items()},
        independent_candidate_count=len(episodes),candidate_plan_rejections=rejected,candidate_groups=groups,
        source_sha256=before,source_fingerprints_unchanged=before==after,
        script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        protocol_sha256=hashlib.sha256((OUTPUT/'study_protocol.md').read_bytes()).hexdigest(),
        caveats=['Historical data previously inspected; not an untouched validation sample.',
            'Independent candidate episodes overlap; their averages are not executable portfolio results or independent observations.',
            'Historical bar spreads estimate ask extremes; costs are modeled allowances, not verified commission/swap.',
            'Excursions exclude the terminal candle; intrabar chronology is unknown.',
            'No causal economic-event attribution and no evidence of deliberate stop hunting.',
            'Software execution validates reproducibility only, not trading profitability.'])
    save_json(OUTPUT/'study_results.json',summary)
    print(json.dumps({k:summary[k] for k in ('technical_candidates','portfolio_scenarios','source_fingerprints_unchanged')},indent=2),flush=True)


if __name__=='__main__':
    main()
