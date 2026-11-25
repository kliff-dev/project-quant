import pandas as pd
import pytest

from engine.data_loader import demo_data
from strategies.trend_following import MovingAverageCrossover


def test_expected_crosses_and_full_warmup(bars):
    data = bars([3, 2, 1, 2, 4, 1, 1, 2])
    assert list(MovingAverageCrossover(2, 3).generate_signals(data)) == [0, 0, 0, 0, 1, 0, -1, 1]


def test_future_bars_do_not_change_past_signals():
    data = demo_data(100)
    strategy = MovingAverageCrossover(3, 10)
    full = strategy.generate_signals(data)
    for length in [11, 30, 60, 90]:
        pd.testing.assert_series_equal(strategy.generate_signals(data.iloc[:length]), full.iloc[:length])
    altered = data.copy()
    altered.loc[altered.index[60]:, "close"] *= 10
    pd.testing.assert_series_equal(strategy.generate_signals(altered).iloc[:60], full.iloc[:60])


def test_first_valid_relationship_is_not_a_crossover(bars):
    signals = MovingAverageCrossover(2, 3).generate_signals(bars([1, 2, 3, 4, 5]))
    assert not signals.any()


@pytest.mark.parametrize("short,long", [(5, 5), (6, 5), (0, 5), (True, 5)])
def test_invalid_windows(short, long):
    with pytest.raises(ValueError):
        MovingAverageCrossover(short, long)


def test_insufficient_warmup_is_explained(bars):
    with pytest.raises(ValueError, match="at least"):
        MovingAverageCrossover(2, 3).generate_signals(bars([1, 2, 3]))
