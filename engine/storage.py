"""Immutable local CSV snapshots indexed by SQLite with SHA-256 integrity checks."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from engine.data_loader import from_csv_text, validate_ohlcv

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def canonical_csv(data: pd.DataFrame) -> str:
    """Serialize normalized bars deterministically, preserving float precision."""
    return validate_ohlcv(data).to_csv(date_format="%Y-%m-%d", float_format="%.17g", lineterminator="\n")


class DataStore:
    """Store immutable market-data snapshots; explicit refresh makes a new snapshot.

    SQLite tracks provenance and paths. SHA-256 detects changed CSV contents.
    A snapshot ID includes the content and metadata, so ticker labels cannot mix.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else PROJECT_ROOT / "data"
        self.raw_dir = self.root / "raw"
        self.processed_dir = self.root / "processed"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.processed_dir / "research.sqlite3"
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS datasets (
                id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, metadata TEXT NOT NULL,
                created_at TEXT NOT NULL, rows INTEGER NOT NULL
            )""")

    def save(self, data: pd.DataFrame, metadata: dict[str, Any]) -> str:
        """Save validated data and caller-supplied source/adjustment provenance."""
        csv = canonical_csv(data)
        content_hash = hashlib.sha256(csv.encode("utf-8")).hexdigest()
        metadata_text = json.dumps(metadata, sort_keys=True, allow_nan=False)
        dataset_id = hashlib.sha256((content_hash + metadata_text).encode("utf-8")).hexdigest()
        path = self.processed_dir / f"{dataset_id}.csv"
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != content_hash:
            raise ValueError("Existing snapshot was modified. Restore or remove the damaged snapshot explicitly.")
        if not path.exists():
            path.write_bytes(csv.encode("utf-8"))
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO datasets VALUES (?, ?, ?, ?, ?)",
                (dataset_id, content_hash, metadata_text, datetime.now(timezone.utc).isoformat(), len(data)),
            )
        return dataset_id

    def list_datasets(self) -> list[dict[str, Any]]:
        """Return provenance records, newest first, for dashboard selection."""
        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT id, sha256, metadata, created_at, rows FROM datasets ORDER BY created_at DESC, id"
            ).fetchall()
        return [dict(id=row[0], sha256=row[1], metadata=json.loads(row[2]), created_at=row[3], rows=row[4]) for row in rows]

    def load(self, dataset_id: str) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Read a known snapshot and verify its bytes before trusting the data."""
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute("SELECT sha256, metadata FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown dataset snapshot: {dataset_id}")
        # Use the registered hash as the filename, never an unchecked caller path.
        metadata = json.loads(row[1])
        safe_id = hashlib.sha256((row[0] + json.dumps(metadata, sort_keys=True, allow_nan=False)).encode("utf-8")).hexdigest()
        if safe_id != dataset_id:
            raise ValueError("Snapshot metadata integrity check failed.")
        content = (self.processed_dir / f"{safe_id}.csv").read_bytes()
        if hashlib.sha256(content).hexdigest() != row[0]:
            raise ValueError("Snapshot integrity check failed: CSV contents were changed.")
        return from_csv_text(content.decode("utf-8")), metadata
