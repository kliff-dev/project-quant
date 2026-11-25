# Five executable DEMO setups for EUR/USD and Gold

The `multi_setup_v1` profile contains **five eligible setups for EURUSDm and five for XAUUSDm**. They share the existing protected DEMO execution engine. They are custom research hypotheses adapted from our R1–R5 work; the combination has not established a profitable edge.

In the app, open **Seven-setup research lab → Five executable DEMO setups**. Select EUR/USD or Gold and use these three tabs:

- **Read each setup:** purpose, market conditions, exact entry rule, stop, target, research basis and limitations.
- **Signals & execution decisions:** five separate rule checks, dated history, technical reasons and execution blockers. Download the filtered records as CSV.
- **Market conditions & days:** observed trigger frequencies by weekday, session, regime or month, with the number of fresh candles actually monitored as the denominator.

The page name is retained for navigation compatibility. Its **Seven research-only patterns** tab remains a separate historical scanner. Those seven scanner rules do not send orders. BTC retains its own controls and policy.

The first tab also displays the saved **Historical outcome evidence · $500 model** panel, including per-setup technical opportunities, modeled admissions, completed episodes, modeled win rate and net mean R. Its earlier/later split is descriptive: both periods reuse previously examined data. The current study has a negative aggregate across independent episodes, especially Gold; expanding the profile has not established profitability. The normal and doubled-slippage scenarios show how sensitive admissions and outcomes are to execution assumptions.

These episodes can overlap and independently reset their starting capital. They are **not native broker fills, a four-position portfolio backtest, compounded returns or account equity**. Blank results for a setup with no completed episodes are unknown, not zero-percent wins. If the local aggregate report is absent, the app states that the study is pending or unavailable rather than inventing results from technical trigger counts.

The app compares the study's recorded setup-kernel and specification fingerprints with the displayed revision. A mismatch is labeled **historical reference only** until the study is rerun; old outcomes do not validate changed rules.

## Technical Summary

Previously, the research scanner could show seven patterns while the Gold/EURUSD execution engine used only two. The new app separates the five executable patterns from the seven research patterns and exposes the actual five-pattern observation history. Reading a guide or changing its filters never activates a monitor.

### The five patterns

| Pattern | When it can qualify | What the entry means |
| --- | --- | --- |
| Trend EMA reclaim | A measured slower uptrend or downtrend | A completed entry candle crosses back through EMA20 after a pullback, with matching candle direction, EMA50 position and RSI. |
| Trend sweep and recovery | A measured slower trend | Price briefly exceeds the opposite edge of the prior 20-candle channel and closes back inside it in the trend direction. This is a price pattern, not evidence of who traded. |
| Trend channel breakout | A measured slower trend | A completed candle closes outside the prior 20-candle channel in the trend direction, while still inside the permitted chase distance. |
| Compression breakout | A prior unusually narrow range; no opposing slower trend | A completed candle escapes the prior channel after a previously measured bandwidth squeeze. A squeeze alone supplies no direction. |
| Range band re-entry | The instrument-specific low-trend/range context; Gold uses a stricter flat definition | Price pierces the prior 20-close statistical band and closes back inside, toward the mean, with matching candle direction and RSI. |

### EUR/USD parameters

- Entry candles: **H1**. Context: fully completed **H4** candles.
- Common volatility range: current H1 ATR14 / median of the last 100 H1 ATR14 values between **0.5 and 2.0**. Reject a signal candle larger than **3 ATR**.
- Reclaim RSI: **50–65 BUY / 35–50 SELL**. Channel and compression breakout chase limit: **0.5 H1 ATR** beyond the prior channel edge.
- Existing structural stop: BUY below the lower of the last five lows minus 0.1 ATR, or ask minus 1 ATR; SELL uses the mirrored high, spread and 1 ATR floor. Round outward to the broker tick.
- Existing adaptive target selector: **1R–4R**, subject to confirmed prior H4 obstacles, context and costs. A farther target is not assumed merely because price broke a channel.

### Gold parameters

- Entry candles: **M5**. Context: fully completed **M15** candles.
- Common volatility range: current M5 ATR14 / median of the last 100 M5 ATR14 values between **0.75 and 1.75**. Reject a signal candle larger than **2.5 ATR**.
- Reclaim RSI: **50–68 BUY / 32–50 SELL**. Channel and compression breakout chase limit: **0.75 M5 ATR**.
- Existing structural stop: BUY below the lower of the last three lows minus 0.2 ATR, or ask minus 1.2 ATR; SELL uses the mirrored high, spread and 1.2 ATR floor. Round outward to the broker tick.
- Existing Gold target selector: **1R–4R**, constrained by confirmed prior M15 obstacles, context and costs.

The exact current rules displayed in the app come directly from [the versioned specification](../config/five_setup_spec.json). This prevents a separately typed app guide from silently drifting away from the technical specification.

### What “adapting” means

The profile measures conditions from candles available at decision time:

- **EUR/USD low-trend/range:** absolute H4 EMA50–EMA200 distance no more than **2 H4 ATR**, and absolute five-H4-candle EMA50 change no more than **0.5 H4 ATR**. These restore the thresholds registered in our earlier November 14 research; the range need not be completely flat.
- **Gold flat range:** absolute M15 EMA50–EMA200 distance no more than **0.75 M15 ATR**, and absolute five-M15-candle EMA50 change no more than **0.20 M15 ATR**. Gold's definition is unchanged.
- **Trend:** outside that range definition, context EMA order, close position and slope agree on a direction.
- **Mixed:** neither definition fits. Only a non-opposed compression release may qualify.

The instrument's range definition takes precedence: a candle classified as range is not also classified as directional trend. Trend entries require aligned context. Thresholds are fixed for this version. The EUR/USD correction restores prior research provenance; it is not a claim that looser thresholds will improve outcomes. The program does not retrain itself, forecast the economy, or change the rules after a losing trade.

Weekday, month, and DST-aware London/New York session labels are recorded. **They are descriptive observations, not proven higher-probability times.** Weekends are rejected. Native broker availability and quote freshness still apply. There is no live economic-event calendar; shock/volatility checks cannot guarantee protection from news or gaps.

### Unchanged risk and position rules

- DEMO account verified natively; REAL and CONTEST rejected.
- **1% baseline planned risk** per trade. Only the existing qualified minimum-lot exception may exceed 1%, up to **2%**. The ceiling is not a target size.
- Maximum **two EUR/USD plus two Gold positions**, four total, within **8% combined planned risk** including existing protected exposures. Existing same-market positions count toward this limit.
- Every additional position needs a fresh setup on a new candle. Persistent matching conditions do not generate repeated entries.
- Same-direction overlaps choose the first fresh setup in the declared order above; opposing raw directions reject the candle. If the chosen setup fails execution checks, the monitor does not try another pattern on that candle.
- Existing stop placement, adaptive targets, spread/slippage, margin, freshness, order checks and deal/position verification remain required. No automatic order retry.

**A range setup normally must fit the 1% baseline.** The exception up to 2% requires strong-trend conditions, which a measured range usually cannot satisfy. Gold's minimum lot can therefore still make a valid range pattern unaffordable. The engine skips it rather than tightening its stop or increasing risk authority.

At $500, 1% is approximately $5; 2% approximately $10; the combined 8% ceiling approximately $40. The engine uses the current conservative account basis and native profit calculations, rather than these examples. Gaps, fill changes and transaction costs can make realized losses differ from planned losses. Gold and EUR/USD trades can also share exposure to the US dollar; four positions are not four independent experiments.

## Root Cause

The earlier app used one research page for historical pattern frequency and a separate two-pattern execution profile. Research visibility did not grant execution eligibility. The labels and separation were insufficient to make that distinction obvious.

## How It Was Diagnosed

The relevant evidence is the selected execution profile and each saved observation's `setup_signals`, `selected_setup`, `execution_status`, `execution_reason`, freshness flag and market context. A technical trigger can exist without an affordable or broker-permitted order.

The new profile's immutable records are in `logs/observations/multi_setup_v1/events/`. Earlier A/B observations remain separately labeled; they are not relabeled as five-setup outcomes.

## Solution

The app reads the five-pattern specification and renders all five guides for each instrument. Its history keeps these outcomes separate:

| Displayed outcome | Meaning |
| --- | --- |
| No technical trigger | That pattern did not generate a fresh directional condition. |
| Condition persists — no new trigger | The underlying direction still matches, but it was already present on the previous candle. It does not authorize another entry. |
| Historical / not fresh | The observed candle was unsuitable for counting as a current entry opportunity. |
| Technical trigger — execution blocked | A pattern appeared but a risk, cost, target, inventory or broker requirement prevented an order. Read the recorded reason. |
| Overlapping signal — not selected | Another same-candle pattern won the fixed priority. This row is not an additional trade. |
| Entry recorded — check trade journal | The selected execution record reports a verified entry. Actual trade journal holds the verified broker tickets and cashflows. |

Condition tables count **technical triggers / fresh candles observed**, not winning trades. Historical replay remains the **original A-only account simulation** and does not validate this expanded profile.

Offline checks cover complete per-instrument guides, exact specification text, bad-record handling, duplicate-event handling, signal/entry/overlap distinctions, condition denominators and page navigation without automatic activation. Full execution and installed-app verification are documented in the implementation report for this update.

## What I Learned

1. More displayed patterns do not mean more executable strategies; inspect the selected profile.
2. A technical trigger, execution approval and verified broker fill are different events.
3. Count opportunities against time actually monitored before comparing weekdays or sessions.
4. Different patterns still share the same position and risk budget; overlaps are correlated evidence.
5. A profitable claim requires enough actual or carefully modeled outcomes after costs, not a high signal count.

## If This Happens Again

Check the live monitor's heartbeat and deadline first. Then open **Latest checks across all five setups** or the setup lab's **Signals & execution decisions** tab. Identify whether there was no trigger, a stale candle, a priority conflict or an execution blocker. Use **Actual trade journal** to establish whether a trade really filled. If the guide is unavailable, review the packaged specification/version rather than assuming its rules from an older screenshot.
