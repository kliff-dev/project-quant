"""Forward observations and installed explanations reproduce the native profile."""
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from analytics.five_setups import SETUPS
from dashboard.research_model import saved_decision
from engine.setup_observer import SetupObserver
from engine.setup_profiles import MULTI, metadata
from test_five_setups import fixture_frame


@pytest.mark.parametrize('symbol', ['EURUSDm', 'XAUUSDm'])
@pytest.mark.parametrize('setup_id', SETUPS)
def test_five_observer_records_all_families_and_actual_decision_once(tmp_path, symbol, setup_id):
    frame = fixture_frame(symbol, setup_id)
    interval = frame.index[-1] - frame.index[-2]
    prefix = frame.iloc[:1].copy()
    prefix.index -= interval
    raw = pd.concat([prefix, frame]).to_csv(float_format='%.17g', date_format='%Y-%m-%dT%H:%M:%SZ', lineterminator='\n').encode()
    path = tmp_path / 'logs/input.csv'
    path.parent.mkdir()
    path.write_bytes(raw.replace(b'\n', b'\r\n'))
    candle = (frame.index[-1] + interval).isoformat()
    collected = dict(status='observed', input_file='logs/input.csv', input_sha256=hashlib.sha256(raw).hexdigest(),
        quote_at_observation={'bid':float(frame.close.iloc[-1]), 'ask':float(frame.close.iloc[-1])+.00008},
        funds={'balance':500, 'equity':500}, signal_fresh_at_observation=True)
    watch = SetupObserver(tmp_path, SimpleNamespace(), profile=MULTI)
    watch.collector = SimpleNamespace(observe=Mock(return_value=collected))
    execution = dict(decision_time_utc=candle, status='blocked', reason='Offline affordability example', setup_id=setup_id)
    event = watch.observe_decision(symbol, candle, execution_decision=execution)
    assert event['selected_setup'] == setup_id
    assert set(event['setup_signals']) == set(SETUPS)
    assert event['execution_status'] == 'blocked'
    assert event['execution_setup_id'] == setup_id
    assert event['source_evidence'] == metadata(MULTI)
    assert event['setup_market_context']['weekday'] == 'Friday'
    assert event['execution_authority'] is False
    assert event['input_storage_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert watch.observe(symbol, candle) == event
    restarted = SetupObserver(tmp_path, SimpleNamespace(), profile=MULTI)
    assert restarted.observe(symbol, candle) == event
    assert watch.collector.observe.call_count == 1
    assert len(list(watch.folder.glob('events/*.json'))) == 1
    assert watch.states[symbol]['technical_counts'][setup_id] == 1
    assert setup_id in (tmp_path/'reports/latest_setup_comparison.md').read_text()


@pytest.mark.parametrize('symbol', ['EURUSDm', 'XAUUSDm'])
def test_saved_explanation_checks_five_source_identity_and_reproduces_pattern(tmp_path, symbol):
    frame = fixture_frame(symbol, 'channel_breakout')
    path = tmp_path/'logs/input.csv'
    path.parent.mkdir()
    frame.to_csv(path, float_format='%.17g', date_format='%Y-%m-%dT%H:%M:%SZ', lineterminator='\n')
    decision = dict(entry_timing='user_activation_v1', **metadata(MULTI), input_file='logs/input.csv',
        context_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        decision_time_utc=(frame.index[-1]+(frame.index[-1]-frame.index[-2])).isoformat())
    explanation = saved_decision(tmp_path, decision, symbol)
    assert explanation['selected_setup'] == 'channel_breakout'
    assert len(explanation['setup_signals']) == 5
    assert len(explanation['frame']) == len(frame)-1
    assert explanation['gates'][-1]['BUY'] == 'Not evaluated'
    changed = dict(decision, setup_source_sha256=dict(decision['setup_source_sha256']))
    changed['setup_source_sha256']['analytics/five_setups.py'] = '0'*64
    with pytest.raises(ValueError, match='App and monitor'):
        saved_decision(tmp_path, changed, symbol)
