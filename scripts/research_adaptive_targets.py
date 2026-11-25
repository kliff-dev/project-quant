"""One frozen entry setup, two exit policies, saved-data research only."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
import math
from pathlib import Path
import sys

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.account_research import ReplaySettings, replay_account
from analytics.adaptive_targets import policy_plan, prepare_pullback_signals
from analytics.candidate_metrics import research_metrics
from analytics.paired_exits import replay_opportunity
from analytics.setup_research import validate_bars
from engine.execution_audit import atomic_json
from scripts.research_six_month_account import flat_csv, monthly_rows

SPEC = ROOT / 'reports/market_analysis/adaptive_target_spec_2026-11-14.json'
SPEC_SHA = 'c149c1412cbb1ca78ca9f7bf341c6d251d226cd86da42051d96238fb0dcb17f8'
OUTPUT = ROOT / 'reports/market_analysis/adaptive_targets_2026-11-14'
POLICIES = ('fixed_2r', 'adaptive_1_2_3')
PHASES = ('development', 'validation', 'recent')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def outcomes(records):
    closed = [t for t in records if t['status'] == 'closed']
    result = research_metrics(dict(trades=closed))
    positive = sum(t['net_pnl'] for t in closed if t['net_pnl'] > 0)
    negative = -sum(t['net_pnl'] for t in closed if t['net_pnl'] < 0)
    result.update(mean_completed_r=sum(t['realized_r'] for t in closed) / len(closed) if closed else None,
                  net_completed_trade_pnl=round(sum(t['net_pnl'] for t in closed), 2),
                  profit_factor=positive / negative if negative else None,
                  open_positions=sum(t['status'] != 'closed' for t in records))
    return result


def branches(run):
    records = [*run['trades'], *([run['open_position']] if run['open_position'] else [])]
    return {str(r): outcomes([t for t in records if t['selected_reward_r'] == r]) for r in (1, 2, 3)}


def audit_account(run):
    previous_exit = None
    records = [*run['trades'], *([run['open_position']] if run['open_position'] else [])]
    for t in records:
        entry = pd.Timestamp(t['entry_time_utc'])
        assert pd.Timestamp(t['signal']['decision_time_utc']) == entry
        assert pd.Timestamp(t['obstacle_confirmed_at_utc']) <= entry
        assert pd.Timestamp(t['signal']['input_window_start_utc']) < entry
        assert pd.Timestamp(t['signal']['last_completed_h4_open_utc']) + pd.Timedelta(hours=4) <= entry
        if previous_exit is not None:
            assert entry > previous_exit
        previous_exit = pd.Timestamp(t['exit_time_utc']) if t['status'] == 'closed' else None
        assert t['planned_risk'] <= t['risk_budget'] + 1e-9 <= t['balance_before_entry'] * .01 + 1e-8
        assert .01 <= t['lots'] <= .1 and math.isclose(t['lots'] / .01, round(t['lots'] / .01), abs_tol=1e-8)
        d = t['direction']
        assert d * (t['reference_entry'] - t['stop_loss']) > 0 and d * (t['take_profit'] - t['reference_entry']) > 0
        modeled_reward = d * (t['take_profit'] - d * t['slip'] - t['entry_price']) * 100000 * t['lots'] - t['cost_reserve']
        assert modeled_reward + 1e-7 >= t['selected_reward_r'] * t['planned_risk']
        assert t['target_room_flags']['1']
        if t['exit_policy'] == 'adaptive_1_2_3':
            assert t['target_room_flags'][str(t['selected_reward_r'])]
            if t['selected_reward_r'] == 3:
                assert t['strong_context'] and t['normal_volatility']
    opened = run['open_position']
    open_cost = opened['entry_fee'] + opened['financing_cost'] if opened else 0.
    s = run['summary']
    assert math.isclose(s['final_balance'], s['initial_balance'] + sum(t['net_pnl'] for t in run['trades']) - open_cost, abs_tol=1e-7)
    assert math.isclose(s['final_equity'], s['final_balance'] + (opened['unrealized_price_pnl'] if opened else 0), abs_tol=1e-7)
    return len(records)


def phase_gate(s, minimum):
    return bool(s['completed_trades'] >= minimum and s['mean_completed_r'] is not None and s['mean_completed_r'] > 0
                and s['final_equity'] > s['initial_balance'] and s['profit_factor'] is not None and s['profit_factor'] > 1
                and s['max_h1_close_equity_drawdown_percent'] <= 15)


def num(value, digits=1):
    return 'unavailable' if value is None else f'{value:.{digits}f}'


def report(payload):
    rows = payload['runs']
    primary = [r for r in rows if r['cost_case'] == 'conservative' and r['ambiguity'] == 'stop_first']
    lines = ['# One pullback setup: fixed versus adaptive targets', '', '## Technical Summary', '',
             '**One frozen entry setup, no new entry-family search:** existing EURUSD H4-trend/H1 EMA20-reclaim, minimum 1 ATR structural stop and 1% planned risk ceiling. Compare fixed 2R with a predeclared adaptive 1R/2R/3R policy.', '',
             '**Historical research only. No live policy, broker position, monitor or authorization changed.**', '',
             '| Period / policy | Wins / closed | Net win rate | Balance | Marked equity | Mean net R | H1-close drawdown |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for r in primary:
        s = r['summary']
        lines.append(f'| {r["phase"]} / {r["policy"]} | {s["wins"]} / {s["completed_trades"]} | {num(s["win_rate_percent"])}% | ${s["final_balance"]:.2f} | ${s["final_equity"]:.2f} | {num(s["mean_completed_r"],3)} | {s["max_h1_close_equity_drawdown_percent"]:.2f}% |')
    lines += ['', 'A higher win rate is not sufficient: compare the equity difference and mean net R, and inspect the small closed-trade counts before drawing conclusions. No branch has been removed or retuned after seeing its results.', '',
              '### Adaptive minus fixed: conservative account differences', '',
              '| Period | Equity difference USD | Mean net R difference | Drawdown difference, percentage points |',
              '| --- | ---: | ---: | ---: |']
    for phase in PHASES:
        a = payload['assessment']
        lines.append(f'| {phase} | {a[phase+"_adaptive_minus_fixed_equity_usd"]:+.2f} | {num(a[phase+"_adaptive_minus_fixed_mean_r"],4)} | {a[phase+"_adaptive_minus_fixed_drawdown_percentage_points"]:+.2f} |')
    lines += ['', 'A positive drawdown difference means adaptive had a deeper H1-close equity drawdown. Each arm recalculates size from its own account balance; return differences may include compounding and lot rounding as well as exit effects.', '',
              'Periods (Manila): development 14 May 2024–14 May 2025; validation comparison 14 May 2025–14 May 2026; recent comparison 14 May–14 November 2026, latest completed Friday session ending 06:00. Each account/phase starts flat at $500. These periods were inspected in prior research; they are descriptive comparisons, not pristine holdouts.', '',
              '[Interactive equity comparison](comparison.html) · [All summaries and hashes](summary.json) · [Inspection table](comparison.csv) · [Verification](verification.json) · [Frozen rules](../adaptive_target_spec_2026-11-14.json)', '',
              '## Root Cause', '',
              'The earlier entry candidates had not established profitable expectancy. A smaller target can raise the observed winning percentage while reducing payoff. This experiment measures whether a single rule-based adaptation improves net outcomes, rather than assuming a larger target is appropriate whenever a chart looks strong.', '',
              '## How It Was Diagnosed', '',
              '### Exactly what changes', '',
              '- Entry remains the same previously defined trend-reclaim setup; no breakout or range-reversal entry is added.',
              '- Both arms require at least 1R of modeled target room before the nearest directional historical H4 swing reference. Swings use two completed H4 neighbors on either side within the last 120 completed H4 rows. Previously breached references are retained; no reference means unknown room and a skipped entry.',
              '- Room uses actual tick-rounded targets with planning reserves, a .1 H1 ATR buffer, and ask-side correction for short exits. It does not mean the target will be reached.',
              '- Adaptive 3R requires room plus H4 EMA50/200 separation >=1 H4 ATR, five-H4 EMA50 movement >=.25 H4 ATR, and H1 ATR/last100-ATR-median between .75 and 1.5 inclusive. These are fixed hypotheses, not estimated win probabilities.',
              '- Otherwise use 2R if it fits, then 1R if it fits. SL/TP remain frozen after entry. The stop, lots, risk and reserve do not change between candidate targets for the same entry and balance.',
              '- The fixed control always uses 2R on that shared 1R-room-qualified stream. It may target beyond the historical reference. Its added room filter means it is not the original unfiltered baseline.', '',
              '### Recent cost and execution sensitivities', '',
              '| Cost case / policy | Wins / closed | Equity | Mean net R | Drawdown | Open |',
              '| --- | ---: | ---: | ---: | ---: | ---: |']
    for r in rows:
        if r['phase'] != 'recent' or r['ambiguity'] != 'stop_first':
            continue
        s = r['summary']
        lines.append(f'| {r["cost_case"]} / {r["policy"]} | {s["wins"]} / {s["completed_trades"]} | ${s["final_equity"]:.2f} | {num(s["mean_completed_r"],3)} | {s["max_h1_close_equity_drawdown_percent"]:.2f}% | {s["open_positions"]} |')
    lines += ['', 'All cases include recorded H1 spreads. Conservative costs include two points adverse slippage per side and a hypothetical debit equal to 5% of the risk budget. The no-extra-fee case retains that allowance only in planning. Financing stress adds an assumed $0.10 per .01 lot per ordinary NY17:00 rollover with Wednesday triple units; execution stress also doubles slippage to four points. These are sensitivities, not verified historical fee/swap schedules.', '',
              '### Adaptive target branches: conservative account trades', '',
              '| Period / target | Closed | Net win rate | Descriptive 95% interval | Mean net R | Average net win/loss |',
              '| --- | ---: | ---: | --- | ---: | ---: |']
    for r in primary:
        if r['policy'] != 'adaptive_1_2_3':
            continue
        for target, b in r['branches'].items():
            interval = f'{num(b["win_rate_wilson95_lower_percent"])}–{num(b["win_rate_wilson95_upper_percent"])}%' if b['completed_trades'] else 'unavailable'
            lines.append(f'| {r["phase"]} / {target}R | {b["completed_trades"]} | {num(b["win_rate_percent"])}% | {interval} | {num(b["mean_completed_r"],3)} | {num(b["realized_payoff_ratio"],2)} |')
    lines += ['', 'Wilson intervals assume independent observations; correlated trades, regime changes and prior strategy searches add uncertainty beyond these bounds. A sparse branch does not establish a stable success probability.', '',
              '### Predeclared assessment', '', *[f'- **{k}:** {v}' for k, v in payload['assessment'].items()], '',
              'The 1R gate requires at least 30 closed development 1R trades, positive conservative mean R/profit factor, and positive 1R financing-stress mean R. A failure keeps the entire adaptive policy research-only; the losing/under-sampled branch was not removed and retested. Sparse 2R/3R branches remain unvalidated too. No fixed 45% threshold substitutes for actual net expectancy.', '',
              '### Opportunity flow: recent conservative case', '',
              '| Policy | Raw entries | Room-qualified at $500 | Screening rejects | Account inventory blocks | Actual entry rejects | Closed / open |',
              '| --- | ---: | ---: | --- | ---: | ---: | --- |']
    for r in primary:
        if r['phase'] == 'recent':
            f, s = r['flow'], r['summary']
            lines.append(f'| {r["policy"]} | {f["raw_signals"]} | {f["screen_eligible_at_500"]} | {json.dumps(f["screen_rejections"],sort_keys=True)} | {s["inventory_blocked_signals"]} | {s["entry_risk_rejections"]} | {s["completed_trades"]} / {s["open_positions"]} |')
    lines += ['', 'Screening uses a fixed $500 basis to compare eligibility. Actual accounts compound and can differ in affordability as well as occupancy. Therefore they share entry rules, not necessarily an identical final trade list.', '',
              '### Paired exit diagnostic', '',
              '| Period / cost | Common eligible | Both closed | Incomplete pairs | Mean adaptive-minus-fixed R |',
              '| --- | ---: | ---: | ---: | ---: |']
    for p in payload['paired']:
        lines.append(f'| {p["phase"]} / {p["cost_case"]} | {p["eligible_pairs"]} | {p["both_closed_pairs"]} | {p["incomplete_pairs"]} | {num(p["mean_delta_r_both_closed"],3)} |')
    lines += ['', 'Each matched opportunity starts independently with $500 and identical entry, stop, lots and reserve. Overlap is allowed only for this diagnostic; its summed P&L is not an executable portfolio. The delta uses only pairs where both exits occurred before the phase boundary; incomplete/open pairs are retained separately, which also reveals boundary-censoring limitations.', '',
              '## Solution', '',
              'The adaptive policy, optional offline planner hook, paired diagnostic and reproducible account study are implemented. Definitions are saved as [non-executable research rules](../../../strategies/adaptive_pullback_2026-11-14.json).', '',
              '```powershell', r'.\.venv\Scripts\python.exe -B scripts/research_adaptive_targets.py', '```', '',
              'Limitations: H1 bid candles plus constant per-bar spread approximate ask extremes and intrabar ordering. Stop-first is primary; recent target-first sensitivity is retained. Historical margin, stop-out, broker permissions, outages and financing schedules are not known. H1-close drawdown can miss deeper losses. SL/TP-only positions can span days/weekends, and gaps can exceed the planned 1% risk. Open positions remain marked rather than forced closed.', '',
              '## What I Learned', '',
              '- One entry setup can still have several exit branches that each need evidence.',
              '- The same risk budget does not imply the same payoff or win rate when targets change.',
              '- Shared eligibility and matched diagnostics help distinguish exit effects from trade-selection and occupancy effects.',
              '- A higher historical win rate alone does not validate an adaptive policy.', '',
              '## If This Happens Again', '',
              'Inspect branch counts and mean net R alongside aggregate equity, then inspect costs, no-trade reasons and the paired/account distinction. Keep the policy frozen; new thresholds need a new registered experiment. Independent future observations remain necessary.', '',
              'References: [CME position sizing](https://www.cmegroup.com/education/courses/trade-and-risk-management/proper-position-size), [CME expectancy](https://www.cmegroup.com/education/courses/trading-psychology/the-mathematics-of-trading-success), [backtest overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).']
    (OUTPUT / 'findings.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    if digest(SPEC) != SPEC_SHA:
        raise ValueError('Frozen adaptive specification changed.')
    spec = json.loads(SPEC.read_text(encoding='utf-8'))
    manifest = json.loads((ROOT / spec['input_manifest']).read_text(encoding='utf-8'))
    if digest(ROOT / manifest['file']) != spec['input_sha256'] or manifest['sha256'] != spec['input_sha256']:
        raise ValueError('Frozen market data changed.')
    if digest(ROOT / 'analytics/candidate_signals.py') != spec['original_entry_code_sha256']:
        raise ValueError('The single entry setup changed.')
    c = manifest['contract']
    if any(c[k] != value for k, value in dict(currency_base='EUR', currency_profit='USD', trade_contract_size=100000., point=.00001, trade_tick_size=.00001, volume_min=.01, volume_step=.01).items()):
        raise ValueError('Expected the frozen linear EURUSD/USD contract.')
    data = pd.read_csv(ROOT / manifest['file'])
    data.index = pd.DatetimeIndex(pd.to_datetime(data.pop('timestamp_utc'), utc=True))
    data = validate_bars(data)
    boundaries = [pd.Timestamp(spec[k]) for k in ('development_start_utc', 'validation_start_utc', 'recent_start_utc', 'end_utc')]
    assert boundaries[-1] == data.index[-1] + pd.Timedelta(hours=1)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    protected = ['config/trading_policy.json', 'logs/monitor/status.json', 'logs/monitor/state.json', 'data/processed/trade_journal.json', 'logs/demo_test_state.json',
                 'logs/sessions/mon-20261113T110000Z-a2986a1e/demo_test_state.json', 'logs/sessions/sltp-5pct-2r-20261113/demo_test_state.json']
    protected_hashes = {p: digest(ROOT / p) for p in protected}
    code = ['analytics/account_research.py', 'analytics/adaptive_targets.py', 'analytics/paired_exits.py', 'analytics/candidate_signals.py', 'analytics/candidate_metrics.py', 'scripts/research_adaptive_targets.py']
    implementation = dict(recorded_before_outcomes_at_utc=datetime.now(timezone.utc).isoformat(), spec_sha256=SPEC_SHA,
                          source_sha256=spec['input_sha256'], code_sha256={p: digest(ROOT / p) for p in code})
    if not (OUTPUT / 'implementation_initial.json').exists():
        atomic_json(OUTPUT / 'implementation_initial.json', implementation)
    signals = prepare_pullback_signals(data, first_entry=boundaries[0])
    atomic_json(OUTPUT / 'signals.json', {str(k): v for k, v in signals.items()})
    print(f'Prepared {len(signals)} signals from exactly one frozen entry setup.', flush=True)
    rows, paired, recent_curves = [], [], {}
    audited = 0
    for phase_index, phase in enumerate(PHASES):
        start, end = boundaries[phase_index:phase_index + 2]
        phase_signals = {i: s for i, s in signals.items() if start <= data.index[i] < end}
        for case, cost in spec['cost_cases'].items():
            settings = ReplaySettings(minimum_stop_atr=1., reward_r=2., **cost)
            for policy in POLICIES:
                builder = partial(policy_plan, policy=policy)
                rejects = Counter()
                eligible = 0
                for i, signal in phase_signals.items():
                    plan, reason = builder(signal, float(data.open.iloc[i]), 500., settings)
                    if plan:
                        eligible += 1
                    else:
                        rejects[reason] += 1
                run = replay_account(data, phase_signals, start, end, settings, plan_builder=builder)
                run['summary']['config_id'] = policy
                audited += audit_account(run)
                path = OUTPUT / case / policy
                path.mkdir(parents=True, exist_ok=True)
                atomic_json(path / f'{phase}_account.json', {k: v for k, v in run.items() if k != 'equity_curve'})
                flat_csv(run['trades'], path / f'{phase}_trades.csv')
                flat_csv(run['equity_curve'], path / f'{phase}_equity.csv')
                row = dict(phase=phase, cost_case=case, policy=policy, ambiguity='stop_first', settings=asdict(settings),
                           summary=run['summary'], metrics=research_metrics(run), branches=branches(run),
                           flow=dict(raw_signals=len(phase_signals), screen_eligible_at_500=eligible, screen_rejections=dict(rejects)),
                           account_file=str((path / f'{phase}_account.json').relative_to(OUTPUT)).replace('\\', '/'))
                rows.append(row)
                if phase == 'recent' and case == 'conservative':
                    recent_curves[policy] = run
                    pd.DataFrame(monthly_rows(run)).to_csv(OUTPUT / f'{policy}_monthly.csv', index=False)
                print(f'{phase}/{case}/{policy}: {num(run["summary"]["win_rate_percent"])}% / {run["summary"]["completed_trades"]}; equity={run["summary"]["final_equity"]:.2f}', flush=True)
            pairs, paired_rejects = [], Counter()
            for i, signal in phase_signals.items():
                results = {policy: replay_opportunity(data, i, signal, end, settings, plan_builder=partial(policy_plan, policy=policy)) for policy in POLICIES}
                a, b = [results[p]['trade'] for p in POLICIES]
                if a is None or b is None:
                    assert a is None and b is None and results[POLICIES[0]]['rejection'] == results[POLICIES[1]]['rejection']
                    paired_rejects[results[POLICIES[0]]['rejection']] += 1
                    continue
                for field in ('entry_price', 'stop_loss', 'lots', 'planned_risk', 'risk_budget', 'entry_fee', 'cost_reserve'):
                    assert a[field] == b[field]
                both_closed = a['status'] == b['status'] == 'closed'
                pairs.append(dict(entry_index=i, entry_time_utc=a['entry_time_utc'], fixed=a, adaptive=b, both_closed=both_closed,
                                  delta_r=b['realized_r'] - a['realized_r'] if both_closed else None))
            complete = [p for p in pairs if p['both_closed']]
            pdirectory = OUTPUT / 'paired' / case
            pdirectory.mkdir(parents=True, exist_ok=True)
            atomic_json(pdirectory / f'{phase}.json', dict(pairs=pairs, rejections=dict(paired_rejects), warning='Overlapping isolated opportunities, not an executable portfolio.'))
            paired.append(dict(phase=phase, cost_case=case, eligible_pairs=len(pairs), both_closed_pairs=len(complete),
                               incomplete_pairs=len(pairs) - len(complete), mean_delta_r_both_closed=sum(p['delta_r'] for p in complete) / len(complete) if complete else None,
                               fixed=outcomes([p['fixed'] for p in pairs]), adaptive=outcomes([p['adaptive'] for p in pairs]), rejections=dict(paired_rejects)))
    for policy in POLICIES:
        settings = ReplaySettings(minimum_stop_atr=1., reward_r=2., ambiguity='target_first', **spec['cost_cases']['conservative'])
        run = replay_account(data, signals, boundaries[2], boundaries[3], settings, plan_builder=partial(policy_plan, policy=policy))
        run['summary']['config_id'] = policy
        audited += audit_account(run)
        path = OUTPUT / 'ambiguity' / policy
        atomic_json(path / 'recent_target_first.json', {k: v for k, v in run.items() if k != 'equity_curve'})
        rows.append(dict(phase='recent', cost_case='conservative', policy=policy, ambiguity='target_first', settings=asdict(settings), summary=run['summary'],
                         metrics=research_metrics(run), branches=branches(run), account_file=str((path / 'recent_target_first.json').relative_to(OUTPUT)).replace('\\', '/')))
    lookup = {(r['phase'], r['cost_case'], r['policy']): r for r in rows if r['ambiguity'] == 'stop_first'}
    assessment = {}
    for phase in PHASES:
        adaptive = lookup[(phase, 'conservative', 'adaptive_1_2_3')]['summary']
        fixed = lookup[(phase, 'conservative', 'fixed_2r')]['summary']
        assessment[f'{phase}_adaptive_primary_gate'] = phase_gate(adaptive, 30 if phase == 'development' else 20)
        assessment[f'{phase}_adaptive_minus_fixed_mean_r'] = adaptive['mean_completed_r'] - fixed['mean_completed_r'] if adaptive['mean_completed_r'] is not None and fixed['mean_completed_r'] is not None else None
        assessment[f'{phase}_adaptive_minus_fixed_equity_usd'] = round(adaptive['final_equity'] - fixed['final_equity'], 2)
        assessment[f'{phase}_adaptive_minus_fixed_drawdown_percentage_points'] = adaptive['max_h1_close_equity_drawdown_percent'] - fixed['max_h1_close_equity_drawdown_percent']
        if phase != 'development':
            stress = lookup[(phase, 'financing_stress', 'adaptive_1_2_3')]['summary']
            assessment[f'{phase}_adaptive_financing_positive'] = stress['mean_completed_r'] is not None and stress['mean_completed_r'] > 0 and stress['final_equity'] > 500
    one = lookup[('development', 'conservative', 'adaptive_1_2_3')]['branches']['1']
    one_stress = lookup[('development', 'financing_stress', 'adaptive_1_2_3')]['branches']['1']
    assessment['one_r_development_closed'] = one['completed_trades']
    assessment['one_r_development_gate'] = bool(one['completed_trades'] >= 30 and one['mean_completed_r'] is not None and one['mean_completed_r'] > 0
                                              and one['profit_factor'] is not None and one['profit_factor'] > 1
                                              and one_stress['mean_completed_r'] is not None and one_stress['mean_completed_r'] > 0)
    assessment['deployment_status'] = 'research_only; no automatic deployment; independent future evidence still required'
    payload = dict(generated_at_utc=datetime.now(timezone.utc).isoformat(), spec=spec, spec_sha256=SPEC_SHA, source_manifest=manifest,
                   implementation=implementation, raw_signal_count=len(signals), runs=rows, paired=paired, assessment=assessment,
                   orders_sent=0, monitor_started=False)
    atomic_json(OUTPUT / 'summary.json', payload)
    flat_csv([dict(phase=r['phase'], cost_case=r['cost_case'], policy=r['policy'], ambiguity=r['ambiguity'], **r['summary']) for r in rows], OUTPUT / 'comparison.csv')
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=.09, subplot_titles=('Recent hypothetical equity: shared room-qualified entries', 'H1-close equity drawdown (%)'))
    for policy, run in recent_curves.items():
        curve = pd.DataFrame(run['equity_curve'])
        fig.add_trace(go.Scatter(x=curve.time_utc, y=curve.equity, name=policy), row=1, col=1)
        fig.add_trace(go.Scatter(x=curve.time_utc, y=curve.drawdown_percent, name=policy + ' drawdown'), row=2, col=1)
    fig.add_hline(y=500, line_dash='dash', line_color='#888', row=1, col=1)
    fig.update_layout(title='One setup, two frozen target policies: conservative modeled costs', template='plotly_white', height=800)
    fig.update_yaxes(title_text='Equity USD', row=1, col=1)
    fig.update_xaxes(title_text='UTC', row=2, col=1)
    fig.write_html(OUTPUT / 'comparison.html', include_plotlyjs=True)
    atomic_json(ROOT / 'strategies/adaptive_pullback_2026-11-14.json', dict(status='offline_research_only', executable=False, spec_sha256=SPEC_SHA,
                entry=spec['entry'], stop_and_risk=spec['stop_and_risk'], policies=spec['policies'], obstacle=spec['obstacle'],
                strong_context=spec['strong_context'], normal_volatility=spec['normal_volatility'], assessment=assessment))
    report(payload)
    assert all(digest(ROOT / p) == sha for p, sha in implementation['code_sha256'].items())
    assert {p: digest(ROOT / p) for p in protected} == protected_hashes
    atomic_json(OUTPUT / 'verification.json', dict(completed_at_utc=datetime.now(timezone.utc).isoformat(), spec_and_data_hashes_verified=True,
                account_runs=len(rows), paired_phase_cost_cases=len(paired), audited_account_entries=audited, paired_geometry_equal=True,
                protected_files_unchanged=protected_hashes, code_hashes_verified=True, orders_sent=0, monitor_started=False,
                automated_tests='Final full suite recorded separately.', scope='Actual offline data replay, not historical broker fill verification.'))
    print(json.dumps(dict(assessment=assessment, account_runs=len(rows), paired_cases=len(paired), audited_account_entries=audited, report=str(OUTPUT / 'findings.md')), indent=2))


if __name__ == '__main__':
    main()
