import hashlib
import json
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pandas as pd

from engine.config import BacktestConfig
from engine.data_loader import demo_data
from engine.research import export_run, report_zip, run_research
from engine.storage import DataStore
from strategies.trend_following import MovingAverageCrossover


def test_export_contains_replayable_data_and_integrity_checks(tmp_path):
    store = DataStore(tmp_path / "data")
    strategy, config = MovingAverageCrossover(5, 15), BacktestConfig()
    run = run_research(demo_data(120), strategy, config, store, {"source": "synthetic", "symbol": "TEST"})
    directory = export_run(run, tmp_path / "reports")
    report = json.loads((directory / "run.json").read_text())
    assert report["config"]["costs"]["commission_bps"] == 5
    assert "next observed daily open" in report["assumptions"]["execution"]
    checksums = json.loads((directory / "checksums.json").read_text())
    for name, digest in checksums.items():
        assert hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
    restored, metadata = store.load(report["dataset_id"])
    repeated = run_research(restored, strategy, config, store, metadata)
    assert repeated.report["run_id"] == run.report["run_id"]
    pd.testing.assert_frame_equal(repeated.result.equity, run.result.equity, check_exact=True)
    with ZipFile(BytesIO(report_zip(run))) as archive:
        assert set(archive.namelist()) == set(checksums) | {"checksums.json"}
        assert json.loads(archive.read("run.json"))["metrics"] == report["metrics"]


def test_cli_demo_and_snapshot_replay(tmp_path):
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / "main.py"), "--data-dir", str(tmp_path / "data"), "--output", str(tmp_path / "reports")]
    first = subprocess.run(command + ["--demo"], cwd=root, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert "SIMULATED RESEARCH" in first.stdout
    reports = list((tmp_path / "reports").glob("*/run.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    replay = subprocess.run(command + ["--dataset", report["dataset_id"]], cwd=root, capture_output=True, text=True)
    assert replay.returncode == 0, replay.stderr
    assert len(list((tmp_path / "reports").glob("*/run.json"))) == 1


def test_cli_rejects_invalid_parameters(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "main.py"), "--short", "50", "--long", "20", "--data-dir", str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "shorter" in result.stderr
