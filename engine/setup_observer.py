"""Persist versioned setup opportunities separately from execution authority."""
from __future__ import annotations
from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd

from analytics.two_setups import evaluate_setups, PROFILE, SETUPS
from engine.confirmation_observer import ConfirmationObserver
from engine.execution_audit import atomic_json, atomic_text
from engine.setup_profiles import metadata


class SetupObserver:
    """Research records cannot send, retry or close an order."""
    def __init__(self,root,connector,*,profile=PROFILE):
        self.root=Path(root).resolve()
        from engine.setup_profiles import MULTI,MULTI_SETUPS
        if profile not in (PROFILE,MULTI):
            raise ValueError('Unknown observation setup profile.')
        self.profile=profile
        self.setup_ids=MULTI_SETUPS if profile==MULTI else SETUPS
        if profile==MULTI:
            from analytics.five_setups import evaluate_setups as evaluate_five
            self.evaluate=evaluate_five
        else:self.evaluate=evaluate_setups
        self.folder=self.root/'logs/observations'/profile
        self.collector=ConfirmationObserver(root,connector)
        self.states={}

    def observe_decision(self,symbol,candle,*,position_open=False,execution_decision=None):
        metadata(self.profile)
        decision=pd.Timestamp(candle)
        if decision.tz is None:
            raise ValueError('Aware closed-candle timestamp required.')
        path=self.folder/f'{symbol}_checkpoint.json'
        if symbol not in self.states:
            self.states[symbol]=json.loads(path.read_text()) if path.exists() else dict(
                version=1,symbol=symbol,last_decision_time_utc=None,observations=0,
                technical_counts={key:0 for key in self.setup_ids},overlaps=0,conflicts=0,latest={})
        state=self.states[symbol]
        if state.get('version')!=1 or state.get('symbol')!=symbol:
            raise ValueError('Two-setup checkpoint identity changed.')
        previous=state['last_decision_time_utc']
        if previous:
            if decision==pd.Timestamp(previous):return state['latest']
            if decision<pd.Timestamp(previous):raise ValueError('Two-setup clock moved behind its checkpoint.')
        state.update(last_decision_time_utc=decision.isoformat(),
            latest=dict(status='reserved',decision_time_utc=decision.isoformat(),execution_authority=False))
        atomic_json(path,state)
        collected=self.collector.observe(symbol,candle,position_open=position_open)
        if collected.get('status')!='observed':
            raise ValueError('Underlying observation was interrupted; do not repeat.')
        input_path=self.root/collected['input_file']
        import hashlib
        stored_bytes=input_path.read_bytes()
        # The older collector hashes UTF-8/LF text before the atomic Windows
        # text writer expands LF to CRLF. Verify its declared representation,
        # while separately preserving the actual on-disk byte fingerprint.
        if hashlib.sha256(stored_bytes.replace(b'\r\n',b'\n')).hexdigest()!=collected['input_sha256']:
            raise ValueError('Observed input checksum changed.')
        frame=pd.read_csv(input_path,index_col=0,float_precision='round_trip')
        frame.index=pd.DatetimeIndex(pd.to_datetime(frame.index,utc=True))
        comparison=self.evaluate(frame.iloc[1:],symbol)
        execution_decision=execution_decision or {}
        same_candle=execution_decision.get('decision_time_utc')==decision.isoformat()
        record=dict(status='observed',profile=self.profile,symbol=symbol,decision_time_utc=decision.isoformat(),
            checked_at_utc=datetime.now(timezone.utc).isoformat(),execution_authority=False,
            source_evidence=metadata(self.profile), input_file=collected['input_file'],input_sha256=collected['input_sha256'],
            input_hash_representation='UTF-8 with LF newlines',
            input_storage_sha256=hashlib.sha256(stored_bytes).hexdigest(),
            quote_at_observation=collected['quote_at_observation'],funds=collected['funds'],
            position_open_when_observed=bool(position_open),
            signal_fresh_at_observation=collected['signal_fresh_at_observation'],
            setup_signals=comparison['setup_signals'],selected_setup=comparison['selected_setup'],
            setup_market_context=comparison.get('market_context',{}),
            selection_reason=comparison['selection_reason'],
            execution_status=execution_decision.get('status') if same_candle else 'not_evaluated_position_open' if position_open else 'not_evaluated',
            execution_reason=execution_decision.get('reason') if same_candle else None,
            execution_setup_id=execution_decision.get('setup_id') if same_candle else None,
            session_id=execution_decision.get('session_id') if same_candle else None,
            limitation='Technical opportunities and actual execution decisions are separate; rejected signals have no actual broker P&L.')
        event=self.folder/'events'/f'{symbol}_{decision.strftime("%Y%m%dT%H%M%SZ")}.json'
        if event.exists():raise ValueError('Observation already exists; never overwrite an immutable event.')
        atomic_json(event,record)
        for key in self.setup_ids:state['technical_counts'][key]+=int(bool(comparison['setup_signals'][key]['side']))
        state['overlaps']+=int(sum(bool(v.get('side')) for v in comparison['setup_signals'].values())>1 and comparison['selected_setup'] is not None)
        state['conflicts']+=int(comparison['selection_reason']=='conflicting_setup_directions')
        state.update(observations=state['observations']+1,latest=record)
        atomic_json(path,state)
        self.report()
        return record

    def observe(self,symbol,candle,*,position_open=False):
        return self.observe_decision(symbol,candle,position_open=position_open)

    def report(self):
        if self.profile!='reclaim_sweep_v1':
            return self._multi_report()
        lines=['# Two-setup DEMO comparison','',
            '**A (trend_reclaim): trend pullback / EMA20 reclaim. B (trend_sweep): trend-aligned prior-20-bar sweep/rejection.**',
            'Both are eligible under the same DEMO controller and shared risk limits. A has priority on overlap; opposite directions skip both.',
            '', '| Instrument | Latest close (Manila) | Fresh | A side | B side | Selected | Actual execution decision | Reason |',
            '| --- | --- | --- | --- | --- | --- | --- | --- |']
        for symbol,state in self.states.items():
            item=state.get('latest',{})
            if item.get('status')!='observed':continue
            sides=item['setup_signals']
            reason=str(item.get('execution_reason') or item['selection_reason']).replace('|','/').replace('\n',' ')
            lines.append(f"| {symbol} | {pd.Timestamp(item['decision_time_utc']).tz_convert('Asia/Manila').isoformat()} | {item['signal_fresh_at_observation']} | "
                f"{sides[SETUPS[0]]['side'] or 'none'} | {sides[SETUPS[1]]['side'] or 'none'} | "
                f"{item['selected_setup'] or 'none'} | {item['execution_status']} | {reason} |")
        lines+=['','## Opportunity counts since this experiment started','',
            '| Instrument | Closed-candle observations | A signals | B signals | Overlaps | Conflicts |',
            '| --- | ---: | ---: | ---: | ---: | ---: |']
        for symbol,state in self.states.items():
            lines.append(f"| {symbol} | {state['observations']} | {state['technical_counts'][SETUPS[0]]} | "
                f"{state['technical_counts'][SETUPS[1]]} | {state['overlaps']} | {state['conflicts']} |")
        lines+=['','Counts include technical observations during an existing position and possibly stale startup candles; use each record\'s freshness field when counting actionable signals. These are not executed-trade counts.',
            'Actual positions, deal verification, fees and results are in the [trade journal](trade_journal.md).',
            'Check the monitor heartbeat for current activity; a saved observation is historical evidence.',
            'Sizing and portfolio limits are enforced by the current session; see latest_monitor_status.md.',
            '[Frozen setup rules](../config/two_setup_spec.json) · [Historical A/B comparison](market_analysis/two_setups_2026-11-18/report.md)',
            f'[Immutable forward observations](../logs/observations/{PROFILE}/events/)','']
        for name in ('latest_setup_comparison.md','latest_strategy_comparison.md'):
            atomic_text(self.root/'reports'/name,'\n'.join(lines))

    def summary(self):
        return dict(mode=self.profile,execution_authority=False,
            execution_entry_mode='five_setup_shared_account' if len(self.setup_ids)==5 else 'A_reclaim_or_B_sweep_shared_account',
            symbols={s:dict(observations=v['observations'],technical_counts=v['technical_counts'],latest=v['latest'])
                     for s,v in self.states.items()})

    def _multi_report(self):
        lines=['# Five-setup DEMO monitoring','',
            'Five executable entry families per market; one deterministic candidate per completed candle.',
            'Technical triggers still require native risk, cost, target-room, permission and portfolio admission.',
            'Opposing signals skip that candle; same-side overlaps use frozen priority; no fallback after rejection.','',
            '| Market | Latest close (Manila) | Selected setup | Decision | Reason |',
            '| --- | --- | --- | --- | --- |']
        for symbol,state in self.states.items():
            item=state.get('latest',{})
            if item.get('status')!='observed':continue
            reason=str(item.get('execution_reason') or item.get('selection_reason')).replace('|','/').replace('\n',' ')
            lines.append(f"| {symbol} | {pd.Timestamp(item['decision_time_utc']).tz_convert('Asia/Manila').isoformat()} | {item['selected_setup'] or 'none'} | {item['execution_status']} | {reason} |")
        lines+=['','## Technical trigger counts','',
            '| Market | Setup | Signals |','| --- | --- | ---: |']
        for symbol,state in self.states.items():
            for key in self.setup_ids:lines.append(f"| {symbol} | {key} | {state['technical_counts'][key]} |")
        lines+=['','Counts are technical observations, not executed trades or profitability evidence.',
            'Weekday, month and DST-aware session context are descriptive; no weekday win-rate advantage is assumed.',
            'Current account risk and position limits: see [monitor status](latest_monitor_status.md).',
            '[Setup guide](../docs/FIVE_SETUP_GUIDE.md) · [Actual trade journal](trade_journal.md)',
            f'[Saved observations](../logs/observations/{self.profile}/events/)','']
        for name in ('latest_setup_comparison.md','latest_strategy_comparison.md'):
            atomic_text(self.root/'reports'/name,'\n'.join(lines))
