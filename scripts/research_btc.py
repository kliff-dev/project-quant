"""Reproducible, offline BTC CFD research. Never connects to a broker."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from analytics.btc_replay import ContractSpec, ReplaySettings, prepare_bars, replay
from analytics.btc_setups import SETUPS, VERSION, WARMUP, scan


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False,
        default=lambda item: item.isoformat() if hasattr(item, 'isoformat') else str(item)), encoding='utf-8')


def load_archive(directory: Path):
    source = directory / 'BTCUSDm_H1.csv'
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    if sha != manifest.get('csv_sha256'):
        raise ValueError('BTC archive checksum mismatch.')
    if manifest.get('symbol') != 'BTCUSDm' or manifest.get('price_basis') != 'bid' or manifest.get('account_currency') != 'USD':
        raise ValueError('This replay requires the exact broker BTCUSDm Bid/USD archive.')
    native = manifest.get('profit_calculation_verification', {})
    values=[manifest['contract_size'],native.get('volume'),native.get('price_change_usd'),native.get('buy_profit'),native.get('sell_profit')]
    try:
        if any(isinstance(value,bool) or not math.isfinite(float(value)) or float(value)<=0 for value in values): raise ValueError()
        size,volume,change,buy,sell=map(float,values)
        expected=size*volume*change
        valid=math.isfinite(expected) and all(math.isclose(value,expected,rel_tol=0,abs_tol=1e-8) for value in (buy,sell))
    except (TypeError,ValueError,OverflowError):
        valid=False
    if not valid:
        raise ValueError('Manifest lacks a verified USD-linear native profit calculation.')
    frame = pd.read_csv(source)
    frame.index = pd.to_datetime(frame.pop('timestamp_utc'), utc=True)
    frame = prepare_bars(frame)
    contract = ContractSpec(symbol=manifest['symbol'], point=manifest['point'],
        contract_size=manifest['contract_size'], volume_min=manifest['volume_min'],
        volume_max=manifest['volume_max'], volume_step=manifest['volume_step'])
    return frame, contract, manifest, sha


def pct(value):
    return 'n/a' if value is None else f'{value * 100:.1f}%'


def render_report(run, out: Path):
    rows = [
        '# BTC CFD research comparison', '',
        f"Generated: {run['generated_at_utc']}. Rule version: `{VERSION}`.", '',
        '**These are modeled research outcomes, not verified broker net returns or a claim that the strategies are proven profitable.**', '',
        f"Source: {run['source']['provider']}, exact `{run['source']['symbol']}` broker Bitcoin CFD, H1 Bid candles; "
        f"{run['source']['rows']:,} bars from {run['source']['first_open_utc']} to {run['source']['last_close_utc']}.", '',
        '## What was frozen', '',
        'B1 SMA20/100 crossover; B2 previous-48-bar channel breakout; B3 one-percent filter on previous-24-bar close extremes; '
        'B4 168-hour momentum sign change confirmed by 12-hour momentum. Stops use the previous/current completed five-bar '
        'structure plus 0.2 ATR with a 1.5 ATR minimum; at the entry quote a stop may only expand outward to maintain the ATR floor. '
        'A quote already beyond the original structural stop is rejected. Stops and targets round outward to the broker tick. '
        'Targets are 2R from the requested quote, so adverse execution fills reduce the realized reward-to-risk ratio. '
        'See the frozen setup specification and primary-source review.', '',
        'Rules were specified before running this comparison. Chronological first-two-thirds development and final-third validation '
        'use separate accounts and the same frozen parameters; the latter is a historical diagnostic, not genuinely unseen future evidence. '
        'The last six months are shown separately and overlap the longer sample. No threshold search or selection by the winning result was performed.', '',
        '## Account and costs', '',
        '- USD 500 / 1,000 independently funded accounts; 1% current-balance planned risk, maximum 0.10 lots, broker 0.01-lot minimum and step. '
        'Below-minimum allocations are rejected, never rounded up. B1–B4 accounts are independent comparisons; their profits cannot be added as one funded portfolio.',
        '- Combined account: one BTC position at a time, B1→B4 same-direction priority, skip simultaneous conflicting directions, no fallback after a selected candidate is rejected. '
        'Signals occurring while occupied are recorded as rejected, including the candle on which the existing position later exits.',
        '- Entry at the next contiguous H1 open only. BUY uses Ask approximation, SELL uses Bid. Recorded historical bar spread is converted with the broker point size. '
        'Entry and exit each incur adverse USD 2 per BTC slippage; spread plus twice slippage must be no more than 10% of initial stop distance.',
        '- Baseline includes spread/slippage in execution prices. Explicit commission and financing are zero because the full historical fee schedule is unavailable; '
        'therefore baseline is **not verified after all broker costs**. The present broker contract reports long swap −1332.6 points and short swap zero; '
        'this current snapshot is not a historical swap series.',
        '- Cost stress 1 doubles both spread and adverse slippage. Cost stress 2 adds 5 basis points of notional per side in commission '
        'and USD 20 per lot per day financing, continuously accrued for both directions. These are hypothetical stress assumptions, not claimed Exness fees. '
        'Stress replays resize and re-screen candidates, so their admitted trades and account paths can differ; they are not the identical baseline trades with a fee simply subtracted.',
        '- A 5% reserve leaves 95% of the 1% budget available for modeled stop loss including adverse entry/exit slippage; '
        'it is unspent headroom, not a fee deducted from cash. '
        'USD-linear contract profit was checked against native `order_calc_profit` in the archive manifest.',
        '- Stop gaps fill at the worse open plus adverse slippage. When both stop and target occur in one H1 candle, stop wins. '
        'H1 cannot establish the intrabar price path, historical Ask extremes or real slippage; the target receives no favorable gap price improvement.',
        '- Remaining positions are marked using the final quote with liquidation slippage and modeled exit costs, not invented as closed. '
        'Drawdown uses H1 closing liquidation equity, including open P&L; intrabar drawdown may be larger. Margin availability, historical spread extrema, '
        'liquidity, stop freezes and account-specific historical financing are not fully reconstructed.', '',
        '## Results', '',
        'Mean R is the equally weighted average of each trade\'s cash result divided by its own initial planned risk. '
        'Different lot sizes and risk budgets mean positive mean R can coexist with negative total dollars; both are reported.', '',
        '| Period | Account | Cost case | Setup | Closed | Win rate | Mean R | Closed P&L USD | End equity USD | H1 close DD | Open |',
        '|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for item in run['scenarios']:
        s = item['summary']
        mean = 'n/a' if s['expectancy_r'] is None else f"{s['expectancy_r']:.3f}"
        rows.append(f"| {item['period']} | {item['initial_balance']:.0f} | {item['cost_case']} | {item['setup_id']} | "
            f"{s['closed_trades']} | {pct(s['win_rate'])} | {mean} | {s['closed_net_pnl_usd']:.2f} | "
            f"{s['end_equity']:.2f} | {pct(s['max_drawdown_close_mark_fraction'])} | {s['open_positions']} |")
    rows += ['', '## Technical frequency is separate from admission', '',
        '| Setup | Raw triggers in full archive |', '|---|---:|']
    for key in SETUPS:
        rows.append(f"| {key}: {SETUPS[key][0]} | {run['signal_counts'].get(key, 0)} |")
    rows += ['', '## Current finding', '']
    for capital in (500., 1000.):
        combined = next(item['summary'] for item in run['scenarios']
            if item['period'] == 'full_history' and item['cost_case'] == 'baseline'
            and item['setup_id'] == 'combined' and item['initial_balance'] == capital)
        rows.append(f"The USD {capital:.0f} combined full-history baseline closes {combined['closed_trades']} trades, "
            f"with modeled closed P&L USD {combined['closed_net_pnl_usd']:.2f} and H1 close-mark drawdown "
            f"{pct(combined['max_drawdown_close_mark_fraction'])}.")
        rows.append('')
    no_admission = [key for key in SETUPS if all(item['summary']['closed_trades'] == 0
        and item['summary']['open_positions'] == 0 for item in run['scenarios']
        if item['period'] == 'full_history' and item['cost_case'] == 'baseline' and item['setup_id'] == key)]
    if no_admission:
        rows += [f"No full-history trade is admitted for {', '.join(no_admission)} at either starting capital under these risk/cost rules.", '']
    rows += ['The correct next step is labeled DEMO/research observation with frozen rules and explicit costs, '
        'not a claim that adding four setups has solved profitability. A few favorable trades cannot substantiate a positive edge.']
    rows += ['', 'Raw triggers are observations, not executable entries. Risk/minimum-lot and cost rejections, occupancy and direction conflicts explain '
        'why fills can be much less frequent. Exact rejected decisions and every closed trade are retained in each scenario JSON.', '',
        f"Dataset SHA-256: `{run['source']['csv_sha256']}`.", '',
        '[Compact machine-readable results](run.json) · [Frozen raw signals](signals.csv)', '',
        '## How to reproduce', '',
        'Run `.venv\\Scripts\\python.exe -B scripts/research_btc.py` from the Project Quant workspace. '
        'This script reads local saved files only and imports no broker connector. Offline fixtures cover quote sides, fees, risk sizing, '
        'gaps, ambiguous candles, missing next bars, conflicts, occupancy and data validation.', '',
        '## Interpretation', '',
        'Assess the final chronological segment, trade count, costs and drawdown together. A high win rate or a positive full-period result '
        'alone is insufficient. If every setup loses after the modeled assumptions, retain that finding; changing the setup to force a positive '
        'backtest would require a new version and genuinely fresh validation. Four setup families add hypotheses, not guaranteed diversification.', '',
    ]
    (out / 'report.md').write_text('\n'.join(rows), encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=ROOT / 'data/research/btc')
    parser.add_argument('--out', type=Path, default=ROOT / 'reports/market_analysis/btc_2026-11-22')
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    frame, contract, manifest, sha = load_archive(args.data)
    end = frame.index[-1] + pd.Timedelta(hours=1)
    scanned = scan(frame, as_of=end)
    signals = scanned['signals']
    signals.to_csv(args.out / 'signals.csv', index=False)
    first = frame.index[WARMUP - 1] + pd.Timedelta(hours=1)
    split = frame.index[int(len(frame) * 2 / 3)]
    periods = {'full_history': (first, end), 'development_first_two_thirds': (first, split),
               'validation_final_third': (split, end),
               'last_six_months': (max(first, end - pd.DateOffset(months=6)), end)}
    settings = ReplaySettings()
    run = dict(version=VERSION, generated_at_utc=datetime.now(timezone.utc).isoformat(),
        source=dict(provider=manifest['source'], symbol=contract.symbol,
            instrument_type=manifest['instrument_type'], rows=len(frame), csv_sha256=sha,
            first_open_utc=frame.index[0].isoformat(), last_close_utc=end.isoformat(),
            missing_hour_count=int((frame.index.to_series().diff().dropna() / pd.Timedelta(hours=1) - 1).clip(lower=0).sum()),
            manifest_path=str((args.data / 'manifest.json').resolve()),
            profit_calculation_verification=manifest['profit_calculation_verification']),
        contract=asdict(contract), baseline_settings=asdict(settings),
        source_fingerprints={str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (ROOT/'analytics/btc_setups.py', ROOT/'analytics/btc_replay.py', ROOT/'scripts/research_btc.py')},
        signal_counts={key: int((signals.setup_id == key).sum()) for key in SETUPS},
        chronological_split_utc=split.isoformat(),
        limitations=['Historical financing and commissions are unverified; zero baseline is not all-cost net.',
            'Bid bars plus one recorded spread do not reconstruct historical Ask extrema.',
            'H1 OHLC ambiguity is resolved stop first; no intrabar execution timestamps.',
            'H1 close-mark drawdown can understate intrabar drawdown; margin availability is not modeled.',
            'Chronological validation is historical research, not genuinely untouched future data.'],
        scenarios=[])
    for period, (start, stop) in periods.items():
        for capital in (500., 1000.):
            for setup in (*SETUPS, 'combined'):
                name = f'{period}_{int(capital)}_{setup}_baseline'
                print(f'Replaying {name}', flush=True)
                result = replay(frame, signals, contract, replace(settings, initial_balance=capital),
                                setup_id=setup, start=start, end=stop)
                save_json(args.out / (name + '.json'), result)
                run['scenarios'].append(dict(id=name, period=period, initial_balance=capital,
                    cost_case='baseline', setup_id=setup, result_file=name + '.json', summary=result['summary']))
    for cost_case, stressed in (
        ('double_spread_slippage', replace(settings, spread_multiplier=2., slippage_price=4.)),
        ('commission_financing_stress', replace(settings, commission_bps_per_side=5., financing_usd_per_lot_day=20.))):
        for capital in (500., 1000.):
            name = f'full_history_{int(capital)}_combined_{cost_case}'
            result = replay(frame, signals, contract, replace(stressed, initial_balance=capital),
                            start=first, end=end)
            save_json(args.out / (name + '.json'), result)
            run['scenarios'].append(dict(id=name, period='full_history', initial_balance=capital,
                cost_case=cost_case, setup_id='combined', result_file=name + '.json', summary=result['summary']))
    save_json(args.out / 'run.json', run)
    render_report(run, args.out)
    print(json.dumps({'scenarios': len(run['scenarios']), 'signals': run['signal_counts'],
                      'report': str(args.out / 'report.md')}), flush=True)
    return run


if __name__ == '__main__':
    main()
