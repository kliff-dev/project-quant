"""Reproduce frozen saved-data accounts with an opt-in 4R target ceiling.

No terminal API, credentials, network or execution application is used.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
from functools import partial
import hashlib
import json
import math
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.account_research import ReplaySettings, replay_account
from analytics.extended_targets import EXIT_POLICY, SPEC_SHA, policy_plan
from analytics.paired_exits import replay_opportunity
from analytics.setup_research import validate_bars
from engine.execution_audit import atomic_json


SPEC = ROOT / 'reports/market_analysis/extended_target_spec_2026-11-15.json'
PREVIOUS = ROOT / 'reports/market_analysis/adaptive_targets_2026-11-14'
OUTPUT = ROOT / 'reports/market_analysis/extended_targets_2026-11-15'
SIGNALS_SHA = '75a2e5d154e48aeb70a990454123e000bc218497f2eb47f448488ed0858d2816'
PHASES = ('development', 'validation', 'recent')
POLICIES = ('adaptive_1_2_3', EXIT_POLICY)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def without_extension(value):
    """Financial-regression projection; retain all pre-existing evidence."""
    if isinstance(value, list):
        return [without_extension(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: without_extension(item) for key, item in value.items()
              if key not in ('extended_spec_sha256', 'base_spec_sha256', 'base_selected_reward_r')}
    if result.get('exit_policy') == EXIT_POLICY:
        result['exit_policy'] = 'adaptive_1_2_3'
    if result.get('config_id') == EXIT_POLICY:
        result['config_id'] = 'adaptive_1_2_3'
    if 'target_room_flags' in result:
        result['target_room_flags'].pop('4', None)
    return result


def records(run):
    return [*run['trades'], *([run['open_position']] if run['open_position'] else [])]


def branch_counts(run):
    items = records(run)
    return {str(reward): dict(
        entries=sum(t['selected_reward_r'] == reward for t in items),
        closed=sum(t['selected_reward_r'] == reward and t['status'] == 'closed' for t in items))
        for reward in (1, 2, 3, 4)}


def audit(run):
    for trade in records(run):
        reward = trade['selected_reward_r']
        assert type(reward) is int and 1 <= reward <= 4
        assert trade['planned_risk'] <= trade['risk_budget'] + 1e-9
        assert trade['risk_budget'] <= trade['balance_before_entry'] * .01 + 1e-9
        assert trade['target_room_flags'][str(reward)]
        assert pd.Timestamp(trade['obstacle_confirmed_at_utc']) <= pd.Timestamp(trade['entry_time_utc'])
        if reward >= 3:
            assert trade['strong_context'] and trade['normal_volatility']
        net_reward = (trade['direction'] *
                      (trade['take_profit'] - trade['direction'] * trade['slip'] - trade['entry_price'])
                      * trade['contract_size'] * trade['lots'] - trade['cost_reserve'])
        assert net_reward + 1e-7 >= reward * trade['planned_risk']
    return len(records(run))


def render_report(payload):
    lines = ['# Extend the target ceiling to 4R', '',
        '## Technical Summary', '',
        'The single frozen H4-trend/H1 EMA20-reclaim setup now has an offline 4R extension. '
        'Use 4R only when the unchanged strong-trend and normal-volatility tests hold and the actual '
        'rounded, cost-reserved target fits before the same nearest confirmed H4 swing reference. '
        'Otherwise retain exactly the existing 1R/2R/3R choice. Planned risk stays at most 1%.', '',
        '**The saved sample produces no 4R selections. Historical outcomes are unchanged, so this '
        'study provides no empirical estimate of 4R performance and no evidence of increased profitability.**', '',
        '| Period / conservative cost policy | Closed | Wins | Net win rate | Ending equity | Mean net R | H1-close drawdown |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for row in payload['account_runs']:
        if row['cost_case'] != 'conservative' or row['ambiguity'] != 'stop_first':
            continue
        s = row['summary']
        win_rate = 'unavailable' if s['win_rate_percent'] is None else f'{s["win_rate_percent"]:.1f}%'
        mean = 'unavailable' if s['mean_completed_r'] is None else f'{s["mean_completed_r"]:+.4f}'
        lines.append(f'| {row["phase"]} / {row["policy"]} | {s["completed_trades"]} | '
                     f'{s["wins"]} | {win_rate} | ${s["final_equity"]:.2f} | {mean} | '
                     f'{s["max_h1_close_equity_drawdown_percent"]:.3f}% |')
    v = payload['verification']
    lines += ['', '## Root Cause', '',
        'The prior target policy was explicitly capped at 3R. Raising the ceiling does not create '
        'additional target room: the same nearby historical reference can still limit a trade to 1R or 2R. '
        'A historical reference is not a guarantee that price will turn there.', '',
        '## How It Was Diagnosed', '',
        f'- Replayed **{len(payload["account_runs"])} one-position account scenarios**: both policies '
        'across three periods and four cost cases, plus recent conservative target-first sensitivity.',
        f'- Replayed **{v["paired_scenario_observations"]} eligible paired scenario observations** '
        f'across 12 period/cost cases. These include **{v["unique_conservative_opportunities"]} unique '
        'conservative-cost opportunities**; repeats under other costs are not additional independent trades.',
        f'- The actual conservative one-position accounts contain **{v["base_conservative_account_entries"]} '
        'entries across the three separately reset periods**, fewer than isolated opportunities because '
        'existing positions block later entries.',
        f'- Observed 3R-room flags: **{v["paired_room_3_true"]}**; 4R-room flags: '
        f'**{v["paired_room_4_true"]}**; extended account 4R selections: **{v["extended_four_r_entries"]}**.',
        '- Base account records were reproduced against the saved prior outputs. Extended financial '
        'records and H1 equity curves match the base exactly when the new branch does not activate.', '',
        'The specification was saved before this extension was replayed. The prior zero-3R-room audit '
        'was already known and is disclosed in that specification. All periods were used in earlier '
        'research; this is a reproducibility comparison, not an untouched holdout.', '',
        '## Solution', '',
        '- A separate versioned planner preserves the original entry kernel, stop, lots, risk and '
        'reserves. The base planner and original research artifacts were not edited.',
        '- Synthetic BUY and SELL tests exercise actual 4R selection, exact boundary inclusion, '
        'fractional-tick rejection, weaker-trend fallback, volatility bounds and identical dollar risk.',
        '- Existing SL and TP stay fixed after entry. This change does not trail targets, widen stops '
        'or require larger account exposure.', '',
        '```powershell', r'.\.venv\Scripts\python.exe -B scripts/research_extended_targets.py', '```', '',
        '[Frozen extension specification](../extended_target_spec_2026-11-15.json) · '
        '[Full summaries](summary.json) · [Verification](verification.json) · '
        '[Account comparison](comparison.csv)', '',
        'Costs remain the frozen assumptions: recorded spread, adverse slippage of two points per '
        'side and a planning reserve of 5% of the risk budget. Conservative runs debit that hypothetical '
        'allowance; no-extra-fee runs do not debit it. Financing stress uses assumed $0.10 per .01 lot '
        'per ordinary New York rollover with Wednesday triple units; execution stress doubles slippage '
        'to four points. These are scenarios, not verified historical commissions or swap schedules.', '',
        'H1 candles approximate ask prices and intrabar ordering. Stop-first is primary; target-first '
        'is only a sensitivity. No forced closing at the period boundary, no historical margin or '
        'broker fill verification, and no claim that a 4R trade will finish within a day. Gaps can '
        'exceed planned loss. Paired opportunities overlap and cannot be summed as an executable portfolio.', '',
        '## What I Learned', '',
        '- A higher allowed target is different from a frequently available target.',
        '- Raising potential reward can lower hit rate and lengthen holding time; expectancy needs future evidence.',
        '- Scenario repeats and overlapping opportunities do not increase the number of independent trades.',
        '- Synthetic branch coverage verifies implementation but cannot establish profitability.', '',
        '## If This Happens Again', '',
        'Check the selected branch, all room flags, strong-context and volatility flags, and the '
        'nearest obstacle recorded before entry. Do not loosen a gate merely to force an extended '
        'target. Keep future DEMO outcomes separate from these already-inspected historical results.', '',
        'This script sent no orders, contacted no terminal and changed no execution authorization or policy.']
    (OUTPUT / 'findings.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    if digest(SPEC) != SPEC_SHA:
        raise ValueError('Frozen extension specification changed.')
    spec = json.loads(SPEC.read_text(encoding='utf-8'))
    base_path = ROOT / spec['base_specification']
    if digest(base_path) != spec['base_specification_sha256']:
        raise ValueError('Base specification changed.')
    base = json.loads(base_path.read_text(encoding='utf-8'))
    for name, sha in spec['base_kernel_sha256'].items():
        if digest(ROOT / name) != sha:
            raise ValueError(f'Frozen base source changed: {name}')
    manifest = json.loads((ROOT / base['input_manifest']).read_text(encoding='utf-8'))
    source = ROOT / manifest['file']
    if digest(source) != spec['input_sha256'] or manifest['sha256'] != spec['input_sha256']:
        raise ValueError('Frozen market data changed.')
    signal_path = PREVIOUS / 'signals.json'
    if digest(signal_path) != SIGNALS_SHA:
        raise ValueError('Frozen saved entry signals changed.')
    data = pd.read_csv(source)
    data.index = pd.DatetimeIndex(pd.to_datetime(data.pop('timestamp_utc'), utc=True))
    data = validate_bars(data)
    signals = {int(key): value for key, value in json.loads(signal_path.read_text(encoding='utf-8')).items()}
    boundaries = [pd.Timestamp(base[key]) for key in
                  ('development_start_utc', 'validation_start_utc', 'recent_start_utc', 'end_utc')]
    for index, signal_value in signals.items():
        assert pd.Timestamp(signal_value['decision_time_utc']) == data.index[index]
        assert pd.Timestamp(signal_value['last_completed_h4_open_utc']) + pd.Timedelta(hours=4) <= data.index[index]
    assert boundaries[-1] == data.index[-1] + pd.Timedelta(hours=1)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    protected_names = ['config/trading_policy.json', 'config/monday_adaptive_policy.json',
        'config/monday_demo_2026-11-16.json', 'logs/monitor/status.json', 'logs/monitor/state.json',
        'logs/demo_test_state.json', 'data/processed/trade_journal.json']
    # Other workers may prepare the separate Monday pilot while this offline
    # study runs. Its policy/manifest are not study inputs and are not opened.
    protected_names = [name for name in protected_names if not name.startswith('config/monday')]
    protected = {name: digest(ROOT / name) for name in protected_names if (ROOT / name).exists()}
    provenance = dict(recorded_before_this_run_at_utc=datetime.now(timezone.utc).isoformat(),
        extension_spec_sha256=SPEC_SHA, base_spec_sha256=spec['base_specification_sha256'],
        source_sha256=spec['input_sha256'], saved_signals_sha256=SIGNALS_SHA,
        implementation_sha256={name: digest(ROOT / name) for name in
            ('analytics/extended_targets.py', 'scripts/research_extended_targets.py')},
        base_source_sha256=spec['base_kernel_sha256'], prior_information_disclosed=True)
    atomic_json(OUTPUT / 'implementation_initial.json', provenance)
    runs, paired_summaries, screening = [], [], []
    account_entries = four_entries = base_conservative_entries = 0
    unique_conservative = set()
    room_three = room_four = paired_observations = 0
    for phase_index, phase in enumerate(PHASES):
        start, end = boundaries[phase_index:phase_index + 2]
        phase_signals = {index: value for index, value in signals.items() if start <= data.index[index] < end}
        for case, cost in base['cost_cases'].items():
            settings = ReplaySettings(**cost)
            builders = {policy: partial(policy_plan, policy=policy) for policy in POLICIES}
            current_runs = {}
            for policy, builder in builders.items():
                run = replay_account(data, phase_signals, start, end, settings, plan_builder=builder)
                run['summary']['config_id'] = policy
                account_entries += audit(run)
                current_runs[policy] = run
                four_entries += sum(t['selected_reward_r'] == 4 for t in records(run))
                if case == 'conservative' and policy == POLICIES[0]:
                    base_conservative_entries += len(records(run))
                path = OUTPUT / case / policy / f'{phase}_account.json'
                atomic_json(path, run)
                runs.append(dict(phase=phase, cost_case=case, policy=policy, ambiguity='stop_first',
                    settings=asdict(settings), summary=run['summary'], branch_counts=branch_counts(run),
                    account_file=path.relative_to(OUTPUT).as_posix()))
            a, b = [current_runs[policy] for policy in POLICIES]
            saved = json.loads((PREVIOUS / case / POLICIES[0] / f'{phase}_account.json').read_text(encoding='utf-8'))
            assert {key: value for key, value in a.items() if key != 'equity_curve'} == saved
            if not any(t['selected_reward_r'] == 4 for t in records(b)):
                assert without_extension(b) == a
            pairs, rejects = [], Counter()
            saved_pairs = json.loads((PREVIOUS / 'paired' / case / f'{phase}.json').read_text(encoding='utf-8'))
            saved_lookup = {p['entry_index']: p for p in saved_pairs['pairs']}
            for index, signal_value in phase_signals.items():
                results = {policy: replay_opportunity(data, index, signal_value, end, settings,
                            plan_builder=builder) for policy, builder in builders.items()}
                old, extended = [results[policy]['trade'] for policy in POLICIES]
                if old is None or extended is None:
                    assert old is None and extended is None
                    assert results[POLICIES[0]]['rejection'] == results[POLICIES[1]]['rejection']
                    rejects[results[POLICIES[0]]['rejection']] += 1
                    continue
                assert old == saved_lookup[index]['adaptive']
                for key in ('entry_price', 'stop_loss', 'lots', 'planned_risk', 'risk_budget', 'cost_reserve', 'entry_fee'):
                    assert old[key] == extended[key]
                if extended['selected_reward_r'] != 4:
                    assert without_extension(extended) == old
                paired_observations += 1
                room_three += int(extended['target_room_flags']['3'])
                room_four += int(extended['target_room_flags']['4'])
                if case == 'conservative':
                    unique_conservative.add(index)
                screening.append(dict(phase=phase, cost_case=case, entry_index=index,
                    entry_time_utc=old['entry_time_utc'], base_reward_r=old['selected_reward_r'],
                    extended_reward_r=extended['selected_reward_r'], room=extended['target_room_flags'],
                    strong_context=extended['strong_context'], normal_volatility=extended['normal_volatility']))
                both_closed = old['status'] == extended['status'] == 'closed'
                pairs.append(dict(entry_index=index, base=old, extended=extended, both_closed=both_closed,
                    delta_r=extended['realized_r'] - old['realized_r'] if both_closed else None))
            assert len(pairs) == len(saved_lookup) and dict(rejects) == saved_pairs['rejections']
            completed = [p for p in pairs if p['both_closed']]
            atomic_json(OUTPUT / 'paired' / case / f'{phase}.json', dict(pairs=pairs, rejections=dict(rejects),
                warning='Overlapping isolated opportunities, not an executable account.'))
            paired_summaries.append(dict(phase=phase, cost_case=case, eligible_pairs=len(pairs),
                both_closed_pairs=len(completed), incomplete_pairs=len(pairs) - len(completed),
                mean_extended_minus_base_r=sum(p['delta_r'] for p in completed) / len(completed) if completed else None))
            print(f'{phase}/{case}: base and extended accounts replayed; {len(pairs)} paired opportunities.', flush=True)
    settings = ReplaySettings(ambiguity='target_first', **base['cost_cases']['conservative'])
    ambiguity_runs = {}
    for policy in POLICIES:
        run = replay_account(data, signals, boundaries[2], boundaries[3], settings,
                             plan_builder=partial(policy_plan, policy=policy))
        run['summary']['config_id'] = policy
        account_entries += audit(run)
        four_entries += sum(t['selected_reward_r'] == 4 for t in records(run))
        ambiguity_runs[policy] = run
        path = OUTPUT / 'ambiguity' / policy / 'recent_target_first.json'
        atomic_json(path, run)
        runs.append(dict(phase='recent', cost_case='conservative', policy=policy, ambiguity='target_first',
            settings=asdict(settings), summary=run['summary'], branch_counts=branch_counts(run),
            account_file=path.relative_to(OUTPUT).as_posix()))
    saved_ambiguity = json.loads((PREVIOUS / 'ambiguity' / POLICIES[0] / 'recent_target_first.json').read_text(encoding='utf-8'))
    assert {key: value for key, value in ambiguity_runs[POLICIES[0]].items() if key != 'equity_curve'} == saved_ambiguity
    if not any(t['selected_reward_r'] == 4 for t in records(ambiguity_runs[POLICIES[1]])):
        assert without_extension(ambiguity_runs[POLICIES[1]]) == ambiguity_runs[POLICIES[0]]
    assert all(digest(ROOT / name) == sha for name, sha in protected.items())
    assert all(digest(ROOT / name) == sha for name, sha in spec['base_kernel_sha256'].items())
    verification = dict(completed_at_utc=datetime.now(timezone.utc).isoformat(),
        account_runs=len(runs), paired_cases=len(paired_summaries), audited_account_entries=account_entries,
        paired_scenario_observations=paired_observations, unique_conservative_opportunities=len(unique_conservative),
        base_conservative_account_entries=base_conservative_entries, paired_room_3_true=room_three,
        paired_room_4_true=room_four, extended_four_r_entries=four_entries,
        base_saved_account_and_pair_regression=True, no_extension_financial_and_equity_equality=True,
        shared_stop_lots_risk_and_costs=True, protected_files_unchanged=protected,
        source_hashes_verified=True, orders_sent=0, monitor_started=False, terminal_contacted=False,
        limitation='Synthetic branch coverage is not evidence of realized 4R profitability.')
    payload = dict(generated_at_utc=datetime.now(timezone.utc).isoformat(), spec_sha256=SPEC_SHA,
        spec=spec, provenance=provenance, account_runs=runs, paired=paired_summaries, verification=verification,
        assessment='No 4R selections in the frozen sample; no measured historical change and no established 4R edge.')
    atomic_json(OUTPUT / 'summary.json', payload)
    atomic_json(OUTPUT / 'eligibility_audit.json', dict(observations=screening,
        warning='Cost-case repetitions and overlapping opportunities are not independent portfolio trades.'))
    atomic_json(OUTPUT / 'verification.json', verification)
    pd.DataFrame([dict(phase=row['phase'], cost_case=row['cost_case'], policy=row['policy'],
        ambiguity=row['ambiguity'], **row['summary']) for row in runs]).to_csv(OUTPUT / 'comparison.csv', index=False)
    render_report(payload)
    print(json.dumps(verification, indent=2), flush=True)


if __name__ == '__main__':
    main()
