"""Read-only, exact-symbol access to the broker's XAU/USD Gold CFD.

This is neither COMEX futures nor an exchange spot-gold feed.  Prices are USD per
troy ounce; volume is broker lots.  The broker contract must explicitly confirm
100 troy ounces per lot before data can be used by this project's Gold pilot.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from engine.mt5_connector import DemoAccountRequiredError, MT5ConnectionError, MT5Connector

GOLD_SYMBOL = "XAUUSDm"
PINNED_IDENTITY = (464192812, "Exness-MT5Trial17")
PINNED_BROKER = "Exness Technologies Ltd"


def _finite_positive(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise MT5ConnectionError(f"Gold {field} must be finite and positive.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MT5ConnectionError(f"Gold {field} must be finite and positive.") from exc
    if not math.isfinite(number) or number <= 0:
        raise MT5ConnectionError(f"Gold {field} must be finite and positive.")
    return number


def validate_gold_contract(info: Any) -> dict[str, Any]:
    """Fail closed on a different product, invalid sizing, or non-bid charts."""
    if (getattr(info, "name", None) != GOLD_SYMBOL or
            getattr(info, "currency_base", None) != "XAU" or
            getattr(info, "currency_profit", None) != "USD"):
        raise MT5ConnectionError("Exact XAUUSDm XAU/USD contract required; no substitute is allowed.")
    values = {key: _finite_positive(getattr(info, key, None), key) for key in (
        "trade_contract_size", "volume_min", "volume_max", "volume_step", "point", "trade_tick_size")}
    if values["trade_contract_size"] != 100:
        raise MT5ConnectionError("Gold pilot requires a verified 100-troy-ounce contract per lot.")
    if values["volume_min"] != .01 or values["volume_step"] != .01:
        raise MT5ConnectionError("Gold pilot requires verified 0.01 minimum lots and 0.01 volume step.")
    if values["volume_max"] < values["volume_min"]:
        raise MT5ConnectionError("Gold maximum volume is below its minimum.")
    chart_mode = getattr(info, "chart_mode", None)
    if chart_mode is not None and (type(chart_mode) is not int or chart_mode != 0):
        raise MT5ConnectionError("Gold research requires bid OHLC charts (chart_mode=0).")
    digits = getattr(info, "digits", None)
    if type(digits) is not int or not 0 <= digits <= 10:
        raise MT5ConnectionError("Gold digits are unavailable or invalid.")
    return {"symbol": GOLD_SYMBOL, "instrument_type": "broker Gold CFD (XAU/USD)",
            "price_unit": "USD per troy ounce", "contract_size_unit": "troy ounces per lot",
            "volume_unit": "broker lots", "currency_base": "XAU", "currency_profit": "USD",
            "chart_mode": chart_mode, "chart_price_basis": "bid" if chart_mode == 0 else "unverified",
            "digits": digits, **values}


class GoldConnector(MT5Connector):
    """Use the existing MT5 DEMO login; never handle passwords or send orders."""

    def verify_pinned_account(self) -> dict[str, Any]:
        account = self.verify_demo_account()
        if (self.account_identity != PINNED_IDENTITY or account["broker"] != PINNED_BROKER
                or account["currency"] != "USD"):
            raise DemoAccountRequiredError("Gold research requires the pinned USD DEMO account; account identity differs.", account=account)
        return account

    def inspect(self, symbol: str | None = None) -> dict[str, Any]:
        if symbol not in (None, GOLD_SYMBOL):
            raise MT5ConnectionError("Gold inspection accepts exact XAUUSDm only; no substitution.")
        before = self.verify_pinned_account()
        identity = self.account_identity
        symbols = self.symbols()
        selected = [s for s in symbols if getattr(s, "name", None) == GOLD_SYMBOL]
        if len(selected) != 1:
            raise MT5ConnectionError("Exact XAUUSDm is unavailable or ambiguous; no alternative was substituted.")
        if not getattr(selected[0], "visible", False) and not self._api.symbol_select(GOLD_SYMBOL, True):
            raise MT5ConnectionError("Cannot subscribe to XAUUSDm.", api_error=self._api.last_error())
        info = self._api.symbol_info(GOLD_SYMBOL)
        if info is None:
            raise MT5ConnectionError("Gold symbol_info() returned None.", api_error=self._api.last_error())
        contract = validate_gold_contract(info)
        tick = self._api.symbol_info_tick(GOLD_SYMBOL)
        if tick is None:
            raise MT5ConnectionError("No XAUUSDm tick available.", api_error=self._api.last_error())
        bid = _finite_positive(getattr(tick, "bid", None), "bid")
        ask = _finite_positive(getattr(tick, "ask", None), "ask")
        if ask < bid:
            raise MT5ConnectionError("Gold bid/ask is crossed.")
        milliseconds = getattr(tick, "time_msc", 0)
        seconds = milliseconds / 1000 if milliseconds else getattr(tick, "time", None)
        seconds = _finite_positive(seconds, "tick timestamp")
        try:
            tick_time = datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise MT5ConnectionError("Gold tick timestamp is invalid.") from exc
        now = datetime.now(timezone.utc)
        age = (now - tick_time).total_seconds()
        warnings = []
        if age > 120:
            warnings.append("Quote is older than 120 seconds; last-known price does not prove an open market.")
        if age < -5:
            warnings.append("Quote timestamp is ahead of the local clock; check synchronization.")
        if contract["chart_mode"] is None:
            warnings.append("Broker did not expose chart_mode; bid OHLC basis remains unverified.")
        if contract["point"] != .001 or contract["trade_tick_size"] != .001:
            warnings.append("Broker Gold point/tick differs from 0.001; use the verified native sizes recorded here.")
        after = self.verify_pinned_account()
        if identity != self.account_identity or before != after:
            raise DemoAccountRequiredError("Account identity or permissions changed during Gold inspection.", account=after)
        terminal = self._api.terminal_info()
        if terminal is None or not getattr(terminal, "connected", False):
            raise MT5ConnectionError("Terminal disconnected during Gold inspection.")
        return {
            "status": "verified_demo_connection", "read_only": True, "checked_at_utc": now.isoformat(),
            "package_version": self.package_version, "terminal_path": str(self.terminal_path),
            "terminal_build": getattr(terminal, "build", None), "account": after,
            "permissions": {"terminal_trade_allowed": getattr(terminal, "trade_allowed", None),
                            "external_python_trading_disabled": getattr(terminal, "tradeapi_disabled", None)},
            "symbol_count": len(symbols),
            "symbols": [{"name": s.name, "description": getattr(s, "description", ""),
                         "base_currency": getattr(s, "currency_base", ""),
                         "profit_currency": getattr(s, "currency_profit", "")} for s in symbols],
            "quote": {"symbol": GOLD_SYMBOL, "bid": bid, "ask": ask, "spread_price": ask - bid,
                      "spread_price_unit": "USD per troy ounce", "spread_pips": None,
                      "spread_points": (ask - bid) / contract["point"],
                      "timestamp_utc": tick_time.isoformat(), "age_seconds": age},
            "symbol_properties": {**contract, **{k: getattr(info, k, None) for k in (
                "filling_mode", "trade_mode", "trade_exemode", "trade_stops_level", "trade_freeze_level")}},
            "instrument": contract,
            "market_session": "Not verified. A quote is not proof the broker will accept an order.",
            "warnings": warnings, "orders_sent": 0,
        }
