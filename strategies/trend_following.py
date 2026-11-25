"""Long-only simple moving-average crossover with full-window warmup."""

from dataclasses import dataclass

import pandas as pd

from strategies.base_strategy import BaseStrategy, Signal


@dataclass(frozen=True)
class MovingAverageCrossover(BaseStrategy):
    short_window: int = 20
    long_window: int = 50

    def __post_init__(self) -> None:
        for window in (self.short_window, self.long_window):
            if isinstance(window, bool) or not isinstance(window, int) or window < 1:
                raise ValueError("Moving-average windows must be positive integers.")
        if self.short_window >= self.long_window:
            raise ValueError("Short moving average must be shorter than long moving average.")

    def indicators(self, data: pd.DataFrame) -> pd.DataFrame:
        """Trailing windows only: no centering, backfilling, or future values."""
        return pd.DataFrame({
            "short_sma": data["close"].rolling(self.short_window, min_periods=self.short_window).mean(),
            "long_sma": data["close"].rolling(self.long_window, min_periods=self.long_window).mean(),
        }, index=data.index)

    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        if len(data) < self.long_window + 1:
            raise ValueError(f"Need at least {self.long_window + 1} bars to observe a crossover after warmup.")
        averages = self.indicators(data)
        difference = averages["short_sma"] - averages["long_sma"]
        previous = difference.shift(1)
        signals = pd.Series(int(Signal.HOLD), index=data.index, dtype="int64", name="signal")
        signals.loc[(difference > 0) & (previous <= 0)] = int(Signal.BUY)
        signals.loc[(difference < 0) & (previous >= 0)] = int(Signal.EXIT)
        # The first valid average has no prior valid relationship: no invented cross.
        return signals

    def parameters(self) -> dict[str, str | int | float]:
        return {"name": "MovingAverageCrossover", "short_window": self.short_window, "long_window": self.long_window}
