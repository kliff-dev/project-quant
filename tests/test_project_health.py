"""The health check must remain offline and refuse unreviewed batch execution."""
import json
from pathlib import Path

import pytest

from scripts.check_project_health import check, check_cli_help, main, source_inventory


def test_inventory_compiles_without_executing_top_level_code(tmp_path):
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'main.py').write_text('raise RuntimeError("must not execute")\n')
    (tmp_path / 'scripts/batch.py').write_text('raise RuntimeError("must not execute")\n')
    rows = source_inventory(tmp_path)
    assert len(rows) == 2 and all(row['syntax_ok'] for row in rows)
    assert rows[1]['runtime_check'] == 'not_executed_batch_or_helper'


def test_health_fails_for_syntax_and_changed_reviewed_sources(tmp_path):
    (tmp_path / 'main.py').write_text('def broken(\n')
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/activation_demo_plan.json').write_text(json.dumps(
        {'source_sha256': {'main.py': '0' * 64}}))
    result = check(tmp_path)
    assert not result['passed']
    assert not result['inventory'][0]['syntax_ok']
    assert result['source_pins'][0]['mismatches'] == ['main.py']


def test_health_does_not_treat_saved_failed_monitor_as_live(tmp_path):
    (tmp_path / 'main.py').write_text('pass\n')
    folder = tmp_path / 'logs/monitor'
    folder.mkdir(parents=True)
    (folder / 'status.json').write_text(json.dumps({'status': 'failed', 'stop_reason': 'demo_unverified'}))
    result = check(tmp_path)
    assert result['passed']  # Offline code health and native connection health are different.
    assert result['saved_monitor']['status'] == 'failed'
    assert result['saved_monitor']['evidence'] == 'saved_only_not_live_broker_verification'
    assert not result['cli_checks']


def test_cli_smoke_refuses_old_batch_studies(tmp_path):
    with pytest.raises(ValueError, match='reviewed'):
        check_cli_help(tmp_path, Path('python.exe'), 'scripts/research_five_setups.py')


def test_health_command_writes_evidence_without_running_a_script(tmp_path, capsys):
    (tmp_path / 'main.py').write_text('raise RuntimeError("not executed")\n')
    evidence = tmp_path / 'evidence.json'
    assert main(['--root', str(tmp_path), '--output', str(evidence)]) == 0
    assert json.loads(evidence.read_text())['passed']
    assert json.loads(capsys.readouterr().out)['mode'] == 'offline_read_only'
