# Two-setup DEMO experiment

Version: `reclaim_sweep_v1`, frozen 18 November 2026. This is an exploratory DEMO comparison. Neither setup is established as profitable.

## Entries

**A — `trend_reclaim`:** the existing trend pullback/EMA20 reclaim. EUR/USD uses completed H4 trend and H1 entry candles; Gold uses completed M15 trend and M5 entry candles. Existing EMA, RSI and Gold volatility/shock conditions are retained.

**B — `trend_sweep`:** a separate failed-range-break/rejection in the same higher-timeframe trend. For a BUY, the last entry candle must trade strictly below the lowest low of the preceding 20 entry candles, then close bullish and strictly above that old low. A SELL is the reverse at the preceding 20 highs. The signal candle is excluded from the reference range. B does not require an EMA20 crossover or impose an additional RSI threshold. This price pattern does not establish that market participants deliberately targeted stops.

Both use the same completed-context EMA50/200 alignment, close relative to EMA50 and five-context-bar EMA50 slope. Gold retains its 0.75-1.75 volatility ratio and maximum 2.5 ATR signal range. Warmup, continuity, freshness, native contract and account verification remain required.

## One account and one budget

- Evaluate both patterns on each completed entry candle. No valid candidate means no order.
- One qualifying pattern: evaluate its native plan. Same-direction overlap: choose A, recording B as overlap. Opposite directions: skip both.
- A planning rejection or failed/uncertain send does not trigger a fallback or retry through B.
- At most one position per instrument, two positions account-wide. Setups do not get separate position slots.
- Baseline planned risk stays 1%; the existing qualified minimum-lot-only exception can use up to 2%. Total combined planned risk stays at 2%.
- Structural/ATR stops, adaptive 1R-4R targets, cost reserves, broker checks and verification of actual deals/positions remain in effect. Stop prices and planned risk do not guarantee execution prices or cap gap losses.
- The session deadline stops new entries; positions retain broker protection. No trailing, averaging down, automatic manual closes or continuous real-account trading was added.

## Records

- `reports/latest_setup_comparison.md`: both technical decisions, selected setup, actual admission/execution status and opportunity counts.
- `logs/observations/reclaim_sweep_v1/events/`: immutable per-symbol/per-candle observations, quote, funds, input hash, setup labels and rejection reasons. Technical signals are also observed while an existing position occupies the symbol. These records are not actual fills or broker P&L.
- `reports/trade_journal.md`, `data/processed/trade_journal.csv`, `data/processed/trade_journal.json`: actual filled trades with `setup_id`, `entry_setups`, original protection, fees, swap and realized R. Rule exits and manual overrides stay separate. Older trades retain legacy labels.
- `reports/market_analysis/two_setups_2026-11-18/report.md`: A-only, B-only and combined chronological account models, with separate $500/$1,000 scenarios and slippage stress. These are hypothetical accounts, not broker statements.

The combined account's outcomes are affected by A priority, existing positions, affordability and changing account balance. Compare these records with the independent A-only and B-only models; do not interpret unequal actual sample counts as a fair randomized trial.

## Historical result and limits

On the previously examined May-November archive, the $500 A-only model completed 33 trades and ended at $489.14. B alone completed 39 and ended at $414.13. The shared A/B model completed 63 and ended at $425.62, with 30.34% maximum close-marked drawdown. The added pattern increased activity and worsened the historical modeled result. It is enabled only as the requested DEMO experiment, not as an approved profitable strategy.

The replay includes recorded bar spreads, adverse slippage and a modeled extra cost allowance. Historical swaps are zero and financing/actual Ask extrema/margin conditions are not reconstructed exactly. Data has already been used in earlier research; it is development evidence, not an untouched holdout. No rule search or threshold tuning was performed after this comparison.

The installed desktop application's existing Activate/Stop controls launch this workspace's reviewed engine. Its bundled historical Replay page still represents the older A-only model; use the linked A/B research report for the new comparison. This update does not rebuild the desktop installer or alter the old replay's results.

## Operation and troubleshooting

Use the existing explicit DEMO activation and deadline controls. A green light means an active verified monitor, not an open position. The strategy profile is recorded in the source-pinned activation plan and policy JSON files.

If no orders occur, check the live monitor heartbeat, native account verification, technical counts and exact planning rejection. More technical signals cannot overcome the broker's minimum volume or the shared risk ceiling. Never remove a consumed authorization, an uncertain-send reservation or a risk check to generate more trades.

Offline checks:

```powershell
.\.venv\Scripts\python.exe -B -m pytest tests/test_two_setups.py -q
.\.venv\Scripts\python.exe -B scripts/research_two_setups.py
```

The observer uses the existing closed-candle collection component. Its older one-bar follow-through comparison remains research-only and has no order authority.
