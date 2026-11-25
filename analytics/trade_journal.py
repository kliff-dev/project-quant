"""Read-only DEMO trade journal from saved execution evidence and optional MT5 reads.

Execution tests are never included in strategy performance. Open or uncertain
trades are never counted as completed wins/losses. This module cannot send orders.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from engine.execution_audit import atomic_json

MANILA = ZoneInfo('Asia/Manila')
STRATEGY = 'H4/H1 trend continuation: EMA pullback or breakout (custom v1)'
REASONS = {0: 'desktop_terminal', 1: 'mobile_terminal', 2: 'web_terminal',
           3: 'expert_or_python', 4: 'stop_loss', 5: 'take_profit', 6: 'stop_out'}
CSV_COLUMNS = ['trade_id', 'category', 'strategy', 'setup_id', 'entry_setups', 'trigger', 'status', 'symbol', 'side', 'volume_lots',
    'entry_time_utc', 'entry_time_manila', 'exit_time_utc', 'exit_time_manila', 'holding_seconds',
    'entry_price', 'exit_price', 'initial_stop_loss', 'initial_take_profit', 'current_stop_loss', 'current_take_profit',
    'risk_limit_percent', 'risk_mode', 'risk_ceiling_percent', 'risk_branch', 'risk_budget_usd', 'planned_risk_usd', 'planned_risk_percent_used', 'risk_at_fill_usd',
    'gross_sl_loss_usd', 'gross_tp_profit_usd', 'entry_spread_pips', 'entry_slippage_points',
    'commission_usd', 'fee_usd', 'booked_swap_usd', 'gross_realized_pnl_usd', 'booked_cashflows_usd',
    'realized_net_pnl_usd', 'unrealized_price_pnl_usd', 'unrealized_swap_usd', 'realized_r',
    'exit_reason', 'exit_evaluation', 'last_observed_at_utc', 'observation_source', 'position_ticket', 'position_id',
    'entry_order_ticket', 'entry_deal_tickets', 'exit_deal_tickets', 'context_sha256', 'source_state_file']


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError('Trade evidence requires finite numeric values.')
    return float(value)


def _stamp(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat()


def _local(stamp: str | None) -> str | None:
    return datetime.fromisoformat(stamp).astimezone(MANILA).isoformat() if stamp else None


def load_sources(directory: Path) -> tuple[list[tuple[Path, dict]], list[str]]:
    sources, warnings, seen = [], [], {}
    for path in [directory / 'demo_test_state.json', *sorted((directory / 'sessions').glob('*/demo_test_state.json'))]:
        if not path.is_file():
            continue
        state = json.loads(path.read_text(encoding='utf-8'))
        if not state.get('entry_verified_deals'):
            warnings.append(f'{path}: no verified entry deals; excluded from filled-trade statistics.')
            continue
        account = state['account']
        if account.get('demo_verified') is not True or type(account.get('trade_mode')) is not int or account['trade_mode'] != 0:
            raise ValueError(f'{path}: source is not verified DEMO evidence.')
        if account['currency'] != 'USD':
            raise ValueError('This journal currently reports USD accounts only.')
        key = (state['account_identity']['login'], state['account_identity']['server'], state['position_id'])
        if key in seen:
            if seen[key] != state:
                raise ValueError('Conflicting state files for the same account/position; resolve before reporting.')
            warnings.append(f'{path}: identical duplicate position evidence skipped.')
            continue
        seen[key] = state
        sources.append((path, state))
    return sources, warnings


def capture_broker_snapshot(connector: Any, sources: list[tuple[Path, dict]]) -> dict:
    """Optional native reads only. Requires the recorded account, never credentials."""
    account = connector.verify_demo_account()
    identity = connector.account_identity
    if not sources or any((s['account_identity']['login'], s['account_identity']['server']) != identity for _, s in sources):
        raise ValueError('A live refresh must use the exact recorded DEMO account for all sources.')
    start = min(datetime.fromisoformat(s['started_at_utc']) for _, s in sources) - timedelta(minutes=5)
    end = datetime.now(timezone.utc) + timedelta(minutes=1)
    api = connector.terminal_api
    deals, orders, positions = api.history_deals_get(start, end), api.history_orders_get(start, end), api.positions_get()
    if deals is None or orders is None or positions is None:
        raise ValueError('Native history/inventory is unavailable; no refreshed result will be published.')
    connector.verify_demo_account()
    if connector.account_identity != identity:
        raise ValueError('Account changed during the read-only journal refresh.')
    identifiers = {s['position_id'] for _, s in sources}
    return dict(version=1, captured_at_utc=datetime.now(timezone.utc).isoformat(), account=account,
        account_identity=dict(login=identity[0], server=identity[1]), from_utc=start.isoformat(), to_utc=end.isoformat(),
        deals=[d._asdict() for d in deals if d.position_id in identifiers],
        orders=[o._asdict() for o in orders if o.position_id in identifiers],
        positions=[p._asdict() for p in positions if p.identifier in identifiers])


def _validate_deals(deals: list[dict], state: dict, *, closing: bool) -> None:
    if len({d['ticket'] for d in deals}) != len(deals):
        raise ValueError('Duplicate actual deal tickets; refusing to double-count cashflows.')
    expected = 1 - state['request']['type'] if closing else state['request']['type']
    for deal in deals:
        if (deal['position_id'] != state['position_id'] or deal['type'] != expected or
            deal['entry'] not in ((1, 3) if closing else (0,)) or
            deal.get('symbol', state['symbol']) != state['symbol'] or
            (not closing and deal['order'] != state['open_result']['order'])):
            raise ValueError('Actual deals do not match this exact entry/position/direction.')
        if _number(deal['volume']) <= 0 or _number(deal['price']) <= 0 or _number(deal['time_msc']) <= 0:
            raise ValueError('Actual deals require positive volume, price and timestamps.')
        for field in ('profit', 'commission', 'fee', 'swap'):
            _number(deal[field])


def replay_signal(state: dict, directory: Path) -> dict:
    """Replay only the frozen pre-entry H1 snapshot; never download later data."""
    intent = (state.get('protection') or {}).get('strategy_intent')
    if not intent:
        return dict(status='not_a_strategy_trade')
    fingerprint = intent['context_sha256']
    if len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint):
        return dict(status='unverified', warning='Invalid entry context fingerprint.')
    if ((state.get('protection') or {}).get('policy') or {}).get('target_mode')=='gold_intraday_v1':
        from engine.gold_orders import verified_context
        from engine.strategy_orders import StrategyIntent
        from types import SimpleNamespace
        contract=SimpleNamespace(name='XAUUSDm',currency_base='XAU',currency_profit='USD',
            trade_contract_size=100.,volume_min=.01,volume_max=200.,volume_step=.01,
            point=.001,trade_tick_size=.001,digits=3,chart_mode=0)
        try:
            features=verified_context(StrategyIntent(**intent),contract)
        except (ValueError,OSError,KeyError) as exc:
            return dict(status='unverified',warning=f'Gold frozen input replay failed: {exc}')
        return dict(status='verified',context_sha256=fingerprint,decision_time_utc=intent['decision_time_utc'],
            strategy='Gold M15/M5 '+(intent.get('adaptive_context') or {}).get('setup_id','EMA20 reclaim'),features=features,
            limitation='Frozen closed-candle causal replay; does not prove execution profitability.')
    if ((state.get('protection') or {}).get('policy') or {}).get('entry_timing') == 'user_activation_v1':
        from engine.adaptive_orders import _verified_context
        from engine.strategy_orders import StrategyIntent
        from types import SimpleNamespace
        try:
            features = _verified_context(StrategyIntent(**intent),
                SimpleNamespace(name=intent['symbol'], chart_mode=0, currency_base='EUR',
                                currency_profit='USD', trade_tick_size=.00001),
                target_mode='adaptive_pullback_v2')
        except (ValueError, OSError, KeyError) as exc:
            return dict(status='unverified', warning=f'Activated EUR frozen input replay failed: {exc}')
        return dict(status='verified', context_sha256=fingerprint,
            decision_time_utc=intent['decision_time_utc'], features=features,
            entry_timing='user_activation_v1',
            limitation='Frozen causal replay of activation timing; does not establish profitability.')
    matches = list((directory / 'strategy_inputs').glob(f'*_H1_{fingerprint}.csv'))
    if len(matches) != 1:
        return dict(status='unverified', warning='Frozen entry input snapshot is missing or ambiguous.')
    path = matches[0]
    if hashlib.sha256(path.read_bytes()).hexdigest() != fingerprint:
        return dict(status='unverified', warning='Frozen entry input checksum does not match.')
    import pandas as pd
    from analytics.setup_research import signal_features
    data = pd.read_csv(path, index_col=0, parse_dates=True)
    data.index = pd.DatetimeIndex(pd.to_datetime(data.index, utc=True))
    row = signal_features(data).iloc[-1]
    if row.decision_time.isoformat() != intent['decision_time_utc']:
        return dict(status='unverified', warning='Input snapshot ends at a different decision time.')
    direction = 'short' if intent['side'] == 'sell' else 'long'
    expected = ['pullback', 'breakout'] if intent['trigger'] == 'both' else [intent['trigger']]
    matches_signal = (all(bool(row[f'{trigger}_{direction}']) for trigger in expected)
                      and bool(row.session) and bool(row.recent_is_consecutive))
    h4 = data.resample('4h', origin='epoch').agg({'close': 'last'}).dropna()
    h4 = h4.loc[h4.index + pd.Timedelta(hours=4) <= row.decision_time].copy()
    h4['ema50'] = h4.close.ewm(span=50, adjust=False, min_periods=50).mean()
    h4['ema200'] = h4.close.ewm(span=200, adjust=False, min_periods=200).mean()
    last = h4.iloc[-1]
    return dict(status='verified' if matches_signal else 'unverified', source_file=str(path.resolve()),
        context_sha256=fingerprint, decision_time_utc=row.decision_time.isoformat(),
        h4_trend='bearish' if bool(row.trend_short) else 'bullish' if bool(row.trend_long) else 'neutral',
        h4_close=float(last.close), h4_ema50=float(last.ema50), h4_ema200=float(last.ema200),
        h4_ema50_change_over_5_bars=float(last.ema50-h4.ema50.iloc[-6]),
        h1_open=float(row.open), h1_high=float(row.high), h1_low=float(row.low), h1_close=float(row.close),
        h1_ema20=float(row.ema20), h1_atr14=float(row.atr14), previous_h1_low=float(data.low.iloc[-2]),
        previous_h1_high=float(data.high.iloc[-2]), structural_high5=float(row.high5), structural_low5=float(row.low5),
        trigger=intent['trigger'], matching_trigger=matches_signal, london_session=bool(row.session),
        consecutive_structure_bars=bool(row.recent_is_consecutive),
        limitation='Replayed with current calculation code; historical code version was not frozen in the entry record.')


def build_trade(path: Path, state: dict, directory: Path, *, snapshot: dict | None = None,
                monitor: dict | None = None) -> dict:
    entries, exits = state['entry_verified_deals'], state.get('close_verified_deals', [])
    warnings = []
    observation = state.get('verified_at_utc', state['started_at_utc'])
    source, position, fresh = 'saved_verified_execution_state', None, False
    if snapshot is not None:
        account = snapshot['account']
        if (account.get('demo_verified') is not True or type(account.get('trade_mode')) is not int or account['trade_mode'] != 0):
            raise ValueError('Broker snapshot is not independently verified DEMO.')
        if (snapshot['account_identity'] == state['account_identity'] and
            datetime.fromisoformat(snapshot['captured_at_utc']) >= datetime.fromisoformat(observation)):
            fresh, source, observation = True, 'cached_native_broker_snapshot', snapshot['captured_at_utc']
            selected = [d for d in snapshot['deals'] if d['position_id'] == state['position_id']]
            entries = [d for d in selected if d['entry'] == 0]
            exits = [d for d in selected if d['entry'] in (1, 3)]
            if not {d['ticket'] for d in state['entry_verified_deals']}.issubset({d['ticket'] for d in entries}):
                raise ValueError('Broker history does not contain the recorded entry deal(s).')
            for saved in state['entry_verified_deals']:
                actual = next(d for d in entries if d['ticket'] == saved['ticket'])
                if (any(actual[k] != saved[k] for k in ('order', 'position_id', 'entry', 'type', 'time_msc')) or
                    any(not math.isclose(actual[k], saved[k], rel_tol=0, abs_tol=1e-12) for k in ('price', 'volume'))):
                    raise ValueError('Native entry evidence conflicts with the saved execution facts.')
            positions = [p for p in snapshot['positions'] if p['identifier'] == state['position_id']]
            if len(positions) > 1:
                raise ValueError('Multiple positions match one journal identity.')
            position = positions[0] if positions else None
            if position is not None:
                warnings.append('Floating P&L and inventory are point-in-time broker samples; later changes are not reflected until another native refresh.')
            for deal in entries + exits:
                matching = [o for o in snapshot['orders'] if o['ticket'] == deal['order'] and
                            o['position_id'] == state['position_id'] and o['symbol'] == state['symbol'] and
                            o['type'] == deal['type'] and o['state'] == 4]  # ORDER_STATE_FILLED.
                if len(matching) != 1:
                    raise ValueError('Actual deal lacks its exact filled order in the native snapshot.')
        else:
            warnings.append('Cached broker snapshot is older than this state or belongs to another account; ignored.')
    _validate_deals(entries, state, closing=False)
    _validate_deals(exits, state, closing=True)
    volume = sum(d['volume'] for d in entries)
    if not math.isclose(volume, state['executed_volume'], abs_tol=1e-9, rel_tol=0):
        raise ValueError('Entry deal volume does not reconcile to the recorded fill.')
    exit_volume = sum(d['volume'] for d in exits)
    full_exit = math.isclose(exit_volume, volume, abs_tol=1e-9, rel_tol=0)
    if fresh:
        remaining = 0 if position is None else _number(position['volume'])
        valid_inventory = math.isclose(exit_volume + remaining, volume, abs_tol=1e-9, rel_tol=0)
        if position is not None and (position['symbol'] != state['symbol'] or position['type'] != state['request']['type']):
            valid_inventory = False
        status = ('closed' if position is None else 'partially_closed' if exits else 'open') if valid_inventory else 'unverified'
        if status == 'unverified':
            warnings.append('Actual exits and remaining inventory do not reconcile; excluded from completed results.')
    else:
        status = 'closed' if state['status'] == 'closed_verified' and full_exit else 'open_last_verified' if state['status'] == 'open_verified' and not exits else 'unverified'
        if status == 'open_last_verified':
            if monitor and monitor.get('active_position_ticket') == state['position_ticket'] and monitor.get('open_position_count') == 1:
                observation, source = monitor['heartbeat_utc'], 'saved_monitor_inventory'
            warnings.append('Open at the last saved check; current position/profit is not live-verified by this offline update.')
    if exits and min(d['time_msc'] for d in exits) < min(d['time_msc'] for d in entries):
        raise ValueError('Exit timestamp predates the actual entry.')
    entry_price = sum(d['price'] * d['volume'] for d in entries) / volume
    plan = state.get('protection') or {}
    strategy = plan.get('mode') == 'strategy_stop_first'
    risk = plan.get('planned_loss_with_reserves', plan.get('estimated_loss_at_sl'))
    risk = _number(risk) if risk is not None else None
    if risk is not None and risk <= 0:
        raise ValueError('Planned risk must be positive when provided.')
    deals = entries + exits
    costs = {key: sum(d[key] for d in deals) for key in ('commission', 'fee', 'swap')}
    gross = sum(d['profit'] for d in deals)
    booked = gross + sum(costs.values())
    net = booked if status == 'closed' else None
    if net is not None and not fresh and not math.isclose(net, state['realized_net_pnl'], abs_tol=1e-8, rel_tol=0):
        raise ValueError('Saved net P&L differs from actual deal cashflows.')
    entry_time = _stamp(min(d['time_msc'] for d in entries))
    exit_time = _stamp(max(d['time_msc'] for d in exits)) if status == 'closed' else None
    reason = ', '.join(sorted({REASONS.get(d['reason'], f"broker_reason_{d['reason']}") for d in exits if 'reason' in d}))
    if exits and not reason:
        reason = 'confirmed_executor_close' if state.get('close_result') else 'unknown'
    exit_reasons={d.get('reason') for d in exits}
    evaluation=('pending' if status!='closed' else 'rule_exit' if exit_reasons and exit_reasons.issubset({4,5})
                else 'manual_override' if exit_reasons.intersection({0,1,2}) else 'other_exit')
    if strategy and evaluation=='manual_override':
        warnings.append('User/manual exit: retained in actual cash results, excluded from SL/TP-rule performance.')
    identity = json.dumps(state['account_identity'], sort_keys=True).encode()
    trade_id = f"position-{state['position_id']}-{hashlib.sha256(identity).hexdigest()[:8]}"
    intent = plan.get('strategy_intent') or {}
    setup_id=(intent.get('adaptive_context') or {}).get('setup_id')
    if strategy and setup_id is None:
        setup_id='legacy_'+str(intent.get('trigger') or 'unknown')
    entry_setups=(plan.get('policy') or {}).get('entry_setups','reclaim_only')
    replay = replay_signal(state, directory)
    if strategy and replay['status'] != 'verified':
        warnings.append('Entry signal replay could not be verified; inspect the frozen inputs.')
    return dict(trade_id=trade_id, category='strategy' if strategy else 'execution_test',
        strategy=(('Gold M15/M5' if state['symbol']=='XAUUSDm' else 'EUR/USD H4/H1')+' '+str(setup_id)+' (multi_setup_v1)'
                  if entry_setups=='multi_setup_v1' else ('Gold M15/M5' if state['symbol']=='XAUUSDm' else 'EUR/USD H4/H1')+' trend-aligned prior20 sweep/rejection (B)'
                  if setup_id=='trend_sweep' else 'Gold M15 trend / M5 EMA20 reclaim (gold_intraday_v1)' if (plan.get('policy') or {}).get('target_mode')=='gold_intraday_v1'
                  else STRATEGY) if strategy else f"Protected execution test ({plan['risk_percent']:g}% policy)" if plan else 'Connection/execution test; arbitrary BUY',
        setup_id=setup_id,entry_setups=entry_setups,
        trigger=intent.get('trigger'), status=status, symbol=state['symbol'], side='BUY' if state['request']['type'] == 0 else 'SELL',
        volume_lots=volume, entry_time_utc=entry_time, entry_time_manila=_local(entry_time),
        exit_time_utc=exit_time, exit_time_manila=_local(exit_time),
        holding_seconds=(datetime.fromisoformat(exit_time)-datetime.fromisoformat(entry_time)).total_seconds() if exit_time else None,
        entry_price=entry_price, exit_price=sum(d['price']*d['volume'] for d in exits)/exit_volume if exit_time else None,
        initial_stop_loss=state['request'].get('sl'), initial_take_profit=state['request'].get('tp'),
        current_stop_loss=position.get('sl') if position else None, current_take_profit=position.get('tp') if position else None,
        risk_limit_percent=plan.get('risk_percent'), risk_budget_usd=plan.get('risk_budget'), planned_risk_usd=risk,
        risk_mode=(plan.get('policy') or {}).get('risk_mode','fixed'),
        risk_ceiling_percent=(plan.get('policy') or {}).get('risk_percent',plan.get('risk_percent')),
        risk_branch=(plan.get('conditional_risk_evidence') or {}).get('reason','fixed_risk_policy'),
        planned_risk_percent_used=100*risk/plan['risk_basis_amount'] if risk is not None else None,
        risk_at_fill_usd=(state.get('protection_at_fill') or {}).get('planned_loss_with_reserves', (state.get('protection_at_fill') or {}).get('estimated_loss_at_sl')),
        gross_sl_loss_usd=plan.get('estimated_loss_at_sl'), gross_tp_profit_usd=plan.get('estimated_profit_at_tp'),
        entry_spread_pips=state['quote'].get('spread_pips'), entry_slippage_points=state.get('entry_slippage_points'),
        commission_usd=costs['commission'], fee_usd=costs['fee'], booked_swap_usd=costs['swap'],
        gross_realized_pnl_usd=gross, booked_cashflows_usd=booked, realized_net_pnl_usd=net,
        unrealized_price_pnl_usd=_number(position['profit']) if position is not None else None,
        unrealized_swap_usd=_number(position['swap']) if position is not None else None,
        realized_r=net/risk if net is not None and risk is not None else None, exit_reason=reason or None,exit_evaluation=evaluation,
        last_observed_at_utc=observation, observation_source=source, position_ticket=state['position_ticket'], position_id=state['position_id'],
        entry_order_ticket=state['open_result']['order'], entry_deal_tickets=[d['ticket'] for d in entries],
        exit_deal_tickets=[d['ticket'] for d in exits], context_sha256=intent.get('context_sha256'),
        source_state_file=str(path.resolve()), source_state_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        account=dict(broker=state['account']['broker'], server=state['account']['server'], currency='USD',
                     masked_id=state['account']['account_id_masked'], account_type='DEMO'),
        policy=plan.get('policy'), signal_replay=replay, warnings=warnings)


def performance(trades: list[dict]) -> dict:
    closed = [t for t in trades if t['status'] == 'closed']
    values = [t['realized_net_pnl_usd'] for t in closed]
    wins, losses = [v for v in values if v > 0], [v for v in values if v < 0]
    loss_total = -sum(losses)
    return dict(total_recorded=len(trades), completed=len(closed), open_or_unverified=len(trades)-len(closed),
        wins=len(wins), losses=len(losses), breakevens=sum(v == 0 for v in values),
        realized_net_pnl_usd=sum(values), win_rate_percent=100*len(wins)/len(values) if values else None,
        expectancy_usd_per_completed_trade=sum(values)/len(values) if values else None,
        profit_factor=sum(wins)/loss_total if loss_total else None,
        profit_factor_status='finite' if loss_total else 'no_losses_observed' if wins else 'undefined',
        average_win_usd=sum(wins)/len(wins) if wins else None,
        average_loss_usd=sum(losses)/len(losses) if losses else None,
        average_holding_seconds=sum(t['holding_seconds'] for t in closed)/len(closed) if closed else None,
        average_realized_r=sum(t['realized_r'] for t in closed if t['realized_r'] is not None)/sum(t['realized_r'] is not None for t in closed)
                           if any(t['realized_r'] is not None for t in closed) else None)


def build_journal(directory: Path, *, snapshot: dict | None = None, now: datetime | None = None) -> dict:
    sources, warnings = load_sources(directory)
    monitor_path = directory / 'monitor/status.json'
    monitor = json.loads(monitor_path.read_text(encoding='utf-8')) if monitor_path.exists() else {}
    trades = [build_trade(path, state, directory, snapshot=snapshot, monitor=monitor) for path, state in sources]
    notes_path=directory/'trade_journal/user_notes.json'
    notes=json.loads(notes_path.read_text(encoding='utf-8')) if notes_path.exists() else {}
    for trade in trades:
        trade['user_note']=notes.get(str(trade['position_ticket']))
        if trade['user_note'] is not None and not isinstance(trade['user_note'],str):
            raise ValueError('Trade user notes must be text, separate from broker execution evidence.')
    trades.sort(key=lambda t: t['entry_time_utc'])
    strategy_trades = [t for t in trades if t['category'] == 'strategy']
    group_key=lambda t:(t['symbol'],(t.get('policy') or {}).get('target_mode','legacy'),
        (t.get('policy') or {}).get('entry_timing','clock_session'),
        (t.get('policy') or {}).get('risk_mode','fixed'),t['trigger'],t['side'])
    groups=sorted({group_key(t) for t in strategy_trades},key=str)
    return dict(version=1, generated_at_utc=(now or datetime.now(timezone.utc)).isoformat(),
        source_scope='Project execution-state files only; not the full broker account history.',
        monitor=dict(status=monitor.get('status', 'not_recorded'), deadline_utc=monitor.get('deadline_utc'), stop_reason=monitor.get('stop_reason')),
        strategy_summary=performance([t for t in trades if t['category'] == 'strategy']),
        strategy_rule_exit_summary=performance([t for t in strategy_trades if t['exit_evaluation']=='rule_exit']),
        manual_override_summary=performance([t for t in strategy_trades if t['exit_evaluation']=='manual_override']),
        strategy_groups=[dict(symbol=symbol,target_mode=mode,entry_timing=timing,risk_mode=risk_mode,trigger=trigger,side=side,
            **performance([t for t in strategy_trades if group_key(t)==(symbol,mode,timing,risk_mode,trigger,side)]))
            for symbol,mode,timing,risk_mode,trigger,side in groups],
        setup_groups=[dict(setup_id=setup,symbol=symbol,entry_setups=profile,
            actual_results=performance([t for t in strategy_trades if (t['setup_id'],t['symbol'],t['entry_setups'])==(setup,symbol,profile)]),
            rule_exit_results=performance([t for t in strategy_trades if (t['setup_id'],t['symbol'],t['entry_setups'])==(setup,symbol,profile) and t['exit_evaluation']=='rule_exit']))
            for setup,symbol,profile in sorted({(t['setup_id'],t['symbol'],t['entry_setups']) for t in strategy_trades})],
        execution_test_summary=performance([t for t in trades if t['category'] == 'execution_test']),
        strategy_assessment='Insufficient completed strategy trades to establish whether the rules have an edge.',
        warnings=warnings, trades=trades)


def _text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.replace(path)


def _link(path: Path, origin: Path) -> str:
    return quote(os.path.relpath(path, origin.parent).replace('\\', '/'), safe='/._-:')


def _display(value: Any, digits: int = 2) -> str:
    if value is None:
        return 'not available'
    return f'{value:.{digits}f}' if isinstance(value, (float, int)) else str(value).replace('|', '\\|').replace('\n', ' ')


def write_journal(root: Path, journal: dict) -> None:
    output, report = root / 'data/processed', root / 'reports/trade_journal.md'
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(journal['generated_at_utc']).astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    archive = output / 'trade_journal_history' / f'{stamp}.json'
    if archive.exists() and json.loads(archive.read_text(encoding='utf-8')) != journal:
        raise ValueError('Conflicting journal generation timestamp; historical snapshots are never overwritten.')
    if not archive.exists():
        atomic_json(archive, journal)
    atomic_json(output / 'trade_journal.json', journal)
    csv_path = output / 'trade_journal.csv'
    temporary = csv_path.with_suffix('.csv.tmp')
    with temporary.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for trade in journal['trades']:
            writer.writerow({k:json.dumps(trade[k]) if isinstance(trade[k], list) else trade[k] for k in CSV_COLUMNS})
    temporary.replace(csv_path)
    lines = ['# Project Quant trade journal', '',
        f"Generated: {_local(journal['generated_at_utc'])} (Asia/Manila). USD DEMO accounts only.", '',
        'This is a point-in-time evidence report. `open_last_verified` means open at the named saved check, not a current live quote.', '',
        f"Monitor: **{journal['monitor']['status']}**; reason: {journal['monitor']['stop_reason'] or 'not recorded'}", '',
        '## Actual strategy-entry results (including manual exits)', '',
        'Setup/version and exit reason are recorded for each trade. Manual exits do not test the original SL/TP exit rules.', '',
        f"- Recorded strategy trades: {journal['strategy_summary']['total_recorded']}; completed: {journal['strategy_summary']['completed']}; open/unverified: {journal['strategy_summary']['open_or_unverified']}.",
        f"- Realized result from completed strategy trades: **{journal['strategy_summary']['realized_net_pnl_usd']:.2f} USD**.",
        f"- Win rate (percent): {_display(journal['strategy_summary']['win_rate_percent'])}; expectancy (USD per completed trade): {_display(journal['strategy_summary']['expectancy_usd_per_completed_trade'])}.",
        f"- Profit factor: {_display(journal['strategy_summary']['profit_factor'])} ({journal['strategy_summary']['profit_factor_status']}).", '',
        '## SL/TP-rule results', '',
        f"- Completed broker SL/TP exits: {journal['strategy_rule_exit_summary']['completed']}; win rate: {_display(journal['strategy_rule_exit_summary']['win_rate_percent'])}; net P&L: {journal['strategy_rule_exit_summary']['realized_net_pnl_usd']:.2f} USD.",
        f"- Manual overrides excluded from those rule results: {journal['manual_override_summary']['completed']}; their actual net P&L remains {journal['manual_override_summary']['realized_net_pnl_usd']:.2f} USD.", '',
        '**Assessment: insufficient completed strategy trades to establish whether the rules work.** Open profit and execution tests are excluded from these metrics.', '',
        '## Trade register', '', '| Entry (Manila) | Position / details | Purpose | Side / lots | Status | Entry | SL / TP | Realized USD |',
        '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for trade in journal['trades']:
        detail = root / 'reports/trades' / f"{trade['trade_id']}.md"
        lines.append(f"| {trade['entry_time_manila']} | [{trade['position_ticket']}]({_link(detail, report)}) | {trade['category']} | {trade['side']} / {trade['volume_lots']:.2f} | {trade['status']} | {trade['entry_price']:.5f} | {_display(trade['initial_stop_loss'],5)} / {_display(trade['initial_take_profit'],5)} | {_display(trade['realized_net_pnl_usd'])} |")
        details = [f"# {trade['symbol']} {trade['side']} — position {trade['position_ticket']}", '',
            f"Purpose: **{trade['category']}**. Strategy/setup: {trade['strategy']}; setup ID: {trade.get('setup_id') or 'not applicable'}; profile: {trade.get('entry_setups','reclaim_only')}; trigger: {trade['trigger'] or 'not strategy-driven'}.", '',
            '## Execution and protection', '',
            f"- Status: **{trade['status']}**; observation: {_local(trade['last_observed_at_utc'])} from {trade['observation_source']}.",
            f"- Entry: {trade['entry_time_manila']}; {trade['volume_lots']:.2f} lots at {trade['entry_price']:.5f}.",
            f"- Exit: {trade['exit_time_manila'] or 'no verified full exit'}; price: {_display(trade['exit_price'],5)}; reason: {trade['exit_reason'] or 'none verified'}.",
            f"- Exit evaluation: **{trade['exit_evaluation']}**; manual overrides remain cash results but do not establish the SL/TP rules' win rate.",
            f"- Completed holding time: {_display(trade['holding_seconds'])} seconds.",
            f"- Initial SL / TP: {_display(trade['initial_stop_loss'],5)} / {_display(trade['initial_take_profit'],5)}.",
            f"- Current SL / TP (only if broker-sampled): {_display(trade['current_stop_loss'],5)} / {_display(trade['current_take_profit'],5)}.",
            f"- Risk ceiling: {_display(trade['risk_limit_percent'])}%; budget {_display(trade['risk_budget_usd'])} USD; initial planned risk {_display(trade['planned_risk_usd'])} USD ({_display(trade['planned_risk_percent_used'])}% of the entry risk basis).",
            f"- Risk mode: {trade['risk_mode']}; policy maximum: {_display(trade['risk_ceiling_percent'])}%; branch: {trade['risk_branch']}.",
            f"- Recomputed risk at fill: {_display(trade['risk_at_fill_usd'])} USD. Realized R uses the frozen initial planned risk, not this later recomputation.",
            f"- Gross modeled SL loss / TP profit: {_display(trade['gross_sl_loss_usd'])} / {_display(trade['gross_tp_profit_usd'])} USD; these are estimates, not guaranteed fills.",
            f"- Entry spread: {_display(trade['entry_spread_pips'])} pips; entry slippage: {_display(trade['entry_slippage_points'],4)} points (positive means adverse).", '',
            '## Result and costs', '',
            f"- Realized net P&L: **{_display(trade['realized_net_pnl_usd'])} USD**; realized R: {_display(trade['realized_r'],4)}.",
            f"- Booked deal cashflows so far: {_display(trade['booked_cashflows_usd'])} USD; gross realized price P&L: {_display(trade['gross_realized_pnl_usd'])} USD.",
            f"- Signed commissions / fees / booked swap: {_display(trade['commission_usd'])} / {_display(trade['fee_usd'])} / {_display(trade['booked_swap_usd'])} USD.",
            f"- Sampled floating price P&L / unbooked swap: {_display(trade['unrealized_price_pnl_usd'])} / {_display(trade['unrealized_swap_usd'])} USD; excluded from completed-trade metrics.", '',
            'Net result = actual deal profit + signed commissions + fees + swap. Spread and slippage are already reflected in actual fills and are not deducted again.', '',
            '## Entry context and reproducibility', '',
            f"Replay status: **{trade['signal_replay']['status']}**. Frozen context hash: `{trade['context_sha256'] or 'not applicable'}`.", '',
            '```json', json.dumps(trade['signal_replay'], indent=2, allow_nan=False), '```', '',
            'The displayed chart timeframe does not change the strategy inputs. Entry policy:', '',
            '```json', json.dumps(trade['policy'], indent=2, allow_nan=False), '```', '',
            '## Evidence', '',
            f"- Broker/account: {trade['account']['broker']} / {trade['account']['server']} / {trade['account']['masked_id']} / DEMO.",
            f"- Order: {trade['entry_order_ticket']}; entry deals: {trade['entry_deal_tickets']}; exit deals: {trade['exit_deal_tickets']}.",
            f"- [Source execution state]({_link(Path(trade['source_state_file']), detail)}); SHA256 `{trade['source_state_sha256']}`.", '',
            '## Review', '',
            f"- User note: {trade.get('user_note') or 'none recorded'}.",
            '- Rule adherence: inspect the frozen-input replay above; execution tests have no strategy signal.',
            '- Outcome: keep pending until a full exit is verified; one outcome cannot establish profitability.',
            '- Intratrade maximum favorable/adverse excursion: not collected; do not infer it from the final result.',
            '- Research questions: compare pullback versus breakout, BUY versus SELL, holding time, costs and realized R after collecting completed strategy trades.', '',
            '## Data warnings', '', *[f'- {warning}' for warning in trade['warnings']]]
        if not trade['warnings']:
            details.append('No additional data warnings in this saved report.')
        _text(detail, '\n'.join(details)+'\n')
    tests = journal['execution_test_summary']
    lines += ['', '## Setup attribution', '',
        'Versioned setup IDs distinguish reclaim, sweep, channel/compression breakout and range re-entry. Older strategy versions retain their original labels.',
        '[Current A/B opportunity and rejection records](latest_setup_comparison.md).', '',
        '| Setup / profile | Instrument | Actual completed | Actual net USD | SL/TP-rule completed | SL/TP-rule net USD |',
        '| --- | --- | ---: | ---: | ---: | ---: |']
    for group in journal.get('setup_groups',[]):
        actual,rule=group['actual_results'],group['rule_exit_results']
        lines.append(f"| {group['setup_id']} / {group['entry_setups']} | {group['symbol']} | {actual['completed']} | {actual['realized_net_pnl_usd']:.2f} | {rule['completed']} | {rule['realized_net_pnl_usd']:.2f} |")
    lines += ['', '## Strategy breakdown', '',
        '| Instrument / version | Trigger / direction | Recorded | Completed | Realized USD | Average R | Average holding seconds |',
        '| --- | --- | --- | --- | --- | --- | --- |']
    for group in journal['strategy_groups']:
        lines.append(f"| {group['symbol']} / {group['target_mode']} / {group.get('entry_timing','clock_session')} / {group.get('risk_mode','fixed')} | {group['trigger']} / {group['side']} | {group['total_recorded']} | {group['completed']} | {group['realized_net_pnl_usd']:.2f} | {_display(group['average_realized_r'],4)} | {_display(group['average_holding_seconds'])} |")
    lines += ['', '## Execution tests (excluded from strategy performance)', '',
        f"{tests['completed']} completed execution tests; combined realized result **{tests['realized_net_pnl_usd']:.2f} USD**. These tested connectivity, order submission, protection and closing, not the H4/H1 entry rules.", '',
        '## How to evaluate subsequent trades', '',
        'Compare completed strategy trades by trigger and direction using net P&L, realized R, win rate, average win/loss, expectancy and holding time. Include all recorded fills, not just winners. An early sample is descriptive, not proof of an edge; a profitability backtest and later independent demo observations are still required.', '',
        'R is net completed-trade P&L divided by the frozen pre-entry planned risk (including modeled reserves where present). A risk ceiling is a maximum, not a sizing target. Conditional minimum-lot exceptions are grouped separately from fixed-risk trades. Changes to strategy rules should be recorded as a new version, rather than rewriting earlier entries.', '',
        'No equity/tick path is stored in this journal, so account drawdown and intratrade MAE/MFE are not calculated. Open or uncertain trades, manual overrides and missing cost evidence must remain visible. Cash adjustments charged outside individual trade deals are not allocated by this report.', '',
        f"[CSV dataset]({_link(csv_path,report)}) · [Full JSON and entry replays]({_link(output/'trade_journal.json',report)})", '',
        'Every generation also preserves a timestamped JSON snapshot under `data/processed/trade_journal_history/`; updating the current register does not append duplicate trades.', '',
        '## Refresh', '',
        '```powershell', r'.\.venv\Scripts\python.exe -B scripts/update_trade_journal.py',
        '# Optional native reads; uses the existing DEMO login and may launch MT5:',
        r'.\.venv\Scripts\python.exe -B scripts/update_trade_journal.py --refresh-mt5', '```', '',
        'Default is offline: it does not start MT5. Native refresh reads deals, filled orders and positions; it does not open/close/modify orders, restart monitoring or alter execution state. Reports update when this command is run; no background journal service is installed.', '',
        '## Coverage and warnings', '', journal['source_scope'], '', *[f'- {w}' for w in journal['warnings']]]
    for trade in journal['trades']:
        lines.extend(f"- Position {trade['position_ticket']}: {warning}" for warning in trade['warnings'])
    _text(report, '\n'.join(lines)+'\n')
