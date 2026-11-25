"""Bounded BTC-only DEMO sessions, with durable one-attempt execution records.

The research rules are pure Python. This module alone crosses into the broker
API, and only through an explicitly started, source-verified local session. A
failure after order_send is never retried. Broker-side SL/TP remain in place
after a session deadline; this module never closes a position automatically.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Callable
import uuid

import pandas as pd

from engine.execution_audit import atomic_json
from engine.execution_state import legacy_unresolved, unresolved_execution_files
from engine.gold_connector import GoldConnector
from engine.market_monitor import monitor_lock
from engine.mt5_connector import DemoAccountRequiredError

SYMBOL = 'BTCUSDm'
MODE = 'btc_demo_series'
MAGIC = 82611224
SETUP_IDS = ('B1', 'B2', 'B3', 'B4')
RISK_PERCENT = 1.0
MAX_LOTS = .1
POLL_SECONDS = 10
MAX_QUOTE_AGE = 15
MAX_DECISION_AGE = 300
SLIPPAGE_PRICE = 2.0  # USD/BTC; 200 points when the verified point is $0.01.


class BTCBlocked(RuntimeError):
    """An entry is rejected before sending; a different future candle may qualify."""


class BTCOutcomeUncertain(RuntimeError):
    """A send may have executed; stop the session and inspect broker history."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise BTCBlocked('A timezone-aware timestamp is required.')
    return value.astimezone(timezone.utc)


def _number(value: Any, field: str, *, positive: bool = True) -> float:
    if isinstance(value, bool):
        raise BTCBlocked(f'{field} is unverified or invalid.')
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BTCBlocked(f'{field} is unverified or invalid.') from exc
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        raise BTCBlocked(f'{field} is unverified or invalid.')
    return result


def _money(value: Any, field: str) -> float:
    if value is None or isinstance(value, bool):
        raise BTCOutcomeUncertain(f'Actual broker {field} is unavailable or invalid.')
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BTCOutcomeUncertain(f'Actual broker {field} is unavailable or invalid.') from exc
    if not math.isfinite(result):
        raise BTCOutcomeUncertain(f'Actual broker {field} is unavailable or invalid.')
    return result


def _round(value: float, step: float, *, up: bool) -> float:
    native = Decimal(str(step))
    return float((Decimal(str(value)) / native).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR) * native)


def _facts(connector: Any, now: datetime, *, require_flat: bool = True,
           clock: Callable[[], datetime] | None = None) -> dict:
    api = connector.terminal_api
    summary = connector.verify_pinned_account()
    identity = tuple(connector.account_identity)
    account = api.account_info()
    if (account is None or type(getattr(account, 'trade_mode', None)) is not int
            or account.trade_mode != api.ACCOUNT_TRADE_MODE_DEMO
            or (getattr(account, 'login', None), getattr(account, 'server', None)) != identity
            or summary.get('demo_verified') is not True or summary.get('account_type') != 'DEMO'):
        raise DemoAccountRequiredError('BTC requires a verified pinned DEMO account; account changed or cannot be verified.')
    if getattr(account, 'currency', None) != 'USD':
        raise BTCBlocked('BTC pilot requires the pinned USD account; no currency conversion is assumed.')
    terminal = api.terminal_info()
    if (terminal is None or getattr(terminal, 'connected', None) is not True
            or getattr(terminal, 'trade_allowed', None) is not True
            or getattr(terminal, 'tradeapi_disabled', None) is not False
            or getattr(account, 'trade_allowed', None) is not True
            or getattr(account, 'trade_expert', None) is not True):
        raise BTCBlocked('Terminal, external-Python, or account trading permissions are disabled or unverified.')
    positions, orders = api.positions_get(), api.orders_get()
    if positions is None or orders is None:
        raise BTCBlocked('Account positions and pending orders cannot be verified.')
    if require_flat and (positions or orders):
        raise BTCBlocked('BTC uses an isolated flat account: close other positions/orders first; no Gold/EURUSD overlap.')
    info = api.symbol_info(SYMBOL)
    if info is None:
        raise BTCBlocked('Exact BTCUSDm contract is unavailable; no substitute is allowed.')
    if not getattr(info, 'visible', False):
        if not api.symbol_select(SYMBOL, True):
            raise BTCBlocked('Cannot subscribe to exact BTCUSDm.')
        info = api.symbol_info(SYMBOL)
    if (info is None or getattr(info, 'name', None) != SYMBOL
            or getattr(info, 'currency_base', None) != 'BTC'
            or getattr(info, 'currency_profit', None) != 'USD'
            or type(getattr(info, 'chart_mode', None)) is not int or info.chart_mode != 0):
        raise BTCBlocked('Exact BTC/USD broker CFD with verified bid OHLC charts is required.')
    contract = {key: _number(getattr(info, key, None), key) for key in (
        'trade_contract_size', 'volume_min', 'volume_max', 'volume_step', 'point', 'trade_tick_size')}
    for key in ('trade_stops_level', 'trade_freeze_level'):
        contract[key] = _number(getattr(info, key, None), key, positive=False)
    for key in ('digits', 'trade_mode', 'trade_exemode', 'filling_mode', 'order_mode'):
        item = getattr(info, key, None)
        if type(item) is not int or item < 0:
            raise BTCBlocked(f'BTC contract {key} is unverified.')
        contract[key] = item
    if contract['digits'] > 10:
        raise BTCBlocked('Invalid contract digits.')
    if (contract['volume_max'] < contract['volume_min']
            or Decimal(str(contract['volume_min'])) % Decimal(str(contract['volume_step'])) != 0):
        raise BTCBlocked('Broker volume minimum/maximum/step are invalid or not aligned.')
    if not contract['order_mode'] & 1 or not contract['order_mode'] & 16 or not contract['order_mode'] & 32:
        raise BTCBlocked('BTC contract must support market orders and broker-side SL and TP.')
    tick = api.symbol_info_tick(SYMBOL)
    if tick is None:
        raise BTCBlocked('BTC tick is unavailable.')
    bid, ask = (_number(getattr(tick, key, None), key) for key in ('bid', 'ask'))
    seconds = getattr(tick, 'time_msc', None)
    seconds = _number(seconds, 'quote time') / 1000 if seconds else _number(getattr(tick, 'time', None), 'quote time')
    quote_time = datetime.fromtimestamp(seconds, timezone.utc)
    age = (_utc(clock()) if clock is not None else now) - quote_time
    age = age.total_seconds()
    if ask < bid or not -2 <= age <= MAX_QUOTE_AGE:
        raise BTCBlocked('BTC quote is crossed, stale (>15 seconds), or ahead of the PC clock.')
    funds = {key: _number(getattr(account, key, None), key) for key in ('balance', 'equity', 'margin_free')}
    if tuple(connector.account_identity) != identity:
        raise DemoAccountRequiredError('Account identity changed during BTC preflight.')
    return {'account': summary, 'identity': identity, 'contract': contract,
            'bid': bid, 'ask': ask, 'quote_time_utc': quote_time.isoformat(), 'quote_age_seconds': age,
            'funds': funds, 'positions': tuple(positions), 'orders': tuple(orders)}


def _filling(api: Any, contract: dict) -> int:
    if (contract['trade_exemode'] in (getattr(api, 'SYMBOL_TRADE_EXECUTION_REQUEST', 0),
                                    getattr(api, 'SYMBOL_TRADE_EXECUTION_INSTANT', 1))
            or contract['filling_mode'] & 1):
        return api.ORDER_FILLING_FOK
    if contract['filling_mode'] & 2:
        return api.ORDER_FILLING_IOC
    raise BTCBlocked('No supported FOK/IOC filling mode; no substitution or retry.')


def _profit(api: Any, side_type: int, volume: float, entry: float, exit_price: float) -> float:
    result = api.order_calc_profit(side_type, SYMBOL, volume, entry, exit_price)
    if result is None or isinstance(result, bool) or not math.isfinite(float(result)):
        raise BTCBlocked('Broker order_calc_profit() cannot verify USD risk.')
    return float(result)


def _validate_intent(intent: dict, now: datetime) -> None:
    if intent.get('side') not in ('buy', 'sell') or intent.get('selected_setup') not in SETUP_IDS:
        raise BTCBlocked('No supported BTC setup selected.')
    decision = _utc(intent.get('decision_time_utc'))
    if not 0 <= (now - decision).total_seconds() <= MAX_DECISION_AGE:
        raise BTCBlocked('Only a newly closed H1 decision within 300 seconds can enter.')
    if decision.minute or decision.second or decision.microsecond:
        raise BTCBlocked('BTC decisions must use an exact H1 candle-close timestamp.')
    _number(intent.get('atr14'), 'ATR14')
    _number(intent.get('stop_distance'), 'strategy stop distance')
    _number(intent.get('stop_price'), 'frozen structural stop price')
    if intent.get('reward_r') != 2 or isinstance(intent.get('reward_r'), bool):
        raise BTCBlocked('Reviewed BTC pilot uses exactly 2R targets.')


def _fixed_risk(connector: Any, facts: dict, request: dict, intent: dict, slippage: float) -> dict:
    api, contract = connector.terminal_api, facts['contract']
    side = intent['side']
    order_type = api.ORDER_TYPE_BUY if side == 'buy' else api.ORDER_TYPE_SELL
    if (contract['trade_mode'] not in (getattr(api, 'SYMBOL_TRADE_MODE_FULL', 4),
            getattr(api, 'SYMBOL_TRADE_MODE_LONGONLY', 1) if side == 'buy'
            else getattr(api, 'SYMBOL_TRADE_MODE_SHORTONLY', 2))):
        raise BTCBlocked(f'BTC contract does not permit {side} entries now.')
    entry = facts['ask'] if side == 'buy' else facts['bid']
    stop, target = request['sl'], request['tp']
    distance = entry - stop if side == 'buy' else stop - entry
    reward_distance = target - entry if side == 'buy' else entry - target
    minimum = max(contract['trade_stops_level'], contract['trade_freeze_level']) * contract['point']
    executable = facts['bid'] if side == 'buy' else facts['ask']
    if ((side == 'buy' and facts['bid'] <= intent['stop_price'])
            or (side == 'sell' and facts['ask'] >= intent['stop_price'])):
        raise BTCBlocked('Fresh quote crossed the frozen structural stop; setup is invalidated.')
    if side == 'buy':
        valid = stop < executable - minimum and target > executable + minimum
    else:
        valid = stop > executable + minimum and target < executable - minimum
    if not valid or distance <= 0 or reward_distance <= 0:
        raise BTCBlocked('Broker stop/freeze distance or SL/TP direction is invalid at the fresh quote.')
    if reward_distance / distance < 1.95:
        raise BTCBlocked('Fresh quote leaves less than the reviewed target/risk tolerance.')
    spread = facts['ask'] - facts['bid']
    if spread + 2 * slippage > .1 * distance + 1e-9:
        raise BTCBlocked('Spread plus two slippage allowances exceeds 10% of stop distance.')
    volume = request['volume']
    if (not contract['volume_min'] <= volume <= min(MAX_LOTS, contract['volume_max'])
            or Decimal(str(volume)) % Decimal(str(contract['volume_step'])) != 0):
        raise BTCBlocked('Volume no longer satisfies the broker limits or 0.1-lot cap.')
    worst_entry = entry + slippage if side == 'buy' else entry - slippage
    worst_exit = stop - slippage if side == 'buy' else stop + slippage
    if min(worst_entry, worst_exit) <= 0:
        raise BTCBlocked('Invalid worst-case prices.')
    risk = -_profit(api, order_type, volume, worst_entry, worst_exit)
    if risk <= 0:
        raise BTCBlocked('Broker calculation does not show a loss at the stop.')
    budget = min(facts['funds']['balance'], facts['funds']['equity']) * RISK_PERCENT / 100
    # Five percent of the risk budget is reserved for unmodeled fees. Actual
    # broker slippage/fees can exceed estimates; a stop is not a guaranteed fill.
    reserve = budget * .05
    if risk + reserve > budget + 1e-8:
        raise BTCBlocked('Minimum/selected BTC lot exceeds the 1% risk budget including reserve.')
    margin = api.order_calc_margin(order_type, SYMBOL, volume, entry)
    margin = _number(margin, 'estimated margin', positive=False)
    if margin > facts['funds']['margin_free'] * .9:
        raise BTCBlocked('Insufficient free margin with the 10% free-margin reserve.')
    return {'risk_budget_usd': budget, 'estimated_stop_loss_usd': risk, 'fee_reserve_usd': reserve,
            'planned_risk_usd': risk + reserve, 'risk_percent': RISK_PERCENT,
            'spread_price': spread, 'slippage_allowance_price_each_side': slippage,
            'estimated_margin_usd': margin, 'fresh_entry_reference': entry,
            'target_r_at_fresh_quote': reward_distance / distance}


def prepare_plan(connector: Any, intent: dict, *, now: datetime | None = None) -> dict:
    """Read-only sizing/permissions plan. Does not call order_check/order_send."""
    current = _utc(now or utc_now())
    _validate_intent(intent, current)
    facts = _facts(connector, current)
    api, contract = connector.terminal_api, facts['contract']
    side = intent['side']
    entry = facts['ask'] if side == 'buy' else facts['bid']
    distance = _number(intent['stop_distance'], 'stop distance')
    tick = contract['trade_tick_size']
    frozen_stop = _number(intent['stop_price'], 'frozen structural stop price')
    if ((side == 'buy' and facts['bid'] <= frozen_stop)
            or (side == 'sell' and facts['ask'] >= frozen_stop)):
        raise BTCBlocked('Fresh quote already crossed the frozen structural stop; the setup is invalidated.')
    # Preserve the completed-candle structural anchor. A lower price floor is
    # measured from the actual new entry, rather than moving the structure with
    # every quote. Never resurrect an already crossed structural stop.
    floor = _number(intent['atr14'], 'ATR14') * 1.5
    stop_level = min(frozen_stop, entry - floor) if side == 'buy' else max(frozen_stop, entry + floor)
    # Round away from entry so tick normalization never tightens the stop.
    stop = _round(stop_level, tick, up=side == 'sell')
    actual_distance = abs(entry - stop)
    target = _round(entry + actual_distance * 2 if side == 'buy' else entry - actual_distance * 2,
                    tick, up=side == 'buy')
    if min(stop, target) <= 0:
        raise BTCBlocked('Calculated SL/TP is non-positive.')
    slip = max(tick, _round(SLIPPAGE_PRICE, tick, up=True))
    order_type = api.ORDER_TYPE_BUY if side == 'buy' else api.ORDER_TYPE_SELL
    minimum = contract['volume_min']
    budget = min(facts['funds']['balance'], facts['funds']['equity']) * .01
    worst_entry = entry + slip if side == 'buy' else entry - slip
    worst_exit = stop - slip if side == 'buy' else stop + slip
    minimum_loss = -_profit(api, order_type, minimum, worst_entry, worst_exit)
    if minimum_loss <= 0:
        raise BTCBlocked('Broker minimum-lot loss calculation is invalid.')
    affordable = (budget * .95 / minimum_loss) * minimum
    volume = _round(min(affordable, MAX_LOTS, contract['volume_max']), contract['volume_step'], up=False)
    if volume < minimum:
        raise BTCBlocked('Broker minimum BTC lot exceeds the 1% risk budget including reserves; skip.')
    tag = hashlib.sha256((intent['decision_time_utc'] + intent['selected_setup']).encode()).hexdigest()[:12]
    request = {'action': api.TRADE_ACTION_DEAL, 'symbol': SYMBOL, 'volume': volume,
               'type': order_type, 'sl': stop, 'tp': target,
               'deviation': int(math.ceil(slip / contract['point'])), 'magic': MAGIC,
               'comment': f'PQBTC-{intent["selected_setup"]}-{tag}', 'type_time': api.ORDER_TIME_GTC,
               'type_filling': _filling(api, contract)}
    if contract['trade_exemode'] != getattr(api, 'SYMBOL_TRADE_EXECUTION_MARKET', 2):
        request['price'] = entry
    risk = _fixed_risk(connector, facts, request, intent, slip)
    return {'created_at_utc': current.isoformat(), 'symbol': SYMBOL, 'intent': dict(intent),
            'account': facts['account'], 'account_identity': list(facts['identity']),
            'contract': contract, 'request': request, 'risk': risk,
            'quote': {k: facts[k] for k in ('bid', 'ask', 'quote_time_utc', 'quote_age_seconds')}}


def _serial(value: Any) -> dict:
    names = ('retcode', 'order', 'deal', 'volume', 'price', 'comment', 'request_id', 'retcode_external')
    return {key: getattr(value, key, None) for key in names}


def _validate_request(connector: Any, plan: dict) -> None:
    """Reject corrupted/mutated plans before a checked request can be sent."""
    api, request, contract, intent = (connector.terminal_api, plan['request'], plan['contract'], plan['intent'])
    required = {'action', 'symbol', 'volume', 'type', 'sl', 'tp', 'deviation', 'magic',
                'comment', 'type_time', 'type_filling'}
    if contract['trade_exemode'] != getattr(api, 'SYMBOL_TRADE_EXECUTION_MARKET', 2):
        required.add('price')
    if set(request) != required:
        raise BTCBlocked('BTC request fields differ from the reviewed market-order schema.')
    expected_type = api.ORDER_TYPE_BUY if intent['side'] == 'buy' else api.ORDER_TYPE_SELL
    tag = hashlib.sha256((intent['decision_time_utc'] + intent['selected_setup']).encode()).hexdigest()[:12]
    expected = {'action': api.TRADE_ACTION_DEAL, 'symbol': SYMBOL, 'type': expected_type,
                'magic': MAGIC, 'type_time': api.ORDER_TIME_GTC,
                'type_filling': _filling(api, contract),
                'comment': f'PQBTC-{intent["selected_setup"]}-{tag}'}
    if any(request.get(key) != value for key, value in expected.items()):
        raise BTCBlocked('BTC request ownership, symbol, side, action or filling mode was altered.')
    for key in ('volume', 'sl', 'tp'):
        _number(request[key], f'request {key}')
    expected_slip = max(contract['trade_tick_size'], _round(SLIPPAGE_PRICE, contract['trade_tick_size'], up=True))
    if (plan['risk']['slippage_allowance_price_each_side'] != expected_slip
            or type(request['deviation']) is not int
            or request['deviation'] != math.ceil(expected_slip / contract['point'])):
        raise BTCBlocked('BTC slippage allowance or points conversion was altered.')


def _verify_fill(connector: Any, plan: dict, result: Any, *, final_risk: dict | None = None) -> dict:
    api, request = connector.terminal_api, plan['request']
    if (type(getattr(result, 'retcode', None)) is not int or result.retcode != api.TRADE_RETCODE_DONE
            or type(getattr(result, 'order', None)) is not int or result.order <= 0
            or type(getattr(result, 'deal', None)) is not int or result.deal <= 0):
        raise BTCOutcomeUncertain('Order did not return verified full execution with valid order and deal tickets; inspect broker history, no retry.')
    connector.verify_pinned_account()
    if list(connector.account_identity) != plan['account_identity']:
        raise BTCOutcomeUncertain('Account changed after BTC send; execution cannot be verified.')
    order_rows = api.history_orders_get(ticket=result.order)
    deal_rows = api.history_deals_get(ticket=result.deal)
    positions = api.positions_get()
    pending = api.orders_get()
    if order_rows is None or deal_rows is None or positions is None or pending is None:
        raise BTCOutcomeUncertain('Broker order/deal/position history is unavailable after send.')
    orders = [o for o in order_rows if getattr(o, 'ticket', None) == result.order]
    deals = [d for d in deal_rows if getattr(d, 'ticket', None) == result.deal]
    if len(orders) != 1 or len(deals) != 1 or len(positions) != 1 or pending:
        raise BTCOutcomeUncertain('Exact one order, one entry deal and one protected position could not be verified.')
    order, deal, position = orders[0], deals[0], positions[0]
    for row in (order, deal, position):
        if (getattr(row, 'symbol', None) != SYMBOL or getattr(row, 'magic', None) != MAGIC
                or getattr(row, 'type', None) != request['type']):
            raise BTCOutcomeUncertain('Broker order/deal/position ownership or direction differs from the submitted request.')
    if (getattr(order, 'state', None) != getattr(api, 'ORDER_STATE_FILLED', 4)
            or getattr(deal, 'order', None) != result.order
            or getattr(deal, 'entry', None) != getattr(api, 'DEAL_ENTRY_IN', 0)
            or type(getattr(deal, 'position_id', None)) is not int or deal.position_id <= 0
            or getattr(position, 'identifier', None) != deal.position_id
            or type(getattr(position, 'ticket', None)) is not int or position.ticket <= 0):
        raise BTCOutcomeUncertain('Actual order/deal/position ticket linkage is not verified.')
    tolerance = plan['contract']['trade_tick_size'] * .51
    for actual in (getattr(result, 'volume', None), getattr(deal, 'volume', None), getattr(position, 'volume', None)):
        if not math.isclose(_number(actual, 'executed volume'), request['volume'], abs_tol=1e-9):
            raise BTCOutcomeUncertain('Actual volume differs; partial fills are never retried.')
    for key in ('sl', 'tp'):
        if not math.isclose(_number(getattr(position, key, None), f'position {key}'), request[key], abs_tol=tolerance):
            raise BTCOutcomeUncertain('The broker position does not have the requested SL/TP protection.')
    price = _number(getattr(deal, 'price', None), 'execution price')
    if not math.isclose(price, _number(getattr(position, 'price_open', None), 'position price'), abs_tol=tolerance):
        raise BTCOutcomeUncertain('Actual deal and position entry prices differ.')
    cost_fields = {key: _money(getattr(deal, key, None), key) for key in ('commission', 'fee', 'swap', 'profit')}
    if not all(math.isfinite(v) for v in cost_fields.values()):
        raise BTCOutcomeUncertain('Actual entry costs cannot be verified.')
    risk = final_risk or plan['risk']
    reference = risk['fresh_entry_reference']
    adverse_slippage = price - reference if plan['intent']['side'] == 'buy' else reference - price
    actual_risk = -_profit(api, request['type'], request['volume'], price, request['sl'])
    entry_cost = -min(0, sum(cost_fields[key] for key in ('commission', 'fee', 'swap')))
    return {'order_ticket': result.order, 'deal_ticket': result.deal,
            'position_ticket': position.ticket, 'position_id': deal.position_id,
            'volume': request['volume'], 'price': price, 'sl': request['sl'], 'tp': request['tp'],
            'adverse_slippage_price': adverse_slippage, 'entry_costs': cost_fields,
            'approved_entry_reference': reference,
            'approved_adverse_entry_bound': reference + risk['slippage_allowance_price_each_side'] if plan['intent']['side'] == 'buy'
                else reference - risk['slippage_allowance_price_each_side'],
            'actual_entry_to_stop_usd': actual_risk,
            'risk_estimate_exceeded': actual_risk + entry_cost > risk['risk_budget_usd'] + 1e-8,
            'slippage_allowance_exceeded': adverse_slippage > risk['slippage_allowance_price_each_side'] + tolerance}


def send_once(connector: Any, plan: dict, ledger_path: str | Path, *,
              now: Callable[[], datetime] = utc_now, verify_sources: Callable[[], Any] | None = None) -> dict:
    """One durable attempt, no retries. The caller owns the shared monitor lock."""
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive reservation persists on errors/crashes. Atomic JSON replaces this
    # owned file only; an existing reservation is never reset automatically.
    with path.open('x', encoding='utf-8') as handle:
        json.dump({'status': 'reserved', 'plan': plan}, handle, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    state = {'status': 'reserved', 'plan': plan, 'reserved_at_utc': _utc(now()).isoformat(), 'send_calls': 0}
    sent = False
    try:
        if verify_sources is not None:
            verify_sources()
        _validate_intent(plan['intent'], _utc(now()))
        _validate_request(connector, plan)
        facts = _facts(connector, _utc(now()), clock=now)
        if list(facts['identity']) != plan['account_identity'] or facts['contract'] != plan['contract']:
            raise BTCBlocked('BTC account or contract changed after planning.')
        _fixed_risk(connector, facts, plan['request'], plan['intent'], plan['risk']['slippage_allowance_price_each_side'])
        check = connector.terminal_api.order_check(dict(plan['request']))
        if check is None or type(getattr(check, 'retcode', None)) is not int or check.retcode != 0:
            raise BTCBlocked(f'BTC order_check rejected the request: {getattr(check, "retcode", None)} {getattr(check, "comment", "no response")}.')
        state['order_check'] = {'retcode': check.retcode, 'comment': str(getattr(check, 'comment', ''))}
        if verify_sources is not None:
            verify_sources()
        # The exact checked SL/TP/volume/filling request remains frozen. Recheck
        # current cash risk at fresh quotes rather than silently replacing it.
        _validate_intent(plan['intent'], _utc(now()))
        facts = _facts(connector, _utc(now()), clock=now)
        if list(facts['identity']) != plan['account_identity'] or facts['contract'] != plan['contract']:
            raise BTCBlocked('BTC account or contract changed during order_check.')
        state['final_risk'] = _fixed_risk(connector, facts, plan['request'], plan['intent'], plan['risk']['slippage_allowance_price_each_side'])
        # Native calls are synchronous and can block. Check the clock and a new
        # tick after profit/margin calculations, immediately before the durable
        # send reservation. Ordinary moves inside the already funded adverse
        # entry envelope are accepted. Quote movement and actual fill slippage
        # share that envelope; a market-order fill is never guaranteed.
        final_facts = _facts(connector, _utc(now()), clock=now)
        _validate_intent(plan['intent'], _utc(now()))
        if list(final_facts['identity']) != plan['account_identity'] or final_facts['contract'] != plan['contract']:
            raise BTCBlocked('BTC account or contract changed at the final send boundary.')
        final_budget = min(final_facts['funds']['balance'], final_facts['funds']['equity']) * .01
        if (state['final_risk']['estimated_stop_loss_usd'] + final_budget * .05 > final_budget + 1e-8
                or state['final_risk']['estimated_margin_usd'] > final_facts['funds']['margin_free'] * .9):
            raise BTCBlocked('Final BTC funds no longer cover the approved monetary risk or margin.')
        bid, ask, final_age = final_facts['bid'], final_facts['ask'], final_facts['quote_age_seconds']
        slip = plan['risk']['slippage_allowance_price_each_side']
        side, request = plan['intent']['side'], plan['request']
        reference = state['final_risk']['fresh_entry_reference']
        entry = ask if side == 'buy' else bid
        bound = reference + slip if side == 'buy' else reference - slip
        if (side == 'buy' and entry > bound) or (side == 'sell' and entry < bound):
            raise BTCBlocked('Final BTC quote moved beyond the funded adverse entry envelope.')
        distance = entry - request['sl'] if side == 'buy' else request['sl'] - entry
        reward_distance = request['tp'] - entry if side == 'buy' else entry - request['tp']
        minimum = max(facts['contract']['trade_stops_level'], facts['contract']['trade_freeze_level']) * facts['contract']['point']
        executable = bid if side == 'buy' else ask
        native_ok = ((request['sl'] < executable - minimum and request['tp'] > executable + minimum) if side == 'buy'
                     else (request['sl'] > executable + minimum and request['tp'] < executable - minimum))
        structural_ok = bid > plan['intent']['stop_price'] if side == 'buy' else ask < plan['intent']['stop_price']
        if (not native_ok or not structural_ok or distance <= 0 or reward_distance / distance < 1.95
                or ask - bid + 2 * slip > .1 * distance + 1e-9):
            raise BTCBlocked('Final BTC quote invalidates structural stop, target, or relative-cost limits.')
        state['final_quote'] = {'bid': bid, 'ask': ask, 'quote_age_seconds': final_age,
                                'approved_adverse_entry_bound': bound}
        if verify_sources is not None:
            verify_sources()  # Session wrapper also rechecks stop/deadline here.
        state['status'], state['send_calls'] = 'sending_outcome_unknown', 1
        state['sending_at_utc'] = _utc(now()).isoformat()
        atomic_json(path, state)  # Persist BEFORE crossing the order-send boundary.
        sent = True
        result = connector.terminal_api.order_send(dict(plan['request']))
        state['returned_result'] = _serial(result)
        atomic_json(path, state)
        state['fill'] = _verify_fill(connector, plan, result, final_risk=state['final_risk'])
        state['status'] = 'filled_verified'
        state['verified_at_utc'] = _utc(now()).isoformat()
        atomic_json(path, state)
        return state
    except BaseException as exc:
        state['status'] = 'outcome_uncertain' if sent else 'blocked_before_send'
        state['reason'] = f'{type(exc).__name__}: {exc}'
        atomic_json(path, state)
        if sent:
            raise BTCOutcomeUncertain(state['reason']) from exc
        raise


def _closed_trade(connector: Any, state: dict) -> dict | None:
    api = connector.terminal_api
    connector.verify_pinned_account()
    if list(connector.account_identity) != state['plan']['account_identity']:
        raise BTCOutcomeUncertain('BTC account changed while observing the protected position.')
    positions, pending = api.positions_get(), api.orders_get()
    if positions is None or pending is None:
        raise BTCOutcomeUncertain('BTC position inventory is unavailable.')
    fill, request = state['fill'], state['plan']['request']
    matching = [p for p in positions if getattr(p, 'identifier', None) == fill['position_id']]
    if len(matching) == 1 and len(positions) == 1 and not pending:
        position = matching[0]
        if (getattr(position, 'symbol', None) != SYMBOL or getattr(position, 'magic', None) != MAGIC
                or getattr(position, 'ticket', None) != fill['position_ticket']
                or getattr(position, 'type', None) != request['type']
                or not math.isclose(float(getattr(position, 'volume', 0)), fill['volume'], abs_tol=1e-9)
                or any(not math.isclose(float(getattr(position, key, 0)), request[key], abs_tol=1e-8) for key in ('sl', 'tp'))):
            raise BTCOutcomeUncertain('BTC position or its protection changed; inspect manually before another entry.')
        return None
    if matching or positions or pending:
        raise BTCOutcomeUncertain('Unexpected account exposure while observing BTC; stop new entries.')
    rows = api.history_deals_get(position=fill['position_id'])
    if rows is None or not rows:
        raise BTCOutcomeUncertain('Position vanished but the closing deal is not verified.')
    linked = [d for d in rows if getattr(d, 'position_id', None) == fill['position_id']
              and getattr(d, 'symbol', None) == SYMBOL]
    out_codes = (getattr(api, 'DEAL_ENTRY_OUT', 1), getattr(api, 'DEAL_ENTRY_OUT_BY', 3))
    outgoing = [d for d in linked if getattr(d, 'entry', None) in out_codes]
    entry_deals = [d for d in linked if getattr(d, 'ticket', None) == fill['deal_ticket']
                   and getattr(d, 'entry', None) == getattr(api, 'DEAL_ENTRY_IN', 0)]
    if len(entry_deals) != 1:
        raise BTCOutcomeUncertain('Full entry deal history is required to verify lifetime trade costs.')
    if (not outgoing or any(getattr(d, 'type', None) == request['type'] for d in outgoing)
            or not math.isclose(sum(float(d.volume) for d in outgoing), fill['volume'], abs_tol=1e-9)):
        raise BTCOutcomeUncertain('Closing deal volume does not match the verified entry volume.')
    tickets = [getattr(d, 'ticket', None) for d in linked]
    if any(type(t) is not int or t <= 0 for t in tickets) or len(set(tickets)) != len(tickets):
        raise BTCOutcomeUncertain('Closing history deal tickets are invalid or duplicated.')
    money = {key: sum(_money(getattr(d, key, None), key) for d in linked) for key in ('profit', 'commission', 'fee', 'swap')}
    if not all(math.isfinite(v) for v in money.values()):
        raise BTCOutcomeUncertain('Closing history costs are invalid.')
    exit_times = []
    for deal in outgoing:
        milliseconds = getattr(deal, 'time_msc', 0)
        seconds = milliseconds / 1000 if milliseconds else getattr(deal, 'time', None)
        try:
            stamp = datetime.fromtimestamp(_number(seconds, 'closing deal time'), timezone.utc)
        except (BTCBlocked, OverflowError, OSError, ValueError) as exc:
            raise BTCOutcomeUncertain('Actual closing deal timestamp cannot be verified.') from exc
        exit_times.append(stamp)
    return {'position_id': fill['position_id'], 'closing_deal_tickets': [d.ticket for d in outgoing],
            'all_deal_tickets': tickets, **money, 'net_pnl_usd': sum(money.values()),
            'closed_at_utc': max(exit_times).isoformat(),
            'exit_times_utc': [stamp.isoformat() for stamp in exit_times],
            'exit_prices': [float(d.price) for d in outgoing],
            'exit_reasons': [getattr(d, 'reason', None) for d in outgoing]}


def _bars(connector: Any, now: datetime) -> pd.DataFrame:
    api = connector.terminal_api
    rows = api.copy_rates_from_pos(SYMBOL, api.TIMEFRAME_H1, 0, 1401)
    if rows is None:
        raise BTCBlocked('BTC H1 history is unavailable.')
    frame = pd.DataFrame(rows)
    if len(frame) < 1201 or not {'time', 'open', 'high', 'low', 'close'} <= set(frame):
        raise BTCBlocked('At least 1,200 completed BTC H1 bars are required.')
    frame['timestamp_utc'] = pd.to_datetime(frame['time'], unit='s', utc=True)
    frame = frame.loc[frame['timestamp_utc'] + pd.Timedelta(hours=1) <= pd.Timestamp(now)].copy()
    if len(frame) < 1200 or frame['timestamp_utc'].duplicated().any() or not frame['timestamp_utc'].is_monotonic_increasing:
        raise BTCBlocked('BTC H1 history is short, duplicated, or out of order.')
    values = frame[['open', 'high', 'low', 'close']].apply(pd.to_numeric, errors='coerce')
    if (not all(math.isfinite(float(v)) and v > 0 for v in values.to_numpy().flat)
            or (values.high < values[['open', 'close', 'low']].max(axis=1)).any()
            or (values.low > values[['open', 'close', 'high']].min(axis=1)).any()):
        raise BTCBlocked('BTC OHLC history contains invalid prices.')
    frame = frame.tail(1400).set_index('timestamp_utc', drop=False)
    frame.index = pd.DatetimeIndex(frame.index, tz='UTC')
    return frame


def _event(path: Path, status: dict, event: str, current: datetime, **values: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {'timestamp_utc': current.isoformat(), 'run_id': status['run_id'],
              'activation_id': status['activation_id'], 'event': event, **values}
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, allow_nan=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def _save_input(root: Path, folder: Path, key: str, frame: pd.DataFrame) -> dict:
    """Persist exact UTF-8/LF bytes before evaluating a closed-candle input."""
    path = folder / 'inputs' / f'{key}.csv'
    path.parent.mkdir(parents=True, exist_ok=True)
    # Native history uses UTC candle-open timestamps and broker bid OHLC. Keep
    # spread/tick-volume fields for reproduction; neither is exchange volume.
    data = frame.drop(columns=['timestamp_utc'], errors='ignore').to_csv(
        index=True, index_label='timestamp_utc', lineterminator='\n', float_format='%.17g').encode('utf-8')
    with path.open('xb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return {'input_file': str(path.relative_to(root)), 'input_sha256': hashlib.sha256(data).hexdigest(),
            'input_rows': len(frame), 'input_format': 'UTF-8 CSV; exact bytes SHA256; UTC H1 candle opens',
            'input_source': 'MT5 broker BTCUSDm CFD', 'input_price_basis': 'bid'}


def run_session(root: str | Path, activation_id: str, until: datetime, *,
                setup_ids: tuple[str, ...] = SETUP_IDS,
                connector_factory: Callable[..., Any] = GoldConnector,
                now: Callable[[], datetime] = utc_now,
                sleep: Callable[[float], Any] = time.sleep,
                verify_sources: Callable[[], Any] | None = None,
                evaluate: Callable[..., dict] | None = None,
                max_iterations: int | None = None) -> dict:
    """Run an explicitly activated BTC-only session, never start one on import.

    A source verifier is mandatory for a runnable session. The app owns the
    user's activation manifest; this worker only accepts its bounded deadline.
    Offline tests inject a verifier, a fake connector and a deterministic clock.
    """
    root, start, deadline = Path(root), _utc(now()), _utc(until)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', activation_id):
        raise ValueError('Invalid BTC activation ID.')
    if not start < deadline <= start + timedelta(hours=24):
        raise ValueError('BTC deadline must be in the future and no more than 24 hours away.')
    if (not setup_ids or len(set(setup_ids)) != len(setup_ids)
            or any(s not in SETUP_IDS for s in setup_ids)):
        raise ValueError('Select one or more distinct reviewed BTC setup IDs.')
    if verify_sources is None:
        raise ValueError('A reviewed-source verifier is required for BTC DEMO activation.')
    if evaluate is None:
        from analytics.btc_setups import evaluate as evaluate_rules
        evaluate = evaluate_rules
    folder = root / 'logs/btc_monitor'
    status_path, events_path = folder / 'status.json', folder / 'events.jsonl'
    attempts = folder / 'attempts'
    run_id = uuid.uuid4().hex
    status = {'status': 'starting', 'active': False, 'mode': MODE, 'run_id': run_id,
              'activation_id': activation_id, 'pid': os.getpid(), 'symbol': SYMBOL,
              'started_at_utc': start.isoformat(), 'heartbeat_utc': start.isoformat(),
              'deadline_utc': deadline.isoformat(), 'setup_ids': list(setup_ids),
              'risk_percent': RISK_PERCENT, 'combined_risk_ceiling_percent': 2,
              'positions_limit': 1, 'poll_seconds': POLL_SECONDS, 'orders_sent': 0,
              'verified_entries': 0, 'closed_trades': 0, 'reconciled_trades': 0, 'open_positions_count': 0,
              'entry_attempts': 0, 'latest_decision': {}, 'stop_reason': None,
              'account': {'demo_verified': False, 'account_type': 'UNVERIFIED'}}
    owned_state: dict | None = None
    owned_path: Path | None = None

    def entry_authority() -> None:
        verify_sources()
        if unresolved_execution_files(root,ignore=(owned_path,)):
            raise BTCBlocked('An unrelated account execution remains unresolved; reconcile it before any new entry.')
        stop_path = folder / 'stop.json'
        stop = json.loads(stop_path.read_text(encoding='utf-8')) if stop_path.exists() else {}
        if _utc(now()) >= deadline or stop.get('run_id') == run_id:
            raise BTCBlocked('BTC session authority expired or this exact run was stopped before send.')

    # Both existing native monitor and BTC worker use this exact OS-held lock.
    # Failure to obtain it does not overwrite the active monitor's BTC status.
    with monitor_lock(root / 'logs/monitor/monitor.lock'):
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(status_path, status)
        try:
            verify_sources()
            if legacy_unresolved(root):
                raise BTCOutcomeUncertain('Earlier Gold/EURUSD execution remains unresolved; reconcile it before starting BTC.')
            unresolved = []
            known_positions = []
            for path in attempts.glob('*.json'):
                saved = json.loads(path.read_text(encoding='utf-8'))
                if saved.get('status') == 'filled_verified':
                    known_positions.append((path, saved))
                elif saved.get('status') not in ('closed_verified', 'blocked_before_send'):
                    unresolved.append(path.name)
            if unresolved:
                raise BTCOutcomeUncertain('An earlier BTC send/position remains unresolved; inspect its ledger before a new session.')
            with connector_factory(timeout_ms=10000) as connector:
                status['account'] = connector.verify_pinned_account()
                # Expiry intentionally leaves broker SL/TP intact. A subsequent
                # explicit session may reconcile a KNOWN verified entry using
                # full actual close history. Never reset an ambiguous send.
                for path, saved in known_positions:
                    closed = _closed_trade(connector, saved)
                    if closed is None:
                        status['open_positions_count'] = 1
                        raise BTCBlocked('An existing protected BTC position is still open; this new session cannot add another position.')
                    current = _utc(now())
                    saved.update(status='closed_verified', closed=closed,
                                 closed_at_utc=closed['closed_at_utc'], reconciled_at_utc=current.isoformat(),
                                 close_observed_at_utc=current.isoformat())
                    atomic_json(path, saved)
                    status['closed_trades'] += 1
                    status['reconciled_trades'] += 1
                    status['realized_net_pnl_usd'] = status.get('realized_net_pnl_usd', 0) + closed['net_pnl_usd']
                    _event(events_path, status, 'position_closed_verified', current, close=closed,
                           reconciled=True, ledger_file=str(path.relative_to(root)))
                facts = _facts(connector, _utc(now()))
                status['account'] = facts['account']
                status['status'], status['active'] = 'running', True
                _event(events_path, status, 'session_started', _utc(now()))
                iterations = 0
                while True:
                    current = _utc(now())
                    status['heartbeat_utc'] = current.isoformat()
                    verify_sources()
                    status['account'] = connector.verify_pinned_account()
                    stop_path = folder / 'stop.json'
                    stop = json.loads(stop_path.read_text(encoding='utf-8')) if stop_path.exists() else {}
                    if current >= deadline or stop.get('run_id') == run_id:
                        status['stop_reason'] = 'deadline_reached' if current >= deadline else 'user_stop_requested'
                        break
                    if owned_state is not None:
                        closed = _closed_trade(connector, owned_state)
                        if closed is not None:
                            owned_state.update(status='closed_verified', closed=closed,
                                               closed_at_utc=closed['closed_at_utc'], close_observed_at_utc=current.isoformat())
                            atomic_json(owned_path, owned_state)
                            status['closed_trades'] += 1
                            status['open_positions_count'] = 0
                            status['realized_net_pnl_usd'] = status.get('realized_net_pnl_usd', 0) + closed['net_pnl_usd']
                            _event(events_path, status, 'position_closed_verified', current, close=closed)
                            owned_state, owned_path = None, None
                    if owned_state is None:
                        frame = _bars(connector, current)
                        decision = _utc(frame['timestamp_utc'].iloc[-1].to_pydatetime()) + timedelta(hours=1)
                        # Reserve before evaluation/planning/check/send, including
                        # non-signals and rejections. Do not replay old candles.
                        key = hashlib.sha256(f'{connector.account_identity}|{SYMBOL}|{decision.isoformat()}'.encode()).hexdigest()
                        reservation = folder / 'decisions' / f'{key}.json'
                        reservation.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            with reservation.open('x', encoding='utf-8') as handle:
                                json.dump({'run_id': run_id, 'decision_time_utc': decision.isoformat(), 'status': 'reserved'}, handle)
                                handle.flush()
                                os.fsync(handle.fileno())
                        except FileExistsError:
                            pass
                        else:
                            input_record = _save_input(root, folder, key, frame)
                            atomic_json(reservation, {'run_id': run_id, 'decision_time_utc': decision.isoformat(),
                                                      'status': 'reserved', **input_record})
                            if not 0 <= (current - decision).total_seconds() <= MAX_DECISION_AGE:
                                intent = {'side': None, 'selected_setup': None, 'decision_time_utc': decision.isoformat(),
                                          'reason': 'No newly closed H1 candle within the last five minutes.'}
                            else:
                                intent = dict(evaluate(frame, as_of=current, setup_ids=setup_ids))
                                if intent.get('decision_time_utc') is None:
                                    intent['decision_time_utc'] = decision.isoformat()
                                if _utc(intent['decision_time_utc']) != decision:
                                    raise BTCBlocked('BTC evaluator decision timestamp differs from the captured closed candle.')
                            record = {'status': 'WAIT', 'decision_time_utc': decision.isoformat(), 'intent': intent, **input_record,
                                      'reason': intent.get('reason', '')}
                            if intent.get('side') in ('buy', 'sell') and intent.get('selected_setup') in setup_ids:
                                status['entry_attempts'] += 1
                                try:
                                    plan = prepare_plan(connector, intent, now=_utc(now()))
                                    plan['input'] = input_record
                                    plan['session'] = {'run_id': run_id, 'activation_id': activation_id}
                                    owned_path = attempts / f'{key}.json'
                                    owned_state = send_once(connector, plan, owned_path, now=now, verify_sources=entry_authority)
                                    status['orders_sent'] += 1
                                    status['verified_entries'] += 1
                                    status['open_positions_count'] = 1
                                    record.update(status='FILLED', fill=owned_state['fill'], reason='Actual entry deal and protected position verified.')
                                    _event(events_path, status, 'entry_verified', _utc(now()), fill=owned_state['fill'],
                                           setup_id=intent['selected_setup'], ledger_file=str(owned_path.relative_to(root)))
                                    if owned_state['fill']['risk_estimate_exceeded'] or owned_state['fill']['slippage_allowance_exceeded']:
                                        raise BTCOutcomeUncertain('Verified position exceeded the modeled risk/slippage allowance; stop further entries and inspect. Broker SL/TP remain.')
                                except BTCBlocked as exc:
                                    record.update(status='BLOCKED', reason=str(exc))
                                    owned_path, owned_state = None, None
                            elif intent.get('side') in ('buy', 'sell'):
                                record.update(reason='Selected setup is not enabled for this BTC session.')
                            atomic_json(reservation, {'run_id': run_id, **record})
                            status['latest_decision'] = record
                            _event(events_path, status, 'candle_evaluated', _utc(now()), decision=record)
                    status['heartbeat_utc'] = _utc(now()).isoformat()
                    atomic_json(status_path, status)
                    iterations += 1
                    if max_iterations is not None and iterations >= max_iterations:
                        status['stop_reason'] = 'iteration_limit'
                        break
                    sleep(min(POLL_SECONDS, max(0, (deadline - _utc(now())).total_seconds())))
                status['status'] = 'stopped'
        except Exception as exc:
            status['status'] = 'failed'
            status['stop_reason'] = f'{type(exc).__name__}: {exc}; no execution retry.'
            if isinstance(exc, BTCOutcomeUncertain):
                status['execution_outcome_uncertain'] = True
                # A result may exist even when the call returned no usable value.
                # Unknown is deliberately not displayed as zero broker exposure.
                status['open_positions_count'] = None
                status['orders_sent'] = None
            _event(events_path, status, 'session_failed', _utc(now()), reason=status['stop_reason'])
        finally:
            status['active'] = False
            status['heartbeat_utc'] = _utc(now()).isoformat()
            atomic_json(status_path, status)
            _event(events_path, status, 'session_ended', _utc(now()), reason=status['stop_reason'],
                   open_positions_count=status['open_positions_count'])
    return status
