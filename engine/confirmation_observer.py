"""Read-only forward comparison of immediate and confirmed reclaim signals.

This component has no execution authority and never builds or submits orders.
Its output is technical eligibility only: a signal is not an affordable trade.
Existing positions, sizing, targets and entry decisions are controlled elsewhere.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pandas as pd

from analytics.activation_signals import gold_features,eur_features
from analytics.entry_confirmation import MODE,ROWS,confirmed_features,side_of
from analytics.gold_strategy import validate_m5
from analytics.setup_research import validate_bars
from engine.execution_audit import atomic_json,atomic_text

ROOT=Path(__file__).resolve().parents[1]


def source_evidence(root=ROOT):
    return {p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in (
        'config/entry_confirmation_spec.json','analytics/entry_confirmation.py','analytics/activation_signals.py')}


class ConfirmationObserver:
    """One observation per closed candle; persisted across controller restarts."""
    def __init__(self,root,connector):
        self.root,self.connector=Path(root).resolve(),connector
        self.folder=self.root/'logs/observations'/MODE
        self.states={}
        self.sources=None  # No filesystem/network work in construction; checked before observation.

    def observe(self,symbol,candle,*,position_open=False):
        if symbol not in ROWS:
            raise ValueError('Only approved exact instruments can be observed.')
        decision=pd.Timestamp(candle)
        if decision.tz is None:
            raise ValueError('Aware closed-candle timestamp required.')
        interval=pd.Timedelta(minutes=5 if symbol=='XAUUSDm' else 60)
        if decision!=decision.floor('5min' if symbol=='XAUUSDm' else 'h'):
            raise ValueError('Observer candle is off its timeframe grid.')
        path=self.folder/f'{symbol}_checkpoint.json'
        if symbol not in self.states:
            self.states[symbol]=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {
                'version':1,'symbol':symbol,'last_decision_time_utc':None,'observations':0,'latest':{}}
        state=self.states[symbol]
        if state.get('version')!=1 or state.get('symbol')!=symbol:
            raise ValueError('Observer checkpoint identity changed.')
        previous=state['last_decision_time_utc']
        if previous:
            if decision==pd.Timestamp(previous):
                return state['latest']
            if decision<pd.Timestamp(previous):
                raise ValueError('Observation clock moved behind its checkpoint.')
        sources=source_evidence(self.root)
        if self.sources is None:
            self.sources=sources
        if sources!=self.sources:
            raise ValueError('Observation specification/source changed during this run.')
        # A crash cannot repeat an observation or turn it into an entry attempt.
        state.update(last_decision_time_utc=decision.isoformat(),observations=state['observations']+1,
            latest=dict(status='reserved',decision_time_utc=decision.isoformat(),execution_authority=False))
        atomic_json(path,state)
        account=self.connector.verify_pinned_account()
        identity=self.connector.account_identity
        api=self.connector.terminal_api
        timeframe=api.TIMEFRAME_M5 if symbol=='XAUUSDm' else api.TIMEFRAME_H1
        rates=api.copy_rates_from_pos(symbol,timeframe,0,ROWS[symbol]+1)
        if rates is None or not len(rates):
            raise ValueError('Observer has no native historical candles.')
        frame=pd.DataFrame(rates)
        frame.index=pd.DatetimeIndex(pd.to_datetime(frame.pop('time'),unit='s',utc=True))
        frame=(validate_m5(frame) if symbol=='XAUUSDm' else validate_bars(frame))
        frame=frame.loc[frame.index+interval<=decision].iloc[-ROWS[symbol]:]
        if len(frame)!=ROWS[symbol] or frame.index[-1]+interval!=decision:
            raise ValueError('Observation history does not end at the requested closed candle.')
        tick,info,funds=api.symbol_info_tick(symbol),api.symbol_info(symbol),api.account_info()
        if tick is None or info is None or funds is None or info.name!=symbol or info.chart_mode!=0:
            raise ValueError('Observer cannot verify quote, contract or funds.')
        if not 0<tick.bid<=tick.ask:
            raise ValueError('Observer received an invalid/crossed quote.')
        quote_time=pd.Timestamp(tick.time_msc,unit='ms',tz='UTC')
        after=self.connector.verify_pinned_account()
        if after!=account or identity!=self.connector.account_identity:
            raise ValueError('Account changed during read-only observation.')
        raw=frame.to_csv(float_format='%.17g',date_format='%Y-%m-%dT%H:%M:%SZ',lineterminator='\n')
        sha=hashlib.sha256(raw.encode()).hexdigest()
        input_path=self.folder/'inputs'/f'{symbol}_{sha}.csv'
        if input_path.exists():
            if input_path.read_bytes()!=raw.encode():
                raise ValueError('Observation input checksum mismatch.')
        else:
            atomic_text(input_path,raw)
        baseline=(gold_features if symbol=='XAUUSDm' else eur_features)(frame.iloc[1:])
        alternative=confirmed_features(frame,symbol)
        record=dict(status='observed',mode=MODE,symbol=symbol,decision_time_utc=decision.isoformat(),
            checked_at_utc=datetime.now(timezone.utc).isoformat(),execution_authority=False,
            account=account,position_open_when_observed=bool(position_open),source_sha256=self.sources,
            funds=dict(balance=funds.balance,equity=funds.equity,currency=funds.currency),
            quote_at_observation=dict(bid=tick.bid,ask=tick.ask,timestamp_utc=quote_time.isoformat(),
                seconds_after_decision=(quote_time-decision).total_seconds()),
            signal_fresh_at_observation=0<=(pd.Timestamp(datetime.now(timezone.utc))-decision).total_seconds()<=(60 if symbol=='XAUUSDm' else 300),
            contract={key:getattr(info,key) for key in ('trade_contract_size','point','trade_tick_size','volume_min','volume_step')},
            input_file=str(input_path.relative_to(self.root)),input_sha256=sha,
            immediate_reclaim=dict(side=side_of(baseline,symbol),features=baseline),
            confirmed_reclaim=dict(side=side_of(alternative,symbol),reason=alternative.get('reason'),features=alternative),
            limitation='Technical observations only; no native plan admission, fills or profitability are implied.')
        event=self.folder/'events'/f'{symbol}_{decision.strftime("%Y%m%dT%H%M%SZ")}.json'
        if event.exists():
            raise ValueError('This observation already has a durable event; do not overwrite it.')
        atomic_json(event,record)
        state['latest']=record
        atomic_json(path,state)
        self._report()
        return record

    def _report(self):
        lines=['# Forward strategy comparison','',
            '**Execution setup: existing immediate trend/reclaim.**',
            'The one-bar follow-through alternative is observation only and cannot send orders.',
            'Its historical comparison was weak/negative and is not adopted as an entry filter.','',
            '| Instrument | Latest close (Manila) | Immediate technical side | Confirmed technical side | Confirmation reason | Fresh when observed | Records in checkpoint |',
            '| --- | --- | --- | --- | --- | --- | ---: |']
        for symbol,state in self.states.items():
            item=state.get('latest',{})
            if item.get('status')!='observed':
                continue
            local=pd.Timestamp(item['decision_time_utc']).tz_convert('Asia/Manila').isoformat()
            lines.append(f"| {symbol} | {local} | {item['immediate_reclaim']['side'] or 'none'} | "
                f"{item['confirmed_reclaim']['side'] or 'none'} | {item['confirmed_reclaim']['reason']} | "
                f"{item['signal_fresh_at_observation']} | {state['observations']} |")
        lines += ['', 'Technical sides do not establish target room, affordable volume, broker admission or execution.',
            'These timestamps are observations, not proof that the monitor is still running; check its heartbeat and PID.',
            'Entry risk remains 1% baseline, qualified minimum-lot-only up to 2%, and 2% combined.',
            '[Historical comparison](market_analysis/entry_confirmation_2026-11-16/report.md).',
            f'[Raw observations](../logs/observations/{MODE}/).','']
        atomic_text(self.root/'reports/latest_strategy_comparison.md','\n'.join(lines))

    def summary(self):
        return dict(mode=MODE,execution_authority=False,
            execution_entry_mode='immediate_reclaim',
            adoption_decision='Confirmation experiment not adopted: weak/negative historical comparison and too few affordable trades.',
            symbols={s:dict(observations=v['observations'],latest=v['latest']) for s,v in self.states.items()})
