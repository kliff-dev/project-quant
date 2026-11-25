from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"


def test_dashboard_runs_offline_demo_and_displays_results(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_QUANT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    app.button[0].click().run()
    assert not app.exception
    assert not app.error
    assert len(app.metric) == 4
    assert app.metric[0].label == "Final equity"
    assert app.session_state["research_run"].report["source"]["source"] == "synthetic"
    assert len(app.get("plotly_chart")) == 3
    assert len(app.dataframe) >= 5
    app.selectbox(key="data_source").set_value("Saved local snapshot").run()
    app.button[0].click().run()
    assert not app.exception
    assert not app.error


def test_dashboard_shows_parameter_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_QUANT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.number_input[0].set_value(60)
    app.button[0].click().run()
    assert not app.exception
    assert "shorter" in app.error[0].value


def test_dashboard_upload_requires_a_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_QUANT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    app.selectbox(key="data_source").set_value("CSV upload").run()
    app.button[0].click().run()
    assert not app.exception
    assert "Choose a CSV" in app.error[0].value
