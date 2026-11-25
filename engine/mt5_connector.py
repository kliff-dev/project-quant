"""Read-only MetaTrader 5 terminal integration using its existing account login.

Python communicates with the local terminal through the vendor's native IPC
bridge. The terminal communicates with its broker. This module never logs in with
credentials and contains no order-check, order-send, or position-closing path.
"""

from __future__ import annotations

import importlib
import math
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Any

DEFAULT_TERMINAL = Path(r"C:\Program Files\MetaTrader 5\terminal64.exe")


class MT5ConnectionError(RuntimeError):
    """Terminal/quote access failed; an MT5 error is retained when available."""

    def __init__(self, message: str, *, api_error: tuple[int, str] | None = None, account: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.api_error = api_error
        self.account = account


class DemoAccountRequiredError(MT5ConnectionError):
    """Account information is absent, unknown, or explicitly not DEMO."""


def account_summary(account: Any, api: Any) -> dict[str, Any]:
    """Allowlisted diagnostics: no name, password, balance, or full account ID."""
    mode = getattr(account, "trade_mode", None)
    labels = {
        api.ACCOUNT_TRADE_MODE_DEMO: "DEMO",
        api.ACCOUNT_TRADE_MODE_CONTEST: "CONTEST",
        api.ACCOUNT_TRADE_MODE_REAL: "REAL",
    }
    # bool is a subclass of int in Python; False must never pass as DEMO (0).
    valid_mode = type(mode) is int and mode in labels
    login = str(getattr(account, "login", ""))
    return {
        "broker": getattr(account, "company", ""),
        "server": getattr(account, "server", ""),
        "currency": getattr(account, "currency", ""),
        "account_type": labels[mode] if valid_mode else "UNKNOWN",
        "trade_mode": mode if type(mode) is int else None,
        "demo_verified": valid_mode and mode == api.ACCOUNT_TRADE_MODE_DEMO,
        "account_id_masked": "****" + login[-4:] if login else "unavailable",
        "account_trade_allowed": getattr(account, "trade_allowed", None),
        "account_expert_allowed": getattr(account, "trade_expert", None),
    }


class MT5Connector:
    """One terminal connection, no credential storage, retries, or trading methods.

    Initialization may launch MT5. A demo account must already be signed in there.
    shutdown() releases the Python IPC connection; it does not close the terminal.
    api injection is solely for deterministic, account-free unit testing.
    """

    def __init__(self, terminal_path: str | Path = DEFAULT_TERMINAL, *, timeout_ms: int = 20_000, api: Any | None = None) -> None:
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= 60_000:
            raise ValueError("timeout_ms must be an integer from 1 to 60000.")
        self.terminal_path = Path(terminal_path)
        self.timeout_ms = timeout_ms
        self._api = api
        self._initialized = False
        self._account_identity: tuple[int, str] | None = None

    @property
    def package_version(self) -> str:
        return str(getattr(self._api, "__version__", "not loaded"))

    @property
    def terminal_api(self) -> Any:
        """Native API for the separate, explicitly confirmed demo executor."""
        self._require_connection()
        return self._api

    @property
    def account_identity(self) -> tuple[int, str]:
        """Return the verified identity for binding local execution state."""
        self._require_connection()
        if self._account_identity is None:
            raise DemoAccountRequiredError("Account identity is not verified.")
        return self._account_identity

    def connect(self) -> dict[str, Any]:
        """Initialize once, require a broker connection, and verify the demo enum."""
        if self._initialized:
            raise MT5ConnectionError("This connector is already initialized.")
        if self._api is None:
            if not self.terminal_path.is_file():
                raise MT5ConnectionError(f"MT5 terminal not found at {self.terminal_path}.")
            try:
                self._api = importlib.import_module("MetaTrader5")
            except ImportError as exc:
                raise MT5ConnectionError("MetaTrader5 package unavailable. Install requirements-mt5.txt with the project's Windows Python.") from exc
        try:
            initialized = self._api.initialize(str(self.terminal_path), timeout=self.timeout_ms)
            if not initialized:
                error = self._api.last_error()
                raise MT5ConnectionError(f"MT5 initialize failed: {error}. Sign in manually to a DEMO account in the terminal; do not put credentials in code.", api_error=error)
            self._initialized = True
            terminal = self._api.terminal_info()
            if terminal is None:
                raise MT5ConnectionError("terminal_info() returned no information.", api_error=self._api.last_error())
            if not getattr(terminal, "connected", False):
                raise MT5ConnectionError("The terminal is not connected to a broker. No account or quote verification is possible.")
            return self.verify_demo_account()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._api is not None:
            self._api.shutdown()
        self._initialized = False
        self._account_identity = None

    def __enter__(self) -> MT5Connector:
        self.connect()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        self.close()

    def _require_connection(self) -> None:
        if not self._initialized:
            raise MT5ConnectionError("Connect to a verified demo account first.")

    def verify_demo_account(self) -> dict[str, Any]:
        """Read account_info() afresh and fail closed on REAL, CONTEST, or unknown.

        Server/account names are descriptive only. The vendor's trade_mode enum
        is the only source of account-type verification.
        """
        self._require_connection()
        account = self._api.account_info()
        if account is None:
            raise DemoAccountRequiredError("account_info() returned None; DEMO account cannot be verified.", api_error=self._api.last_error())
        summary = account_summary(account, self._api)
        if not summary["demo_verified"]:
            raise DemoAccountRequiredError(f"Account type is {summary['account_type']}; only verified DEMO accounts are accepted.", account=summary)
        login = getattr(account, "login", None)
        if type(login) is not int or login <= 0 or not all(summary[key] for key in ("broker", "server", "currency")):
            raise DemoAccountRequiredError("Account identity is incomplete; DEMO account cannot be verified.", account=summary)
        self._account_identity = (login, summary["server"])
        return summary

    def symbols(self) -> tuple[Any, ...]:
        self.verify_demo_account()
        symbols = self._api.symbols_get()
        if symbols is None:
            raise MT5ConnectionError("symbols_get() failed.", api_error=self._api.last_error())
        if not symbols:
            raise MT5ConnectionError("The broker returned no symbols.")
        return tuple(symbols)

    def inspect(self, symbol: str | None = None) -> dict[str, Any]:
        """Read a demo account, all symbols, and one EUR/USD quote, without orders.

        Prefer exact EURUSD. A unique broker suffix is accepted only when the
        symbol's base/profit currencies confirm EUR/USD; ambiguous names fail.
        Tick age is reported. A recent quote alone does not prove an open session.
        """
        account_before = self.verify_demo_account()
        identity_before = self._account_identity
        symbols = self.symbols()
        pairs = [item for item in symbols if getattr(item, "currency_base", None) == "EUR" and getattr(item, "currency_profit", None) == "USD"]
        if symbol is not None:
            candidates = [item for item in pairs if item.name == symbol]
        else:
            exact = [item for item in pairs if item.name == "EURUSD"]
            candidates = exact or pairs
        if len(candidates) != 1:
            names = ", ".join(item.name for item in pairs) or "none"
            raise MT5ConnectionError(f"EUR/USD symbol selection is unavailable or ambiguous. Candidates: {names}. Use --symbol with an exact broker symbol.")
        selected = candidates[0]
        if not getattr(selected, "visible", False):
            if not self._api.symbol_select(selected.name, True):
                raise MT5ConnectionError(f"Could not subscribe to {selected.name} in Market Watch.", api_error=self._api.last_error())
        info = self._api.symbol_info(selected.name)
        if info is None:
            raise MT5ConnectionError("symbol_info() failed.", api_error=self._api.last_error())
        tick = self._api.symbol_info_tick(selected.name)
        if tick is None:
            raise MT5ConnectionError("No EUR/USD tick available yet. Let the terminal receive quotes, then run this check again.", api_error=self._api.last_error())
        bid, ask = float(tick.bid), float(tick.ask)
        if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask < bid:
            raise MT5ConnectionError("EUR/USD bid/ask is missing, non-finite, non-positive, or crossed.")
        milliseconds = getattr(tick, "time_msc", 0)
        seconds = milliseconds / 1000 if milliseconds else getattr(tick, "time", 0)
        if not math.isfinite(seconds) or seconds <= 0:
            raise MT5ConnectionError("EUR/USD tick has no valid timestamp.")
        tick_time = datetime.fromtimestamp(seconds, timezone.utc)
        now = datetime.now(timezone.utc)
        age = (now - tick_time).total_seconds()
        warnings = []
        if age > 120:
            warnings.append("Quote is older than 120 seconds; it is a last-known quote, not evidence of an open market.")
        if age < -5:
            warnings.append("Quote timestamp is ahead of the local clock; check clock synchronization.")
        account_after = self.verify_demo_account()
        if identity_before != self._account_identity or account_before != account_after:
            raise DemoAccountRequiredError("Account identity or permissions changed during inspection; discard the snapshot and check again.", account=account_after)
        terminal = self._api.terminal_info()
        if terminal is None or not getattr(terminal, "connected", False):
            raise MT5ConnectionError("Terminal disconnected during inspection; discard the snapshot.")
        point = float(getattr(info, "point", 0))
        return {
            "status": "verified_demo_connection", "read_only": True,
            "checked_at_utc": now.isoformat(), "package_version": self.package_version,
            "terminal_path": str(self.terminal_path), "terminal_build": getattr(terminal, "build", None),
            "account": account_after,
            "permissions": {"terminal_trade_allowed": getattr(terminal, "trade_allowed", None), "external_python_trading_disabled": getattr(terminal, "tradeapi_disabled", None)},
            "symbol_count": len(symbols),
            "symbols": [{"name": item.name, "description": getattr(item, "description", ""), "base_currency": getattr(item, "currency_base", ""), "profit_currency": getattr(item, "currency_profit", "")} for item in symbols],
            "quote": {"symbol": selected.name, "bid": bid, "ask": ask, "spread_price": ask - bid,
                      "spread_pips": (ask - bid) / .0001,
                      "spread_points": (ask - bid) / point if math.isfinite(point) and point > 0 else None,
                      "timestamp_utc": tick_time.isoformat(), "age_seconds": age},
            "symbol_properties": {key: getattr(info, key, None) for key in ("volume_min", "volume_max", "volume_step", "filling_mode", "trade_mode", "trade_exemode", "digits", "point")},
            "market_session": "Not verified. A quote is not proof the broker will accept an order.",
            "warnings": warnings,
            "orders_sent": 0,
        }
