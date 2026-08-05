"""Tests for the pricing engine.

The percentile and trimmed-mean expectations below are computed by hand in the
comments, not by calling another implementation. If the engine drifts, these
break.
"""

from __future__ import annotations

import statistics

import pytest

from conftest import make_comps, make_listing
from gearwatch import stats
from gearwatch.models import Condition, PriceStat


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def test_percentile_hand_computed_odd_sample():
    # [10, 20, 30, 40, 50], n = 5, position = q * (n - 1) = q * 4
    values = [30, 10, 50, 20, 40]  # deliberately unsorted
    assert stats.percentile(values, 0.0) == 10.0
    assert stats.percentile(values, 0.25) == 20.0   # position 1.0 -> exact index
    assert stats.percentile(values, 0.50) == 30.0   # position 2.0
    assert stats.percentile(values, 0.75) == 40.0   # position 3.0
    assert stats.percentile(values, 1.0) == 50.0


def test_percentile_hand_computed_even_sample_interpolates():
    # [100, 200, 300, 400], n = 4, position = q * 3
    values = [100, 200, 300, 400]
    assert stats.percentile(values, 0.25) == 175.0  # 0.75 -> 100 + 0.75 * 100
    assert stats.percentile(values, 0.50) == 250.0  # 1.5  -> 200 + 0.50 * 100
    assert stats.percentile(values, 0.75) == 325.0  # 2.25 -> 300 + 0.25 * 100


def test_percentile_agrees_with_statistics_median():
    for sample in ([1, 2, 3], [1, 2, 3, 4], [7, 7, 9, 100, 3, 2]):
        assert stats.percentile(sample, 0.5) == pytest.approx(statistics.median(sample))


def test_percentile_single_value_and_errors():
    assert stats.percentile([42.0], 0.25) == 42.0
    with pytest.raises(ValueError):
        stats.percentile([], 0.5)
    with pytest.raises(ValueError):
        stats.percentile([1, 2], 1.5)


# ---------------------------------------------------------------------------
# Trimmed mean
# ---------------------------------------------------------------------------


def test_trimmed_mean_hand_computed():
    # 1..20, cut = floor(20 * 0.1) = 2 from each end, leaving 3..18.
    # mean(3..18) = (3 + 18) / 2 = 10.5
    assert stats.trimmed_mean(list(range(1, 21))) == pytest.approx(10.5)


def test_trimmed_mean_below_ten_values_is_the_plain_mean():
    # cut = floor(5 * 0.1) = 0
    values = [10, 20, 30, 40, 1000]
    assert stats.trimmed_mean(values) == pytest.approx(sum(values) / 5)


def test_trimmed_mean_ignores_order_and_rejects_bad_input():
    assert stats.trimmed_mean([5, 1, 3]) == pytest.approx(3.0)
    with pytest.raises(ValueError):
        stats.trimmed_mean([])
    with pytest.raises(ValueError):
        stats.trimmed_mean([1, 2, 3], fraction=0.5)


# ---------------------------------------------------------------------------
# Outliers
# ---------------------------------------------------------------------------


def test_iqr_removal_drops_the_high_outlier_and_reports_it():
    # [100,102,104,106,108,110,112,114,1000], n = 9
    # p25 = index 2 = 104, p75 = index 6 = 112, IQR = 8
    # fences = 104 - 12 = 92 and 112 + 12 = 124
    values = [100, 102, 104, 106, 108, 110, 112, 114, 1000]
    kept, dropped, iqr, low, high = stats.split_outliers(values)
    assert iqr == 8.0
    assert (low, high) == (92.0, 124.0)
    assert dropped == [1000]
    assert len(kept) == 8
    assert 1000 not in kept


def test_iqr_removal_drops_the_low_outlier_too():
    values = [10, 100, 102, 104, 106, 108, 110, 112, 114]
    kept, dropped, _iqr, low, high = stats.split_outliers(values)
    assert dropped == [10]
    assert low == 90.0 and high == 122.0
    assert len(kept) == 8


def test_outliers_are_reported_not_hidden():
    comps = make_comps([860, 872, 880, 890, 900, 910, 925, 940, 320])
    stat = stats.compute_stat(comps, condition=Condition.EXCELLENT)
    assert stat.sufficient is True
    assert stat.outliers_dropped == 1
    assert stat.outlier_prices == (320.0,)
    assert stat.raw_count == 9
    assert stat.count == 8
    assert "1 outlier dropped" in stat.sample_note


# ---------------------------------------------------------------------------
# The refusal. This is the most important behaviour in the program.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 2, 3, 4])
def test_insufficient_sample_returns_a_flag_and_no_band(count):
    comps = make_comps([900 + 10 * i for i in range(count)])
    stat = stats.compute_stat(comps, condition=Condition.EXCELLENT)
    assert stat.sufficient is False
    assert stat.median is None
    assert stat.p25 is None
    assert stat.p75 is None
    assert stat.minimum is None
    assert stat.maximum is None
    assert stat.trimmed_mean is None
    assert "insufficient data" in stat.reason
    assert str(count) in stat.reason
    assert stat.trend.available is False


def test_exactly_the_minimum_sample_does_produce_a_band():
    comps = make_comps([860, 880, 900, 920, 940])
    stat = stats.compute_stat(comps, condition=Condition.EXCELLENT)
    assert stat.count == 5
    assert stat.sufficient is True
    assert stat.reason == "ok"
    assert stat.median == 900.0
    assert stat.p25 == 880.0
    assert stat.p75 == 920.0


def test_minimum_is_configurable_upward():
    comps = make_comps([860, 880, 900, 920, 940])
    stat = stats.compute_stat(comps, min_comps=6)
    assert stat.sufficient is False
    assert "need at least 6" in stat.reason


def test_outlier_removal_can_itself_trigger_the_refusal():
    # Five comps, four identical: the IQR is zero so the fifth is outside the
    # fence, leaving four, which is below the minimum.
    comps = make_comps([500, 500, 500, 500, 900])
    stat = stats.compute_stat(comps)
    assert stat.sufficient is False
    assert stat.median is None
    assert stat.outliers_dropped == 1
    assert "after outlier removal" in stat.reason
    assert "4 of 5" in stat.reason


# ---------------------------------------------------------------------------
# Degenerate samples
# ---------------------------------------------------------------------------


def test_identical_prices_have_zero_spread_and_do_not_divide_by_zero():
    comps = make_comps([500.0] * 8)
    stat = stats.compute_stat(comps)
    assert stat.sufficient is True
    assert stat.count == 8
    assert stat.outliers_dropped == 0
    assert stat.p25 == 500.0 and stat.median == 500.0 and stat.p75 == 500.0
    assert stat.spread == 0.0
    assert stat.iqr == 0.0
    assert stat.trimmed_mean == 500.0
    assert stat.trend.delta == 0.0
    assert stat.trend.pct_change == 0.0


# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------


def test_trend_on_a_falling_series():
    comps = make_comps([1000, 990, 980, 970, 900, 890, 880, 870])
    trend = stats.compute_trend(comps)
    assert trend.available is True
    assert trend.earlier_count == 4 and trend.later_count == 4
    assert trend.earlier_median == pytest.approx(985.0)
    assert trend.later_median == pytest.approx(885.0)
    assert trend.delta == pytest.approx(-100.0)
    assert trend.arrow == "down"
    assert trend.weak is True                      # halves of 4 are below 6
    assert "weak signal" in trend.note


def test_trend_on_a_rising_series_with_a_strong_sample():
    comps = make_comps([100, 102, 104, 106, 108, 110, 200, 202, 204, 206, 208, 210])
    trend = stats.compute_trend(comps)
    assert trend.delta > 0
    assert trend.arrow == "up"
    assert trend.weak is False
    assert "weak signal" not in trend.note
    # earlier median = 105, later median = 205, delta = 100, 100 / 105 = 95.24%
    assert trend.earlier_median == 105.0
    assert trend.later_median == 205.0
    assert trend.pct_change == pytest.approx(95.238, abs=0.01)


def test_trend_refuses_on_a_tiny_sample():
    trend = stats.compute_trend(make_comps([100, 110, 120]))
    assert trend.available is False
    assert trend.weak is True
    assert trend.arrow == "?"


# ---------------------------------------------------------------------------
# Deal scoring
# ---------------------------------------------------------------------------


def test_price_percentile_below_at_and_above():
    prices = [100.0, 200.0, 300.0, 400.0, 500.0]
    assert stats.price_percentile(prices, 50.0) == 0.0        # below every comp
    assert stats.price_percentile(prices, 600.0) == 100.0     # above every comp
    assert stats.price_percentile(prices, 300.0) == 50.0      # exactly the median
    assert stats.price_percentile(prices, 200.0) == 30.0      # (1 + 0.5) / 5
    with pytest.raises(ValueError):
        stats.price_percentile([], 100.0)


def test_price_percentile_on_a_flat_distribution_is_the_midpoint():
    assert stats.price_percentile([500.0] * 6, 500.0) == 50.0


def test_deal_score_inverts_the_percentile():
    assert stats.deal_score(0.0) == 100
    assert stats.deal_score(50.0) == 50
    assert stats.deal_score(100.0) == 0


def test_verdict_always_carries_the_sample_size():
    assert stats.verdict_for(10.0, 14) == "under p25 of recent sold, 14 comps"
    thin = stats.verdict_for(60.0, 7)
    assert "7 comps" in thin
    assert "thin data" in thin
    assert "above the median" in thin
    assert "above p75" in stats.verdict_for(90.0, 30)
    assert "at the median" in stats.verdict_for(50.0, 30)
    assert "between p25 and the median" in stats.verdict_for(40.0, 30)


def test_score_listing_against_a_real_band():
    comps = make_comps([860, 872, 880, 885, 890, 899, 905, 915, 925, 930, 940, 950])
    stat = stats.compute_stat(comps, condition=Condition.EXCELLENT, currency="USD")
    deal = stats.score_listing(make_listing(849.0), stat)
    assert deal.scored is True
    assert deal.percentile == 0.0
    assert deal.score == 100
    assert "12 comps" in deal.verdict
    assert deal.is_good is True


def test_score_listing_refuses_when_the_band_is_insufficient():
    stat = stats.compute_stat(make_comps([900, 910, 920]))
    deal = stats.score_listing(make_listing(500.0), stat)
    assert deal.scored is False
    assert deal.score == 0
    assert deal.percentile is None
    assert "insufficient data" in deal.verdict
    assert deal.is_good is False


def test_score_listing_refuses_across_currencies():
    comps = make_comps([860, 880, 900, 920, 940], currency="USD")
    stat = stats.compute_stat(comps, currency="USD")
    deal = stats.score_listing(make_listing(700.0, currency="EUR"), stat)
    assert deal.scored is False
    assert "does not convert currencies" in deal.verdict


def test_score_listing_flags_a_condition_mismatch():
    comps = make_comps([860, 880, 900, 920, 940], condition=Condition.EXCELLENT)
    stat = stats.compute_stat(comps, condition=Condition.EXCELLENT)
    deal = stats.score_listing(make_listing(870.0, condition=Condition.LIKE_NEW), stat)
    assert deal.scored is True
    assert "graded like_new" in deal.verdict
    assert "band is excellent" in deal.verdict


def test_stat_prices_are_the_post_outlier_sample():
    comps = make_comps([100, 102, 104, 106, 108, 110, 5000])
    stat = stats.compute_stat(comps)
    assert 5000.0 not in stat.prices
    assert len(stat.prices) == stat.count == 6
    assert stat.prices == tuple(sorted(stat.prices))


def test_empty_stat_still_reports_its_identity():
    stat: PriceStat = stats.compute_stat(
        [], model_key="k", condition=Condition.GOOD, currency="GBP"
    )
    assert stat.model_key == "k"
    assert stat.condition is Condition.GOOD
    assert stat.currency == "GBP"
    assert stat.count == 0
    assert stat.sample_note == "0 comps"
