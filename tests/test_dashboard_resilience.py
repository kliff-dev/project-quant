"""Broken saved evidence must not crash pages or imply active execution."""
import json

import pandas as pd
import pytest

from dashboard.research_model import monitor_health
from dashboard.setup_catalog import history_rows
from dashboard.setup_lab import catalog, execution_history
from test_five_setup_ui import observation


@pytest.mark.parametrize('deadline', [None, '', 'not-a-date', 'NaT', [], {},
                                     '2026-11-25T14:00:00'])
def test_running_monitor_with_unverifiable_deadline_is_unknown(deadline):
    status = dict(heartbeat_utc='2026-11-25T12:00:00Z', status='running',
                  deadline_utc=deadline)
    result = monitor_health(status, now=pd.Timestamp('2026-11-25T12:00:01Z'), alive=True)
    assert result['state'] == 'UNKNOWN'
    assert 'deadline' in result['explanation'].lower()


@pytest.mark.parametrize('stamp', ['2026-11-23T08:00:00Z',
    '2026-11-23T16:00:00+08:00', '2026-11-23T08:00:00',
    '2026-11-23T08:00:00.123456789Z'])
def test_history_scalar_timestamp_keeps_utc_and_manila_semantics(stamp):
    rows = history_rows(observation(when=stamp))
    assert len(rows) == 5
    expected = pd.to_datetime(stamp, utc=True)
    assert all(row['decision_time_utc'] == expected.isoformat() for row in rows)
    assert all(row['weekday_manila'] == expected.tz_convert('Asia/Manila').day_name()
               for row in rows)


@pytest.mark.parametrize('stamp', [None, '', 'NaT', 'invalid', [], {}, True])
def test_history_rejects_malformed_timestamps_without_inventing_signals(stamp):
    assert history_rows(observation(when=stamp)) == []


@pytest.mark.parametrize('bad', [[], None, 42, {'metadata': []},
    {'metadata': None}, {'metadata': {'source': 'x', 'instrument': 'y',
                                    'timeframe': 'H1'}, 'last_open_utc': []}])
def test_catalog_skips_wrong_shapes_and_retains_good_manifest(tmp_path, bad):
    folder = tmp_path / 'data/research/setup_lab'
    folder.mkdir(parents=True)
    (folder / 'bad.json').write_text(json.dumps(bad), encoding='utf-8')
    good = dict(metadata=dict(source='Exness MT5', instrument='EURUSDm', timeframe='H1',
                              acquired_at_utc='2026-11-25T12:00:00Z'),
                last_open_utc='2026-11-25T10:00:00Z')
    (folder / 'good.json').write_text(json.dumps(good), encoding='utf-8')
    result = catalog(tmp_path)
    assert len(result) == 1 and result[0][0].name == 'good.json'


@pytest.mark.parametrize('bad', [[], None, 42, {'setup_signals': []},
    {'setup_signals': {'bad': None}}, {'setup_signals': {'bad': {'side': ['buy']}}}])
def test_legacy_history_skips_wrong_shapes_and_retains_good_event(tmp_path, bad):
    folder = tmp_path / 'logs/observations/reclaim_sweep_v1/events'
    folder.mkdir(parents=True)
    (folder / 'bad.json').write_text(json.dumps(bad), encoding='utf-8')
    good = dict(decision_time_utc='2026-11-25T12:00:00Z', symbol='EURUSDm',
                setup_signals={'trend_reclaim': {'side': 'buy'}, 'malformed': None},
                signal_fresh_at_observation=True, execution_status='BLOCKED')
    (folder / 'good.json').write_text(json.dumps(good), encoding='utf-8')
    result = execution_history(tmp_path)
    assert len(result) == 1
    assert result.iloc[0].setup_id == 'trend_reclaim'
    assert result.iloc[0].status == 'BLOCKED'
