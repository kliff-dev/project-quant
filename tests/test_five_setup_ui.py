"""Offline guide/evidence tests; rendering is not permission to trade."""
from pathlib import Path
import json
import hashlib

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from analytics.five_setups import load_spec
from dashboard.setup_catalog import (PROFILE, SETUP_IDS, SETUP_NAMES, setup_guides,
    history_rows, saved_history, condition_counts, load_research_study, research_rows,
    pooled_research_mean, research_source_status, STUDY_PATH, STUDY_KIND)
from dashboard.overview import saved_summary

ROOT = Path(__file__).resolve().parents[1]


def observation(when='2026-11-23T08:00:00+00:00', *, symbol='EURUSDm', status='BLOCKED',
                signals=('channel_breakout',), selected='channel_breakout', fresh=True):
    return dict(profile=PROFILE, symbol=symbol, decision_time_utc=when,
        signal_fresh_at_observation=fresh,
        setup_signals={key: dict(side='buy' if key in signals else None,
            reason='new_directional_condition' if key in signals else 'pattern_not_present') for key in SETUP_IDS},
        selected_setup=selected, execution_status=status,
        execution_reason='Costs exceed the permitted fraction of the stop.',
        execution_setup_id=selected,
        setup_market_context=dict(market_regime='trend_up', volatility_ratio=1.1,
            session_context='london_new_york_overlap'))


def save_event(root, value, name='event.json'):
    folder = root / 'logs/observations' / PROFILE / 'events'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


@pytest.mark.parametrize('symbol', ['EURUSDm', 'XAUUSDm'])
def test_each_market_has_five_detailed_guides_matching_the_execution_spec(symbol):
    specification = load_spec()
    guides = setup_guides(symbol)
    assert [guide['setup_id'] for guide in guides] == list(SETUP_IDS)
    for guide in guides:
        frozen = specification['instruments'][symbol]['setups'][guide['setup_id']]
        for field in ('rule', 'stop', 'target', 'market_conditions', 'limitations', 'research_origin'):
            assert guide[field] == frozen[field] and len(guide[field]) > 25
        assert guide['purpose'] and guide['failure']
    assert guides[0]['entry_timeframe'] == ('H1' if symbol == 'EURUSDm' else 'M5')
    assert guides[0]['context_timeframe'] == ('H4' if symbol == 'EURUSDm' else 'M15')


def test_catalog_rejects_missing_setup_or_wrong_profile_instead_of_making_up_rules():
    specification = load_spec()
    with pytest.raises(ValueError, match='profile'):
        setup_guides('EURUSDm', specification=dict(specification, profile='wrong'))
    del specification['instruments']['EURUSDm']['setups']['range_reentry']
    with pytest.raises(ValueError, match='exactly five'):
        setup_guides('EURUSDm', specification=specification)


def test_history_distinguishes_five_checks_signals_overlaps_and_actual_entry():
    rows = history_rows(observation(status='EXECUTED_VERIFIED', signals=('channel_breakout', 'compression_breakout')))
    assert len(rows) == 5
    keyed = {row['setup_id']: row for row in rows}
    assert keyed['channel_breakout']['outcome'] == 'Entry recorded — check trade journal'
    assert keyed['compression_breakout']['outcome'] == 'Overlapping signal — not selected'
    assert keyed['trend_reclaim']['outcome'] == 'No technical trigger'
    assert keyed['channel_breakout']['regime'] == 'trend_up'
    assert keyed['channel_breakout']['session'] == 'london_new_york_overlap'
    assert all(row['weekday_manila'] == 'Monday' for row in rows)
    stale = history_rows(observation(fresh=False))
    assert next(row for row in stale if row['technical_signal'])['outcome'] == 'Historical / not fresh'
    persistent = observation(signals=(), selected=None)
    persistent['setup_signals']['channel_breakout'].update(raw_side='buy', reason='condition_persists_no_new_trigger')
    repeated = next(row for row in history_rows(persistent) if row['setup_id'] == 'channel_breakout')
    assert repeated['outcome'] == 'Condition persists — no new trigger'
    assert not repeated['technical_signal'] and repeated['raw_side'] == 'buy'


def test_partial_unknown_or_legacy_record_does_not_invent_an_entry():
    assert history_rows({}) == []
    assert history_rows(dict(observation(), profile='reclaim_sweep_v1')) == []
    assert history_rows(dict(observation(), symbol='BTCUSDm')) == []
    assert history_rows(dict(observation(), decision_time_utc='invalid')) == []
    assert history_rows(dict(observation(), setup_signals=['bad'])) == []


def test_saved_history_deduplicates_and_conditions_include_observation_denominator(tmp_path):
    event = observation()
    save_event(tmp_path, event, 'one.json')
    save_event(tmp_path, event, 'duplicate.json')
    save_event(tmp_path, observation('2026-11-23T09:00Z', signals=(), selected=None), 'two.json')
    save_event(tmp_path, observation('2026-11-23T10:00Z', fresh=False), 'old.json')
    save_event(tmp_path, {}, 'empty.json').write_text('{partial', encoding='utf-8')
    legacy = tmp_path / 'logs/observations/reclaim_sweep_v1/events/legacy.json'
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps(event), encoding='utf-8')
    data = saved_history(tmp_path)
    assert len(data) == 15
    summary = condition_counts(data)
    channel = summary.loc[summary.setup_id.eq('channel_breakout')].iloc[0]
    assert channel.evaluated_candles == 2
    assert channel.technical_triggers == 1
    assert channel.trigger_rate_percent == 50
    assert set(summary.columns).isdisjoint({'win_rate', 'profit', 'expected_profit'})
    assert len(saved_summary(tmp_path)['decisions']) == 2  # stale signal remains labeled stale
    assert saved_summary(tmp_path)['decision_profile'] == 'five-setup'
    with pytest.raises(ValueError, match='Unsupported'):
        condition_counts(data, 'win_rate')


@pytest.mark.parametrize('symbol', ['EURUSDm', 'XAUUSDm'])
def test_app_renders_five_readable_guides_and_separate_research_without_activation(tmp_path, monkeypatch, symbol):
    monkeypatch.setenv('PROJECT_QUANT_ROOT', str(tmp_path))
    from dashboard import monitor_control, btc_control
    def forbid(*args, **kwargs):
        raise AssertionError('Reading guides must never activate a monitor')
    monkeypatch.setattr(monitor_control, 'request_start', forbid)
    monkeypatch.setattr(btc_control, 'request_start', forbid)
    save_event(tmp_path, observation(symbol=symbol))
    app = AppTest.from_file(str(ROOT / 'dashboard/research_app.py'), default_timeout=45).run()
    app.radio(key='workspace').set_value('Seven-setup research lab').run()
    if symbol != 'EURUSDm':
        app.selectbox(key='five_market').set_value(symbol).run()
    assert not app.exception and not app.error
    labels = [expander.label for expander in app.expander]
    for number, guide in enumerate(setup_guides(symbol), 1):
        assert f"{number}. {guide['name']} · {guide['setup_id']}" in labels
        assert any(guide['rule'] in item.value for item in app.markdown)
    assert any('Research only' in item.value and 'do not place orders' in item.value for item in app.info)
    assert any('not a win rate' in item.value for item in app.markdown)
    assert any('outcome' in table.value and 'Technical trigger — execution blocked' in table.value.outcome.tolist()
               for table in app.dataframe)
    assert not (tmp_path / 'logs/monitor/app_request.json').exists()


def modeled_study():
    """Synthetic aggregate model fixture, deliberately unlike trigger frequency."""
    setups = {}
    phase_rows = {}
    for key in SETUP_IDS:
        empty = key == 'compression_breakout'
        metrics = dict(technical_opportunities=1000, modeled_admissions=0 if empty else 2,
            closed_episodes=0 if empty else 2, wins=0, win_rate=None if empty else 0.,
            mean_net_r=None if empty else -.5)
        setups[key] = dict(overall=metrics)
        phase_rows[key] = {name: metrics for name in ('earlier_development', 'later_descriptive')}
    scenario = dict(overall=dict(closed_episodes=8, mean_net_r=-.5),
        by_setup=setups, by_setup_phase=phase_rows)
    return dict(profile=PROFILE, kind=STUDY_KIND, generated_at_utc='2026-11-23T10:00Z',
        period=dict(start='2026-05-19T00:00Z', split='2026-09-21T00:00Z', end='2026-11-14T00:00Z'),
        markets={'XAUUSDm': dict(scenarios={'500_slip1': scenario}),
            'EURUSDm': dict(scenarios={'500_slip1': dict(overall=dict(closed_episodes=2, mean_net_r=.2))})})


def save_study(root, value):
    path = root / STUDY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def test_saved_research_keeps_admissions_outcomes_and_missing_results_separate(tmp_path):
    assert load_research_study(tmp_path) is None
    save_study(tmp_path, modeled_study())
    item = load_research_study(tmp_path)
    rows = research_rows(item, 'XAUUSDm')
    assert len(rows) == 5
    assert rows.technical_opportunities.sum() == 5000
    assert rows.modeled_admissions.sum() == 8
    assert rows.closed_episodes.sum() == 8
    assert (rows.mean_net_r.dropna() == -.5).all()
    empty = rows.loc[rows.setup_id.eq('compression_breakout')].iloc[0]
    assert pd.isna(empty.mean_net_r) and pd.isna(empty.win_rate_percent)
    assert len(research_rows(item, 'XAUUSDm', phases=True)) == 10
    mean, count = pooled_research_mean(item)
    assert mean == pytest.approx(-.36) and count == 10
    save_study(tmp_path, dict(modeled_study(), kind='native_broker_fills'))
    with pytest.raises(ValueError, match='independent-episode'):
        load_research_study(tmp_path)


def test_research_revision_mismatch_is_distinguished_from_current_source_evidence():
    item = modeled_study()
    assert research_source_status(item)[0] == 'unverifiable'
    item['sources_sha256'] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ('analytics/five_setups.py', 'config/five_setup_spec.json')}
    assert research_source_status(item) == ('matches', [])
    item['sources_sha256']['config/five_setup_spec.json'] = '0' * 64
    assert research_source_status(item) == ('mismatch', ['config/five_setup_spec.json'])


def test_app_displays_negative_modeled_evidence_without_inferring_profit_from_counts(tmp_path, monkeypatch):
    monkeypatch.setenv('PROJECT_QUANT_ROOT', str(tmp_path))
    save_study(tmp_path, modeled_study())
    app = AppTest.from_file(str(ROOT / 'dashboard/research_app.py'), default_timeout=45).run()
    app.radio(key='workspace').set_value('Seven-setup research lab').run()
    app.selectbox(key='five_market').set_value('XAUUSDm').run()
    assert not app.exception and not app.error
    assert any('Negative historical aggregate: -0.360 R' in item.value for item in app.warning)
    modeled = [table.value for table in app.dataframe if 'modeled_admissions' in table.value]
    assert any(len(table) == 5 and table.closed_episodes.sum() == 8 for table in modeled)
    assert any('not a four-position portfolio backtest' in item.value for item in app.markdown)
    assert any('not an untouched holdout' in item.value for item in app.markdown)
    assert any('Neither is a native broker fill' in item.value for item in app.caption)
    changed = modeled_study()
    changed['sources_sha256'] = {'config/five_setup_spec.json': '0' * 64}
    save_study(tmp_path, changed)
    app.run()
    assert any('different setup kernel or specification revision' in item.value for item in app.warning)
    # A study containing technical counts alone cannot fabricate a win rate.
    save_study(tmp_path, dict(modeled_study(), markets={'XAUUSDm': {'technical_counts': {'trend_reclaim': 9999}}}))
    app.run()
    assert not app.exception and not app.error
    assert any('No win rate or profit estimate is inferred from trigger counts' in item.value for item in app.info)
    assert not any('mean_net_r' in table.value for table in app.dataframe)
