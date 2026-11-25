# Bitcoin research and separate DEMO controls

Open **BTC research & DEMO** in Project Quant 0.6.1. A separate **Manage BTC DEMO session** card appears on every page. Choose a Manila end date/time and B1–B4 setups, then click **Activate BTC DEMO**. Opening the app never starts trading.

Keep the PC awake, connected to the internet and MT5 connected to the pinned USD DEMO account, with Algo Trading and external Python trading permitted. The worker requests Windows system wakefulness while running; this does not override manual sleep, shutdown, disconnection or account-server failure. The app may close while its worker continues until its deadline.

Green requires a live worker, fresh heartbeat, verified DEMO trade mode and future deadline. Red includes inactive or unverified states. **Stop BTC entries** revokes new entry authority for the exact displayed run; it does not close a protected position. MT5 broker-side SL/TP may execute after the local session ends.

## Exact instrument and four hypotheses

BTCUSDm is this broker's Bitcoin/USD **CFD**, not owned Bitcoin on an exchange. No spot price feed is substituted for executable broker prices. Native contract checks found 1 BTC per lot, 0.01-lot minimum/step and USD profit arithmetic; the engine rechecks the actual contract every time.

| ID | Completed H1 trigger | Published family |
|---|---|---|
| B1 | SMA20 crosses SMA100 | Moving averages |
| B2 | Close newly breaks previous 48-bar high/low | Trading-range breakout |
| B3 | Close newly exceeds previous 24-close extreme by 1% | Filter rules |
| B4 | Exact 168-hour return changes sign, confirmed by 12-hour return | Time-series momentum |

[Primary evidence and exact limitations](BTC_RESEARCH_SOURCES.md). These are paper-inspired adaptations. Hourly settings, symmetric CFD shorts, structural stops and 2R exits are additional hypotheses, not replicated proof of profitability. No trained AI, paid API or automatic parameter optimization is used.

Each uses completed candles, an ATR14 volatility estimate and a five-candle structural stop with a 0.2 ATR buffer, at least 1.5 ATR from the entry. Fresh price can expand this distance outward; it never tightens the structural stop merely to afford minimum lots. Requested TP is 2R. Same-side signals use B1→B4 priority; conflicting directions skip. Risk rejection does not fall through to another setup.

## Risk and execution

- 1% of the smaller of balance/equity is the planned budget. 5% of that budget is reserved headroom; adverse entry/stop-exit slippage of $2/BTC per side is modeled. Native `order_calc_profit` sizes lots, rounded down. Maximum 0.10 lots, maximum one BTC position. Minimum lots exceeding the budget are rejected.
- Spread plus two slippage allowances must be ≤10% of stop distance; margin must leave 10% free-margin headroom. Quote freshness, candle age, stops/freeze levels, filling permissions, DEMO mode and flat inventory are checked again before sending.
- This is an estimate, not a guaranteed maximum loss: gaps, execution, commissions and future swap can exceed allowances. Requested 2R differs from realized reward after costs and slippage.
- BTC uses the same held OS monitor lock as Gold/EURUSD. Sessions run separately; the other session and its positions must end before BTC starts. Existing Gold/EURUSD policy and its 2% combined ceiling are unchanged.
- A durable one-attempt ledger is written before `order_send`; `order_check` precedes it. Actual order/deal/position and SL/TP are verified. Failed/uncertain sends are never automatically retried. Unknown outcomes require manual broker-history review.
- If a known protected position closes after the session expires, a later explicit activation first reconciles its complete entry/exit history and costs. A still-open position blocks a new session. Unknown ledger states remain blocked.

## Evidence, dates and limitations

The page reads checksum-verified local H1 data and shows dated candles, SMA20/100, raw signal markers/counts, downloadable signal history, cost-test summaries and rejected-entry reasons. Raw technical signals are not affordable orders, closed trades or wins. **Refresh saved BTC data** explicitly downloads completed history only; it never activates trading or recomputes old replay results.

The two-year archive has 17,515 bars and four missing clock-hours across two gaps. Tick volume is broker activity, not consolidated exchange volume. Incremental refresh preserves overlap revisions in checksum-named snapshots. Exact live input snapshots, decisions, statuses and verified attempt records go into `logs/btc_monitor/`.

**The combined historical baseline lost $44.09 from $500 over 30 closed trades, and $119.82 from $1,000 over 185.** It includes recorded bar spreads and modeled slippage, but historical financing/commission are unverified and zero in the baseline. B3 had no affordable entries at either capital. Independent winners cannot be added together, and small positive samples do not establish an edge. See [all 44 tests and assumptions](../reports/market_analysis/btc_2026-11-22/report.md).

The installed app seeds public price history and aggregate replay results, without execution engines, credentials, account logs or activation authority. Trading controls require the configured external Windows development workspace. Source hashes stop activation if reviewed trading code changes.

## Troubleshooting

If red, read the status reason first. Verify connected DEMO mode and Python/Algo permissions in MT5. A busy or unverified Gold/EURUSD session blocks BTC. For few entries, compare raw signals against historical rejection counts—minimum-lot affordability can reject a technically valid setup. Inspect the current worker stderr and `logs/btc_monitor/status.json` for failures; inspect attempt records and broker history for unknown sends. Never delete an uncertain attempt merely to enable another order.

Version 0.6.1 fixes old Windows process IDs being reused by unrelated services: completed sessions retire their saved PID only when the shared execution lock is free. A live worker holding that lock still blocks another session. Do not terminate an unrelated Windows process based on an old saved PID. Unresolved execution records now block switching modes until their outcomes are reconciled.
