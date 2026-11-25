"""Registered fee sensitivity of two already-fixed choices; never select again."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd
import plotly.graph_objects as go

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analytics.account_research import ReplaySettings, prepare_signals, replay_account
from analytics.setup_research import validate_bars
from engine.execution_audit import atomic_json
from scripts.research_six_month_account import flat_csv, SPEC_SHA256

ADDENDUM = ROOT / 'reports/market_analysis/six_month_cost_sensitivity_2026-11-14.json'
ADDENDUM_SHA256 = '21d526fa24b5e501b80c3d9ca06c304450600f113b9c9a7b9dfdb96d387512d4'
OUTPUT = ROOT / 'reports/market_analysis/six_month_2026-11-14'


def main() -> None:
    if hashlib.sha256(ADDENDUM.read_bytes()).hexdigest() != ADDENDUM_SHA256:
        raise ValueError('Registered cost sensitivity changed.')
    primary_path = OUTPUT / 'summary.json'
    primary = json.loads(primary_path.read_text(encoding='utf-8'))
    if primary['spec_sha256'] != SPEC_SHA256:
        raise ValueError('Unexpected original experiment.')
    source = ROOT / primary['source_manifest']['file']
    if hashlib.sha256(source.read_bytes()).hexdigest() != primary['source_manifest']['sha256']:
        raise ValueError('Source data changed.')
    signal_path = OUTPUT / 'signals.csv'
    frame = pd.read_csv(source)
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop('timestamp_utc'), utc=True))
    frame = validate_bars(frame)
    # Recompute full-precision inputs; the inspection CSV rounds numeric columns.
    signals = prepare_signals(frame, first_entry=pd.Timestamp(primary['spec']['training_start_utc']),
                              window_bars=primary['spec']['rolling_closed_h1_bars'])
    if len(signals) != primary['prepared_signal_count']:
        raise ValueError('Signal count differs from the original experiment.')
    start = pd.Timestamp(primary['spec']['evaluation_start_utc'])
    end = frame.index[-1] + pd.Timedelta(hours=1)
    selected = primary['selected_on_earlier_training']
    configs = list(dict.fromkeys(['sl1atr_tp2r', *([selected] if selected else [])]))
    output = OUTPUT / 'cost_sensitivity'
    output.mkdir(parents=True, exist_ok=True)
    runs = []
    fig = go.Figure()
    for config in configs:
        original = next(r for r in primary['runs'] if r['cost_case'] == 'base'
                        and r['ambiguity'] == 'stop_first' and r['evaluation']['config_id'] == config)
        for case, financing in [('zero_extra_fee', 0.), ('zero_extra_fee_financing_stress', .1)]:
            settings = replace(ReplaySettings(**original['settings']), charge_cost_allowance=False,
                               financing_per_001lot_unit=financing)
            run = replay_account(frame, signals, start, end, settings)
            case_path = output / case
            case_path.mkdir(parents=True, exist_ok=True)
            atomic_json(case_path / f'{config}_account.json', run)
            flat_csv(run['trades'], case_path / f'{config}_trades.csv')
            flat_csv(run['equity_curve'], case_path / f'{config}_equity.csv')
            runs.append(dict(cost_case=case, config_id=config, summary=run['summary']))
            curve = pd.DataFrame(run['equity_curve'])
            fig.add_trace(go.Scatter(x=curve.time_utc, y=curve.equity, name=f'{config}: {case}',
                                    line=dict(dash='dash' if financing else 'solid')))
    fig.add_hline(y=500, line_dash='dot', line_color='#888')
    fig.update_layout(title='Hypothetical $500 equity: planning reserve retained, no extra fee debit',
                      template='plotly_white', xaxis_title='UTC', yaxis_title='Marked equity USD', height=650)
    fig.write_html(output / 'equity_curve.html', include_plotlyjs=True)
    payload = dict(generated_at_utc=datetime.now(timezone.utc).isoformat(),
                   registered_addendum=json.loads(ADDENDUM.read_text(encoding='utf-8')),
                   addendum_sha256=ADDENDUM_SHA256, primary_results_sha256=hashlib.sha256(primary_path.read_bytes()).hexdigest(),
                   signals_sha256=hashlib.sha256(signal_path.read_bytes()).hexdigest(),
                   source_sha256=primary['source_manifest']['sha256'],
                   account_code_sha256=hashlib.sha256((ROOT / 'analytics/account_research.py').read_bytes()).hexdigest(),
                   script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   fixed_configs=configs, selected_again=False, runs=runs)
    atomic_json(output / 'summary.json', payload)
    flat_csv([dict(cost_case=r['cost_case'], **r['summary']) for r in runs], output / 'comparison.csv')

    lines = ['# Six months of the current setup: account and SL/TP conclusions', '',
             '## Technical Summary', '',
             f'A hypothetical **$500** account was replayed from **{start.tz_convert("Asia/Manila")}** through the latest completed Friday bar ending **{end.tz_convert("Asia/Manila")}**. The baseline uses the existing H4 trend and H1 pullback/breakout entries, a structural stop with at least 1 ATR, and a cost-reserved 2R target.', '',
             '**The original rules lost money in this period. SL/TP calculations alone do not establish profitability.** The earlier-data-selected alternative made a small modeled gain, sensitive to assumptions. These are research results, not actual demo account returns or a prediction.', '',
             '| Rules / cost assumption | Final balance | Final equity | Equity return | Closed trades | Win rate | H1-close drawdown |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for config in configs:
        label = 'Original: min 1 ATR / 2R' if config == 'sl1atr_tp2r' else 'Earlier-selected: min 2 ATR / 2.5R'
        for case in ['base', 'financing_stress']:
            s = next(r['evaluation'] for r in primary['runs'] if r['cost_case'] == case
                     and r['ambiguity'] == 'stop_first' and r['evaluation']['config_id'] == config)
            cost = 'conservative allowance debit' if case == 'base' else 'allowance debit + financing stress'
            lines.append(f'| {label}; {cost} | ${s["final_balance"]:.2f} | **${s["final_equity"]:.2f}** | {s["equity_return_percent"]:+.2f}% | {s["completed_trades"]} | {s["win_rate_percent"]:.1f}% | {s["max_h1_close_equity_drawdown_percent"]:.2f}% |')
        for row in [r for r in runs if r['config_id'] == config]:
            s = row['summary']
            cost = 'no extra fee, zero swap' if row['cost_case'] == 'zero_extra_fee' else 'no extra fee + financing stress'
            lines.append(f'| {label}; {cost} | ${s["final_balance"]:.2f} | **${s["final_equity"]:.2f}** | {s["equity_return_percent"]:+.2f}% | {s["completed_trades"]} | {s["win_rate_percent"]:.1f}% | {s["max_h1_close_equity_drawdown_percent"]:.2f}% |')
    lines += ['', 'Each run starts flat with $500; each ends with **one hypothetical open position**. Balance includes realized cashflows and any modeled open-position charges; equity adds floating price P&L. Open positions are not counted as completed winners.', '',
              '[Original conservative account chart](equity_curve.html) · [No-extra-fee equity chart](cost_sensitivity/equity_curve.html) · [Baseline hypothetical trades](base/stop_first/sl1atr_tp2r_trades.csv) · [Full nine-choice experiment and monthly path](findings.md)', '',
              '## Root Cause', '',
              'A 1% risk budget controls planned exposure; it does not make entries profitable. Profitability depends on the frequency of wins, realized payoff sizes, costs, gaps and time in positions. Changing a target also changes which later entries are available while only one position may be open. A planning cost reserve is not necessarily an actual broker fee.', '',
              '## How It Was Diagnosed', '',
              'The original experiment declared nine combinations of minimum stop distance (1, 1.5, 2 ATR) and reward multiple (1.5, 2, 2.5R). It selected using May 2024–May 2026 data only, then applied that choice unchanged to the later six months. The already-selected choice is **' + str(selected) + '**. The recent period had been viewed in earlier research, so this is a descriptive chronological evaluation, not a completely untouched holdout.', '',
              'The first experiment charged the 5%-of-budget planning allowance as a hypothetical cost. Saved actual project trades showed zero commission/fee; this does not prove pricing for the whole history. A separately registered sensitivity therefore keeps the reserve in lot/SL/TP planning but debits no extra fee. No parameter was reselected from these sensitivity results. All cases still include recorded spread and two points of modeled adverse slippage per side. Financing stress is an assumed $0.10 per 0.01 lot per ordinary rollover, with Wednesday triple units; it is not a known historical broker swap schedule.', '',
              '## Solution', '',
              '**Research decision: retain the current live policy while validating further.** The alternative deserves a fixed-rule forward DEMO test and more independent tick/spread/financing checks, rather than an immediate profitability claim. Stop and target changes were made only inside the offline simulator. No order, live parameter, monitor or authorization was changed.', '',
              'Reproduce offline, in this order:', '', '```powershell',
              r'.\.venv\Scripts\python.exe -B scripts/research_six_month_account.py',
              r'.\.venv\Scripts\python.exe -B scripts/research_account_cost_sensitivity.py', '```', '',
              'Both experiments use completed H1 bars and a rolling 3,000-bar signal window; position sizing recalculates the 1% ceiling after each trade and rounds lots down. H1 execution approximates ask extremes with each recorded bar spread. Intrabar event order, actual historical fills, fees, financing, outages, margin changes and deeper intrabar drawdown are not fully known. Gaps can exceed the planned 1% loss. No time exit or daily loss cap was added.', '',
              '[Frozen primary specification](../six_month_spec_2026-11-14.json) · [Registered fee sensitivity](../six_month_cost_sensitivity_2026-11-14.json) · [Primary results and hashes](summary.json) · [Fee sensitivity and hashes](cost_sensitivity/summary.json) · [Verification record](verification.json)', '',
              '## What I Learned', '',
              '- A correct SL/TP and lot calculation manages planned loss; it does not guarantee a profitable strategy.',
              '- A higher target can reduce win rate and lengthen holding time; realized account results matter more than the advertised ratio.',
              '- A small historical gain can disappear under different costs. Separate known broker charges from conservative assumptions.',
              '- Choosing parameters from older data helps discipline the experiment; independent forward evidence is still needed.', '',
              '## If This Happens Again', '',
              'Check the date range, data checksum, cost case, open-position count and balance-versus-equity distinction before comparing results. Keep the baseline and every tested choice visible; register new changes before evaluating them. The actual trade journal remains separate from these hypothetical records.', '',
              'Background: [CME on mathematical expectation](https://www.cmegroup.com/education/courses/trading-psychology/the-mathematics-of-trading-success), [Bailey et al. on backtest overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).']
    (OUTPUT / 'conclusion.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps(payload['runs'], indent=2))


if __name__ == '__main__':
    main()
