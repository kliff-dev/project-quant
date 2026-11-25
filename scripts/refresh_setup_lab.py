"""Read-only research data acquisition. Never imports an order executor.

MT5 uses the existing verified DEMO login. Dukascopy uses the independently
maintained dukascopy-node CLI; no key, account or paid subscription is required.
"""
from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
import pandas as pd
from analytics.setup_lab import INTERVALS, SETUPS, register, scan, summarize, read_dataset


def import_duka(root, path, instrument, timeframe):
    data=pd.read_csv(path)
    if 'timestamp' not in data:
        raise ValueError('Expected dukascopy-node CSV with epoch-millisecond timestamp column.')
    stamps=pd.to_datetime(pd.to_numeric(data.pop('timestamp'),errors='raise'),unit='ms',utc=True)
    data.index=pd.DatetimeIndex(stamps)
    return register(root,data,dict(source='Dukascopy',instrument=instrument,timeframe=timeframe,
        price_basis='bid',instrument_type='OTC forex' if instrument=='EURUSD' else 'Dukascopy XAU/USD metal quote; distinct from Exness CFD',
        acquired_at_utc=datetime.now(timezone.utc).isoformat(),
        transport='dukascopy-node 1.51.1, independently maintained third-party downloader',
        volume_quality='Provider volume is not consolidated exchange volume; unused in these rules.',
        cost_quality='Historical ask, commissions, swaps and executable Exness contract terms unavailable.'))


def refresh_mt5(root, days=184):
    from engine.gold_connector import GoldConnector
    results=[]
    end=datetime.now(timezone.utc); start=end-timedelta(days=days)
    with GoldConnector(timeout_ms=10000) as connector:
        connector.verify_pinned_account(); identity=connector.account_identity
        api=connector.terminal_api
        for symbol,tf,constant in [('EURUSDm','H1',api.TIMEFRAME_H1),('XAUUSDm','M5',api.TIMEFRAME_M5)]:
            info=api.symbol_info(symbol)
            if info is None or info.chart_mode != 0:
                raise RuntimeError(f'{symbol} bid chart contract could not be verified.')
            if symbol=='XAUUSDm':
                from engine.gold_connector import validate_gold_contract
                validate_gold_contract(info)
            rates=api.copy_rates_range(symbol,constant,start,end)
            if rates is None or not len(rates):
                raise RuntimeError(f'No {symbol} research candles returned: {api.last_error()}')
            data=pd.DataFrame(rates)
            data.index=pd.DatetimeIndex(pd.to_datetime(data.pop('time'),unit='s',utc=True))
            data=data.loc[data.index+INTERVALS[tf] <= pd.Timestamp(end)]
            connector.verify_pinned_account()
            if connector.account_identity != identity: raise RuntimeError('Account changed during data retrieval.')
            results.append(register(root,data,dict(source='Exness MT5',instrument=symbol,timeframe=tf,
                price_basis='bid',instrument_type='OTC EUR/USD forex' if symbol=='EURUSDm' else 'Exness Gold CFD, 100 troy ounces per lot',
                acquired_at_utc=end.isoformat(),requested_start_utc=start.isoformat(),
                volume_quality='Broker tick volume; not consolidated market volume; unused.',
                cost_quality='Bar spread in broker points, not tick-by-tick ask or full costs.')))
    return results


def refresh_duka(root, start, end):
    if not datetime.fromisoformat(start) < datetime.fromisoformat(end):
        raise ValueError('Download end date must be after start date.')
    if len(start)!=10 or len(end)!=10: raise ValueError('Use YYYY-MM-DD dates.')
    binary=shutil.which('npx.cmd') or shutil.which('npx')
    if not binary: raise RuntimeError('Node.js/npx is needed for the optional Dukascopy downloader, or import its CSV instead.')
    # Preserve the provider files and its acquisition log, never pass passwords.
    folder=Path(root)/'data/raw/dukascopy_lab'/f'{start}_{end}'
    folder.mkdir(parents=True,exist_ok=True)
    paths=[]
    for instrument,tf in [('eurusd','h1'),('xauusd','m5')]:
        target=folder/f'{instrument}-{tf}-bid-{start}-{end}.csv'
        if not target.is_file():
            args=[binary,'--yes','dukascopy-node@1.51.1','-i',instrument,'-from',start,'-to',end,
                  '-t',tf,'-p','bid','-f','csv','-dir',str(folder.resolve()),'-ch',
                  '-chpath',str((Path(root)/'data/raw/dukascopy_lab/cache').resolve()),
                  '--waf-mode','off','--rate-limit-retries','0']
            result=subprocess.run(args,capture_output=True,text=True,timeout=120)
            (folder/f'{instrument}_download.log').write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
            if result.returncode: raise RuntimeError(f'Dukascopy download failed; see {folder/instrument}_download.log')
        if not target.is_file(): raise RuntimeError('Provider download produced no expected CSV; no replacement feed was used.')
        paths.append(import_duka(root,target,instrument.upper(),tf.upper()))
    return paths


def save_scan(root, manifest):
    data,info=read_dataset(manifest)
    result=scan(data,info['metadata'])
    destination=Path(root)/'reports/setup_lab'/Path(manifest).stem
    destination.mkdir(parents=True,exist_ok=True)
    result['signals'].to_csv(destination/'signals.csv',index=False)
    summarize(result['signals']).to_csv(destination/'summary.csv',index=False)
    payload={k:v for k,v in result.items() if k not in ('signals','latest')}
    payload.update(input_sha256=info['sha256'],manifest=str(manifest),latest=result['latest'].to_dict('records'))
    payload['calculation_sha256']={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
        for name in ('analytics/setup_lab.py','analytics/candidate_signals.py')}
    (destination/'scan.json').write_text(json.dumps(payload,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return dict(manifest=str(manifest),rows=result['completed_bars'],signals=len(result['signals']),
                start_utc=result['start_utc'],end_utc=result['end_utc'],output=str(destination))


def save_overview(root):
    latest={}
    for manifest in (Path(root)/'data/research/setup_lab').glob('*.json'):
        info=json.loads(manifest.read_text(encoding='utf-8')); meta=info['metadata']
        key=(meta['source'],meta['instrument'],meta['timeframe'])
        if key not in latest or meta.get('acquired_at_utc','')>latest[key][1]['metadata'].get('acquired_at_utc',''):
            latest[key]=(manifest,info)
    lines=['# Latest seven-setup research snapshot','',
        'Research-only trigger history; no execution authority, SL/TP outcomes, win rates or after-cost profitability claims.',
        '',f'Generated UTC: {datetime.now(timezone.utc).isoformat()}',
        '', '| Source / instrument / timeframe | Non-flat bars | Flat excluded | R1 | R2 | R3 | R4 | R5 | R6 | R7 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    details=[]
    for key,(manifest,info) in sorted(latest.items()):
        directory=Path(root)/'reports/setup_lab'/manifest.stem
        report=directory/'scan.json'; summary=directory/'summary.csv'
        if not report.is_file() or not summary.is_file(): continue
        data=json.loads(report.read_text(encoding='utf-8'))
        table=pd.read_csv(summary).set_index('setup_id')
        values=' | '.join(str(int(table.loc[setup,'triggers'])) for setup in SETUPS)
        lines.append(f"| {' / '.join(key)} | {data['completed_bars']} | {data['excluded_flat_bars']} | {values} |")
        details.extend(['',f"- **{' / '.join(key)}**: {data['start_utc']} to {data['end_utc']}; "
            f"{data['eligible_bars']} guard-eligible candles. [Signal history](setup_lab/{manifest.stem}/signals.csv), "
            f"[provenance](setup_lab/{manifest.stem}/scan.json)."])
    lines+=details+['','Counts can overlap and are not executable order counts. Inputs have slightly different coverage and warmup; compare matching dates in the app.',
        '', 'See [rule definitions, source limitations and promotion requirements](../docs/SEVEN_SETUP_LAB.md).',
        '', 'The existing native two-setup monitor and its risk limits are unchanged. Refresh is on demand; opening this page does not activate trading.','']
    destination=Path(root)/'reports/latest_setup_lab.md';destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text('\n'.join(lines),encoding='utf-8')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--provider',choices=['mt5','dukascopy','saved'],default='saved')
    parser.add_argument('--start',default=(datetime.now(timezone.utc)-timedelta(days=184)).date().isoformat())
    parser.add_argument('--end',default=datetime.now(timezone.utc).date().isoformat(),help='Dukascopy exclusive UTC end date')
    args=parser.parse_args(argv)
    if args.provider=='mt5': manifests=refresh_mt5(args.root)
    elif args.provider=='dukascopy': manifests=refresh_duka(args.root,args.start,args.end)
    else: manifests=sorted((args.root/'data/research/setup_lab').glob('*.json'))
    for manifest in manifests: print(json.dumps(save_scan(args.root,manifest)))
    save_overview(args.root)
    return 0


if __name__=='__main__': raise SystemExit(main())
