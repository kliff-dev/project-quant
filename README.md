# Project Quant

Local Python research and backtesting on Windows. MT5 supports read-only account
diagnostics and explicitly authorized **DEMO execution**. Preview is the default.
REAL/CONTEST accounts, missing verification, trading-permission failures, and
invalid quote timestamps block execution. The local monitor can create previews,
submit one authorized entry (`demo_once`), or take sequential fresh setups in a
bounded authorized session (`demo_series`). The current Gold/EURUSD activation
permits two positions per market, four total, under an 8% combined planned-risk
ceiling. Standalone and older dated sessions retain their original limits. Manual commands
still require typed confirmation. No paid AI API, paid data service, or cloud setup
is required.

## Windows desktop application

The installable local app is **Project Quant 0.8.1**. Its Windows x64 installer
is built into `release/ProjectQuant-Setup-0.8.1-x64.exe`. It includes Python,
opens its own window, creates Start menu/desktop shortcuts and remembers the
workspace folder. The PC's Microsoft WebView2 runtime displays the local charts.

The app opens on **Overview**, with saved results, monitoring status and direct
links to research, decision details and actual trades. A consistent navy/teal
theme, readable chart styling and short explanations make the workspace easier
to scan. **Manage DEMO session** contains the existing start/stop controls.
Research has separate **Overview / Signal history / Setup guide** tabs, clear
provider/instrument/timeframe selectors, and Manila timestamps. Advanced data
and diagnostics stay available in expandable sections.

Open **Seven-setup research lab** for separately versioned, research-only EMA
reclaim, liquidity sweep, channel breakout, compression breakout, range re-entry,
London opening-range and RSI momentum-return hypotheses. Select an Exness or
Dukascopy dataset, filter dates/setup, inspect trigger counts, weekly history and
price markers, and download the signal history. Use **Refresh MT5 research data**
to read recent completed candles without activating trading. The new seven rules
remain research-only. The separate executable profile now has five instrument-specific
families per market: trend reclaim, trend sweep, channel breakout, compression
breakout and range re-entry. Read their exact rules and saved execution decisions
in the **Five executable DEMO setups** tab, or the [five-setup guide](docs/FIVE_SETUP_GUIDE.md).
Frequency is not profitability;
the seven-rule page does not yet simulate cost-aware SL/TP outcomes. See
[the seven-setup guide](docs/SEVEN_SETUP_LAB.md).

Use **Activate DEMO trading** at the top of any page to start Gold and EUR/USD
until the displayed Manila end date/time (default 10 PM, always within 24 hours).
The light is **green only for a verified running DEMO monitor** with a fresh
heartbeat and unexpired authority; **red means inactive or unverified**, including
startup, stopping, stale data or a failed check. **Stop new entries** revokes entry
authority without closing positions. Opening the app never activates trading.
The configured execution workspace, its `.venv`, and signed-in MT5 are required
for trading controls; a newly seeded research-only workspace cannot trade.
Sizing retains a 1% baseline / qualified minimum-lot-only 2% exception. The explicit
23 November DEMO update permits two positions per market, four total, with an 8%
combined planned-risk ceiling. Adaptive SL/TP rules remain in force. Closing the app
does not stop an authorized monitor. Status refreshes every five seconds.

Use **Market data & dates** for date-filtered Gold/EURUSD candles and CSV export,
**Actual trade journal** for entry-date/instrument/category filters and booked
P&L graphs, and the existing monitor/replay pages for strategy evidence. Closing
the app releases its own display server; the separately authorized DEMO monitor
keeps its existing authority and deadline. See
[desktop installation, build and troubleshooting instructions](docs/DESKTOP_APP.md).

## Local monitor and research dashboard

Open **http://127.0.0.1:8502** for the current Gold/EURUSD strategy workspace:
live waiting-condition checklists and closed-bar charts, current-policy modeled
portfolio replay, rejection reasons, downloads and the actual DEMO journal.
It reads local files and performs offline research; the existing monitor owns
execution. Actual results and simulations remain separate. See the
[workspace guide, assumptions, validation and launch instructions](docs/RESEARCH_WORKSPACE.md).

```powershell
.\.venv\Scripts\python.exe -B scripts/start_research_dashboard.py
```

The original daily-SMA research app remains in `dashboard/app.py`.

## Offline maintenance checks

Version 0.8.1 speeds up saved setup-history loading without caching away new
observations. Invalid monitor deadlines are shown as unknown; malformed saved
research files no longer crash the catalog or legacy signal history. Desktop
builds recreate their staging payload from the allowlist on every build.

```powershell
.\.venv\Scripts\python.exe -B scripts/check_project_health.py --smoke-cli --output reports/project_health.json
.\.venv\Scripts\python.exe -B -m pytest -q
```

The health command checks source syntax, current approved execution-source
checksums and the reviewed command-line help paths. It reads saved monitor
status but never connects to MT5, activates trading or runs historical batch
studies. A successful help check does not prove every runtime path works.
Older dated research scripts without an argument parser are listed as batch
or helper modules; passing `--help` to them can execute their study instead.

## Strategy indicators in MT5

Four labeled Project Quant charts show the EMA trend/reclaim visual baseline:
Gold M5 entry / M15 trend, and EUR/USD H1 entry / H4 trend. Blue is EMA20,
orange EMA50, purple EMA200; entry charts also show the strategy's RSI bands,
and all four show Wilder ATR14. The other four executable families are explained
in the app's five-setup guide; these MT5 overlays alone do not depict every entry rule.
They are display-only visual guides; Python's
closed-bar decisions and bounded calculation history remain authoritative.
See [the chart guide, verification and restoration instructions](mt5/README.md).

## Current mode: activate Gold and EUR/USD immediately

An explicit activation now enables entries immediately for **six hours**, with
no fixed clock-entry schedule or routine US clock-blackout filter. Weekends and
unavailable broker sessions remain blocked. An explicit `--until`
can override the six-hour default with an aware end time within 24 hours.
There is no scheduler
or automatic restart. A valid completed-candle setup is still required; the
program never enters just because it has been started. Gold uses M15 context / M5
entries; EUR/USD uses H4 context / H1 entries. Each instrument evaluates its own
trend reclaim, trend sweep, channel breakout, compression breakout and range
re-entry rules from the fixed `multi_setup_v1` specification. Signal freshness remains
60 seconds for Gold and five minutes for EUR/USD. New entries use **1% baseline
planned risk**, with a separately authorized **2% ceiling** for the minimum-volume
exception described below, at most **two positions per instrument (four total)**, and an **8% combined
planned-risk ceiling**. Native sizing, costs, margin, DEMO verification, SL/TP,
adaptive 1R-4R targets, verified exits and no retries remain mandatory.

Each additional position requires a fresh later completed candle, a valid versioned setup,
and sufficient account risk and margin. Repeated polls never duplicate one candle.
The expanded scope requires the pinned DEMO hedging account: netting accounts
cannot preserve two independent positions on one symbol. Each ticket has its own
frozen sizing, SL/TP, entry proof and independently reconciled exit. All existing
positions, including the same symbol, count toward the 8% ceiling against the lower
current balance/equity. BTC retains its separate one-position, 1% scope and cannot
run alongside this monitor. This increases permitted exposure, not expected profitability.
Four 2% losses are approximately 8% before execution overruns; at $500 the planned
aggregate ceiling is $40. The engine does not increase normal sizing to use that amount.

This is the separately recorded `user_activation_v1` timing profile. Previous
clock-filtered backtests do not validate its performance. It has no actual
economic-event calendar. Broker availability, current quotes, spreads and
volatility still determine whether an eligible technical setup can be traded.

The `minimum_lot_rescue_v1` risk mode keeps normal sizing within 1%. Only when
the broker's minimum lot exceeds that budget can it admit **minimum volume only**,
up to 2%, provided verified trend strength, normal volatility, and at least a 2R
target fit the existing rules. It never increases volume toward 2%, narrows the
structural stop, or treats 1.3% as a minimum. Natural planned risk may be 1.3%,
1.6%, or another value within the ceiling. Costs/slippage reserves are included;
gaps can still exceed planned losses. Aggregate frozen planned risk in the
current four-position activation must stay within its 8% ceiling against current
lower balance/equity. Older sessions retain their original limits. This is an unproven DEMO
risk experiment, recorded separately in the journal. See
[the risk change and verification](reports/conditional_risk_2026-11-16.md).

```powershell
# Offline saved status by default; --preview makes read-only native checks.
.\.venv\Scripts\python.exe -B scripts/activate_demo.py --status
.\.venv\Scripts\python.exe -B scripts/activate_demo.py --preview
# Explicit start-now authority; give each new user activation a unique ID.
# Reusing a consumed ID is refused. Do not reset IDs or retry failed orders.
.\.venv\Scripts\python.exe -B scripts/activate_demo.py --start --activation-id <new-user-request-id>
# An explicitly requested end time; this example ends at 10 PM Manila today.
.\.venv\Scripts\python.exe -B scripts/activate_demo.py --start --activation-id <another-new-user-request-id> --until 2026-11-16T22:00:00+08:00
# Stop entry authority for both markets; never closes an existing position.
.\.venv\Scripts\python.exe -B scripts/activate_demo.py --stop
```

Keep the PC awake and MT5 open, connected and signed into the pinned DEMO account.
The local Python process runs independently of this conversation. Expiry stops
entries; broker SL/TP remains attached. New activations require a verified flat
account; an existing position is never closed or adopted automatically. Trading
checkpoints are retained across activations so the same candle is not retried.
See [current status](reports/latest_dual_monitor_status.md) and
[the activation change record](reports/activation_timing_2026-11-16.md).

## Previous dated mode: Monday 16 November windows

The user explicitly requested both instruments and then permitted **one Gold
position and one EUR/USD position**. The combined controller keeps **1% maximum
planned risk per trade** and a **2% combined planned-risk ceiling** against current
lower balance/equity. It reuses the frozen strategies, native lot sizing, fixed
SL/TP, adaptive 1R-4R targets and no-retry execution. Unrecognized positions,
duplicate symbols or uncertain attempts stop new entries. Individual launchers
retain their original account-wide one-position behavior.

Manual activation can wait for today's entry windows: **Gold 15:00-21:00 Manila;
EUR/USD 16:00-22:00 Manila**. The controller expires at 22:00 today and does not
restart tomorrow. Deadlines stop entries and do not close positions. See the
[combined activation record and runbook](reports/dual_demo_activation_2026-11-16.md)
and [current combined heartbeat](reports/latest_dual_monitor_status.md).

```powershell
# Offline saved status; a recent heartbeat is required to report running.
.\.venv\Scripts\python.exe -B scripts/start_dual_demo.py --status
# Stop both entry authorities; never closes an existing position.
.\.venv\Scripts\python.exe -B scripts/start_dual_demo.py --stop
```

The old EUR/USD position was manually closed by the user for **+$3.02**, verified
from its actual exit deal and filled order. The journal retains the cash result
and excludes it from SL/TP-rule performance. Journal updates occur locally when
execution evidence changes, without starting a second MT5 connection. Historical
single-position research does not establish this portfolio's profitability.

## Gold: separate experimental DEMO candidate

Gold now has a **M15 trend / M5 EMA20 reclaim** setup using the verified broker
`XAUUSDm` Gold CFD. Structure/ATR determines the stop; native sizing then applies
the **1% maximum planned account risk**. Targets adapt from **1R to 4R** using
trend strength, volatility, costs and confirmed historical swing room. There is
no actual economic calendar or validated seasonal model. No affordable valid
setup means no entry. Individual launchers share one account position limit and
one monitor lock. The combined controller above explicitly permits one of each.

The 35,366-candle May-November replay was **negative after modeled costs**:
$500 ended at $491.53 over four trades (25% wins), and $1,000 at $978.93 over
38 trades (42.1%). The Gold minimum lot rejected most $500 opportunities.
This is a DEMO research candidate, not a profitability-qualified strategy.
No Gold order or monitoring process was started during preparation. The Sunday
native preview verified DEMO, returned WAIT and found one existing position,
which blocks a Gold start until its actual exit is verified.

```powershell
# Offline plan by default; does not arm trading.
.\.venv\Scripts\python.exe -B scripts/start_gold_demo.py --plan
# Native read-only decision; no order_send. May order_check an eligible preview.
.\.venv\Scripts\python.exe -B scripts/start_gold_demo.py --preview
# Explicit manual DEMO activation: weekdays 07:00-17:00 London, max six hours.
.\.venv\Scripts\python.exe -B scripts/start_gold_demo.py --start
```

See the [Gold strategy, risk rules and full runbook](reports/gold_demo_plan.md),
[historical results](reports/market_analysis/gold_intraday_2026-11-15/review_notes.md)
and [latest native preview](reports/latest_gold_market_snapshot.md). Stops and
targets remain fixed after entry; the entry deadline does not close positions.

## Monday 16 November: manual DEMO pilot

The user requested a bounded DEMO pilot of the researched single H4-trend/H1
EMA20-reclaim setup. Following the explicit 15 November choice, its separate
version-2 adaptive policy selects up to **4R** at entry. The 4R target requires
the existing strong-trend and normal-volatility conditions plus enough room
before the same historical swing reference; otherwise use the existing
1R/2R/3R choice. Stop, lot sizing and the 1% risk cap stay the same. This is an explicitly requested
research pilot; the historical study below still **does not establish an edge**.
The original default fixed-2R policy remains unchanged.

The pilot is prepared for **Monday 16 November 2026, 16:00-22:00 Asia/Manila**
(08:00-14:00 UTC/London). Nothing is scheduled or running from this preparation.
Leave MT5 signed into the pinned DEMO account and the PC awake and connected
while the command runs. Credentials stay in MT5.

```powershell
# Local plan/status only; no MT5 connection or orders.
.\.venv\Scripts\python.exe -B scripts/start_monday_demo.py --plan
# Read actual history for the previously saved protected position; never sends orders.
.\.venv\Scripts\python.exe -B scripts/start_monday_demo.py --reconcile
# Run on Monday inside the stated window, after prior exits are verified.
.\.venv\Scripts\python.exe -B scripts/start_monday_demo.py --start
# From a second terminal: stop new entries; retain existing broker SL/TP.
.\.venv\Scripts\python.exe -B scripts/start_monday_demo.py --stop
```

The saved earlier strategy trade remains `open_verified`; its actual present
status has not been queried during preparation. It blocks Monday startup until
actual protective-exit orders/deals are verified. Reconciliation cannot close a
position or clear an uncertain attempt. An actual open position still blocks.

EURUSDm only; native DEMO verification; 1% maximum planned risk from the lower
of current balance/equity; structural stop first, then lot sizing rounded down
to the broker step, capped at 0.1 lots. A too-large minimum lot skips the setup.
Only one position can be open, with a verified full broker SL/TP exit and a later
fresh signal required for another entry. Failed/uncertain attempts are never
retried. A durable launch reservation prevents restarting the same Monday pilot.
The deadline stops entries; it does not close positions or guarantee same-day exits.
Gaps, slippage and actual costs may exceed the planned allowance.

Source/policy hashes pin the reviewed implementation. Startup outside this exact
Monday window fails before connecting. See the [Monday plan and verification](reports/monday_demo_plan_2026-11-16.md).

## Four-R target extension

The [versioned extension study](reports/market_analysis/extended_targets_2026-11-15/findings.md)
tests the requested higher ceiling using the same frozen entry, stop, sizing and
cost assumptions. It preserves the original 1R/2R/3R study as the control.
The prior saved audit found no 3R room in any eligible case; a 4R ceiling alone
cannot create more room. Synthetic cases verify selection when conditions do
permit 4R, without establishing future win rate or profitability.

```powershell
.\.venv\Scripts\python.exe -B scripts/research_extended_targets.py
```

This runs locally against saved data and never connects to MT5 or submits orders.
Larger targets can take longer to reach. Targets remain fixed after entry; this
extension does not add trailing exits, partial closes or a time exit.

## One pullback setup: adaptive target research

The [focused target study](reports/market_analysis/adaptive_targets_2026-11-14/findings.md)
keeps the existing EURUSD trend-reclaim entry and one structural stop with a
1 ATR floor. Both policies use a 1% planned risk ceiling and share a minimum
1R-room filter based on confirmed historical H4 swing references. It compares
fixed 2R with a frozen adaptive 1R/2R/3R rule using target room, H4 trend context
and H1 volatility. This shared filter changes eligibility from the old unfiltered
baseline; its results must not be confused with that earlier account path.

The recent conservative comparison produced 3/4 adaptive winners (75%) versus
2/4 fixed winners (50%), but equity was nearly identical: $508.84 versus $508.59
from $500. On five matched recent opportunities, adaptive averaged about 0.20R
less than fixed; these overlapping diagnostics are separate from the accounts.
The shared filters admitted only five of 35 recent raw setups. The older validation
period was slightly negative, and the development 1R branch lost on average over
only seven closed trades. No primary conservative account trade used 3R.
**The adaptive policy did not qualify for deployment.** The separately requested
Monday DEMO research pilot does not change that historical assessment.

```powershell
.\.venv\Scripts\python.exe -B scripts/research_adaptive_targets.py
```

The command runs offline. It saves one-position account paths, overlapping
matched-opportunity diagnostics, separate 1R/2R/3R statistics, cost sensitivities
and causal swing/ATR context. Targets stay fixed after entry. See the
[non-executable policy](strategies/adaptive_pullback_2026-11-14.json) and
[verification record](reports/market_analysis/adaptive_targets_2026-11-14/verification.json).
The optional planner hook affects only offline research; its default replay was
verified exactly against the archived implementation and prior saved baseline.
The research command itself changes no trading policy, broker position or session.

## New entry setups: 45% win-rate research target

The [new setup study](reports/market_analysis/candidate_setups_2026-11-14/findings.md)
tests three frozen entry families: trend EMA20 reclaim, trend-aligned failed
range break, and low-trend band reentry. Twelve configurations combine those
families with 1/1.5 ATR minimum stops and preferred 2R or alternative 1.5R targets.
**None passed the declared development and later-period requirements.** High
older range-reversal win rates came from only 5-10 completed trades and did not
hold up in the recent period. No profitable 45% setup is claimed or installed.

```powershell
.\.venv\Scripts\python.exe -B scripts/research_candidate_setups.py
```

This command runs offline and saves selection before computing later outcomes.
It reports all 12 choices, four cost cases, candle-order sensitivity, sample
counts, descriptive win-rate intervals, actual payoff, drawdown and holding time.
The [candidate definitions](strategies/candidate_setups_2026-11-14.json) are marked
non-executable research. The [verification record](reports/market_analysis/candidate_setups_2026-11-14/verification.json)
distinguishes automated tests, saved-data replay and historical-fill limitations.

## Six-month $500 account and SL/TP research

The [six-month findings](reports/market_analysis/six_month_2026-11-14/conclusion.md)
replay the existing H4/H1 rules from 14 May to the latest completed Friday
session on 14 November 2026 Manila time. It starts with $500, recalculates the
1% ceiling after each trade, allows one position and retains SL/TP-only exits.
The original rules lost money. A nine-choice SL/TP study selected its alternative
on earlier May 2024-May 2026 data; the later six-month evaluation is descriptive,
not an untouched holdout or evidence of guaranteed profitability.

The report separates a conservative cash charge for the planning allowance from
a zero-extra-fee sensitivity that retains that allowance only for sizing. Both
include spread/slippage, and separate hypothetical financing stress is shown.
Balance and marked equity remain separate when a position is open at data end.
All hypothetical records are kept apart from the actual trade journal.

```powershell
.\.venv\Scripts\python.exe -B scripts/research_six_month_account.py
.\.venv\Scripts\python.exe -B scripts/research_account_cost_sensitivity.py
```

These commands run offline against checksummed broker history. They never start
MT5 or submit an order. See the report's interactive equity charts, trade lists,
registered specifications and verification record. The live policy and expired
monitor session are unaffected.

## Trade journal and execution review

Start with the [trade journal](reports/trade_journal.md). It links a detailed page
for every recorded position and separates execution-integration tests from actual
strategy entries. The first register contains two completed execution tests
(-0.25 USD combined) and one H4/H1 pullback SELL, open at its last saved check.
No completed strategy result is available yet.

The journal stores entry/exit times in UTC and Manila time, actual deals and
prices, lots, original SL/TP, initial risk, spread/slippage, costs, exit reasons,
holding time, net result and realized R. The strategy entry replays its hashed
pre-entry H1 snapshot to show the H4 trend and H1 trigger. Open/uncertain trades
are excluded from completed performance, and execution tests are reported apart.
R uses the original planned risk, which may be below the 1% ceiling; spread and
slippage are not deducted twice from actual fill-based P&L.

```powershell
# Offline: does not start MT5.
.\.venv\Scripts\python.exe -B scripts/update_trade_journal.py
# Optional native history/inventory reads; may launch MT5 using its DEMO login.
.\.venv\Scripts\python.exe -B scripts/update_trade_journal.py --refresh-mt5
```

The reader does not send/check/modify/close orders or restart the expired monitor.
Default updates use saved evidence and label its observation time. Native refresh
requires the exact recorded DEMO account, actual entry/exit deals, filled orders
and reconciled inventory. Position absence alone is not an exit result. It can
document an exit after monitor expiry without changing execution-state files.

Outputs are `data/processed/trade_journal.csv`, `trade_journal.json`, timestamped
JSON history under `data/processed/trade_journal_history/`, and Markdown pages
under `reports/trades/`. Re-running refresh replaces the current register without
duplicating trades and preserves historical generated snapshots. There is no
background journal updater; run the command to refresh these reports. See the
[implementation and verification record](reports/market_analysis/trade_journal_implementation_2026-11-13.md).

The latest [custom strategy research](reports/market_analysis/custom_setup_v1_findings.md)
uses cached broker H1 data to combine trend, pullback/breakout and affordability
checks. It is a signal-feasibility study, **not a profitability backtest**, and
cannot submit orders. Reproduce it offline with:

```powershell
.\.venv\Scripts\python.exe -B scripts/research_custom_setup.py
```

The later [holding-period comparison](reports/market_analysis/holding_period_findings_2026-11-14.md)
models exits for the existing EURUSD rules. It compares matched entries and a
one-position sensitivity under London-day, 24-hour and 72-hour exits. Longer
holding did not increase modeled win rate in that preliminary sample. It uses
H1 execution approximations, cost allowances and hypothetical financing stress;
it is not actual demo performance or a complete account-profitability backtest.
Reproduce with `scripts/research_holding_period.py`; no MT5 connection is needed.

## Current DEMO policy: 1% risk and strategy-based lot size

The user selected **1% maximum planned risk**, retaining **2:1 reward/risk**.
Settings are in `config/trading_policy.json`. At the verified 499.75 USD balance,
1% is 4.9975 USD; rounding down gives a **4.99 USD budget**. Risk is a ceiling,
not a minimum or an amount the program must use completely.

New strategy previews use completed H4/H1 trend and pullback/breakout signals.
They freeze a structural/ATR stop, size lots downward on the broker's volume
step, and reject a minimum lot that exceeds the budget. Strategy volume can
exceed the legacy 0.01-lot test size, with an additional **0.10-lot hard ceiling**.
The actual selected volume must still satisfy the 1% budget and available margin.

The model reserves two broker points of adverse slippage per side and 5% of the
dollar budget for additional costs, rounded up to cents. These estimates do not
guarantee a loss cap. The target provides at least 2:1 under those modeled
reserves, so the displayed gross price-distance ratio may exceed 2:1. The stop
is never widened to spend unused risk budget. A 20-pip example at 499.75 USD fits
0.02 lots: 4.00 USD gross stop loss, or 4.33 USD including the modeled reserves.
0.03 lots would exceed the budget. This example is not an entry signal.

```powershell
.\.venv\Scripts\python.exe -B scripts/test_demo_trade.py --strategy --preview --session strategy-1pct-20261113
```

`WAIT` means no executable preview was created: there may be no signal, the H1
signal may be more than five minutes old, or the London entry session may be
closed. The one-shot command exits; use the monitor below for polling. When a ready preview exists,
review its direction, lot, absolute SL/TP, funds, reserves and broker check:

```powershell
.\.venv\Scripts\python.exe -B scripts/test_demo_trade.py --strategy --open --session strategy-1pct-20261113 --preview-id REVIEWED_ID
```

Manual opening still requires `PLACE DEMO TEST ORDER`. Both BUY and SELL strategy entries
use fresh DEMO checks, broker `order_check`, unchanged preview/context checks and
at most one send. A larger position closes with its exact recorded volume and
position ticket, retaining the separate closing confirmation. The proposed
16:00 London time exit and news/daily/weekly filters are not automatic services.
Strategy profitability is not established; the offline account study above models
historical outcomes. Actual broker execution is documented separately below.

The 13 November live inspection returned **WAIT: outside 08:00-14:00 London**,
with zero new orders and a 4.99 USD risk budget. Native broker P&L calculations
validated the illustrative BUY/SELL sizing. See
`logs/latest_strategy_decision.json`, `logs/risk_policy_change_evidence.json` and
[the policy change record](reports/market_analysis/risk_policy_1pct_2026-11-13.md).

The earlier protected test was manually closed from the desktop at 1.10607 for
-0.15 USD. Its exact exit deal and filled order were reconciled without sending
another order. A backup preserves its previous local state and original 5% policy.
The account was verified flat at 499.75 USD. For explicit future reconciliation
of an already-completed desktop/protective exit, use `--reconcile-external` with
the corresponding session. Position absence alone never establishes closure.

## Local market monitor

`engine/market_monitor.py` adds a bounded monitoring loop around the existing
completed H4/H1 strategy. Its default mode is **monitor-only**: it can save a broker-checked
preview but cannot open, close, modify or retry an order. The exact typed
confirmation in the separate execution command remains required. Leaving the
default monitor unattended will not submit a trade, even if a valid setup appears.
The separately authorized execution modes are described below.

Start in a terminal (Ctrl+C stops it), or use the hidden Windows launcher:

```powershell
.\.venv\Scripts\python.exe -B scripts/monitor_market.py --hours 8
.\scripts\start_market_monitor.ps1 -Hours 8
```

Choose **one** start command. The launcher prints the PID and deadline, then
returns while Python continues locally. It uses the project's existing virtual
environment, with no new dependencies or AI calls. If Windows blocks PowerShell
scripts, use the first command; no execution-policy change is needed.
The launcher checks startup for up to 45 seconds and retains console/error logs
under `logs/monitor/`. It never restarts an exited process automatically.

Inspect or stop from a second terminal:

```powershell
.\.venv\Scripts\python.exe -B scripts/monitor_market.py --status
.\.venv\Scripts\python.exe -B scripts/monitor_market.py --stop
```

`--stop` requests shutdown of the matching run; it does not close positions.
Check `--status` for the stopped acknowledgement. `--once` makes one inspection
and exits (an authorized execution mode may submit an entry during that inspection).
Defaults are eight hours and a 15-second poll; limits are .001-24
hours and 1-60 seconds. Keep Windows awake, online and MT5 signed into DEMO.
The program does not change Windows sleep settings, MT5 permissions or login.
For the current unattended session, `scripts/keep_monitor_awake.py` runs as a
separate hidden helper bound to the monitor process and deadline. It holds a
temporary Windows idle-sleep request, leaving the power plan and display timeout
unchanged. It releases the request when the monitor stops, the deadline arrives
or its heartbeat becomes stale. Manual sleep, shutdown and closing a laptop lid
can still interrupt the program. The helper uses Microsoft's documented
[SetThreadExecutionState API](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate).
Closing VS Code does not stop a detached launcher process; sleep, shutdown or
terminal disconnection can stop its work. There is no restart-at-boot service.

Each poll verifies the native DEMO enum, pinned account/server, policy, inventory,
quote and permissions. A three-bar probe finds the newest completed H1 candle;
the unfinished candle is excluded. Only a **new** close triggers the full history
calculation. New entries remain restricted to weekdays 08:00-14:00 Europe/London
and signals at most five minutes old. It never catches up missed entries.
Data reads and broker checks can take time; stop acknowledgement may wait for a
pending terminal call. Permission/spread/inventory blockers are displayed; loss
of verification, disconnection, corrupt checkpoints, clock reversal or a policy
change stops the session. No reconnect or execution retry is attempted.

A checkpoint reserves each account/symbol/candle before evaluation, so restarting
does not recreate the same preview. An interrupted evaluation can therefore
skip a setup. Blocked candidates are not automatically retried during that
candle. A kernel-held lock excludes duplicate monitors and releases even on
process termination. Its persistent `monitor.lock` file alone does not mean a
process is running; do not remove it while a process may hold it. The separate
execution lock and unresolved-attempt checks continue to protect manual entries.

Outputs:

- [Human-readable status](reports/latest_monitor_status.md): heartbeat, deadline,
  current risk budget and the most recent decision.
- `logs/monitor/status.json`: current sampled conditions and preview expiry.
- `logs/monitor/state.json`: durable candle checkpoint; never execution proof.
- `logs/monitor/events.jsonl`: start/stop/failure and once-per-candle decisions.
- `logs/sessions/mon-*/demo_order_preview.json`: a qualifying candidate's exact
  side, lots, SL/TP, reserves and broker check, plus its manual command in status.

`READY_FOR_CONFIRMATION` means a preview passed checks at creation, not that a
trade happened. Its approval window ends at most five minutes after H1 close.
`--status` labels an old heartbeat `stale_or_overdue`; a saved running status is
not proof the process remains alive. Execution always repeats fresh checks.
There is no automatic time exit, news filter or daily-loss service in this
monitor, and the strategy's profitability remains unestablished.

Vendor references: [MT5 candle numbering](https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrompos_py),
[account information](https://www.mql5.com/en/docs/python_metatrader5/mt5accountinfo_py),
and [broker preflight checks](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercheck_py).

## Current authorized sequential DEMO session

On 13 November the user requested removal of the one-trade session limit so that
fresh valid setups can produce more execution data. The prior single-entry
monitor was stopped while flat, its unused authorization revoked, and a separate
`demo_series` authorization was created from that instruction. The original
**21:02:49 Manila, 13 November 2026** deadline remains in force. This is a bounded
local session, not a permanent trading service. See the
[live status](reports/latest_monitor_status.md) for its current heartbeat.

`engine/demo_series.py` manages a permanent record for each H1 signal. After an
actual filled order, deal and protected position are verified, it watches that
exact position. A new entry requires an actual full broker SL/TP exit with
matching deals and filled orders, followed by a fresh later H1 decision. An
absent position alone is insufficient. There is no numeric one-trade cap within
the session, no overlapping positions, and no re-entry using an old signal.

Each trade recalculates its **1% maximum planned risk** from the lower of current
balance and equity. Lots still follow the fixed strategy stop and broker volume
step, with the 0.10-lot ceiling, cost reserves and at least 2R modeled target.
The H4/H1 strategy, London entry window, freshness and spread filters are unchanged.
Several trades can accumulate losses beyond 1% of the account; no daily-loss cap
is implemented. Stop orders and modeled reserves cannot guarantee actual loss.

Any reserved, failed, partial or uncertain entry stops the series without retry.
Unexpected positions/orders, changed protection, missing exit history or a manual
desktop exit also halt it for review. A stop request revokes this authorization.
The deadline and stop command do not close an open position: its attached broker
SL/TP remain, and its final exit might require later explicit reconciliation.

Only after an explicit session instruction has been recorded, its authorization
can be used with this launcher (do not start a second copy):

```powershell
.\scripts\start_market_monitor.ps1 -DemoSeriesAuthorization APPROVED_AUTHORIZATION_UUID
```

The launcher does not generate approval. Default launches still create previews
only. Both execution modes use the same executor, fresh native DEMO checks,
`order_check`, execution lock and one-send path.

Additional series records:

- `logs/authorizations/<id>.entries/<H1-close>.json`: permanent per-signal attempt
  and outcome, including verified exit time; never delete/reset to retry.
- `logs/sessions/mon-*/demo_test_state.json`: actual order, deal, position and
  protective-exit proof.
- `logs/monitor/series/<id>/completed_trades.csv`: closed trades with actual
  holding time, prices, lots, planned risk, costs, net result and realized R.
  It is rebuilt from verified state on restart without duplicating rows.
- [Implementation and verification record](reports/market_analysis/demo_series_session_2026-11-13.md).

The first live series heartbeat verified DEMO, fresh quotes, no positions/pending
orders and zero new sends. Sequential broker fills and protective exits remain
unverified until actual qualifying setups occur. Offline tests exercise the full
entry/exit/next-entry flow with simulated native broker responses.

## Earlier authorized single-entry DEMO session

The user subsequently instructed: "I dont want to keep monitoring. Just do the
order if there is a valid setup. Just keep things running". This supersedes
per-order confirmation **for one unattended DEMO strategy entry in this bounded
session**. It does not authorize additional trades or REAL/CONTEST accounts.

`engine/demo_authorization.py` records that explicit instruction, verified
account/server/broker, exact EURUSD symbol, policy, a dollar ceiling and expiry.
`engine/demo_session_monitor.py` waits for a newly evaluated valid candidate,
creates its broker-checked preview and calls the separate session-authorized
executor method. It does not manufacture the manual confirmation phrase.
The default mode and manual confirmation flow remain unchanged.

The live session was armed on 13 November at 13:24 Manila, ending at the original
**21:02:49 Manila** deadline. Its ceiling is the lower of current policy risk and
the authorization's **4.99 USD**, with at most **1%**, a strategy-derived fixed
SL, broker-rounded lots no larger than **0.10**, and at least **2R** under modeled
reserves. No qualifying setup means no entry. It is not a guarantee of capped
actual loss, profitability, fill acceptance or a short holding period.

After explicit session approval has already been recorded, a launch uses its
existing authorization ID (replace the placeholder; do not start another copy
while a session is running):

```powershell
.\scripts\start_market_monitor.ps1 -DemoOnceAuthorization APPROVED_AUTHORIZATION_UUID
```

The launcher itself does not create approval. Authorization records are created
by `DemoEntryAuthorization.create` only in response to explicit user authority.
They are local audit records, not passwords. There are no broker credentials.

The executor exclusively reserves that one allowed attempt **before** fresh
preflight, then repeats DEMO/account checks, symbol/volume/margin/inventory,
signal freshness, risk/SL/TP, and `order_check`. Expiry, revocation, policy and
account identity are checked again adjacent to `order_send`. A stop request
revokes the matching session authorization. A crash, failed preflight, timeout,
None result, rejection, partial fill or verification failure cannot release or
reset the reservation. There is no retry or automatic second authorization.

Successful entry requires the actual filled order, actual deal, and unique
position with both SL and TP. Status records lots, price, tickets, slippage,
commission/fee/swap and modeled risk at the fill. The entry process then stops.
Attached broker SL/TP remain active; there is no automatic manual/time exit.
Closing manually still uses its separate confirmation. Never delete usage or
execution-state files to retry an uncertain outcome.

Additional records:

- `logs/authorizations/<id>.json`: immutable approval scope and instruction.
- `logs/authorizations/<id>.usage.json`: permanent reservation and outcome.
- `logs/authorizations/<id>.revoked.json`: cancellation, when requested.
- `logs/monitor/status.json`: `mode=demo_once`, authorization, current decision
  and any verified execution or error. `execution_enabled=true` identifies the
  mode; it is not proof of broker acceptance or permission at a later time.
- [Single-entry implementation record](reports/market_analysis/demo_once_session_2026-11-13.md).

The live arming check returned WAIT with a verified DEMO account, no positions
or pending orders and no new sends. Actual unattended broker execution remains
unverified until a qualifying setup occurs. Offline tests cover both BUY and SELL
fills, one-use/restart behavior, failed sends, missing actual protection, account
changes, cancellation and expiry during the last native verification.

## MT5 connection check — start here

The environment inspected on 13 November 2026 has:

- Windows x64, CPython 3.14.6, and a project-local `.venv`.
- MT5 installed at `C:\Program Files\MetaTrader 5\terminal64.exe`, build 6295.
- MetaTrader5 Python package 5.0.6231 (the matching CPython 3.14 Windows wheel).
- Initial live `initialize()` result: **False**, error **-6, Terminal:
  Authorization failed**. This was resolved after manual sign-in inside MT5.
- The initial probe launched the terminal; `terminal64.exe` is now running.
  A running process does not establish an authorized broker connection.

### Latest verified Stage 1 result

On 13 November 2026 at 10:31 Manila time, the read-only script completed with
exit code 0 and `status=verified_demo_connection`:

| Item | Observed result |
| --- | --- |
| Broker | Exness Technologies Ltd |
| Server | Exness-MT5Trial17 |
| Account currency / type | USD / DEMO, verified from `trade_mode=0` |
| Available symbols | 357 |
| EUR/USD broker symbol | EURUSDm, verified by EUR base and USD profit currencies |
| Bid / ask | 1.10580 / 1.10588 |
| Spread | 0.00008 price units = 0.8 pips = 8 points |
| Quote timestamp | 13 November 2026 10:31:31.656 Asia/Manila (02:31:31.656 UTC) |
| Minimum / maximum / step volume | 0.01 / 200 / 0.01 lots |
| Terminal `trade_allowed` | False |
| External Python trading disabled flag | False |
| Orders sent | 0 |

These are historical observations from this specific check, not current quotes.
The returned tick was approximately 17.5 seconds ahead of the local clock, which
the connector flagged; clock synchronization needs checking before execution.
The initial post-login quote read failed bid/ask validation; later reads were
valid. Its exact transient cause was not established. Market-session availability
and order acceptance remain unverified. No permission settings were changed and
no `order_check()` or `order_send()` was called. This completes the requested
read-only stopping point; it does not demonstrate trade execution.

1. Open the locally installed MetaTrader 5 terminal.
2. Sign in manually to your **DEMO account inside MT5**. Do not put passwords in
   this repository, environment files, command-line arguments, or this chat.
3. Wait until MT5 shows its broker connection and quotes.
4. From this folder in PowerShell, run:

```powershell
.\.venv\Scripts\python.exe scripts/check_mt5_connection.py
```

If the broker has multiple EUR/USD variants, the script lists candidates and
stops. Choose the exact symbol explicitly:

```powershell
.\.venv\Scripts\python.exe scripts/check_mt5_connection.py --symbol EURUSD.a
```

Use `--terminal "C:\path\to\terminal64.exe"` for another installation. The check
makes one initialization attempt and never retries automatically. A nonzero exit
code means verification failed. Do not infer connectivity from the terminal
launching or from a successful Python import.

Outputs (local and ignored by Git):

- `logs/mt5_connection_status.json`: success/failure, safe account metadata,
  terminal permissions, EUR/USD bid/ask and timestamp when available.
- `logs/mt5_symbols.csv`: symbol inventory from a successful inspection.
- `logs/execution_log.csv`: connection, preview, submission, and verification
  audit events. Connection events have blank trade tickets; only verified
  execution events establish trade outcomes.

Old symbol inventory may remain after a failed run. Trust the status and timestamp
in `mt5_connection_status.json` before using any inventory file.

## What the MT5 modules do

| File | Role |
| --- | --- |
| `engine/mt5_connector.py` | Initialize/shutdown the local IPC connection; verify DEMO using `account_info().trade_mode`; read symbols and EUR/USD ticks |
| `scripts/check_mt5_connection.py` | One diagnostic run, console output, JSON/CSV audit files |
| `engine/demo_executor.py` | Preview, exact confirmation gates, fresh DEMO checks, one-shot send, persistent state, and actual order/deal/position verification |
| `engine/protection.py` | Broker-calculated, tick-aligned SL/TP risk budget and reward-to-risk calculations |
| `engine/execution_audit.py` | Flushed CSV audit records and atomic JSON state writes |
| `scripts/test_demo_trade.py` | Preview by default; opening and closing are separate explicit actions with separately typed phrases |

The official package sends requests to the **local MT5 terminal through IPC**
(inter-process communication). The terminal maintains the broker connection and
its login. Python does not need a password when using the terminal's existing
account. `shutdown()` disconnects Python; it leaves the terminal running.

The connector accepts only the vendor's `ACCOUNT_TRADE_MODE_DEMO` value. REAL,
CONTEST, missing, and unknown account types stop inspection. A server name
containing “Demo” is not evidence of account type. It also checks account identity
again after collecting quotes so a changed account invalidates the snapshot.

Quotes report their UTC timestamp, age, spread in price units, points, and pips
(EUR/USD: one pip = 0.0001). Quotes older than 120 seconds are warned as stale.
A quote alone cannot prove an open trading session or order availability.
Trading permissions are read and displayed; this code never changes them.

### One-shot demo test — preview and confirmation

The legacy draft is a **BUY** of the broker's minimum permitted EUR/USD volume,
at most **0.01 lots**. Its direction is arbitrary for execution testing; it is not
a strategy recommendation. The original test had no SL/TP and used a separately
confirmed close. Protected sessions below attach broker-managed SL/TP; submitting
one also authorizes closure when either level triggers. No entry is automatically
submitted, and manual early closing still requires separate confirmation.

```powershell
.\.venv\Scripts\python.exe scripts/test_demo_trade.py --preview
```

Review `logs/demo_order_preview.json`: account type, funds, exact broker symbol,
bid/ask, spread, quantity, filling mode, estimated margin/spread cost, and blockers.
The current demo balance was verified as **500 USD**, with zero open positions or
pending orders. A sampled 0.01-lot draft required about **11.06 USD margin** and
**0.08 USD spread cost**. These are observations, not fixed future execution prices.
Margin is collateral, not a maximum-loss estimate. Commission is unknown until
actual deals are retrieved.

The sampled preview was blocked by `terminal.trade_allowed=False` and a quote
about 17.7 seconds ahead of the PC clock. `w32tm /query /status` reported Windows
time as **not synchronized**, source **Local CMOS Clock**; the exact quote-clock
offset cause remains unconfirmed. An attempted `w32tm /resync` returned **Access
denied (0x80070005)**; no clock configuration was changed. Resolve both conditions before generating a
fresh, ready preview. This program does not change MT5 trading permissions.

Only after reviewing a **ready** preview, invoke the exact preview ID:

```powershell
.\.venv\Scripts\python.exe scripts/test_demo_trade.py --open --preview-id REVIEWED_ID
```

The application prompts for `PLACE DEMO TEST ORDER`. Confirmation is read from
standard input, never from a stored password or automatic-yes flag. The preview
expires after five minutes. Fresh checks must match its account/server, symbol,
side, size, and filling policy; spread must remain at or below two pips. Quotes
must be between five seconds ahead of and 120 seconds behind the PC clock.

Execution requires no existing positions or pending orders across the account.
The engine rechecks DEMO and identity immediately before sending, calls
`order_check()` first, and persists a send reservation **before** `order_send()`.
Successful order checks do not prove a fill or guarantee an open session. The
Python API does not expose the full broker session schedule; server rejection is
reported without retry. FOK is preferred; IOC is used only when advertised, with
partial-fill risk clearly displayed. The symbol's filling **bit mask** is mapped
to the request's distinct filling **enum**. Market Execution omits `price`, as
documented by MetaQuotes; `deviation` does not guarantee a slippage cap there.

An exclusive lock and `logs/demo_test_state.json` prevent another entry after a
send, timeout, exception, None result, partial fill, rejection, or uncertain
verification. There is **no automatic retry or reset**, including after a verified
close. Do not delete a state/lock file to bypass an uncertain result. Inspect the
terminal and audit records first.

Verification requires a matching **filled historical order**, actual matching
deal(s), and the actual position identity/volume/price. A DONE response alone is
insufficient. Safe read-only reconciliation can be rerun without sending:

```powershell
.\.venv\Scripts\python.exe scripts/test_demo_trade.py --verify open
.\.venv\Scripts\python.exe scripts/test_demo_trade.py --preview-close
```

After reviewing the separate close draft:

```powershell
.\.venv\Scripts\python.exe scripts/test_demo_trade.py --close --preview-id REVIEWED_CLOSE_ID
```

This prompts for `CLOSE DEMO TEST POSITION`. The opposite order identifies the
exact verified position ticket and unchanged size/identity. Closure is confirmed
by actual exit deals and absence of the original position. Slippage, commission,
fee, swap, and realized net P&L are recorded. Spread is embedded in deal P&L and
is not subtracted again. A closing attempt cannot be downgraded to an open state
by re-verifying the entry. The first broker-connected DEMO round trip has now been
completed and verified, as recorded below.

### Verified protected DEMO entry: 5% planned risk, 2:1 reward-to-risk

This historical test used **5% planned gross risk** and **2:1
reward-to-risk**, with a **0.01-lot maximum**. The current policy is the 1%
strategy-sizing flow above. For the historical minimum-size
trade, the budget sets the stop distance; the program does not silently increase
lots or claim the levels were selected by a trading strategy.

```powershell
.\.venv\Scripts\python.exe scripts/test_demo_trade.py --preview --session sltp-5pct-2r-20261113 --risk-percent 5 --reward-risk 2
```

The protected preview is saved under
`logs/sessions/sltp-5pct-2r-20261113/demo_order_preview.json`. It uses:

- **Risk basis:** the lower of balance and equity. At 499.90 USD, 5% is 24.995 USD.
  Rounding down to cents gives a **24.99 USD budget** rather than exceeding 5%.
- **Stop-loss:** a finite search over the broker's price ticks calls
  `order_calc_profit()` to find the furthest adverse tick within that budget.
- **Take-profit:** the nearest profitable tick reaching at least twice the
  calculated stop loss. The sampled target is **49.98 USD**.
- **Broker rules:** both SL and TP must be supported, prices must align to the
  tick grid, and BUY protection must satisfy minimum distances against bid.
  Invalid levels are rejected; the stop is never widened beyond the risk budget.

After the user's explicit `PLACE DEMO TEST ORDER` confirmation, the broker filled
one **BUY 0.01 EURUSDm at 1.10622** on 13 November 2026 at 11:29:20 Manila time.
`order_check()` returned **0 / Done**, then the single `order_send()` returned
**10009 / Done**. Verification matched filled order/position **2847116286** and
actual deal **2545621611**, and read the actual position's **SL 1.08123** (249.9
pips) and **TP 1.15620** (499.8 pips). Broker-calculated gross loss/gain from the
fill were **24.99 / 49.98 USD**, preserving **2:1** reward-to-risk. Entry spread
was **0.8 pips**, observed slippage was zero within price precision, and entry
commission, fee, and swap were zero. These prices describe this historical fill,
not a current quote or a strategy signal.

A second read-only check at 11:30:13 Manila confirmed exactly **one open position**,
no pending orders, both protective levels still attached, and exactly one opening
send attempt for this session. Balance was **499.90 USD**, equity **499.72 USD**,
floating P&L **-0.18 USD**, and margin **11.06 USD** at that snapshot. The position
remained open; a broker-triggered SL/TP exit has **not yet been observed**. Evidence
is retained in `logs/sessions/sltp-5pct-2r-20261113/demo_test_state.json`,
`logs/sessions/sltp-5pct-2r-20261113/open_position_snapshot.json`, and
`logs/execution_log.csv`.

Five percent per trade is a substantial allocation: five consecutive full 5%
losses would leave about 77.4% of starting equity before extra costs. These are
planned gross price risks. Fees, gaps, fill slippage, and swaps can change the
realized outcome; a stop loss is not a guaranteed dollar-loss cap. At only 0.01
lots, this budget creates a relatively wide stop and target. A 2:1 ratio does not
establish a profitable entry strategy.

Each new test uses an explicit named session to retain the original completed
test unchanged. All sessions share the log root's execution lock. Any other
unresolved attempt or any actual account position/order blocks new entries.
Completed session state is never reset or deleted automatically.

Legacy budget-derived previews remain available for inspection. New CLI entries
require `--strategy` under the current policy and the exact typed confirmation
bound to a ready preview ID. A completed historical session cannot be reopened.

The saved policy is restored automatically; it cannot be removed or changed
between review and execution. A fresh market quote may move entry, SL, and TP
together, while volume, policy, and reviewed maximum risk budget remain fixed.
Before sending, the checked absolute levels are evaluated again against the new
quote. If the quote increases planned risk or lowers the ratio below the approved
value, submission stops. There is no order retry or fallback to a bare order.

After a fill, the verifier reads **the actual position's `sl` and `tp`** and
requires both reviewed levels. It records risk/reward from the actual fill price,
including whether slippage increased risk above the planned budget; it does not
silently move the stop after entry. Missing protection causes an immediate
verification error requiring inspection. The signal monitor does not manage
positions or send corrective orders.

When the broker later triggers a stop or target, reconcile its actual exit with:

```powershell
.\.venv\Scripts\python.exe scripts/test_demo_trade.py --verify close --session sltp-5pct-2r-20261113
```

This is read-only. It requires the original position to be absent, matching exit
deal volume, broker-reported SL/TP deal reason, and corresponding filled orders.
No synthetic `order_send` result is invented for the broker-triggered exit. A
manual early close still uses `--preview-close`/`--close` and its separate typed
confirmation. Manual terminal intervention or an unresolved/partial exit requires
inspection rather than an automatic state reset.

### Verified demo execution — 13 November 2026

The user separately confirmed opening and closing. Algo Trading was enabled
through the MT5 toolbar and verified through the Python API. Before entry, the
quote clock was within the freshness window and the account had no positions or
pending orders. No additional clock configuration change was needed in that run.

| Item | Actual verified result |
| --- | --- |
| Account | Exness-MT5Trial17, DEMO from `trade_mode=0`, USD |
| Starting balance | 500.00 USD |
| Instrument / size | EURUSDm / 0.01 lots |
| Entry | BUY at 1.10599, 13 November 2026 11:01:43.794 Manila time |
| Opening order / deal | 2846998088 / 2545515037 |
| Position ticket / identifier | 2846998088 |
| Exit | SELL closing the same position at 1.10589, 11:04:47.522 Manila time |
| Closing order / deal | 2847012332 / 2545527951 |
| Realized net P&L | -0.10 USD |
| Commission / fee / swap | 0.00 USD on the recorded entry and exit deals |
| Slippage | Effectively zero on entry; zero on exit relative to the send reference |
| Quoted spread at entry and close | 0.8 pips |
| Final balance / equity / free margin | 499.90 USD / 499.90 USD / 499.90 USD |
| Final margin / positions / pending orders | 0.00 USD / 0 / 0 |

Actual historical filled orders, matching deals, and position state were checked.
The final flat account was separately retrieved from MT5. Full audit evidence is
in `logs/demo_test_state.json`, `logs/demo_test_final_account.json`, and
`logs/execution_log.csv`; these local files are ignored by Git.

**Verification issue and repair:** `history_deals_get(ticket=order_ticket)`
returned `None` with error `-2, Terminal: Invalid params`, despite valid order and
position records. The exact reason for that overload failure is unconfirmed.
A documented UTC interval query returned the expected actual deal. The verifier
now falls back to that read-only query and still matches the exact order, deal,
symbol, magic, direction, position identity, and volume. This is a history-read
fallback, never an order resubmission. The native issue occurred for both entry
and exit; both were verified through actual interval-query records.

The close `order_check` response also contained an inconsistent negative
`margin_free` despite `retcode=0`. Its underlying cause is unconfirmed. The actual
post-close account was independently checked: 499.90 USD free margin, zero margin,
and no positions/orders. The raw response is preserved rather than presented as
the account's actual funds. This test establishes the demo execution connection;
it does not evaluate a trading strategy's profitability.

## Install / reproduce the environment

Use `py`, since this computer's `python` command points to the Windows Store alias.
No activation is required:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-mt5.txt
```

`requirements.lock.txt` pins the complete tested Windows environment. To reproduce
the same versions on a compatible Python/Windows installation:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
```

The MT5 package is optional for historical backtesting and its normal tests.

## Existing historical research baseline

The earlier assignment's custom daily, single-instrument backtester is available:

```powershell
.\.venv\Scripts\python.exe main.py --demo
.\.venv\Scripts\python.exe main.py --symbol SPY --start 2023-01-01 --end 2024-01-01
.\.venv\Scripts\python.exe main.py --csv .\prices.csv --short 10 --long 30
.\.venv\Scripts\python.exe main.py --dataset SNAPSHOT_ID
.\start.ps1
```

The dashboard is at `http://127.0.0.1:8501` while running. If script execution is
restricted, use:

```powershell
.\.venv\Scripts\python.exe -m streamlit run dashboard/app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

Historical CSV columns: `date,open,high,low,close,volume`. Dates are increasing,
unique, timezone-naive daily dates. Bad OHLCV rows fail visibly. Signals use only
trailing averages and fill at the **following bar's open**. The first full SMA
window has no previous relationship, so it does not invent a crossover. The
last-bar signal is recorded as unfilled. Positions are long-only whole shares;
capital allocation reserves entry fees. It is a share-based simulator, **not a
forex-lot or futures-contract execution model**.

For reference, with 1,000 capital, a 100 opening price, 100 bps commission plus a
1 fixed fee, 20 bps full spread, and 5 bps slippage: the simulated buy price is
100.15; only 9 whole shares are affordable after fees. Spread/slippage alter fill
prices, while commission changes cash separately. Costs are not charged twice.

Completed-trade metrics exclude open positions; final equity marks them at the
last close without future exit fees. Sharpe uses per-bar excess simple returns,
sample standard deviation, and `sqrt(252)` annualization by default, including
warmup/flat bars. The annual risk-free rate is geometrically converted; cash earns
no interest. Drawdown is measured from a running peak seeded by initial capital;
average drawdown includes zero-drawdown bars. Undefined Sharpe/profit factor stays
explicitly undefined rather than being replaced with zero.

Each backtest bundle includes market bars, equity, fills, completed trades, order
decisions, assumptions, code/data fingerprints, package versions, and checksums.
Historical data snapshots are immutable CSVs indexed by SQLite. Choose a saved
snapshot to avoid another Yahoo download. Yahoo OHLC is adjusted explicitly;
those are synthetic historical prices, and dividends are not credited separately.

See [architecture and research limitations](docs/ARCHITECTURE.md). Synthetic demo
returns do not demonstrate real profitability. Current-ticker selection creates
survivorship risk; repeated tuning can overfit. There are no liquidity guarantees,
intrabar fills, taxes, or corporate-action cash ledger.

### Market-infrastructure request status

The EUR/USD and gold collection/indicator/report expansion was superseded by the
MT5 Stage 1 request before it was implemented. Multi-timeframe collection, RSI,
MACD, instrument-specific charts, and `reports/latest_market_snapshot.md` are
**not yet implemented**. `XAUUSD=X` returned a quote-not-found/timezone error in a
preliminary probe; it was not silently replaced with `GC=F`. The later explicit
MT5 work now verifies broker `XAUUSDm` H1 and M5 Gold CFD snapshots separately;
see the Gold runbook above. That does not implement the original Yahoo chart
and indicator dashboard request.

## Tests and troubleshooting

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

Automated tests use deterministic synthetic data and fake MT5 responses; they
never log in or submit an order. They cover bad data, costs/cash reconciliation,
next-bar timing, metrics, snapshot replay, report checksums, Streamlit runs, and
DEMO verification failures. A successful test suite does not prove a real terminal
connection or demo trade execution.

The latest full run passed **949 automated tests in 63.12 seconds**, including
the one-each portfolio scope, 2% combined planned-risk ceiling, controller waiting
windows and cancellation, manual-override journal classification, and Gold
contract/data validation, M5/M15 causal rules, 1R-4R native sizing, BUY/SELL
fake-broker entry/exit/export, bounded sessions and EUR/USD regression coverage.
No actual Gold execution was attempted. Coverage also includes native BUY/SELL
4R selection, cross-version rejection, full fake-broker entry/exit/export and
offline historical regression. Ten pandas deprecation
warnings concern explicitly retained round-trip float precision. Coverage includes
the Monday date/account/scope guards, adaptive native sizing/evidence and
**47 monitor tests**,
38 single-entry authorization/session cases, **29 sequential-session cases**, **21 journal cases**, **16 holding-period cases**,
**29 compounded-account research cases**, **57 candidate-signal/statistics cases**,
**100 new adaptive-target/paired-replay/planner-hook cases**,
three monitor/executor integration tests, the strategy sizing/execution checks
and **17 offline setup-research tests**. Coverage includes causal H4/H1
alignment, lot affordability, ATR seeding, cash/risk calculations, tick rounding, broker stop
distances, missing actual SL/TP, movement between checks, session preservation,
unresolved-attempt guards, broker-triggered exit reconciliation, and preview-only
CLI behavior. The separately confirmed protected DEMO entry also passed its
broker check, filled once, and was verified against actual history and the open
position's SL/TP, as recorded above. Broker-triggered exit reconciliation has
automated test coverage but has not yet been validated against an actual exit.
`pip check` found no
broken requirements, and Python compilation completed successfully. Live checks
after manual sign-in verified the DEMO account, symbol inventory, and EUR/USD
bid/ask. Actual deal and position execution was subsequently verified for the
separately confirmed DEMO round trip documented above.

- `-6 Authorization failed`: inspect the account login/status **inside MT5**;
  confirm a demo account and correct server. No specific credential problem has
  been established by this error alone.
- `-10005 IPC timeout`: check terminal startup, login, path, and build. IPC means
  Python is waiting for the local terminal. It is different from an order error.
- Missing package: invoke `.venv\Scripts\python.exe` and install
  `requirements-mt5.txt` in that same environment.
- Wrong/unknown account type: stop and switch manually to a DEMO account; do not
  weaken the guard or rely on a “Demo” server label.
- Missing EUR/USD: inspect `logs/mt5_symbols.csv` from the latest successful
  snapshot and specify the exact broker symbol with `--symbol`.
- Stale quote: inspect its timestamp, broker connectivity, and session. Do not
  treat the last price as proof that a new order would be accepted.
- Algo Trading disabled: enable the terminal's permission manually for this demo
  test, then generate another preview. The application never changes it.
- Unsynchronized PC time: use Windows Date & time settings to synchronize, then
  recheck the quote age. Do not weaken the freshness guard to hide the issue.
- Uncertain send/close: inspect local state and MT5 history; use `--verify` only
  for the recorded phase. Never retry the order or remove its reservation blindly.

## Strategy review and forward observation — 16 November 2026

The one-bar follow-through entry experiment was tested and **not adopted for
orders**: it admitted too few affordable $500 trades and performed poorly in
the $1,000 comparison. Existing immediate trend/reclaim entry and risk rules
remain the DEMO baseline. See the [comparison report](reports/market_analysis/entry_confirmation_2026-11-16/report.md).

The current controller also runs a separate, read-only forward comparison.
Its results appear in [latest_strategy_comparison.md](reports/latest_strategy_comparison.md);
exact inputs/events are under `logs/observations/closed_bar_followthrough_v1/`.
The observer cannot place orders or modify sizing/targets. Signals are technical
observations, not evidence of execution or profitability.

Status/audit publication now uses unique temporary files and bounded Windows
sharing-lock waits; persistent failures still stop execution, and broker orders
are never automatically retried. See the [maintenance report](reports/strategy_updates/confirmation_2026-11-16/report.md).

## Future strategy integration

Keep strategy signals independent of broker execution. A later demo adapter can
translate an approved signal into a broker-specific order draft, validate current
DEMO identity/positions/volume/permissions, and require confirmation. It needs a
persistent order/deal/position state machine to reconcile ambiguous responses and
avoid duplicate sends; never retry an uncertain order automatically. The existing
historical engine remains a separate simulation until broker lot sizing,
contract economics, and execution assumptions are explicitly modeled.

## Official references

- [Python/terminal integration](https://www.mql5.com/en/docs/python_metatrader5)
- [initialize and terminal account reuse](https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py)
- [Account trade-mode enums](https://www.mql5.com/en/docs/constants/environment_state/accountinformation)
- [EUR/USD tick response](https://www.mql5.com/en/docs/python_metatrader5/mt5symbolinfotick_py)
- [Order check and its limitations](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercheck_py)
- [Order send, Market Execution price, and position ticket](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py)
- [Symbol filling flags](https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants)
- [Actual deal history](https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py)
- [Account-currency profit calculation](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py)
- [Broker-reported SL/TP exit reasons](https://www.mql5.com/en/docs/constants/tradingconstants/dealproperties)
- [yfinance download conventions](https://ranaroussi.github.io/yfinance/reference/api/yfinance.download.html)
- [Streamlit app testing](https://docs.streamlit.io/develop/api-reference/app-testing/st.testing.v1.apptest)
# Two-setup DEMO experiment (18 November 2026)

The earlier two-setup DEMO profile evaluated **A: trend/EMA20 reclaim** and **B: trend-aligned prior-20-bar sweep/rejection**, under its then-current one-position-per-instrument limit. A had priority on same-direction overlap; opposite signals skipped both. More activity did not establish profitability: that historical combined $500 model completed 63 trades and ended at $425.62, versus 33 trades and $489.14 for A alone. The current five-family profile and expanded position scope are described at the top of this README; those earlier figures are not a backtest of the new profile.

See the [setup rules and operating guide](docs/TWO_SETUP_DEMO.md), [forward A/B observations](reports/latest_setup_comparison.md), [historical comparison](reports/market_analysis/two_setups_2026-11-18/report.md), and [actual trade journal](reports/trade_journal.md). The existing installed-app activation controls use the updated workspace engine; its bundled historical Replay page remains the older A-only model.

