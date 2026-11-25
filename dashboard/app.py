"""Run from the project root: python -m streamlit run dashboard/app.py."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

# Streamlit launches a script in dashboard/, while modules live at the repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from engine.config import BacktestConfig, TransactionCosts
from engine.data_loader import DataDownloadError, demo_data, download_yahoo, load_csv
from engine.research import ResearchRun, report_json, report_zip, run_research
from engine.storage import DataStore
from strategies.trend_following import MovingAverageCrossover


def render_results(run: ResearchRun) -> None:
    result = run.result
    metrics = result.metrics
    report = run.report
    source = report["source"]
    st.subheader("Last completed simulation")
    st.caption(f"{source.get('symbol', 'Dataset')} · {report['first_bar'][:10]} to {report['last_bar'][:10]} · SMA {report['strategy']['short_window']}/{report['strategy']['long_window']} · {report['bars']:,} bars")
    if source.get("source") == "synthetic":
        st.warning("SYNTHETIC DEMO: generated prices test the application. Returns provide no evidence of market profitability.")
    columns = st.columns(4)
    columns[0].metric("Final equity", f"{metrics['final_equity']:,.2f}")
    columns[1].metric("Net return", f"{metrics['return_pct']:.2f}%")
    columns[2].metric("Maximum drawdown", f"{metrics['max_drawdown_pct']:.2f}%")
    columns[3].metric("Closed trades", str(metrics["number_of_trades"]))

    chart_tab, metrics_tab, ledger_tab, data_tab, assumptions_tab = st.tabs(["Charts", "All metrics", "Trades & positions", "Data", "Assumptions & export"])
    with chart_tab:
        equity = go.Figure(go.Scatter(x=result.equity.index, y=result.equity["equity"], name="Simulated equity"))
        equity.add_hline(y=result.config.initial_capital, line_dash="dot", annotation_text="Initial capital")
        equity.update_layout(title="Equity at each daily close", xaxis_title="Date", yaxis_title="Account currency units", height=350)
        st.plotly_chart(equity, width="stretch")
        drawdown = go.Figure(go.Scatter(x=result.equity.index, y=result.equity["drawdown"] * 100, fill="tozeroy", name="Drawdown", line_color="#d26a55"))
        drawdown.update_layout(title="Drawdown from running equity peak", xaxis_title="Date", yaxis_title="Drawdown (%)", height=260)
        st.plotly_chart(drawdown, width="stretch")
        strategy = MovingAverageCrossover(int(report["strategy"]["short_window"]), int(report["strategy"]["long_window"]))
        indicators = strategy.indicators(run.data)
        price = go.Figure(go.Scatter(x=run.data.index, y=run.data["close"], name="Close"))
        for column in indicators:
            price.add_trace(go.Scatter(x=indicators.index, y=indicators[column], name=column.replace("_", " ").title()))
        for side, symbol, color in [("buy", "triangle-up", "#389577"), ("sell", "triangle-down", "#d26a55")]:
            selected = result.fills[result.fills["side"] == side]
            price.add_trace(go.Scatter(x=selected["timestamp"], y=selected["price"], mode="markers", name=f"{side.title()} fills", marker=dict(symbol=symbol, size=10, color=color)))
        price.update_layout(title="Close, trailing averages, and simulated next-open fills", height=370, xaxis_title="Date", yaxis_title="Price units")
        st.plotly_chart(price, width="stretch")
    with metrics_tab:
        rows = []
        for name, value in metrics.items():
            if name == "profit_factor" and value is None:
                display = str(metrics["profit_factor_status"])
            elif value is None:
                display = "Undefined"
            elif isinstance(value, float):
                display = f"{value:,.4f}"
            else:
                display = str(value)
            rows.append({"Metric": name.replace("_", " ").capitalize(), "Value": display})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.caption("Money values use the input price currency; no FX conversion. Percentages use 0–100 units. Average loss and largest loss are signed negative values.")
    with ledger_tab:
        st.markdown("**Open position at final close**")
        if result.open_position:
            st.dataframe(pd.DataFrame([result.open_position]), hide_index=True, width="stretch")
            st.info("Final equity includes this marked position. Completed-trade statistics exclude it. Future exit costs are not reserved.")
        else:
            st.caption("No open position.")
        st.markdown("**Completed simulated trades**")
        st.dataframe(result.trades, hide_index=True, width="stretch")
        st.markdown("**Every executed simulated fill**")
        st.dataframe(result.fills, hide_index=True, width="stretch")
        st.markdown("**Order decisions, including rejected and unfilled signals**")
        st.dataframe(result.orders, hide_index=True, width="stretch")
    with data_tab:
        st.json(source)
        st.caption(f"Immutable snapshot: {report['dataset_id']}")
        st.dataframe(run.data, width="stretch")
        st.dataframe(result.equity, width="stretch")
    with assumptions_tab:
        st.json(report["config"])
        for name, explanation in report["assumptions"].items():
            st.markdown(f"**{name.replace('_', ' ').capitalize()}:** {explanation}")
        st.caption(f"Run: {report['run_id']} · Created UTC: {report['created_at_utc']}")
        st.download_button("Download complete research bundle", report_zip(run), file_name=f"project-quant-{report['run_id']}.zip", mime="application/zip")
        st.download_button("Download assumptions and metrics JSON", report_json(report), file_name="run.json", mime="application/json")


def main() -> None:
    st.set_page_config(page_title="Project Quant | Research", page_icon="📈", layout="wide")
    st.title("Project Quant")
    st.caption("Local systematic trading research · first milestone")
    st.info("SIMULATED HISTORICAL RESULTS ONLY. No live trading, broker connection, or real-money account access.")
    try:
        store = DataStore(os.environ.get("PROJECT_QUANT_DATA_DIR", str(ROOT / "data")))
        snapshots = store.list_datasets()
    except (OSError, sqlite3.Error, ValueError) as exc:
        st.error(f"Could not open local data storage: {exc}")
        st.stop()

    with st.sidebar:
        st.header("Research inputs")
        source = st.selectbox("Data source", ["Synthetic demo (offline)", "CSV upload", "Yahoo Finance download", "Saved local snapshot"], key="data_source")
        st.caption("Change inputs, then run. Results retain the assumptions of the last completed simulation.")
        with st.form("backtest_inputs"):
            upload = None
            selected_snapshot = None
            ticker = "SPY"
            start, end = date(2020, 1, 1), date(2024, 1, 1)
            label = "CSV"
            if source == "CSV upload":
                upload = st.file_uploader("Daily OHLCV CSV", type=["csv"])
                label = st.text_input("Dataset label", value="CSV")
                st.caption("Columns: date, open, high, low, close, volume. Supply one consistent price-adjustment convention.")
            elif source == "Yahoo Finance download":
                ticker = st.text_input("Ticker", value="SPY")
                start = st.date_input("Start (inclusive)", value=start)
                end = st.date_input("End (exclusive)", value=end)
                st.caption("Explicit download. Adjusted OHLC; no separate dividend cash flows. A new source revision creates a new snapshot.")
            elif source == "Saved local snapshot":
                records = {row["id"]: row for row in snapshots}
                selected_snapshot = st.selectbox("Snapshot", list(records), format_func=lambda key: f"{records[key]['metadata'].get('symbol', 'Dataset')} · {records[key]['rows']} bars · {key[:10]}")
            st.subheader("Strategy")
            short = st.number_input("Short SMA", min_value=1, max_value=1000, value=20, step=1)
            long = st.number_input("Long SMA", min_value=2, max_value=2000, value=50, step=1)
            capital = st.number_input("Initial capital", min_value=1.0, value=100_000.0, step=1000.0)
            allocation = st.number_input("Cash allocation (%)", min_value=0.1, max_value=100.0, value=95.0, step=1.0)
            st.subheader("Costs per fill")
            commission = st.number_input("Commission (bps)", min_value=0.0, max_value=1000.0, value=5.0)
            fixed = st.number_input("Fixed commission", min_value=0.0, value=0.0)
            spread = st.number_input("Full bid-ask spread (bps)", min_value=0.0, max_value=1000.0, value=2.0)
            slippage = st.number_input("Adverse slippage (bps)", min_value=0.0, max_value=1000.0, value=2.0)
            st.caption("1 bp = 0.01%. Half of the full spread is paid on each side, plus slippage and commission.")
            periods = st.number_input("Trading bars per year", min_value=1, max_value=366, value=252, step=1)
            risk_free = st.number_input("Annual risk-free rate for Sharpe (%)", min_value=-99.0, max_value=100.0, value=0.0)
            submitted = st.form_submit_button("Run backtest", type="primary")

    if submitted:
        try:
            strategy = MovingAverageCrossover(int(short), int(long))
            config = BacktestConfig(float(capital), float(allocation) / 100,
                                    TransactionCosts(float(commission), float(fixed), float(spread), float(slippage)),
                                    int(periods), float(risk_free) / 100)
            with st.spinner("Validating data and simulating next-open execution…"):
                if source == "CSV upload":
                    if upload is None:
                        raise ValueError("Choose a CSV file before running.")
                    upload.seek(0)
                    data = load_csv(upload)
                    metadata = {"source": "csv", "symbol": label, "filename": upload.name, "adjustment": "user-supplied; not verified"}
                elif source == "Yahoo Finance download":
                    data = download_yahoo(ticker, start.isoformat(), end.isoformat())
                    metadata = {"source": "yahoo", "symbol": ticker.strip().upper(), "requested_start": start.isoformat(), "requested_end_exclusive": end.isoformat(), "adjustment": "auto_adjust=True; adjusted OHLC; no separate dividend cash flows"}
                elif source == "Saved local snapshot":
                    if not selected_snapshot:
                        raise ValueError("No saved snapshot exists yet. Run a demo, upload, or download first.")
                    data, metadata = store.load(selected_snapshot)
                else:
                    data = demo_data()
                    metadata = {"source": "synthetic", "symbol": "SYNTHETIC", "seed": 42, "rows": 600, "adjustment": "not applicable"}
                st.session_state["research_run"] = run_research(data, strategy, config, store, metadata)
        except (ValueError, OSError, KeyError, sqlite3.Error, DataDownloadError) as exc:
            st.error(f"Backtest failed: {exc}")
            st.stop()
    if "research_run" in st.session_state:
        render_results(st.session_state["research_run"])
    else:
        st.markdown("Choose your data and assumptions in the sidebar, then select **Run backtest**.")
        st.markdown("Start with the offline synthetic demo to inspect next-open fills, account equity, costs, drawdowns, and the trade ledger.")
        st.warning("Use chronological holdouts for research. Current ticker selection can create survivorship bias; repeated parameter tuning can overfit.")


if __name__ == "__main__":
    main()
