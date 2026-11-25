"""Inspect forward-comparison checkpoints; --collect performs read-only DEMO reads."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys

import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from analytics.entry_confirmation import MODE
from engine.confirmation_observer import ConfirmationObserver
from engine.execution_audit import atomic_json


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collect',action='store_true')
    args=parser.parse_args(argv)
    result=dict(mode='read_only_observer_check',checked_at_utc=datetime.now(timezone.utc).isoformat(),
        observation_mode=MODE,orders_sent=0,execution_authority=False,symbols={})
    if args.collect:
        from engine.gold_connector import GoldConnector
        from engine.activation_demo import verify_sources
        verify_sources(ROOT)
        with GoldConnector(timeout_ms=12000) as connector:
            result['account']=connector.verify_pinned_account()
            identity=connector.account_identity
            api=connector.terminal_api
            watcher=ConfirmationObserver(ROOT,connector)
            for symbol,tf,seconds in [('EURUSDm',api.TIMEFRAME_H1,3600),('XAUUSDm',api.TIMEFRAME_M5,300)]:
                rates=api.copy_rates_from_pos(symbol,tf,0,4)
                if rates is None or not len(rates):
                    raise ValueError('No complete native candle for '+symbol)
                now=datetime.now(timezone.utc).timestamp()
                closes=[float(row['time'])+seconds for row in rates if float(row['time'])+seconds<=now]
                if not closes:
                    raise ValueError('No closed native candle for '+symbol)
                candle=datetime.fromtimestamp(max(closes),timezone.utc).isoformat()
                positions=api.positions_get()
                if positions is None:
                    raise ValueError('Cannot verify native inventory')
                item=watcher.observe(symbol,candle,position_open=any(p.symbol==symbol for p in positions))
                result['symbols'][symbol]=dict(status=item['status'],decision_time_utc=candle,
                    baseline_side=item.get('immediate_reclaim',{}).get('side'),
                    confirmation_side=item.get('confirmed_reclaim',{}).get('side'),
                    confirmation_reason=item.get('confirmed_reclaim',{}).get('reason'))
            positions,pending=api.positions_get(),api.orders_get()
            if positions is None or pending is None:
                raise ValueError('Cannot verify final inventory')
            result.update(open_positions=len(positions),pending_orders=len(pending))
            connector.verify_pinned_account()
            if identity!=connector.account_identity:
                raise ValueError('Account changed during observer check')
        atomic_json(ROOT/'reports/strategy_updates/confirmation_2026-11-16/native_observer_check.json',result)
    else:
        folder=ROOT/'logs/observations'/MODE
        for symbol in ['EURUSDm','XAUUSDm']:
            path=folder/f'{symbol}_checkpoint.json'
            result['symbols'][symbol]=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'status':'no_observations'}
    print(json.dumps(result,indent=2,allow_nan=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
