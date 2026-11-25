"""One frozen confirmation alternative versus cached current-policy evidence."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from analytics.entry_confirmation import MODE, ROWS, confirmed_features
from dashboard.research_model import INTERVAL
import dashboard.replay as replay
from scripts.research_market_behaviour import archive_data, cached_research, portfolio_summary, save_json, START, END

BASE=ROOT/'reports/market_analysis/market_behaviour_2026-11-16'
OUT=ROOT/'reports/market_analysis/entry_confirmation_2026-11-16'


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    frames,archives=archive_data()
    seeds=json.loads((BASE/'technical_signals.json').read_text())
    signals,screenings,decisions={},{},[]
    for symbol,items in seeds.items():
        data=frames[symbol]
        found={}
        counts={}
        print('Comparing fixed confirmation rule: '+symbol,flush=True)
        for number,(key,seed) in enumerate(items.items()):
            i=int(key)  # Original next-open entry bar becomes the confirmation bar.
            if i+1>=len(data) or data.index[i+1]!=data.index[i]+INTERVAL[symbol]:
                continue
            if not START<=data.index[i+1]<END:
                continue
            current=confirmed_features(data.iloc[max(0,i-ROWS[symbol]+1):i+1],symbol)
            reason=current['reason']
            counts[reason]=counts.get(reason,0)+1
            if current.get('side'):
                found[i+1]=current
            decisions.append(dict(symbol=symbol,seed_entry_index=i,
                seed_time_utc=data.index[i].isoformat(),confirmation_time_utc=data.index[i+1].isoformat(),
                confirmation_passed=bool(current.get('side')),reason=reason))
            if number%200==0:
                print(f'{symbol}: {number}/{len(items)}',flush=True)
        signals[symbol],screenings[symbol]=found,counts
        print(f'{symbol}: {len(found)} confirmed candidates / {len(items)} seeds',flush=True)
    save_json(OUT/'confirmed_signals.json',{s:{str(k):v for k,v in items.items()} for s,items in signals.items()})
    pd.DataFrame(decisions).to_csv(OUT/'seed_confirmation_decisions.csv',index=False)
    scenarios={}
    for name,capital,slip in [('confirmed_500',500.,1),('confirmed_1000',1000.,1),('confirmed_500_slippage2',500.,2)]:
        print('Replaying '+name,flush=True)
        with cached_research(signals,screenings):
            result=replay.replay_portfolio(frames,START,END,replay.ModelSettings(initial_balance=capital,slippage_multiplier=slip))
        save_json(OUT/(name+'.json'),result)
        scenarios[name]=portfolio_summary(result)
    matches={}
    for name in ['current_500','current_1000']:
        old=json.loads((BASE/(name+'.json')).read_text())
        outcomes={ (d['symbol'],d['seed_time_utc']):d for d in decisions }
        rows=[]
        for trade in old['trades']:
            d=outcomes.get((trade['symbol'],trade['entry_time_utc']))
            rows.append(dict(symbol=trade['symbol'],entry_time_utc=trade['entry_time_utc'],
                baseline_pnl=trade['net_pnl'],baseline_win=trade['net_pnl']>0,
                confirmation_passed=d['confirmation_passed'] if d else None))
        pd.DataFrame(rows).to_csv(OUT/(name+'_matched_seed_outcomes.csv'),index=False)
        matches[name]=dict(baseline_closed_trades=len(rows),
            winners_without_confirmation=sum(r['baseline_win'] and r['confirmation_passed'] is False for r in rows),
            losers_without_confirmation=sum(not r['baseline_win'] and r['confirmation_passed'] is False for r in rows),
            seed_passed=sum(r['confirmation_passed'] is True for r in rows),
            unmatched=sum(r['confirmation_passed'] is None for r in rows))
    result=dict(version=MODE,generated_at_utc=datetime.now(timezone.utc).isoformat(),archives=archives,
        fixed_spec_sha256=hashlib.sha256((ROOT/'config/entry_confirmation_spec.json').read_bytes()).hexdigest(),
        confirmation_kernel_sha256=hashlib.sha256((ROOT/'analytics/entry_confirmation.py').read_bytes()).hexdigest(),
        baseline_source_results='reports/market_analysis/market_behaviour_2026-11-16/study_results.json',
        scenarios=scenarios,matched_baseline_seeds=matches,screenings=screenings,
        notes=['Frozen one-bar follow-through; no parameter search.',
            'Current features and stop/size/target room are recalculated at confirmation.',
            'Previously inspected development history, not untouched out-of-sample.',
            'Rejected baseline winners are missed opportunities, not proof the later quote would have won.',
            'Model costs/Ask extremes/margin are assumptions; no actual execution.'])
    save_json(OUT/'comparison.json',result)
    print(json.dumps(result,indent=2),flush=True)


if __name__=='__main__':
    main()
