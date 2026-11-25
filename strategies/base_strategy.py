"""Strategy contract: causal, close-time events aligned to the input bars."""

from abc import ABC, abstractmethod
from enum import IntEnum

import pandas as pd


class Signal(IntEnum):
    EXIT = -1
    HOLD = 0
    BUY = 1


class BaseStrategy(ABC):
    @abstractmethod
    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        """Return BUY/EXIT/HOLD using information available through each close.

        Implementations must preserve the input index and may not inspect future
        rows to determine a past signal. The backtester applies the execution lag.
        """
        raise NotImplementedError

    @abstractmethod
    def parameters(self) -> dict[str, str | int | float]:
        """Return JSON-serializable strategy identity and parameters for reports."""
        raise NotImplementedError
