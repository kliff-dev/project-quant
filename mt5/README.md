# Reading our strategy in MetaTrader 5

Installed and visually checked on 16 November 2026. Start with the four dark
Project Quant charts; the three existing charts were preserved.

| Market | Entry chart | Trend chart | Entry RSI bands |
| --- | --- | --- | --- |
| Gold, broker `XAUUSDm` CFD | M5 | M15 | BUY 50–68; SELL 32–50 |
| EUR/USD, broker `EURUSDm` | H1 | H4 | BUY 50–65; SELL 35–50 |

## Technical Summary

The setup is **trend following with an EMA20 pullback/reclaim**. An EMA is an
average that gives recent closes more weight. Blue is EMA20, orange EMA50 and
purple EMA200. The entry charts show EMA20/50, RSI14 and Wilder ATR14; the trend
charts show EMA50/200 and Wilder ATR14. Each chart has a small rules panel.

Read the trend chart first. For a BUY, EMA50 must be above EMA200, the completed
close must be above EMA50, and EMA50 must have risen over five completed bars.
For a SELL, all three conditions reverse. This uses moving averages rather than
manually drawn trend lines.

Then read the entry chart:

1. A BUY needs the preceding close at/below its EMA20, followed by a bullish
   completed candle closing above both EMA20 and EMA50. Its RSI must be in the
   market's BUY band, with the higher-timeframe BUY trend confirmed.
2. A SELL needs the preceding close at/above its EMA20, followed by a bearish
   completed candle closing below both averages. Its RSI must be in the SELL
   band, with the higher-timeframe SELL trend confirmed.
3. ATR measures recent price movement. It helps the Python strategy assess
   volatility and place a structure-based stop. A high ATR does not predict
   whether price will rise or fall.

For example, Gold can bounce above the blue line on M5 while M15 still has a
falling orange line below purple. That bounce alone does not qualify as a BUY.
Likewise, RSI entering a band alone does not create an order. This RSI is a
momentum confirmation, not an automatic oversold/overbought reversal signal.

The far-right candle is still forming; its values can change. The monitor uses
**completed candles**. MACD is not part of the current entry rule and was not
added. Old trade markers already shown by MT5 are execution history, not new
signals produced by these indicators.

## Root Cause

Python calculates strategy values separately from MT5's chart display. Those
calculations do not automatically attach visible indicators to a terminal chart.
The existing charts therefore did not explain the monitor's entry rules.

## How It Was Diagnosed

The display settings were derived from the actual rules in
[`gold_strategy.py`](../analytics/gold_strategy.py),
[`candidate_signals.py`](../analytics/candidate_signals.py) and the active
activation policy. The terminal's existing charts and running monitor status
were inspected. The custom displays use the same EMA recurrence and arithmetic
seed followed by Wilder smoothing for RSI/ATR as those Python calculations.

## Solution

The local MQL5 files in `Indicators/ProjectQuant/` implement the three displays.
`Scripts/ProjectQuant/PQ_Strategy_Charts.mq5` opens four charts, attaches the
indicators, adds labels, and saves a template for each chart. It reuses its own
charts when run again. No order, account, login, network or DLL APIs are used by
these files. The Python execution rules, sizing and authorizations were unchanged.

The chart helper uses MT5's documented
[ChartIndicatorAdd](https://www.mql5.com/en/docs/chart_operations/chartindicatoradd)
and [ChartSaveTemplate](https://www.mql5.com/en/docs/chart_operations/chartsavetemplate)
functions. Sources were compiled with the existing MetaEditor installation using
its [command-line compiler](https://www.metatrader5.com/en/metaeditor/help/beginning/integration_ide).

### What was verified

- All four MQL5 sources compiled successfully with zero errors and zero warnings.
- Each of the four charts was opened and visually checked in the actual terminal:
  correct timeframe, moving averages, RSI bands where applicable, ATR and labels.
- The terminal-generated indicator audit and four saved templates were inspected.
- Re-running the helper reused the same four chart IDs without creating duplicates.
- All 45 approved trading-source fingerprints still passed verification.
- The existing Python process remained active with a fresh monitoring heartbeat.

This is compilation, source inspection and actual UI validation. No automated
test suite was added or rerun for the display-only change. Exact numeric parity
between every native chart value and a frozen Python decision snapshot was not
tested. No trade was placed to validate the display.

### Limits of the visual guide

The chart indicators use MT5's loaded chart history, including the forming bar.
The Python strategy reseeds from bounded closed-bar windows (1,200 M5 bars for
Gold; 3,000 H1 bars in the current EUR/USD monitor), and builds its completed
higher-timeframe context from that input. History length and native bar grouping
can change the displayed values, especially EMA200. This is a visual guide, not
an exact copy of the Python decision engine. The monitor's saved input and status
remain authoritative when investigating a decision.

Spreads, shock candles, volatility limits, confirmed swing room, target selection,
lot affordability, permissions and combined risk can still prevent an order.
Adaptive targets up to 4R are selected by Python; these lines do not display a
future entry or pre-authorize a target. Normal sizing remains within 1% planned
risk; the separately authorized qualified minimum-lot exception can reach 2%,
with the combined account ceiling still 2%.

Actual position SL/TP levels are enabled for display by MT5 when present; there
was no open position during this verification. Changing a chart timeframe or
indicator setting does not change the Python strategy configuration.

## What I Learned

- Check direction on the slower chart before looking for an entry on the faster one.
- A moving-average cross or RSI reading is only part of the complete setup.
- ATR is measured in quote-price units: Gold dollars per ounce; EUR/USD price
  units (divide by 0.0001 for pips). ATR is not the full stop distance.
- Compare the last completed candle with the monitor's decision timestamp when
  investigating a possible signal.

## If This Happens Again

Find the tabs `XAUUSDm,M5`, `XAUUSDm,M15`, `EURUSDm,H1`, and `EURUSDm,H4` with
the Project Quant labels. Press **Ctrl+I** on a chart to inspect its indicator list.
Templates are named `ProjectQuant_GOLD_M5`, `ProjectQuant_GOLD_M15`,
`ProjectQuant_EURUSD_H1`, and `ProjectQuant_EURUSD_H4` in the chart's Templates menu.
Applying a saved template replaces the selected chart's display settings, so use
the corresponding Project Quant chart.

The helper is under **Navigator → Scripts → ProjectQuant → PQ_Strategy_Charts**.
It creates missing Project Quant charts and refreshes their labels; existing
owned charts retain their indicators. If an indicator was manually removed,
apply the corresponding saved template. Inspect the Experts/Journal messages
if loading fails. These visual indicators do not require changing Algo Trading
permissions.

Installed sources and `.ex5` files are under the active terminal's
`MQL5\Indicators\ProjectQuant` and `MQL5\Scripts\ProjectQuant` folders. Templates
are under `MQL5\Profiles\Templates`; the native audit is
`MQL5\Files\ProjectQuant_Charts_setup.txt`. The local verification record is
[`mt5_chart_setup_verification_2026-11-16.json`](../reports/mt5_chart_setup_verification_2026-11-16.json).

If the chart seems to show a setup but no trade occurs, inspect
[`latest_monitor_status.md`](../reports/latest_monitor_status.md) and
[`latest_dual_monitor_status.md`](../reports/latest_dual_monitor_status.md) for the
actual rejection reason. Do not relax the execution rules to make the display
and monitor agree.
