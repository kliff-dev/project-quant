"""Read-only current market context for the dated study; no execution methods."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from analytics.candidate_signals import wilder_atr,wilder_rsi
from dashboard.research_model import diagnostics,price_charts
from engine.gold_connector import GoldConnector,validate_gold_contract

OUT=ROOT/'reports/market_analysis/market_behaviour_2026-11-16'


def collect():
    OUT.mkdir(parents=True,exist_ok=True)
    now=datetime.now(timezone.utc)
    result=dict(checked_at_utc=now.isoformat(),read_only=True,orders_sent=0,
                symbols={},warnings=[])
    with GoldConnector(timeout_ms=12000) as connector:
        before=connector.verify_pinned_account()
        identity=connector.account_identity
        api=connector.terminal_api
        result['account']=before
        account=api.account_info()
        result['balance']=account.balance
        result['equity']=account.equity
        positions,orders=api.positions_get(),api.orders_get()
        if positions is None or orders is None:
            raise RuntimeError('Cannot verify current broker inventory')
        result['open_positions']=len(positions)
        result['pending_orders']=len(orders)
        for symbol,tf,count,interval in [('XAUUSDm',api.TIMEFRAME_M5,1201,pd.Timedelta(minutes=5)),
                                         ('EURUSDm',api.TIMEFRAME_H1,3001,pd.Timedelta(hours=1))]:
            info=api.symbol_info(symbol)
            tick=api.symbol_info_tick(symbol)
            if info is None or tick is None:
                raise RuntimeError('No native symbol/quote for '+symbol)
            if symbol=='XAUUSDm':
                contract=validate_gold_contract(info)
            else:
                if (info.currency_base!='EUR' or info.currency_profit!='USD' or
                        info.trade_contract_size!=100000 or info.chart_mode!=0):
                    raise RuntimeError('Unverified EUR/USD bid contract')
                contract={k:getattr(info,k) for k in ['name','description','currency_base','currency_profit',
                    'trade_contract_size','point','trade_tick_size','volume_min','volume_step','chart_mode']}
            item=dict(contract=contract,bid=tick.bid,ask=tick.ask,spread=tick.ask-tick.bid,
                quote_time_utc=pd.Timestamp(tick.time_msc,unit='ms',tz='UTC').isoformat(),
                quote_age_seconds=now.timestamp()-tick.time_msc/1000)
            if item['quote_age_seconds']>120 or item['quote_age_seconds']< -5:
                result['warnings'].append(symbol+' quote age outside -5 to 120 seconds')
            for name,frame_id,size,delta in [('entry',tf,count,interval),('D1',api.TIMEFRAME_D1,261,pd.Timedelta(days=1))]:
                rates=api.copy_rates_from_pos(symbol,frame_id,0,size)
                if rates is None or len(rates)==0:
                    raise RuntimeError('No '+name+' history for '+symbol)
                data=pd.DataFrame(rates)
                data.index=pd.DatetimeIndex(pd.to_datetime(data.pop('time'),unit='s',utc=True),name='timestamp_utc')
                data=data.loc[data.index+delta<=pd.Timestamp(now)]
                if data.index.has_duplicates or not data.index.is_monotonic_increasing:
                    raise RuntimeError('Invalid timestamp order')
                if (data[['open','high','low','close']]<=0).any().any() or (
                    data.high<data[['open','close','low']].max(axis=1)).any() or (
                    data.low>data[['open','close','high']].min(axis=1)).any():
                    raise RuntimeError('Invalid prices')
                path=OUT/f'current_{symbol}_{name}.csv'
                data.to_csv(path)
                item[name+'_dataset']=dict(file=path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    bars=len(data),last_open_utc=data.index[-1].isoformat(),
                    forming_bars_excluded=True)
                if name=='entry':
                    diagnostic=diagnostics(data,symbol)
                    item['latest_closed_entry_features']=diagnostic['features']
                    item['technical_side']=diagnostic['side']
                    item['technical_reason']=diagnostic['reason']
                    entry,context=price_charts(diagnostic)
                    entry.write_html(OUT/f'current_{symbol}_entry.html',include_plotlyjs=True)
                    context.write_html(OUT/f'current_{symbol}_context.html',include_plotlyjs=True)
                else:
                    item['daily_context']=dict(last_closed_bid=float(data.close.iloc[-1]),
                        atr14=float(wilder_atr(data).iloc[-1]),rsi14=float(wilder_rsi(data.close).iloc[-1]),
                        ema20=float(data.close.ewm(span=20,adjust=False).mean().iloc[-1]),
                        ema50=float(data.close.ewm(span=50,adjust=False).mean().iloc[-1]),
                        ema200=float(data.close.ewm(span=200,adjust=False).mean().iloc[-1]),
                        last_5_closed_bar_return_percent=float((data.close.iloc[-1]/data.close.iloc[-6]-1)*100),
                        last_20_closed_bar_return_percent=float((data.close.iloc[-1]/data.close.iloc[-21]-1)*100))
            result['symbols'][symbol]=item
        after=connector.verify_pinned_account()
        if identity!=connector.account_identity or before!=after or not api.terminal_info().connected:
            raise RuntimeError('Broker identity/connection changed during snapshot')
        result['account_verified_before_and_after']=True
    (OUT/'current_market_snapshot.json').write_text(json.dumps(result,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    collect()
