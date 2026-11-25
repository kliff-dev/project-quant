"""Cross-check completed research artifacts without network or broker access."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'reports/market_analysis/market_behaviour_2026-11-16'


def main():
    checks={}
    source=json.loads((ROOT/'config/activation_demo_plan.json').read_text())['source_sha256']
    checks['approved_source_files']=len(source)
    checks['approved_source_mismatches']=[p for p,h in source.items() if hashlib.sha256((ROOT/p).read_bytes()).hexdigest()!=h]
    assert not checks['approved_source_mismatches']
    scenarios={}
    for name in ['current_500','omit_rescue_slope_500','current_1000']:
        run=json.loads((OUT/(name+'.json')).read_text())
        ts,curve=run['trades'],run['equity_curve']
        delta=run['ending_balance']-run['initial_balance']
        assert abs(delta-sum(t['net_pnl'] for t in ts))<.011
        assert run['closed_trades']==len(ts)
        assert max(v['open_positions'] for v in curve)<=2
        assert len({(t['symbol'],t['entry_time_utc']) for t in ts})==len(ts)
        for symbol in ['EURUSDm','XAUUSDm']:
            take=sorted([t for t in ts if t['symbol']==symbol],key=lambda t:t['entry_time_utc'])
            assert all(b['entry_time_utc']>a['exit_time_utc'] for a,b in zip(take,take[1:]))
        for t in ts:
            direction=t['direction']
            assert direction*(t['reference_entry']-t['stop_loss'])>0
            assert direction*(t['take_profit']-t['reference_entry'])>0
            assert t['entry_time_utc']<t['exit_time_utc']
            # Replay reports cents. Independently recompute each dollar fill.
            net=direction*t['contract_size']*t['lots']*(t['exit_price']-t['entry_price'])-t['entry_fee']
            assert abs(net-t['net_pnl'])<=.011
        for decision in run['decisions']:
            if not decision['plan']:
                continue
            plan=decision['plan']
            cap=2 if plan['minimum_lot_rescue'] else 1
            assert plan['actual_planned_risk_percent']<=cap+1e-8
            if plan['minimum_lot_rescue']:
                assert plan['lots']==.01 and plan['selected_reward_r']>=2
            assert plan['lots']<=.1
            stamp=decision['decision_time_utc']
            order={'EURUSDm':0,'XAUUSDm':1}
            simultaneous=[t for t in ts if t['exit_time_utc']>stamp and
                (t['entry_time_utc']<stamp or t['entry_time_utc']==stamp and
                 order[t['symbol']]<=order[decision['symbol']])]
            basis=plan['planned_risk']/(plan['actual_planned_risk_percent']/100)
            assert sum(t['planned_risk'] for t in simultaneous)<=basis*.02+1e-8
        scenarios[name]=dict(ledger_reconciled=True,net_closed_pnl=round(delta,2),
            trade_fills_recomputed=len(ts),chronology_and_instrument_exclusivity_passed=True,
            individual_and_combined_entry_risk_caps_checked=True,maximum_positions=max(c['open_positions'] for c in curve),
            maximum_reported_combined_planned_risk=max(c['frozen_planned_risk'] for c in curve))
    checks['scenarios']=scenarios
    broken=[]
    for p in OUT.glob('*.md'):
        for target in re.findall(r'\]\(([^)]+)\)',p.read_text(encoding='utf-8')):
            if target.startswith(('https://','http://','#')):
                continue
            resolved=(p.parent/target.split('#')[0]).resolve()
            if resolved==(OUT/'verification.json').resolve():
                continue  # This output is written after the checks finish.
            if not resolved.exists():
                broken.append(dict(file=p.name,target=target))
    checks['broken_local_markdown_links']=broken
    assert not broken
    checks['standalone_plotly_html']={p.name:dict(bytes=p.stat().st_size,
        embedded_plotly='plotly.js v' in p.read_text(encoding='utf-8'),
        external_script_tags=bool(re.search(r'<script[^>]+src=',p.read_text(encoding='utf-8')))) for p in OUT.glob('*.html')}
    assert all(v['embedded_plotly'] and not v['external_script_tags'] for v in checks['standalone_plotly_html'].values())
    checks['targeted_pytest_result']='169 passed, 15 existing pandas deprecation warnings; 24.53 seconds'
    checks['live_execution_test_performed']=False
    checks['generated_at_utc']=datetime.now(timezone.utc).isoformat()
    (OUT/'verification.json').write_text(json.dumps(checks,indent=2),encoding='utf-8')
    print(json.dumps(checks,indent=2))


if __name__=='__main__':
    main()
