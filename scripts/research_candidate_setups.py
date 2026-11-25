"""Finite, registered offline entry-family study. Never connects to a terminal."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
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
from analytics.candidate_metrics import phase_passes, research_metrics, select_development
from analytics.candidate_signals import prepare_candidate_signals
from analytics.setup_research import validate_bars
from engine.execution_audit import atomic_json
from scripts.research_six_month_account import flat_csv, monthly_rows

SPEC_FILE = ROOT / 'reports/market_analysis/candidate_strategy_spec_2026-11-14.json'
SPEC_SHA256 = 'd7701c7251390144b01279d28ae29ccb8ce79d53e3f7ddc790cb7dc518632597'
OUTPUT = ROOT / 'reports/market_analysis/candidate_setups_2026-11-14'


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_run(run: dict, reward: float) -> int:
    """Independent geometry, causality, position, money and planned-payoff checks."""
    records = [*run['trades'], *([run['open_position']] if run['open_position'] else [])]
    previous_exit = None
    for t in records:
        direction = t['direction']
        prices = [t['entry_price'], t['reference_entry'], t['stop_loss'], t['take_profit']]
        if not all(math.isfinite(p) and p > 0 for p in prices):
            raise ValueError('Invalid research price.')
        assert direction * (t['reference_entry'] - t['stop_loss']) > 0
        assert direction * (t['take_profit'] - t['reference_entry']) > 0
        assert .01 <= t['lots'] <= .1 and math.isclose(t['lots'] / .01, round(t['lots'] / .01), abs_tol=1e-8)
        budget = float((Decimal(str(t['balance_before_entry'])) * Decimal('.01')).quantize(Decimal('.01'), rounding=ROUND_FLOOR))
        assert t['planned_risk'] <= t['risk_budget'] + 1e-9 and math.isclose(budget, t['risk_budget'], abs_tol=1e-9)
        entry = pd.Timestamp(t['entry_time_utc'])
        signal = t['signal']
        assert pd.Timestamp(signal['decision_time_utc']) == entry
        assert pd.Timestamp(signal['input_window_start_utc']) < entry
        if previous_exit is not None:
            assert entry > previous_exit
        previous_exit = pd.Timestamp(t['exit_time_utc']) if t['status'] == 'closed' else None
        normal_reward = direction * ((t['take_profit'] - direction * t['slip']) - t['entry_price']) * 100000 * t['lots'] - t['cost_reserve']
        normal_loss = direction * (t['entry_price'] - (t['stop_loss'] - direction * t['slip'])) * 100000 * t['lots'] + t['cost_reserve']
        assert math.isclose(normal_loss, t['planned_risk'], abs_tol=1e-7)
        assert normal_reward + 1e-7 >= reward * t['planned_risk']
    summary = run['summary']
    opened = run['open_position']
    open_cost = opened['entry_fee'] + opened['financing_cost'] if opened else 0.
    assert math.isclose(summary['final_balance'], summary['initial_balance'] + sum(t['net_pnl'] for t in run['trades']) - open_cost, abs_tol=1e-7)
    assert math.isclose(summary['final_equity'], summary['final_balance'] + (opened['unrealized_price_pnl'] if opened else 0), abs_tol=1e-7)
    assert summary['wins'] == sum(t['net_pnl'] > 0 for t in run['trades'])
    assert summary['losses'] == sum(t['net_pnl'] < 0 for t in run['trades'])
    return len(records)


def number(value, digits=1) -> str:
    return 'unavailable' if value is None else f'{value:.{digits}f}'


def write_rules(spec: dict, configs: list[dict], selected: str | None, assessment: dict) -> None:
    atomic_json(ROOT / 'strategies/candidate_setups_2026-11-14.json', dict(
        status='offline_research_only', executable=False, spec_sha256=SPEC_SHA256,
        selected_on_development=selected, later_assessment=assessment,
        families=spec['families'], indicators=spec['indicators'], filters=spec['shared_filters'],
        sizing=spec['sizing'], holding=spec['holding'], configurations=configs,
        warning='Historical targets are not a future win-rate guarantee. No live strategy/policy/authority changed.'))


def report(payload: dict) -> None:
    spec = payload['spec']
    selected = payload['selected_on_development']
    primary = [r for r in payload['runs'] if r['cost_case'] == 'conservative' and r['ambiguity'] == 'stop_first']
    by_id = {(r['config_id'], r['phase']): r for r in primary}
    matches = payload['recent_observed_target_matches']
    lines = ['# New entry-family research: 45% win-rate target', '', '## Technical Summary', '',
             '**Offline research only.** Twelve configurations were declared before their results: three entry families, minimum structural stop floors of 1 or 1.5 ATR, and planned reward targets of 2R or 1.5R. The primary goal was an observed net win rate of at least 45%, positive expectancy and controlled drawdown; 2R choices have priority.', '',
             f'**Development-selected configuration: {selected or "none met the declared requirements"}.**', '',
             'The recent period is 14 May 2026 00:00 through 14 November 2026 06:00 Manila. Each phase starts flat with a hypothetical $500, uses a recalculated 1% planned risk ceiling, and allows one position. Balance and equity are distinct; open positions are excluded from closed win rates.', '',
             '[Interactive comparison](comparison.html) · [Every result and source hashes](summary.json) · [Inspection table](comparison.csv) · [Selection saved before later outcomes](selection.json) · [Verification](verification.json)', '',
             '## Root Cause', '',
             'The existing combined H4/H1 entry setup did not establish positive expectancy in the earlier study. Changing only its stop and target did not establish a durable edge. This study tests different entry mechanisms, while preserving the original baseline and all earlier results. A requested win rate is a hypothesis to test, not a number the research is allowed to manufacture.', '',
             '## How It Was Diagnosed', '',
             'Development: 14 May 2024–14 May 2025 Manila. Validation comparison: 14 May 2025–14 May 2026. Recent descriptive comparison: the following six months. All these data were available in prior research; the split is chronological but is **not an untouched holdout**. Multiple tested families/settings and earlier experiments increase selection bias.', '',
             'One development winner is chosen before any later result is calculated: at least 30 completed development trades, at least 45% net wins, positive mean R and equity change, profit factor >1, H1-close equity drawdown <=15%, and positive results with the declared financing stress. Qualifying 2R targets take priority over 1.5R. Within that tier, higher descriptive Wilson lower win-rate bound ranks first. Validation and recent gates require at least 20 closed trades and the same primary performance thresholds. A later failure does not cause substitution of another winner.', '',
             '### Fixed entry rules', '']
    for family in spec['family_order']:
        rules = spec['families'][family]
        lines += [f'**{family}: {rules["name"]}**', '', rules.get('regime', ''), rules.get('bands', ''),
                  '- BUY: ' + rules['buy'], '- SELL: ' + rules['sell'], '']
    lines += ['All entries use only completed H1 candles, completed UTC H4 context, a rolling 3,000-H1 window, weekday 08:00–14:00 London decisions and the next consecutive H1 open. Recent structural bars must be consecutive. Wilder RSI/ATR seeds and the exact thresholds are frozen in the [registered specification](../candidate_strategy_spec_2026-11-14.json).', '',
              '### Every configuration: primary conservative costs', '',
              '| Entry / minimum stop / target | Development wins / closed | Validation wins / closed | Recent wins / closed | Recent 95% interval | Recent equity | Recent mean R | Recent drawdown |',
              '| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |']
    for config in payload['configurations']:
        cid = config['config_id']
        d, v, r = [by_id[(cid, phase)] for phase in ['development', 'validation', 'recent']]
        def sample(row):
            return f'{number(row["summary"]["win_rate_percent"])}% / {row["summary"]["completed_trades"]}'
        m, s = r['metrics'], r['summary']
        interval = f'{number(m["win_rate_wilson95_lower_percent"])}–{number(m["win_rate_wilson95_upper_percent"])}%'
        lines.append(f'| {cid} | {sample(d)} | {sample(v)} | {sample(r)} | {interval} | ${s["final_equity"]:.2f} | {number(s["mean_completed_r"],3)} | {s["max_h1_close_equity_drawdown_percent"]:.2f}% |')
    lines += ['', 'Intervals describe the observed closed trades under an independence assumption; regime changes, correlated trades and selecting among many trials make overall uncertainty larger. They are not promised future win rates. The table shows net winners, not only target hits. Full metrics separately show target-hit rate and average realized winning/losing payoff.', '',
              '### Did the recent period reach the requested target?', '']
    if matches:
        lines += ['The following configurations descriptively passed the recent conservative gate (>=20 closed trades, >=45% wins, positive mean R/equity/profit factor and <=15% drawdown). They were **not** selected from this period:', '',
                  *[f'- {cid}' for cid in matches]]
    else:
        lines.append('**No configuration passed the recent conservative gate.** A high rate from a smaller sample or under easier fees does not satisfy the declared test. No additional parameter search was performed to force a passing result.')
    if selected:
        lines += ['', '### Fixed chosen candidate across assumptions', '',
                  '| Phase / cost case | Net win rate / closed | Balance | Equity | Mean R | Realized average win/loss | Max loss streak | Median hold hours |',
                  '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
        for row in payload['runs']:
            if row['config_id'] != selected or row['ambiguity'] != 'stop_first':
                continue
            s, m = row['summary'], row['metrics']
            lines.append(f'| {row["phase"]} / {row["cost_case"]} | {number(s["win_rate_percent"])}% / {s["completed_trades"]} | ${s["final_balance"]:.2f} | ${s["final_equity"]:.2f} | {number(s["mean_completed_r"],3)} | {number(m["realized_payoff_ratio"],2)} | {m["max_consecutive_net_losses"]} | {number(m["median_holding_hours_upper_bound"])} |')
        lines += ['', 'Later assessment of that fixed selection:', '', *[f'- {k}: {v}' for k, v in payload['selected_assessment'].items()]]
    lines += ['', '### Costs and execution limits', '',
              '- Every case includes recorded H1 spread and adverse modeled slippage on entry and exit. Ask extremes and intrabar spread variation remain approximations.',
              '- Conservative costs reserve 5% of each risk budget and also debit it as a hypothetical fee: about $0.25 for a $5 risk budget. This is not a verified historical commission schedule. The no-extra-fee case retains the reserve in planning but debits no such fee.',
              '- Financing stress adds an assumed $0.10 per .01 lot per ordinary NY17:00 rollover, with Wednesday triple units; it is not the historical broker rate. Execution stress additionally doubles slippage from two to four points each side.',
              '- Primary results use stop-first when an H1 candle touches both stop and target. Recent conservative target-first outcomes are saved as sensitivity, never used to choose a winner.',
              '- A 2R or 1.5R target describes planned reserved payoff before unknown financing and gap effects. Actual average realized win/loss is a separate statistic. No breakeven, partial exits or time exits inflate the win rate.',
              '- Stops can lose more than the planned 1% through gaps. Fixed 100:1 entry margin is illustrative; historical stop-out, permissions, outages and deeper intrabar drawdown are not replayed.',
              '- SL/TP-only trades can span multiple days or weekends; holding and rollover counts are shown in detailed metrics. This study does not silently add a time exit.', '',
              '## Solution', '',
              'The reproducible candidate definitions, full trade records, metrics, equity comparison and selection audit are saved. The [machine-readable setups](../../../strategies/candidate_setups_2026-11-14.json) are explicitly non-executable research. The current live strategy, account positions, trading policy and expired monitor authorization are unchanged.', '',
              '```powershell', r'.\.venv\Scripts\python.exe -B scripts/research_candidate_setups.py', '```', '',
              'A positive historical result still requires independent future observations and verified broker costs. If no candidate passes, the correct research result is no qualified setup; the target is not relaxed after viewing outcomes.', '',
              '## What I Learned', '',
              '- A higher win rate needs to be judged alongside payoff, costs, drawdown and sample size.',
              '- A 45% rate with a 2R planned target is a research goal; it cannot be guaranteed from past prices.',
              '- Separate trend and range hypotheses before adding filters, and keep every tested losing configuration visible.',
              '- Chronological checks help reveal instability but reused data still require genuinely fresh forward evidence.', '',
              '## If This Happens Again', '',
              'Inspect selection.json before the later-period table. Check closed counts, interval width, actual average payoff, fee case and open equity. Keep this specification frozen; any new thresholds or entry family need a new registered experiment, rather than overwriting a failure.', '',
              'Background: [CME on mathematical expectation](https://www.cmegroup.com/education/courses/trading-psychology/the-mathematics-of-trading-success), [Bailey et al. on backtest overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).']
    (OUTPUT / 'findings.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main() -> None:
    if digest(SPEC_FILE) != SPEC_SHA256:
        raise ValueError('Frozen candidate specification changed.')
    spec = json.loads(SPEC_FILE.read_text(encoding='utf-8'))
    manifest = json.loads((ROOT / spec['input_manifest']).read_text(encoding='utf-8'))
    if digest(ROOT / manifest['file']) != spec['input_sha256'] or manifest['sha256'] != spec['input_sha256']:
        raise ValueError('Frozen source data changed.')
    contract = manifest['contract']
    if any(contract[k] != v for k, v in dict(currency_base='EUR', currency_profit='USD', trade_contract_size=100000., point=.00001, trade_tick_size=.00001, volume_min=.01, volume_step=.01).items()):
        raise ValueError('Research requires the frozen linear EURUSD/USD contract.')
    frame = pd.read_csv(ROOT / manifest['file'])
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop('timestamp_utc'), utc=True))
    frame = validate_bars(frame)
    boundaries = [pd.Timestamp(spec[k]) for k in ['development_start_utc', 'validation_start_utc', 'recent_start_utc', 'end_utc']]
    assert frame.index[-1] + pd.Timedelta(hours=1) == boundaries[-1]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    protected = ['config/trading_policy.json', 'logs/monitor/status.json', 'logs/monitor/state.json', 'data/processed/trade_journal.json', 'logs/demo_test_state.json',
                 'logs/sessions/mon-20261113T110000Z-a2986a1e/demo_test_state.json', 'logs/sessions/sltp-5pct-2r-20261113/demo_test_state.json']
    protected_before = {p: digest(ROOT / p) for p in protected}
    code_paths = ['analytics/account_research.py', 'analytics/candidate_signals.py', 'analytics/candidate_metrics.py', 'scripts/research_candidate_setups.py', 'analytics/setup_research.py']
    implementation = dict(recorded_before_candidate_outcomes_at_utc=datetime.now(timezone.utc).isoformat(),
                          spec_sha256=SPEC_SHA256, code_sha256={p: digest(ROOT / p) for p in code_paths}, source_sha256=spec['input_sha256'])
    if not (OUTPUT / 'implementation_initial.json').exists():
        atomic_json(OUTPUT / 'implementation_initial.json', implementation)
    signals = prepare_candidate_signals(frame, first_entry=boundaries[0], window_bars=3000)
    for family, items in signals.items():
        flat_csv([dict(entry_bar_index=i, **s) for i, s in items.items()], OUTPUT / f'{family}_signals.csv')
    print('Prepared causal signals: ' + json.dumps({k: len(v) for k, v in signals.items()}), flush=True)
    configs = []
    for family in spec['family_order']:
        for minimum in spec['minimum_stop_atr_candidates']:
            for reward in spec['reward_r_candidates']:
                configs.append(dict(config_id=f'{family}_sl{minimum:g}atr_tp{reward:g}r', family=family, minimum_stop_atr=minimum, reward_r=reward))
    assert len(configs) == spec['total_configurations']
    rows, development = [], []
    checked_entries = 0
    fig = make_subplots(rows=2, cols=1, shared_xaxes=False, vertical_spacing=.18,
                        subplot_titles=('Recent six-month net win rates: every fixed configuration', 'Recent six-month hypothetical equity: conservative costs'))
    recent_runs = {}

    def execute(config, phase, case, ambiguity='stop_first', *, keep_equity=False):
        nonlocal checked_entries
        settings = ReplaySettings(minimum_stop_atr=config['minimum_stop_atr'], reward_r=config['reward_r'], **spec['cost_cases'][case], ambiguity=ambiguity)
        phase_index = ['development', 'validation', 'recent'].index(phase)
        run = replay_account(frame, signals[config['family']], boundaries[phase_index], boundaries[phase_index + 1], settings)
        run['summary']['config_id'] = config['config_id']
        for record in [*run['trades'], *([run['open_position']] if run['open_position'] else [])]:
            record['trigger'] = config['family']
        checked_entries += audit_run(run, config['reward_r'])
        metrics = research_metrics(run)
        path = OUTPUT / 'runs' / config['config_id'] / case / ambiguity
        path.mkdir(parents=True, exist_ok=True)
        atomic_json(path / f'{phase}_account.json', {k: v for k, v in run.items() if k != 'equity_curve'})
        flat_csv(run['trades'], path / f'{phase}_trades.csv')
        if keep_equity:
            flat_csv(run['equity_curve'], path / f'{phase}_equity.csv')
        row = dict(config_id=config['config_id'], family=config['family'], reward_r=config['reward_r'], minimum_stop_atr=config['minimum_stop_atr'],
                   phase=phase, cost_case=case, ambiguity=ambiguity, settings=asdict(settings), summary=run['summary'], metrics=metrics,
                   account_file=str((path / f'{phase}_account.json').relative_to(OUTPUT)).replace('\\', '/'))
        rows.append(row)
        return run, row

    # Save the winner using development only, before touching any later outcomes.
    for config in configs:
        _, primary = execute(config, 'development', 'conservative')
        _, stress = execute(config, 'development', 'financing_stress')
        development.append(dict(config_id=config['config_id'], reward_r=config['reward_r'], summary=primary['summary'], metrics=primary['metrics'], stress_summary=stress['summary']))
        print(f'Development {config["config_id"]}: {number(primary["summary"]["win_rate_percent"])}% / {primary["summary"]["completed_trades"]}; mean R={number(primary["summary"]["mean_completed_r"],3)}', flush=True)
    selected = select_development(development)
    selection = dict(saved_before_validation_recent_outcomes_at_utc=datetime.now(timezone.utc).isoformat(), selected_on_development=selected,
                     rules=spec['selection'], development=development, spec_sha256=SPEC_SHA256)
    atomic_json(OUTPUT / 'selection.json', selection)
    print(f'Development selection frozen: {selected}', flush=True)
    for phase in ['development', 'validation', 'recent']:
        for config in configs:
            for case in spec['cost_cases']:
                if phase == 'development' and case in ('conservative', 'financing_stress'):
                    continue
                run, row = execute(config, phase, case, keep_equity=(phase == 'recent' and case == 'conservative') or config['config_id'] == selected)
                if phase == 'recent' and case == 'conservative':
                    recent_runs[config['config_id']] = run
                    curve = pd.DataFrame(run['equity_curve'])
                    fig.add_trace(go.Scatter(x=curve.time_utc, y=curve.equity, name=config['config_id'],
                                            line=dict(width=3 if config['config_id'] == selected else 1.2)), row=2, col=1)
                    print(f'Recent {config["config_id"]}: {number(row["summary"]["win_rate_percent"])}% / {row["summary"]["completed_trades"]}; equity={row["summary"]["final_equity"]:.2f}', flush=True)
            if phase == 'recent':
                execute(config, phase, 'conservative', 'target_first')
    recent_rows = [r for r in rows if r['phase'] == 'recent' and r['cost_case'] == 'conservative' and r['ambiguity'] == 'stop_first']
    fig.add_trace(go.Bar(x=[r['config_id'] for r in recent_rows], y=[r['summary']['win_rate_percent'] for r in recent_rows],
                         text=[str(r['summary']['completed_trades']) + ' closed' for r in recent_rows], name='Observed recent net win rate'), row=1, col=1)
    fig.add_hline(y=45, line_dash='dash', line_color='#bc3f3f', row=1, col=1)
    fig.add_hline(y=500, line_dash='dash', line_color='#888', row=2, col=1)
    fig.update_layout(title='Finite candidate research: hypothetical results, no trading changes', template='plotly_white', height=1000,
                      legend=dict(orientation='h', y=-.15), margin=dict(b=180))
    fig.update_xaxes(tickangle=-35, row=1, col=1)
    fig.update_xaxes(title_text='UTC', row=2, col=1)
    fig.update_yaxes(title_text='Win rate %', row=1, col=1)
    fig.update_yaxes(title_text='Marked equity USD', row=2, col=1)
    fig.write_html(OUTPUT / 'comparison.html', include_plotlyjs=True)
    assessment = {}
    if selected:
        for phase in ['validation', 'recent']:
            primary = next(r for r in rows if r['config_id'] == selected and r['phase'] == phase and r['cost_case'] == 'conservative' and r['ambiguity'] == 'stop_first')
            stress = next(r for r in rows if r['config_id'] == selected and r['phase'] == phase and r['cost_case'] == 'financing_stress' and r['ambiguity'] == 'stop_first')
            assessment[f'{phase}_primary_pass'] = phase_passes(primary['summary'], min_trades=20)
            ss = stress['summary']
            assessment[f'{phase}_financing_positive'] = ss['mean_completed_r'] is not None and ss['mean_completed_r'] > 0 and ss['final_equity'] > ss['initial_balance']
        pd.DataFrame(monthly_rows(recent_runs[selected])).to_csv(OUTPUT / 'selected_recent_monthly.csv', index=False)
    observed = [r['config_id'] for r in recent_rows if phase_passes(r['summary'], min_trades=20)]
    payload = dict(generated_at_utc=datetime.now(timezone.utc).isoformat(), spec=spec, spec_sha256=SPEC_SHA256, source_manifest=manifest,
                   implementation=implementation, configurations=configs, signal_counts={k: len(v) for k, v in signals.items()},
                   selected_on_development=selected, selection_sha256=digest(OUTPUT / 'selection.json'), selected_assessment=assessment,
                   recent_observed_target_matches=observed, runs=rows, orders_sent=0, monitor_started=False)
    atomic_json(OUTPUT / 'summary.json', payload)
    flat_csv([dict(config_id=r['config_id'], phase=r['phase'], cost_case=r['cost_case'], ambiguity=r['ambiguity'], **{k: v for k, v in r['summary'].items() if k != 'config_id'}, **{k: v for k, v in r['metrics'].items() if k not in r['summary']}) for r in rows], OUTPUT / 'comparison.csv')
    write_rules(spec, configs, selected, assessment)
    report(payload)
    assert {p: digest(ROOT / p) for p in protected} == protected_before
    assert all(digest(ROOT / p) == sha for p, sha in implementation['code_sha256'].items())
    atomic_json(OUTPUT / 'verification.json', dict(completed_at_utc=datetime.now(timezone.utc).isoformat(), spec_and_data_hashes_verified=True,
                all_run_invariant_audits_passed=True, run_count=len(rows), audited_entry_records=checked_entries, protected_files_unchanged=protected_before,
                selection_saved_before_later_outcomes=True, selection_sha256=payload['selection_sha256'],
                orders_sent=0, monitor_started=False, automated_tests='Recorded separately after final full-suite run.',
                scope='Actual offline saved-data replay; H1 execution approximations, not native historical fill validation.'))
    print(json.dumps(dict(selected_on_development=selected, selected_assessment=assessment, recent_observed_target_matches=observed,
                          runs=len(rows), audited_entries=checked_entries, report=str(OUTPUT / 'findings.md')), indent=2))


if __name__ == '__main__':
    main()
