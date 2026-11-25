# Project Quant architecture and implementation stages

## Scope and sequence

This repository implements the first historical research milestone only. There
are no broker connections, live orders, paper trading, ML, or optimizers.

1. Data foundation: validate daily OHLCV, read CSV, explicitly download adjusted
   Yahoo data, create deterministic synthetic data, and snapshot data locally.
2. Backtesting: generate causal SMA crossover signals; execute on the following
   bar's open; size whole-share positions within available cash including costs;
   record executions, closed trades, outstanding positions, and daily equity.
3. Application: expose the same research workflow in a CLI and Streamlit; export
   data and run assumptions; verify with analytical examples and integration tests.

The requested top-level modules are retained. Small extra modules isolate
configuration (`engine/config.py`), storage (`engine/storage.py`), and workflow
and exports (`engine/research.py`) so the dashboard and CLI share calculations.
`breakout.py` and `mean_reversion.py` reserve the requested extension points; no
unvalidated strategies are presented as available.

## Component contracts

| Component | Input | Output / connection |
| --- | --- | --- |
| Data loader | CSV or one ticker and date range | Validated daily OHLCV DataFrame |
| SQLite data store | Valid bars and source metadata | Immutable CSV, SHA-256, snapshot ID, provenance |
| Strategy | Historical OHLCV, SMA windows | Indexed events: buy=1, exit=-1, hold=0 |
| Risk manager | Cash, actual simulated buy price, fees, allocation | Affordable integer quantity |
| Execution model | Side, next open, quantity, signal/execution dates | Adverse fill price and explicit commission/spread/slippage |
| Backtester | Valid bars, strategy, immutable configuration | Equity, fills, completed trades, order decisions, open position |
| Analytics | Daily equity, completed trades, fills, assumptions | Metrics and drawdown |
| Research workflow | Data/provenance and configuration | Reusable run and auditable exported artifacts |
| Dashboard / CLI | User inputs | Same engine results and exports |

## Key design decisions

- **Event signals instead of target weights:** a crossover produces one event.
  The execution engine owns position state. A rejected buy is not retried every
  day; another true crossover is required.
- **Next open instead of same close:** a close-derived signal is known only after
  that close. Using the next observed open makes that time ordering explicit.
  A final-bar event is recorded as unfilled because its next price is unknown.
- **Mark open positions instead of forced liquidation:** final equity includes
  their final close value. Closed-trade metrics exclude them. Final equity does
  not reserve estimated future liquidation fees.
- **Adjusted prices instead of a corporate-action engine:** Yahoo's adjusted
  OHLC supports a simple consistent research baseline. Prices are synthetic,
  share counts are model units, and dividends are not separately credited.
  Accurate cash flows and executable historical prices need a later explicit
  splits/dividends model. CSV adjustment conventions are the user's responsibility.
- **Snapshots instead of automatic cache refresh:** explicit Yahoo downloads can
  create a new snapshot; saved snapshot selection supports repeatable research.
  Hash verification detects accidental edits; it is not a security signature.
- **Strict validation instead of automatic repair:** bad bars fail visibly.
  Missing sessions require an exchange calendar and are outside this milestone.

## Research limitations

Daily bars cannot establish intrabar order, market depth, trading halts, auction
fills, or actual spread. Costs are deterministic assumptions, not observed quotes.
There is no volume participation cap. One entire next-open fill is assumed and
may be unrealistic for illiquid instruments or large quantities.

Ticker selection can introduce survivorship bias: current symbols exclude many
delisted instruments. This app does not supply a point-in-time universe. Repeated
window tuning can overfit; retain an untouched chronological holdout and record
all experiments before drawing conclusions. Synthetic demo behavior provides no
evidence that a strategy will be profitable on real markets.

All displayed and exported results are simulated, never actual trading returns.
