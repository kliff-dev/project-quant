"""Durable local audit records and atomic execution-state updates."""

import csv
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_COLUMNS = ["timestamp_utc", "event", "status", "broker", "server", "account_type", "symbol", "bid", "ask", "order_ticket", "deal_ticket", "position_ticket", "volume", "price", "commission", "fee", "swap", "message"]


def atomic_text(path: Path, value: str) -> None:
    """Publish complete text; tolerate brief Windows file sharing locks.

    Each writer owns a unique temporary file. Only the filesystem rename is
    retried, for at most 385ms; no broker request is retried. A permanent error
    still propagates to the caller so trading stops with its old state intact.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        delays = (.01, .025, .05, .1, .2)
        for attempt in range(len(delays)+1):
            try:
                temporary.replace(path)
                break
            except OSError as exc:
                if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == len(delays):
                    raise
                time.sleep(delays[attempt])
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Preserve the publication error if antivirus also locks the temporary.


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    """Validate/serialize before publishing, preserving the old state on error."""
    atomic_text(path, json.dumps(value, indent=2, allow_nan=False))


def append_event(directory: Path, event: str, status: str, *, account: dict[str, Any], quote: dict[str, Any], message: str = "", **fields: Any) -> None:
    """Append and flush one CSV audit event. Never include account passwords."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "execution_log.csv"
    header = not path.exists() or path.stat().st_size == 0
    row = dict(timestamp_utc=datetime.now(timezone.utc).isoformat(), event=event, status=status,
               broker=account.get("broker", ""), server=account.get("server", ""),
               account_type=account.get("account_type", "UNVERIFIED"),
               symbol=quote.get("symbol", ""), bid=quote.get("bid", ""), ask=quote.get("ask", ""), message=message)
    row.update({key: value for key, value in fields.items() if key in LOG_COLUMNS})
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_COLUMNS)
        if header:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())
