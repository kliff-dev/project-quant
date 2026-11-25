# Local monitor and research workspace

Open **http://127.0.0.1:8502** on this computer. The server is local and does not
require an AI API, subscription, cloud deployment, or another MT5 connection.

Version 0.3 adds explicit DEMO controls above every page: choose a Manila end
date/time, click **Activate DEMO trading**, and watch the green/red status light.
Green requires a live process, fresh heartbeat, verified DEMO account and
unexpired, unrevoked entry authority. Red includes inactive and unverified states.
The default end time is the next 10 PM, with a maximum 24-hour session. **Stop
new entries** never closes positions. The configured execution workspace and
its Python environment are required; a research-only seed cannot activate trades.
Opening or refreshing the app never starts trading. The light refreshes every
five seconds independently of charts. See [desktop controls](DESKTOP_APP.md).

## Technical Summary

The new workspace is `dashboard/research_app.py`. It complements the original
daily-SMA app in `dashboard/app.py` with the current Gold M15/M5 and EUR/USD H4/H1
activation strategy. There are four pages:

| Page | What it does |
| --- | --- |
| Live monitor | Reads the saved heartbeat, process status, deadline, inventory counts and authoritative decision; recomputes an instrument's technical checklist and charts from its checksum-verified input. Refreshes every ten seconds while the page is open. |
| Historical replay | Replays current activation signals and modeled account admission chronologically across both instruments. Shows equity, closed trades, rejected opportunities, input-candle explanations and downloadable JSON/CSV. |
| Actual trade journal | Shows the existing journal's generation time, actual recorded trades and summary; keeps actual results separate from simulations. |
| How to read this app | Explains the indicators, completed-candle timing and model limits. |

Start with **Live monitor**, select Gold or EUR/USD, and read the trend chart
before the entry chart. The table shows BUY and SELL gates independently as
Passed, Failed or Not evaluated. There is no invented probability or readiness
score. A technical setup does not by itself verify a broker-executable plan.
The monitor's saved decision remains authoritative.

On **Historical replay**, choose capital, UTC dates and cost assumptions, then
select **Run offline replay**. Alternatively, **Load saved two-week example
replay** loads the initial verified example immediately when its logic and data
fingerprints match. The result retains its own dates and settings even if input
controls are edited afterward. The candle inspector can explain a WAIT candle as
well as a historical entry opportunity.

## Root Cause

The previous dashboard was a daily moving-average research tool. Current DEMO
monitoring and intraday strategy research had accumulated separate files and
reports, so the app did not explain the current monitor's waiting conditions or
provide an integrated current-policy replay.

## How It Was Diagnosed

The existing dashboard, monitor status/events, immutable broker inputs, journal,
activation signal kernels, adaptive target selectors and conditional risk policy
were inspected. The current 1% baseline / qualified minimum-lot-only 2% exception
differs from the older clock-filtered historical research. The new replay uses
`analytics.activation_signals.gold_features/eur_features` and the existing
adaptive target selectors, retaining their bounded history and causal pivots.

## Solution

- `dashboard/research_model.py`: read-only file loading, input checksums, safe
  process liveness checks, stale/deadline status, gate explanations and Plotly
  charts using the same bounded data as the technical kernels.
- `dashboard/replay.py`: offline two-symbol account model, current activation
  signals, adaptive 1R–4R targets, conditional minimum-lot exception, combined
  risk/margin checks, conservative exits and reproducible logic fingerprints.
- `dashboard/research_app.py`: the Streamlit pages and downloads.
- `scripts/start_research_dashboard.py`: starts a separate server on loopback,
  saves its process/log information under `logs/dashboard`, and checks HTTP
  health. It refuses an occupied port and leaves the existing process alone.
- `tests/test_research_workspace.py`: offline evidence, gate, causality, risk,
  portfolio accounting and Streamlit component checks.

The trading engine, strategy source files, pinned policies, account, execution
authority and running monitor were not changed by the original research UI.
The current UI still imports no MT5 package. Only explicit activation/stop
clicks call the existing execution backend through a separate workspace worker;
account checks, strategy rules, risk caps and order handling remain there.

The initial replay covers **30 October to 13 November 2026 at 22:00 UTC**, starts
at $500, and uses the allowance-debit model. Its result is preserved in
`reports/research_dashboard/first_replay_2026-11-16.json`. It is an example on
previously inspected history, not an untouched validation set or deployment
qualification. Five completed simulated trades are too few to establish an edge.

### Research model limits

The technical entry calculations are reused exactly. Historical execution is
modeled using the saved USD linear contracts and bid OHLC, signal-bar spread at
entry, exit-bar spread for estimated SELL ask prices, and adverse slippage.
Native historical margin, broker permissions and tick-level execution are not
available. The stop is modeled at the next contiguous open, rather than replaying
a historical native quote/intent/permission transaction. This is not a full
reproduction of native historical order admission.

Sizing first attempts 1%. Only a minimum lot that exceeds 1% may use the strong
trend / normal volatility exception up to 2%, with at least 2R target room.
Rescue never increases volume toward 2%. The model limits inventory to one
position per instrument and frozen combined planned risk to 2% of the lower
modeled balance/equity. EUR/USD is processed first at shared entry timestamps.

A candle's full high/low is released only at its close. For example, an H1 trade
that will hit a target during the hour cannot release risk for a Gold entry five
minutes into that same hour using a future hourly high. This is conservative:
the real exit might have occurred earlier. SL wins if both SL and TP appear in
one candle. Exit timestamps and holding times are therefore upper bounds for
intrabar exits. End-boundary positions remain marked and do not become completed
wins or losses. Equity marks use only currently available opens/completed closes,
and can miss intrabar drawdown.

Both cost options reserve 5% of the risk budget when sizing and setting targets.
`allowance_debit` also charges that amount once as a modeled fee;
`reserve_only` makes no such cash debit. Neither identifies actual historical
broker commission. Swap is assumed zero. The selectable 2× stress doubles adverse
slippage. A 100:1 margin estimate is illustrative. Costs and margins can differ
from actual execution, and losses can exceed planned risk during gaps.

Historical entry eligibility is continuously active inside the chosen date
range. No historical user activation/deadline schedule is invented. This differs
from today's bounded six-hour/explicit-deadline execution authority. Replaying a
previously examined period does not turn it into unseen evidence, and no filter
or risk change is automatically promoted into the running monitor.

### Validation

Targeted tests cover both instruments and directions, exact kernel gate parity,
corrupt/misdated input rejection, path containment, stale/missing/dead/expired
status, no future-bar signal leakage, minimum-lot-only rescue, 2R rescue room,
one position per symbol, shared risk, EUR priority, fee reconciliation, stop-first
ambiguity, delayed H1 outcome release and partial end bars. Streamlit AppTest
checks the pages with synthetic fixtures and separately with actual saved inputs.
The completed initial real-data replay also rendered its result tables, charts,
decision replay and export controls through AppTest.

HTTP health and actual server process checks verify that the server is running;
they alone do not establish UI correctness. Browser-level visual inspection was
unavailable because the browser access host was unavailable. No desktop capture
was restarted. Exact historical broker execution and future profitability remain
unverified. No orders were submitted as part of dashboard validation.

The detailed final counts, source hashes and heartbeat are in
`reports/research_dashboard/verification_2026-11-16.json`.

This first version does not select an optimized strategy, validate economic or
seasonal filters, calculate a probability of winning, or promote changes into
execution. A later research stage can add chronological evaluation across more
periods and condition breakdowns with adequate sample sizes.

## What I Learned

- A fresh heartbeat and a completed-candle decision timestamp answer different
  questions: whether the monitor is alive and which market input it evaluated.
- A passed technical setup is different from an affordable, broker-admitted order.
- Portfolio backtests must share risk and release outcomes in time order; separate
  single-market curves do not establish combined-account performance.
- A sizing reserve is not automatically a verified historical commission charge.
- Faster simulation should preserve the same seeded calculations and candidate
  set; an approximate indicator shortcut can silently change the strategy.

## If This Happens Again

Start the dashboard in the foreground, then use Ctrl+C in **that dashboard
terminal** when finished:

```powershell
.\.venv\Scripts\python.exe -B -m streamlit run dashboard/research_app.py --server.address 127.0.0.1 --server.port 8502 --browser.gatherUsageStats false
```

For a background start with logs and an HTTP health check:

```powershell
.\.venv\Scripts\python.exe -B scripts/start_research_dashboard.py
```

An occupied port is reported rather than killing its owner. An alternative port
can be selected with `--port 8503`. Closing a browser tab does not stop the
dashboard server or the separate DEMO monitor. A stopped dashboard has no effect
on broker SL/TP or the monitor's deadline.

If the monitor page says STALE, inspect `logs/monitor/status.json`, its heartbeat,
PID and deadline first. If input verification fails, inspect the recorded CSV
path, timestamp and SHA256; do not force the table to show Passed. If replay is
slow, use a shorter date interval or load the saved example. Model differences
belong in a new research version rather than silent live policy edits.

Re-run relevant offline tests:

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests/test_research_workspace.py tests/test_dashboard.py tests/test_activation_demo.py tests/test_gold_strategy.py tests/test_extended_targets.py -q
```

Streamlit's automatic panel refresh uses its documented
[`st.fragment(run_every=...)`](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment).
