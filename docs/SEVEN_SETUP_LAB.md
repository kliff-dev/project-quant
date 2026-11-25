# Seven-setup research lab — seven_lab_v1

Open the installed Project Quant 0.5.0 app and choose **Seven-setup research lab**.
Choose a provider, instrument and timeframe, then Manila start/end dates. The charts show trigger frequency
and weekly history; the table/download lists dated BUY/SELL signals. Choose a
signal on the **Signal history** tab to see its completed candle.
**Latest saved research decision** explains whether each rule just triggered,
remains in a continuing condition, has no pattern, or failed a shared guard.

These seven hypotheses are designed to be reproducible with local calculations,
not to claim an established edge. They are new, independent, single-timeframe
research rules. R1/R2 share concepts with native A/B, but they use EMA trend on
the entry timeframe and full-history indicator seeding. Native A/B uses bounded
H4/H1 EUR/USD and M15/M5 Gold context. **Do not equate these signal counts with
the native trading policy's frequency, risk eligibility or broker fills.**

| ID | Setup | Environment / trigger |
|---|---|---|
| R1 | Trend EMA reclaim | Trend, pullback recovers EMA20 with candle/RSI confirmation |
| R2 | Trend liquidity sweep | Trend, prior 20-bar extreme is pierced then recovered |
| R3 | Donchian breakout | Trend, close crosses previous 20-bar channel |
| R4 | Compression breakout | Low prior band width, close breaks previous channel |
| R5 | Range band re-entry | Flat trend regime, rejection of previous 2σ band |
| R6 | London opening-range breakout | Complete 08–09 London range; breakout candle opens 09–12 |
| R7 | Trend RSI momentum return | Trend, RSI crosses back through 50 |

The exact symmetric BUY/SELL definitions are stored in `analytics/setup_lab.py`
and displayed in the app. EMA uses adjust=False; standard deviation uses ddof=0;
ATR/RSI use the existing arithmetically seeded Wilder calculations. All series
are seeded from the dataset start; changing available warmup can change later
values. No parameter search, model fitting or target tuning was performed.

Shared guards: 250 retained quote candles, six recent consecutive bars, ATR14
divided by the trailing 100-ATR median between 0.5 and 2, current range ≤3 ATR.
Zero-range OHLC candles are preserved in raw files but excluded from indicator
inputs. They often represent carried-forward closed-market prices; gaps created
by their removal reset recent continuity. Legitimate unchanged quote candles
would also be conservatively excluded. No gap is filled with synthetic prices.

Only completed candles are read. Rolling extrema/bands exclude the signal
candle. The London range cannot be used until its complete opening hour exists;
Europe/London daylight saving is handled by timezone conversion. R6 permits one
trigger per direction per local date; other rules record a transition into a
directional condition, rather than every persistent match. Simultaneous signals
are retained and marked; they are correlated opportunities, not separate
independent bets. The seven rules never grant execution authority.

## Sources and updates

- **Exness MT5:** EURUSDm H1 OTC forex; XAUUSDm M5 broker Gold CFD (100 oz/lot).
  **Refresh MT5 research data** reads up to 184 days from the existing signed-in
  pinned DEMO account and verifies the Gold contract/bid chart. Actual retrieved
  coverage is displayed. MT5's terminal history availability can restrict it.
  It never calls order_check/order_send or activates monitoring.
- **Dukascopy:** EURUSD H1 and XAUUSD M5 independent OTC forex/metal BID feed.
  This is not the Exness executable quote, an exchange feed, or COMEX futures.
  Downloaded through pinned `dukascopy-node@1.51.1`, an independently maintained
  third-party open-source tool. Dukascopy itself offers free historical export.
  Node.js/npx is needed only for the optional command-line download; the app can
  import its CSV without Node. No key or paid plan is required. Download failures
  stop explicitly; no replacement instrument/provider is substituted.
- Forex/metal provider volume is not consolidated exchange volume. These rules
  do not use volume. Bid OHLC cannot establish actual transaction costs or exact
  tick ordering. Historical ask, commission, swap and broker admission are not
  modeled by this frequency scanner.

```powershell
.venv\Scripts\python.exe -B scripts/refresh_setup_lab.py --provider mt5
.venv\Scripts\python.exe -B scripts/refresh_setup_lab.py --provider dukascopy --start 2026-05-22 --end 2026-11-22
.venv\Scripts\python.exe -B scripts/refresh_setup_lab.py --provider saved
```

The Dukascopy end date is exclusive UTC. The downloader caches its input files
and reuses a completed date-range CSV. Remove/choose a new range explicitly if a
provider later revises that file; the saved file is not assumed immutable at the
provider. The app refreshes on demand; there is **no new continuous seven-setup
collector or seven-setup execution service**. Refresh after new candles close.
Offline calculations and the saved histories work without a connected terminal.

Each normalized dataset has a content checksum and provenance manifest under
`data/research/setup_lab`. Unchanged data is reused; changed data creates a new
snapshot. Provider identities and prices are never merged. The app defaults to
the newest dataset per provider/instrument/timeframe and can show older snapshots.
CLI scans export full signals, summary and coverage into `reports/setup_lab`.
Event IDs are stable across appended snapshots; source revisions may change
signals, so compare immutable input checksums rather than silently merging runs.

Compare the same dates/timeframe across sources. Differences in session
placeholders, bar construction and prices can change triggers. Counts are not
win rates and do not establish which rule is profitable. The native A/B decision
history at the bottom shows actual recorded rejection reasons separately; actual
broker fills and realized money remain in **Actual trade journal**.

## Promotion to trading

Keep the existing execution risk caps while collecting these observations.
Before adding any lab rule to native execution: define its structural stop and
target policy, model spread/slippage/fees/swap and minimum-lot affordability,
evaluate chronological out-of-sample periods, inspect correlated exposure and
drawdown, then forward-test DEMO fills under a reviewed version. More triggers
alone are not a reason to approve seven order-producing strategies. Seven
rule-based setups also do not require a paid AI API or machine learning.

The name “liquidity sweep” describes a price pattern here. Bid OHLC alone does
not reveal actual resting orders, stop locations or consolidated liquidity.

References: [Dukascopy historical export](https://www.dukascopy.com/swiss/english/marketwatch/historical/),
[third-party downloader documentation](https://www.dukascopy-node.app/quick-start),
[QuantConnect indicator definitions](https://www.quantconnect.com/docs/v2/writing-algorithms/indicators/supported-indicators),
[QuantConnect's opening-range stock research](https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/).
The last example illustrates automation mechanics in a different asset class;
its reported returns are not evidence for our Gold/EURUSD thresholds.
