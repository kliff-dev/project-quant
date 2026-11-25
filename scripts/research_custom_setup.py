r"""Reproduce offline profiling and opportunity screens from cached MT5 bars.

Usage: .\.venv\Scripts\python.exe -B scripts/research_custom_setup.py
No terminal connection, credentials, order submission or strategy P&L calculation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SPEC_SHA256 = '16e17c2d136bc8319be54fddd5f685780cf078b407f237da425cac3f7654958b'
sys.path.insert(0, str(ROOT))
from analytics.setup_research import Contract, opportunity_study, signal_features, validate_bars


def main() -> None:
    source = ROOT / 'data/raw/mt5_research_2026-11-13/manifest.json'
    spec_path = ROOT / 'reports/market_analysis/custom_setup_v1_spec.json'
    manifest = json.loads(source.read_text(encoding='utf-8'))
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    spec_hash = hashlib.sha256(spec_path.read_bytes()).hexdigest()
    # This is a frozen experiment registration, not a configurable parameter file.
    if spec['version'] != '1.0' or not spec['frozen_before_signal_study'] or spec_hash != EXPECTED_SPEC_SHA256:
        raise ValueError('Registered v1 specification changed. Register a new experiment and update its analysis together.')
    if manifest['account']['account_type'] != 'DEMO' or manifest['account']['trade_mode'] != 0:
        raise ValueError('Expected provenance from verified DEMO account.')
    if manifest['account']['currency'] != 'USD':
        raise ValueError('This study assumes a USD account.')
    output = ROOT / 'reports/market_analysis/custom_setup_v1'
    output.mkdir(parents=True, exist_ok=True)
    result = {'study_type': 'opportunity feasibility, NOT a strategy profitability backtest',
              'spec_sha256': spec_hash,
              'manifest_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'analysis_code_sha256': hashlib.sha256((ROOT / 'analytics/setup_research.py').read_bytes()).hexdigest(),
              'source_manifest': str(source.relative_to(ROOT)), 'profiles': [], 'summaries': []}
    for item in manifest['datasets']:
        path = ROOT / item['file']
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError(f'Dataset checksum mismatch: {path}')
        frame = pd.read_csv(path)
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop('timestamp_utc'), utc=True))
        frame = validate_bars(frame)
        raw = item['contract']
        expected_base = {'EURUSDm': 'EUR', 'XAUUSDm': 'XAU'}.get(item['symbol'])
        if expected_base is None or raw['currency_base'] != expected_base or raw['currency_profit'] != 'USD':
            raise ValueError('Expected the registered EUR/USD or gold/USD contract.')
        contract = Contract(item['symbol'], raw['trade_contract_size'], raw['point'], raw['trade_tick_size'], raw['volume_min'], raw['volume_step'])
        features = signal_features(frame)
        dev = features.loc[features.decision_time < pd.Timestamp('2025-11-13', tz='UTC')]
        unit = .0001 if item['symbol'] == 'EURUSDm' else 1.0
        gaps = frame.index.to_series().diff().dt.total_seconds().div(3600)
        profile = {'symbol': item['symbol'], 'rows': len(frame), 'first_bar_utc': frame.index[0].isoformat(),
                   'last_bar_utc': frame.index[-1].isoformat(), 'duplicates': 0, 'invalid_ohlc_rows': 0,
                   'gap_count_over_1h': int((gaps > 1).sum()),
                   'gap_note': 'Includes weekends, holidays and gold daily closures; not a confirmed missing-candle count.',
                   'largest_gaps': {str(k): int(v) for k,v in gaps.loc[gaps > 1].value_counts().head(8).items()},
                   'real_volume_nonzero_rows': int(frame.real_volume.ne(0).sum()),
                   'tick_volume_zero_rows': int(frame.tick_volume.eq(0).sum()),
                   'development_atr_quantiles': {str(k):float(v) for k,v in (dev.atr14 / unit).quantile([.1,.5,.9]).items()},
                   'development_spread_quantiles': {str(k):float(v) for k,v in (dev.spread * contract.point / unit).quantile([.1,.5,.9,.99]).items()},
                   'display_unit': 'EUR/USD pips' if unit == .0001 else 'USD per troy ounce'}
        result['profiles'].append(profile)
        opportunities = opportunity_study(frame, contract)
        if opportunities.empty:
            raise ValueError(f'No candidates: {item["symbol"]}')
        opportunities.to_csv(output / f'{item["symbol"]}_opportunities.csv', index=False, float_format='%.10g', lineterminator='\n')
        groups = ['symbol','family','scenario','capital','risk_percent','period']
        summary = opportunities.groupby(groups, dropna=False).agg(candidates=('eligible','size'), eligible=('eligible','sum'))
        summary['eligible_percent'] = summary.eligible / summary.candidates * 100
        for reason in ('minimum_lot_risk','cost_fraction','nonconsecutive_bars'):
            counts = opportunities.loc[opportunities.rejection == reason].groupby(groups).size()
            summary[f'rejected_{reason}'] = counts.reindex(summary.index, fill_value=0)
        summary = summary.reset_index()
        result['summaries'].extend(summary.to_dict(orient='records'))
        all_summary = opportunities.groupby(['family','scenario','capital','risk_percent']).eligible.agg(['size','sum'])
        print(item['symbol'], json.dumps(profile))
        print(all_summary.to_string())
    text = json.dumps(result, indent=2, allow_nan=False)
    (output / 'summary.json').write_text(text + '\n', encoding='utf-8')
    pd.DataFrame(result['summaries']).to_csv(output / 'summary.csv', index=False, float_format='%.8g', lineterminator='\n')
    print('Saved:', output.relative_to(ROOT))


if __name__ == '__main__':
    main()
