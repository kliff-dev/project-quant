"""Rebuilding a distribution must not retain files outside its allowlist."""
import hashlib
import json

from scripts import build_desktop


def test_rebuild_discards_stale_payload_without_touching_workspace_logs(tmp_path, monkeypatch):
    required = ['scripts/__init__.py', 'scripts/research_holding_period.py',
        'config/activation_eurusd_policy.json', 'config/activation_gold_policy.json',
        'config/activation_timing_spec.json', 'config/conditional_risk_spec.json',
        'config/five_setup_spec.json']
    for name in required:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}' if path.suffix == '.json' else '# fixture\n')
    for folder, name, key in [
        ('gold_intraday_2026-11-15', 'XAUUSDm_M5.csv', 'csv_sha256'),
        ('mt5_six_month_2026-11-14_025036', 'EURUSDm_H1.csv', 'sha256')]:
        source = tmp_path / 'data/raw' / folder
        source.mkdir(parents=True)
        raw = b'timestamp_utc,open,high,low,close\n2026-11-25T10:00:00Z,1,2,1,2\n'
        (source / name).write_bytes(raw)
        (source / 'manifest.json').write_text(json.dumps({key: hashlib.sha256(raw).hexdigest()}))
    stale = tmp_path / 'build/desktop-stage/seed_workspace/logs/private-placeholder.txt'
    stale.parent.mkdir(parents=True)
    stale.write_text('Must never be redistributed')
    workspace_log = tmp_path / 'logs/keep.txt'
    workspace_log.parent.mkdir()
    workspace_log.write_text('Keep original workspace evidence')
    monkeypatch.setattr(build_desktop, 'ROOT', tmp_path)
    stage = build_desktop.stage_files()
    assert stage == tmp_path / 'build/desktop-stage'
    assert not stale.exists()
    assert not (stage / 'seed_workspace/logs').exists()
    assert (stage / 'source/config/five_setup_spec.json').is_file()
    assert workspace_log.read_text() == 'Keep original workspace evidence'
