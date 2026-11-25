import pandas as pd
import pytest


@pytest.fixture
def bars():
    def make(closes, opens=None):
        closes = list(closes)
        opens = list(opens) if opens is not None else closes
        return pd.DataFrame({
            "open": opens,
            "high": [max(o, c) + 1 for o, c in zip(opens, closes)],
            "low": [min(o, c) * 0.9 for o, c in zip(opens, closes)],
            "close": closes,
            "volume": [1000] * len(closes),
        }, index=pd.date_range("2024-01-01", periods=len(closes), name="date"))
    return make
