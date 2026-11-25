# Project Quant for Windows

Version 0.8.1 · Windows 10/11 x64 · Local research and DEMO monitor controls

Run `ProjectQuant-Setup-0.8.1-x64.exe` to install for your Windows user. A Start
menu shortcut and optional desktop shortcut open a standalone app window. Python,
VS Code and a terminal are not required. The app uses the installed Microsoft
Edge WebView2 runtime; this PC already has it. Settings and app logs are stored
under `%LOCALAPPDATA%\ProjectQuant`. Uninstalling removes the application and
shortcuts while retaining your workspace and research data.

The app opens your existing `Projects\Project Quant` folder automatically when
present. Use **File → Open workspace** to choose another folder containing
`data/raw` and your reports/logs. It remembers the folder on the next launch.
On a new PC it creates an offline workspace from the included historical market
archives. Your actual account logs, trade journal, credentials and trading
authorizations are not included in the installer.

## Activate DEMO trading

Open **Manage DEMO session** in the status card at the top of any page. The
controls provide:

- **Activate DEMO trading:** choose an end date/time in Manila and click once.
  Default is the next 10 PM; all sessions must end within 24 hours. This enables
  the existing Gold and EUR/USD monitor, not an immediate market order.
- **Green ACTIVE light:** a live monitor process, heartbeat at most 45 seconds
  old, verified DEMO account, supported activation profile, future deadline and
  no saved stop/revocation. An active monitor can be waiting with no position.
- **Red INACTIVE light:** no verified authority, including startup, stopped,
  failed, stale or expired sessions. A still-alive stale process blocks a second
  start; use Stop new entries and investigate rather than spawning duplicates.
- **Stop new entries:** uses the existing revocation command. Positions retain
  their SL/TP. Stopping and session expiry do not close trades.

Status refreshes every five seconds while the app is open. Native preflight
still rejects REAL/CONTEST accounts, open/pending positions, missing permissions
or changed approved sources. Nothing starts on page load or automatic refresh.
Sizing keeps the 1% baseline and qualified minimum-volume-only exception up to 2%.
The explicit 23 November DEMO policy allows two positions per market (four total),
with an 8% combined planned-risk ceiling against current lower balance/equity.
Each additional entry needs a fresh later completed candle and its own valid setup;
the ceiling is never a target. Separate tickets, SL/TP and verified exits require
the pinned hedging DEMO account. BTC remains separate, one position at 1%.

Execution uses the chosen workspace's `.venv/Scripts/python.exe` and
`scripts/app_monitor_session.py`, which delegates to `engine.activation_demo`.
The installed UI continues to exclude MT5/execution modules. A fresh research
seed lacks the configured execution environment and shows disabled controls;
select the existing Project Quant project folder for trading. MT5 must remain
signed into the pinned DEMO account and connected. No passwords are requested.

## Charts, dates and history

The **Seven-setup research lab** page now has a separate **Five executable DEMO
setups** tab. Select EUR/USD or Gold to read five instrument-specific guides:
trend reclaim, trend sweep, channel breakout, compression breakout and range
re-entry. Each guide explains market conditions, candle/indicator rules,
structural SL, adaptive TP and reasons for skipping. The seven original research
patterns remain observation-only. Saved five-setup decisions distinguish new
technical triggers, persistent conditions, overlaps, execution blockers and
verified entries. Weekday, month and DST-aware session summaries describe the
recorded sample; they do not predict winning probabilities.

- **Overview:** monitoring state, saved journal totals, shortcuts to the main
  workflows, accumulated native rejection reasons and saved data coverage.
  Journal figures are explicitly dated saved observations, not live equity.
- **Seven-setup research lab:** choose provider, instrument and timeframe,
  then dates. Use Overview for setup frequency and weekly charts, Signal history
  for individual signals, chart markers and CSV exports, and Setup guide for
  plain-language rules. Advanced source details and data imports are expandable.
- **Live monitor:** saved heartbeat, decision and completed-candle strategy gates,
  plus entry/context candlestick, EMA, RSI and ATR charts. This reads the existing
  monitor's files; a stale/stopped monitor is explicitly identified.
- **Market data & dates:** select Gold or EUR/USD, Manila/UTC dates, and a date
  range. View interactive candlesticks and EMA20/50, inspect native candles and
  export the selected range as CSV. The chart shows at most 5,000 candles and the
  table previews at most 500; the download retains all matching rows.
- **Historical replay:** select dates, capital and modeled costs to simulate the
  original A-only activation strategy across both markets with shared risk.
  It does not simulate the five-setup profile. The existing research assumptions
  and limitations still apply.
- **Actual trade journal:** filter by entry date in Manila, instrument and trade
  category. View recorded per-trade/cumulative booked P&L, prices, lots, SL/TP,
  execution reasons and exports. The booked-result graph is not account equity.

Market charts are archived data, not a newly added live price feed. Their EMA
warmup uses the full archive; the live strategy explainer uses its bounded input.
Changing dates or closing this window does not change the trading strategy.

## Technical Summary

A small Windows window hosts the local Streamlit app through WebView2. The
installer bundles Python and the needed packages. The app starts its own server
on a free loopback-only port and cleans up that owned child process on exit.
The existing DEMO execution monitor remains a separate process with its existing
authority and deadline. The UI imports no MT5 and sends no broker orders itself;
only an explicit control click launches or stops the existing DEMO engine.

## Root Cause

The earlier dashboard required starting Python and opening a browser URL. It did
not have an installable Windows executable, shortcuts, saved workspace selection
or dedicated market/journal date views.

The initial installed build opened the window but raised
`ModuleNotFoundError: streamlit.runtime.scriptrunner.magic_funcs`. Streamlit
inserts this import into its script at runtime; PyInstaller's static dependency
scan could not see it. The earlier health check tested the server listener but
never requested a page run, so it missed this failure. The build now explicitly
includes the helper, and the self-test requests every page over the local
Streamlit protocol, rejecting exceptions or missing market chart data.

## How It Was Diagnosed

The existing Python environment, Windows/WebView2 runtime, current dashboard,
archived data and journal fields were inspected. Packaging tools are isolated in
`.venv-desktop` so the active monitor's environment is not changed. Runtime code
and user datasets have separate roots, allowing a read-only install directory
and a persistent, writable workspace.

## Solution

`dashboard/trading_controls.py` supplies the buttons and status light;
`dashboard/monitor_control.py` handles file-based status and serialized explicit
requests. `scripts/app_monitor_session.py` runs the approved native engine and
starts the existing deadline-bound keep-awake helper after successful startup.
Requests and errors are recorded under `logs/app_control/`. Repeated clicks or
multiple app windows cannot launch a second pending/running activation. There
are no automatic activation/order retries.

Frozen-app launches also reset the inherited bundled DLL search directory for
the external workspace Python and then restore the UI's directory. Otherwise
the workspace interpreter could pick incompatible libraries from the installer.
The external process also receives a clean Python environment.

The display server stays in its Windows process job; the explicitly requested
monitor worker breaks away from that job. Closing the app therefore cleans up
its display server while retaining the independent trading session. A Windows
process job is an OS group used to manage child process lifetime.

`desktop/main.py` supplies the native window and server mode;
`desktop/runtime.py` handles local settings and an isolated Windows job for its
own server. `dashboard/history.py` adds date filtering and booked-result history;
`dashboard/research_app.py` adds the UI. The PyInstaller spec, Inno Setup script,
version metadata and `scripts/build_desktop.py` produce the distributable.

The build includes an explicit allowlist of historical files with sanitized seed
manifests. No entire workspace, monitor state, private actual history or engine
execution modules are packaged. Local dataset contents stay outside the install
directory. A live decision's strategy-kernel fingerprint is checked against the
installed code before explaining its gates.

Build on Windows using the isolated build environment:

```powershell
.\.venv-desktop\Scripts\python.exe -B scripts/build_desktop.py --iscc build-tools/InnoSetup/ISCC.exe
```

The installed executable also supports a non-trading self-test:

```powershell
& "$env:LOCALAPPDATA\Programs\ProjectQuant\ProjectQuant.exe" --self-test "$env:TEMP\ProjectQuant-self-test.json"
```

Validation results and installer hashes are recorded in the dated desktop
verification report under `reports/desktop/`. An installer build or HTTP health
response alone does not establish that charts render; offline component checks,
installed-binary checks and actual window inspection are reported separately.
The page-protocol test verifies script execution and emitted chart data; it is
not a visual inspection of the WebView2 renderer or every native menu action.

## What I Learned

- Installing code and storing user data are separate jobs; upgrades should not
  overwrite the journal or market datasets.
- Bundling Python removes the need for a separately configured user interpreter.
- A local desktop window can reuse the tested chart/research UI.
- Date filters must define timezone and whether they filter entries or exits.
- Closing a viewer should release only its own server, never the trading monitor.

## If This Happens Again

If the app cannot start, inspect `%LOCALAPPDATA%\ProjectQuant\desktop_error.log`
and `logs\server.log` below that folder. If charts/history are empty, check the
selected workspace and date filters. Actual history requires the existing
workspace's `data/processed/trade_journal.json`; an empty journal on a fresh
installation is not a lost broker account history. If WebView2 is missing on
another computer, install Microsoft's runtime from its official download page.
If the light remains red after clicking Activate, read its reason and inspect
the matching `logs/app_control/app-*.json` and `.stderr.log`. Do not delete
authorization/checkpoint files or bypass a failed preflight. If it says active
but has no entries, inspect the Live monitor decision: monitoring is not a
promise that an affordable setup will occur. The independent keep-awake helper
ends with the monitor/deadline and does not change the Windows power plan.
The desktop application does not change Windows security settings.

Official packaging/runtime references:

- [PyInstaller runtime and bundled resources](https://pyinstaller.org/en/stable/runtime-information.html)
- [pywebview Windows dependencies](https://pywebview.flowrl.com/guide/installation.html)
- [Inno Setup compiler](https://jrsoftware.org/inno-setup/documentation/topic_compilercmdline.htm)
- [Windows job objects and explicit child breakaway](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [Streamlit fragment refresh behavior](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment)
- [PyInstaller external-process library isolation](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application)
