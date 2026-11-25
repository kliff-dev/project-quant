# Bitcoin: four research-backed rule families

Reviewed 22 November 2026. Frozen implementation: `analytics/btc_setups.py`,
version `btc_research_v1`. The parameters below were chosen before inspecting
their local replay outcomes. They have not been searched or optimized.

These are **paper-inspired hypotheses**, not four proven profitable systems.
Published results justify testing their families. They do not establish that
our parameters, broker, sample, short trades, stops or 2R targets have an edge.
The engine performs deterministic local calculations, without a trained AI
model, paid AI API or automatic parameter changes.

## Primary evidence

### Gerritsen, Bouri, Ramezanifar and Roubaud (2020)

[The profitability of technical trading rules in the Bitcoin market, Finance Research Letters 34, 101263](https://doi.org/10.1016/j.frl.2019.08.011).
[Author-hosted full paper](https://dirkgerritsen.nl/uploads/gerritsen_et_al_2020_bitcoin_trading_rules.pdf).

The paper studies seven technical-indicator families on daily Bitcoin prices
from 2010–2018. Trading-range breakouts were its main favorable finding,
particularly in trending conditions. Its moving-average appendix defines
short/long simple-average crossover rules. The tested daily MA pairs and
50/150/200-day ranges differ from our hourly settings. The conclusion leaves
transaction-cost inclusion to future work. This supports investigating B1/B2,
but cannot validate our net CFD returns or a 2R exit.

### Deprez and Frömmel (2024)

[Are simple technical trading rules profitable in bitcoin markets?, International Review of Economics & Finance 93, 858–874](https://doi.org/10.1016/j.iref.2024.05.003).
[Ghent University author manuscript](https://backoffice.biblio.ugent.be/download/01HY3C3S169G1N6QNYR55NZMFB/01HY60XZGZYHNQ6188MSVJT0SG).

This study evaluates 75,360 rules across six families and daily, 60-, 30- and
10-minute prices. It uses Bitstamp trades from 2012–2022, exchange fees and
bid/ask spreads, multiple-testing controls and subsequent out-of-sample
portfolios. Moving averages, filter rules and support/resistance are included.
Some portfolios improved risk-adjusted performance; an individual attractive
rule is not sufficient evidence. The implementation is long-or-cash because
the sampled venue did not support shorting. Its family definitions motivate
B1–B3, and its methodology motivates separate costs and chronological testing.
Our symmetric CFD shorts, fixed windows, structural stops and 2R target are
additional hypotheses; the paper did not verify them.

### Liu and Tsyvinski (2018; published 2021)

[Risks and Returns of Cryptocurrency, NBER Working Paper 24877](https://www.nber.org/papers/w24877).
[Working paper PDF](https://www.nber.org/system/files/working_papers/w24877/w24877.pdf).

The authors report historical cryptocurrency-specific time-series momentum,
including daily and weekly horizons. Their weekly-return analysis studies
subsequent multi-week returns, with an out-of-sample exercise. It motivates
testing a momentum family, not treating an indicator as a forecast guarantee.
B4's hourly evaluation of a seven-day return, zero-cross threshold, matching
12-hour confirmation, symmetric shorts and 2R exit are our adaptations. They
are not the paper's tested strategy.

## Frozen setup definitions

All entries use **completed H1 candles**. Timestamps identify candle OPEN;
the decision time is OPEN + one hour. `BUY`/`SELL` below describe technical
directions only. A broker candidate still has to pass DEMO-account, fresh
quote, permissions, lot, spread, margin, portfolio and position checks.

| ID | Name | BUY rule | SELL rule | Evidence boundary |
|---|---|---|---|---|
| B1 | Moving-average crossover | SMA20 becomes strictly greater than SMA100; previously it was equal or below. | SMA20 becomes strictly lower than SMA100; previously it was equal or above. | MA family supported for investigation by the 2020 and 2024 studies; these hourly lengths are unvalidated. |
| B2 | Trading-range breakout | Close becomes strictly greater than the maximum HIGH of the preceding 48 H1 candles. | Close becomes strictly lower than the minimum LOW of the preceding 48 candles. | Breakout family supported by both studies; our 48-hour OHLC channel and exits differ. |
| B3 | One-percent price filter | Close newly exceeds 1.01 × the maximum CLOSE of the preceding 24 H1 candles. | Close newly falls below 0.99 × the minimum CLOSE of the preceding 24 candles. | Filter-family inspiration from the 2024 study; a fixed-window threshold breakout differs from a running-extreme filter rule. |
| B4 | Weekly time-series momentum | Exact 168-hour close return crosses from zero/negative to positive, and exact 12-hour return is positive. | The 168-hour return crosses from zero/positive to negative, and 12-hour return is negative. | Momentum-family motivation from Liu/Tsyvinski; our thresholds, confirmation and H1 execution are unvalidated. |

“Newly” means the condition is false on the previous completed candle and true
on this one. The current signal candle is excluded from B2/B3 reference
windows. Remaining above a level does not fire a new entry every hour.
Repeated signals later in history are permitted after the condition resets.

These are related trend-following families. Four names do not provide four
independent sources of risk diversification. Simultaneous same-direction
signals share deterministic priority B1, B2, B3, B4; opposing enabled signals
cause no candidate. A planner rejection must not trigger a fallback to the
next setup on the same candle.

## Stop, target and data safeguards

- A BUY stop is the lower of `lowest LOW of last 5 candles − 0.2 ATR14`
  and `signal CLOSE − 1.5 ATR14`. A SELL stop mirrors the formula above price.
  ATR14 uses Wilder's average with an arithmetic 14-range seed.
- The stop is frozen from completed information. The target starts at **2R**;
  actual executable entry, stop rounding, fees, spread and slippage determine
  monetary risk. A 2R price target does not mean realized net winners always
  pay twice realized net losses.
- At least 300 observed completed bars initialize the scanner. All setups
  need six recent uninterrupted H1 candles, positive ATR and a non-flat
  latest candle. Older broker maintenance gaps do not restart 300-bar warmup.
- B2 needs 50 uninterrupted candles, B3 needs 26, including their prior-
  condition comparisons. No carried-forward bars fill gaps. B1 averages
  observed candles; a historical missing hour is not an invented quote.
- B4 uses exact timestamp anchors at 168 and 12 hours. A missing anchor blocks
  B4 instead of silently replacing a week with 168 observed rows.
- Invalid/duplicate/unordered timestamps and invalid completed OHLC fail
  validation. Future/forming candle values cannot enter an earlier decision.
- The pure scanner retains historical observations, but `evaluate()` refuses
  a latest candle older than two hours. The execution worker applies its own
  much tighter fresh-signal/quote checks before any order.

## Instrument and validation boundaries

`BTCUSDm` in MetaTrader is the broker's Bitcoin CFD, with broker-specific
contract size, spread, hours and financing. It is not a wallet balance or a
spot exchange trade. `BTCUSDT` external exchange data, if imported, is a
separate instrument and source; it must not provide a supposedly executable
BTCUSDm bid/ask. Candle volume may be tick activity rather than exchange
Bitcoin volume. None of these four rules requires volume.

Before calling a setup successful, evaluate net expectancy, drawdown,
trade count and turnover using spread, commission, adverse slippage,
financing and gaps where available. Keep development and later chronological
validation periods separate. Record costs that are estimates rather than
actual broker charges. A held-out period inspected repeatedly becomes
development evidence; forward DEMO data remains necessary.

The offline tests check completed-bar causality, warmup, duplicate/invalid
data, gap windows, exact momentum anchors, strict thresholds, stop geometry,
staleness and arbitration. These establish implementation behavior, not
profitability or a completed broker fill.
