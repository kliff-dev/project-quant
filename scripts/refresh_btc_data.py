"""Read-only incremental BTCUSDm H1 acquisition. Never sends/checks orders."""
from __future__ import annotations

from datetime import datetime,timedelta,timezone
import argparse
import hashlib
import json
import math
from numbers import Real
from pathlib import Path
import re
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
import pandas as pd


HOUR=pd.Timedelta(hours=1)
SYMBOL='BTCUSDm'


def _number(value,label):
    if isinstance(value,bool) or not isinstance(value,Real) or not math.isfinite(float(value)) or value<=0:
        raise ValueError(f'{label} must be a positive finite number.')
    return float(value)


def _validate_prices(data):
    from analytics.btc_setups import scan
    if data.empty: raise ValueError('No completed BTC candles are available.')
    scan(data,as_of=data.index[-1]+HOUR)  # Validate every retained candle, not only recent/forming bars.
    for name in ('tick_volume','real_volume','spread'):
        if name in data:
            numbers=pd.to_numeric(data[name],errors='raise')
            if numbers.isna().any() or not numbers.map(math.isfinite).all() or (numbers<0).any():
                raise ValueError(f'Invalid BTC {name} values.')


def _missing(data):
    return set(pd.date_range(data.index[0],data.index[-1],freq='h').difference(data.index))


def _read_cache(path,manifest,now):
    if path.is_file()!=manifest.is_file():
        raise ValueError('BTC CSV and manifest must exist as a verified pair; refusing to overwrite an orphan file.')
    if not path.is_file(): return None,None
    info=json.loads(manifest.read_text(encoding='utf-8'))
    if not isinstance(info,dict): raise ValueError('Existing BTC manifest must be an object.')
    expected=dict(source='Exness MT5',symbol=SYMBOL,timeframe='H1',price_basis='bid',account_currency='USD')
    if any(info.get(key)!=value for key,value in expected.items()):
        raise ValueError('Existing BTC source, instrument, timeframe or currency metadata differs.')
    digest=info.get('csv_sha256')
    if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest):
        raise ValueError('Existing BTC checksum metadata is invalid.')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
        raise ValueError('Existing BTC dataset checksum mismatch.')
    data=pd.read_csv(path,index_col=0,float_precision='round_trip')
    data.index=pd.DatetimeIndex(pd.to_datetime(data.index))
    if data.index.tz is None: raise ValueError('Existing BTC timestamps must explicitly identify UTC.')
    data.index=data.index.tz_convert('UTC')
    _validate_prices(data)
    if (data.index[-1]+HOUR>now or type(info.get('rows')) is not int or info['rows']!=len(data)):
        raise ValueError('Existing BTC row-count/closed-candle metadata is invalid.')
    for key,stamp in [('first_open_utc',data.index[0]),('last_close_utc',data.index[-1]+HOUR)]:
        declared=pd.Timestamp(info.get(key))
        if declared.tz is None or declared!=stamp: raise ValueError(f'Existing BTC {key} metadata is invalid.')
    for key in ('contract_size','point','volume_min','volume_max','volume_step'):
        _number(info.get(key),f'Cached {key}')
    if type(info.get('digits')) is not int or not 0<=info['digits']<=10:
        raise ValueError('Cached BTC digits are invalid.')
    return data,info


def _verified_account(connector):
    from engine.gold_connector import PINNED_IDENTITY,PINNED_BROKER
    account=connector.verify_pinned_account()
    if (not isinstance(account,dict) or account.get('demo_verified') is not True or
        type(account.get('trade_mode')) is not int or account['trade_mode']!=0 or
        account.get('account_type')!='DEMO' or account.get('currency')!='USD' or
        account.get('broker')!=PINNED_BROKER or account.get('server')!=PINNED_IDENTITY[1] or
        connector.account_identity!=PINNED_IDENTITY):
        raise ValueError('Exact pinned USD DEMO account cannot be verified for BTC acquisition.')
    return account,connector.account_identity


def _contract(info):
    if (info is None or getattr(info,'name',None)!=SYMBOL or getattr(info,'currency_base',None)!='BTC' or
        getattr(info,'currency_profit',None)!='USD' or type(getattr(info,'chart_mode',None)) is not int or info.chart_mode!=0):
        raise ValueError('Exact BTCUSDm BTC/USD bid-chart contract required. No replacement instrument is allowed.')
    values={key:_number(getattr(info,key,None),key) for key in (
        'trade_contract_size','point','volume_min','volume_max','volume_step','trade_tick_size')}
    if values['volume_max']<values['volume_min'] or type(getattr(info,'digits',None)) is not int or not 0<=info.digits<=10:
        raise ValueError('BTC contract volume limits or digits are invalid.')
    from decimal import Decimal
    if Decimal(str(info.volume_min))%Decimal(str(info.volume_step)):
        raise ValueError('BTC minimum volume must align with its volume step.')
    return values


def refresh(root,*,connector_factory=None,now=None):
    """Publish only verified, closed BTC CFD bars; no order-check/send path.

    Missing provider hours remain explicit quality warnings; no synthetic bars
    are inserted. Signal rules independently require contiguous recent windows.
    A caller can inject an aware acquisition time for deterministic offline tests.
    """
    from engine.gold_connector import GoldConnector
    root=Path(root).resolve();folder=root/'data/research/btc'
    path=folder/'BTCUSDm_H1.csv';manifest=folder/'manifest.json'
    now=pd.Timestamp(now if now is not None else datetime.now(timezone.utc))
    if now.tz is None: raise ValueError('An aware acquisition time is required.')
    now=now.tz_convert('UTC').to_pydatetime()
    old=None;old_info=None
    old,old_info=_read_cache(path,manifest,pd.Timestamp(now))
    start=now-timedelta(days=730) if old is None or old.empty else old.index[-1].to_pydatetime()-timedelta(hours=24)
    with (connector_factory or GoldConnector)(timeout_ms=10000) as connector:
        account,identity=_verified_account(connector);api=connector.terminal_api
        info=api.symbol_info(SYMBOL);contract=_contract(info)
        if old_info:
            pairs={'contract_size':'trade_contract_size','point':'point','volume_min':'volume_min',
                   'volume_max':'volume_max','volume_step':'volume_step','digits':'digits'}
            if any(old_info[key]!=getattr(info,attribute) for key,attribute in pairs.items()):
                raise ValueError('BTC contract changed from cached metadata; review before combining history.')
        rates=api.copy_rates_range(SYMBOL,api.TIMEFRAME_H1,start,now)
        if rates is None or not len(rates): raise RuntimeError('MT5 returned no BTC history.')
        data=pd.DataFrame(rates)
        if not {'time','open','high','low','close'}.issubset(data): raise ValueError('MT5 BTC rates lack time/OHLC fields.')
        seconds=pd.to_numeric(data.pop('time'),errors='raise')
        if seconds.isna().any() or not seconds.map(math.isfinite).all(): raise ValueError('MT5 BTC timestamps are invalid.')
        data.index=pd.DatetimeIndex(pd.to_datetime(seconds.to_numpy(),unit='s',utc=True))
        if data.index.has_duplicates or not data.index.is_monotonic_increasing:
            raise ValueError('MT5 BTC rates contain duplicate or unordered timestamps.')
        if data.index[0]<pd.Timestamp(start) or data.index[-1]>pd.Timestamp(now):
            raise ValueError('MT5 BTC rates extend outside the requested range.')
        data=data.loc[data.index+HOUR<=pd.Timestamp(now)].copy()
        _validate_prices(data)
        response_first=data.index[0]
        previous_missing=_missing(old) if old is not None else set()
        if old is not None:
            # Only this explicit update may replace overlapping revised bars.
            data=pd.concat([old.loc[~old.index.isin(data.index)],data]).sort_index()
        _validate_prices(data)
        tick=api.symbol_info_tick(SYMBOL)
        if tick is None: raise RuntimeError('BTC quote unavailable; cannot verify broker contract arithmetic.')
        bid,ask=_number(getattr(tick,'bid',None),'bid'),_number(getattr(tick,'ask',None),'ask')
        if ask<bid or bid<=100: raise ValueError('BTC quote is crossed or invalid for the arithmetic check.')
        buy=api.order_calc_profit(api.ORDER_TYPE_BUY,SYMBOL,info.volume_min,ask,ask+100)
        sell=api.order_calc_profit(api.ORDER_TYPE_SELL,SYMBOL,info.volume_min,bid,bid-100)
        expected=contract['volume_min']*contract['trade_contract_size']*100
        if any(not math.isclose(_number(value,'Broker profit calculation'),expected,rel_tol=1e-8,abs_tol=1e-6) for value in (buy,sell)):
            raise ValueError('Broker profit calculation differs from the linear USD contract; refusing to publish.')
        after,after_identity=_verified_account(connector)
        if after_identity!=identity: raise RuntimeError('Account changed while refreshing BTC data.')
        fields=['name','description','currency_base','currency_profit','currency_margin','trade_contract_size','point','digits',
            'volume_min','volume_max','volume_step','trade_tick_size','trade_tick_value','trade_stops_level','trade_freeze_level',
            'trade_mode','trade_exemode','filling_mode','order_mode','chart_mode','trade_calc_mode','swap_mode','swap_long','swap_short']
        record=dict(source='Exness MT5',symbol='BTCUSDm',instrument_type='broker BTC/USD CFD; not exchange-owned Bitcoin',
            timeframe='H1',price_basis='bid',account_currency=account['currency'],contract_size=info.trade_contract_size,
            point=info.point,volume_min=info.volume_min,volume_max=info.volume_max,volume_step=info.volume_step,digits=info.digits,
            contract={key:getattr(info,key,None) for key in fields},downloaded_at_utc=now.isoformat(),requested_start_utc=start.isoformat(),
            first_open_utc=data.index[0].isoformat(),last_close_utc=(data.index[-1]+pd.Timedelta(hours=1)).isoformat(),rows=len(data),
            profit_calculation_verification=dict(volume=info.volume_min,price_change_usd=100,buy_profit=float(buy),sell_profit=float(sell),
                linear_expected=expected),demo_verified=True,read_only=True,
            known_missing_h1_candles=len(_missing(data)),new_missing_h1_candles=len(_missing(data)-previous_missing),
            missing_h1_candles_utc=[stamp.isoformat() for stamp in sorted(_missing(data))],
            history_truncated_at_start=old is None and response_first>pd.Timestamp(start).ceil('h'),
            quality_warnings=(['Provider history contains missing H1 candles; affected signal windows are guarded.'] if _missing(data) else [])+
                (['Available provider history starts after the requested start.'] if old is None and response_first>pd.Timestamp(start).ceil('h') else []),
            volume_quality='Broker tick volume; not consolidated exchange volume.')
    raw=data.to_csv(index_label='timestamp_utc',lineterminator='\n').encode();record['csv_sha256']=hashlib.sha256(raw).hexdigest()
    json.dumps(record,allow_nan=False)  # Reject malformed optional broker fields before altering either cache file.
    folder.mkdir(parents=True,exist_ok=True)
    if old_info and record['csv_sha256']!=old_info['csv_sha256']:
        archive=folder/'snapshots'/old_info['csv_sha256'];archive.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,archive/path.name);shutil.copy2(manifest,archive/manifest.name)
    csv_tmp=path.with_suffix('.csv.tmp');csv_tmp.write_bytes(raw);csv_tmp.replace(path)
    from dashboard.monitor_control import write_object
    write_object(manifest,record)
    return record


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=ROOT)
    result=refresh(parser.parse_args().root);print(json.dumps(result,indent=2))
