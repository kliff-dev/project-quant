"""Read-only, account-wide unresolved execution inventory across DEMO modes."""
from __future__ import annotations

import json
from pathlib import Path


def legacy_unresolved(root):
    logs=Path(root)/'logs'
    paths=[logs/'demo_test_state.json', *sorted((logs/'sessions').glob('*/demo_test_state.json'))]
    return [path for path in paths if path.is_file() and _status(path)!='closed_verified']


def _status(path):
    value=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value,dict): raise ValueError(f'Invalid execution record: {path.name}')
    return value.get('status')


def unresolved_execution_files(root, *, ignore=()):
    """A current attempt may exclude its own durable reservation, never others."""
    ignored={Path(path).resolve() for path in ignore if path is not None}
    paths=legacy_unresolved(root)
    for path in sorted((Path(root)/'logs/btc_monitor/attempts').glob('*.json')):
        if path.resolve() in ignored: continue
        if _status(path) not in ('closed_verified','blocked_before_send'): paths.append(path)
    return [path for path in paths if path.resolve() not in ignored]
