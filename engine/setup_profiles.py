"""Versioned setup evidence; extending entry patterns never changes risk scope."""
from __future__ import annotations
import hashlib
from pathlib import Path

DEFAULT = 'reclaim_only'
PROFILE = 'reclaim_sweep_v1'
MULTI = 'multi_setup_v1'
MULTI_SETUPS = ('trend_reclaim','trend_sweep','channel_breakout','compression_breakout','range_reentry')
ROOT = Path(__file__).resolve().parents[1]
# Finalized from the fixed specification and kernel before native admission.
PINS = {'config/two_setup_spec.json': 'f03edb4b371451a330680eeea0619dd1c9ea921ca44e3ab76187100b60e4ed49', 'analytics/two_setups.py': '957195a34e57d4ad84c07705712747e87f89396294a5bdec713f1f374084bd18'}
MULTI_PINS = {'config/five_setup_spec.json':'a8b45f148feb8adf1fbd239c46a409d3e1e578812ae3e48993da651658e6c707',
              'analytics/five_setups.py':'6e1eaa3502af881246d92a915d7fcd7e85cfb99eb5f22667294e2ee94ce004ab'}


def metadata(profile: str) -> dict:
    if profile == DEFAULT:
        return {}
    pins = PINS if profile==PROFILE else MULTI_PINS if profile==MULTI else {}
    required = {'config/two_setup_spec.json','analytics/two_setups.py'} if profile==PROFILE else {'config/five_setup_spec.json','analytics/five_setups.py'}
    if not pins or set(pins) != required:
        raise ValueError('Unknown or unfinalized setup profile.')
    for path, expected in pins.items():
        if hashlib.sha256((ROOT/path).read_bytes()).hexdigest() != expected:
            raise ValueError(f'Setup evidence changed: {path}')
    return dict(entry_setups=profile, setup_source_sha256=dict(pins))


def context_profile(context: dict | None) -> str:
    context = context or {}
    profile = context.get('entry_setups', DEFAULT)
    evidence = metadata(profile)
    if profile == DEFAULT:
        if any(key in context for key in ('entry_setups', 'setup_source_sha256', 'setup_id')):
            raise ValueError('Unexpected setup evidence in legacy reclaim context.')
    elif any(context.get(key) != value for key, value in evidence.items()) or context.get('setup_id') not in (MULTI_SETUPS if profile==MULTI else ('trend_reclaim', 'trend_sweep')):
        raise ValueError('Intent setup evidence does not match its fixed profile.')
    return profile


def features(window, symbol: str, profile: str) -> tuple[dict, dict]:
    metadata(profile)
    if profile == MULTI:
        from analytics.five_setups import evaluate_setups
        comparison=evaluate_setups(window,symbol)
        return comparison['selected'], {key:comparison[key] for key in ('selected_setup','selection_reason','setup_signals')} | {
            'entry_setups':MULTI,'setup_market_context':comparison.get('market_context',{})}
    if profile == PROFILE:
        from analytics.two_setups import evaluate_setups
        comparison = evaluate_setups(window, symbol)
        return comparison['selected'], {key: comparison[key] for key in
            ('selected_setup', 'selection_reason', 'setup_signals')}
    from analytics.activation_signals import gold_features, eur_features
    return (gold_features if symbol == 'XAUUSDm' else eur_features)(window), {}


def trigger(context: dict | None) -> str:
    profile = context_profile(context)
    if profile==MULTI:
        return {'trend_reclaim':'pullback','trend_sweep':'sweep','channel_breakout':'breakout',
                'compression_breakout':'breakout','range_reentry':'range'}[context['setup_id']]
    return 'sweep' if profile == PROFILE and context['setup_id'] == 'trend_sweep' else 'pullback'


def validate_policy(policy, intent) -> None:
    if context_profile(intent.adaptive_context) != policy.entry_setups:
        raise ValueError('Intent setups differ from the explicitly authorized policy.')
    if policy.entry_setups in (PROFILE,MULTI) and intent.trigger != trigger(intent.adaptive_context):
        raise ValueError('Intent trigger does not match its recorded setup.')
