"""Explicit timing-profile evidence shared by the two native planners."""
from __future__ import annotations
import hashlib
from pathlib import Path

DEFAULT = 'clock_session'
ACTIVATION = 'user_activation_v1'
ROOT = Path(__file__).resolve().parents[1]
PINS = {'config/activation_timing_spec.json': '2b9f9fd7fc6c8926f8c6296ef4c3b0f39663fcf92ebf5869d01156ff06350182', 'analytics/activation_signals.py': '017951b6cb4e52c039bf7ea6e84fa5040cb7da7926d88606b8e5798cc67c579d'}


def metadata(profile: str) -> dict:
    if profile == DEFAULT:
        return {}
    if profile != ACTIVATION or set(PINS) != {'config/activation_timing_spec.json', 'analytics/activation_signals.py'}:
        raise ValueError('Unknown or unfinalized entry timing profile.')
    for relative, expected in PINS.items():
        if hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Activation timing evidence changed: {relative}')
    return dict(entry_timing=ACTIVATION,
                entry_timing_spec_sha256=PINS['config/activation_timing_spec.json'],
                entry_timing_kernel_sha256=PINS['analytics/activation_signals.py'])


def context_profile(context: dict | None) -> str:
    context = context or {}
    profile = context.get('entry_timing', DEFAULT)
    expected = metadata(profile)
    keys = ('entry_timing', 'entry_timing_spec_sha256', 'entry_timing_kernel_sha256')
    if profile == DEFAULT:
        if any(key in context for key in keys):
            raise ValueError('Unexpected timing evidence in clock-session context.')
    elif any(context.get(key) != value for key, value in expected.items()):
        raise ValueError('Activation timing context does not match its frozen profile.')
    return profile


def validate_policy(policy, intent) -> None:
    if context_profile(intent.adaptive_context) != policy.entry_timing:
        raise ValueError('Intent timing differs from the explicitly authorized policy.')
    from engine.setup_profiles import validate_policy as validate_setups
    validate_setups(policy, intent)
