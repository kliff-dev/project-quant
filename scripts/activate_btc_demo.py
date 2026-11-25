"""Explicit BTC DEMO entry point and reviewed source verification."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

PLAN=Path('config/btc_demo_plan.json')


def verify_sources(root):
    root=Path(root).resolve()
    from engine.activation_demo import verify_sources as verify_existing
    verify_existing(root)
    plan=json.loads((root/PLAN).read_text(encoding='utf-8'))
    expected=dict(version=1,demo_only=True,symbol='BTCUSDm',mode='btc_demo_series',risk_percent=1,
        maximum_open_positions=1,maximum_lots=.1,maximum_session_hours=24,reward_r=2,
        slippage_price=2,fee_reserve_fraction=.05,account_mode='exclusive_btc',setup_ids=['B1','B2','B3','B4'])
    if any(type(plan.get(key)) is not type(value) or plan.get(key)!=value for key,value in expected.items()):
        raise ValueError('BTC plan differs from the reviewed DEMO scope.')
    required={'engine/btc_demo.py','engine/execution_state.py','analytics/btc_setups.py','analytics/candidate_signals.py',
        'scripts/activate_btc_demo.py','scripts/app_btc_session.py','dashboard/btc_control.py'}
    pins=plan.get('source_sha256',{})
    if not required.issubset(pins): raise ValueError('BTC source review is not finalized.')
    for name,expected_hash in pins.items():
        path=(root/name).resolve()
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected_hash:
            raise ValueError(f'Reviewed BTC source changed: {name}')
    return plan


def start(root,activation_id,*,until,setup_ids=('B1','B2','B3','B4')):
    root=Path(root).resolve();verify_sources(root)
    from engine.btc_demo import run_session
    deadline=datetime.fromisoformat(until) if isinstance(until,str) else until
    return run_session(root,activation_id,deadline,setup_ids=tuple(setup_ids),verify_sources=lambda:verify_sources(root))
