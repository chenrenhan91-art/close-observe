import unittest
from datetime import date, timedelta

import pandas as pd

from pipeline.rules import (
    STRATEGIES,
    WEIGHTS,
    apply_rules,
    attach_industry,
    choose_thresholds,
    combine_scores,
    fundamental_score,
    describe_rules,
    in_top_fraction,
    is_risk_name,
    is_target_board,
    maybe_to_percent,
    normalize_yield,
    sentiment_stage,
    technical_from_bars,
    temperature,
    trading_days_listed,
)


class SentimentTests(unittest.TestCase):
    def test_stage_order(self):
        self.assertEqual(sentiment_stage(0.2, 100, 5), "冰点")
        self.assertEqual(sentiment_stage(0.7, 20, 60), "冰点")
        self.assertEqual(sentiment_stage(0.7, 90, 5), "高潮")
        self.assertEqual(sentiment_stage(0.4, 20, 25), "退潮")
        self.assertEqual(sentiment_stage(0.5, 30, 5), "修复")
        self.assertEqual(sentiment_stage(0.4, 50, 10), "常态")
        self.assertIsNone(sentiment_stage(0.5, None, 10))

    def test_temperature_renormalizes(self):
        score, notes = temperature({"breadth": 40, "limits": None, "volume": None, "industry": None})
        self.assertEqual(score, 40)
        self.assertTrue(notes)


class UniverseTests(unittest.TestCase):
    def test_board_and_name(self):
        self.assertTrue(is_target_board("688001"))
        self.assertTrue(is_target_board("300123"))
        self.assertTrue(is_target_board("001234"))
        self.assertFalse(is_target_board("830001"))
        self.assertFalse(is_target_board("920001"))
        self.assertTrue(is_risk_name("*ST海润"))
        self.assertTrue(is_risk_name("N茅台"))
        self.assertTrue(is_risk_name("退市银鸽"))
        self.assertFalse(is_risk_name("贵州茅台"))

    def test_listing_days_are_inclusive(self):
        start = date(2026, 1, 5)
        dates = [start + timedelta(days=index) for index in range(80)]
        self.assertEqual(trading_days_listed(dates[0], dates[59], dates), 60)
        self.assertEqual(trading_days_listed(dates[1], dates[59], dates), 59)
        self.assertIsNone(trading_days_listed(None, dates[59], dates))


class StrategyTests(unittest.TestCase):
    def test_weights_sum_to_100(self):
        for weights in WEIGHTS.values():
            self.assertEqual(sum(weights.values()), 100)

    def test_relaxes_once_when_fewer_than_five(self):
        spec = next(item for item in STRATEGIES if item["id"] == "limitup-watch")
        frame = pd.DataFrame(
            {
                "in_limit_pool": [True, True, True],
                "sealed_today": [False, False, False],
                "change_pct": [1.0, 1.2, 4.0],
                "turnover": [5.0, 6.0, 8.0],
                "amount": [2e8, 2e8, 2e8],
            }
        )
        _thresholds, relaxed, matched = choose_thresholds(frame, spec)
        self.assertTrue(relaxed)
        self.assertEqual(matched, 3)
        self.assertIn("已放宽", "".join(describe_rules(spec, spec["relax"], True)))

    def test_keeps_base_when_enough_names(self):
        spec = next(item for item in STRATEGIES if item["id"] == "limitup-watch")
        frame = pd.DataFrame(
            {
                "in_limit_pool": [True] * 6,
                "sealed_today": [False] * 6,
                "change_pct": [1.0] * 6,
                "turnover": [5.0] * 6,
                "amount": [2e8] * 6,
            }
        )
        _thresholds, relaxed, matched = choose_thresholds(frame, spec)
        self.assertFalse(relaxed)
        self.assertEqual(matched, 6)

    def test_negative_pe_fails_quality_band(self):
        spec = next(item for item in STRATEGIES if item["id"] == "quality")
        frame = pd.DataFrame(
            {
                "roe": [12],
                "rev_yoy": [5],
                "profit_yoy_used": [5],
                "debt": [40],
                "pe": [-3],
                "pb": [1.2],
                "mcap": [200e8],
                "ret60": [1],
            }
        )
        self.assertFalse(bool(apply_rules(frame, spec["base"]).iloc[0]))

    def test_top_fraction(self):
        self.assertTrue(in_top_fraction(3, 10, 0.30))
        self.assertFalse(in_top_fraction(4, 10, 0.30))
        self.assertFalse(in_top_fraction(float("nan"), 10, 0.30))


class ScoreTests(unittest.TestCase):
    def test_missing_dimension_is_not_zero(self):
        total, notes = combine_scores(
            {"fundamental": 50, "technical": None, "capital": 80, "industry": 60},
            "short",
        )
        self.assertIsNone(total)
        self.assertIn("不足 70%", notes[0])

    def test_sufficient_coverage_renormalizes_missing_dimension(self):
        total, notes = combine_scores({"fundamental": None, "technical": 80, "capital": 60, "industry": 40}, "short")
        self.assertEqual(total, 66.7)
        self.assertIn("基本面", notes[0])

    def test_fundamentals_require_profitability_and_valuation(self):
        self.assertIsNone(fundamental_score(10, 2, None))
        self.assertIsNone(fundamental_score(None, None, 15))
        self.assertIsNotNone(fundamental_score(10, None, 15))

    def test_invalid_bar_values_do_not_generate_a_score(self):
        bars = [{"close": 10, "high": 11, "volume": 100} for _ in range(60)]
        bars[-1]["close"] = float("nan")
        self.assertIsNone(technical_from_bars(bars))

    def test_uptrend_is_above_ma60(self):
        bars = [
            {"close": 10 + index * 0.2, "high": 10.3 + index * 0.2, "volume": 1000 + index * 10}
            for index in range(80)
        ]
        result = technical_from_bars(bars)
        self.assertIsNotNone(result)
        self.assertTrue(result["above_ma60"])
        self.assertGreater(result["score"], 70)

    def test_short_history_has_no_technical_score(self):
        bars = [{"close": 10, "high": 11, "volume": 100} for _ in range(20)]
        self.assertIsNone(technical_from_bars(bars))

    def test_percent_scaling(self):
        ratios = maybe_to_percent(pd.Series([0.12, 0.08, 0.15, 0.2]))
        self.assertAlmostEqual(float(ratios.iloc[0]), 12)
        percent = maybe_to_percent(pd.Series([12, 8, 15, 21]))
        self.assertAlmostEqual(float(percent.iloc[0]), 12)
        yields = normalize_yield(pd.Series([0.03, 0.02, 0.05, 0.04]))
        self.assertAlmostEqual(float(yields.iloc[0]), 3)

    def test_industry_rank_uses_median(self):
        frame = pd.DataFrame(
            {
                "industry": ["银行", "银行", "银行", "白酒"],
                "change_pct": [1.0, 3.0, 5.0, 9.0],
            }
        )
        ranked = attach_industry(frame)
        banks = ranked[ranked["industry"] == "银行"]
        liquor = ranked[ranked["industry"] == "白酒"]
        self.assertEqual(int(banks["industry_n"].iloc[0]), 3)
        self.assertEqual(float(banks["industry_rank"].iloc[0]), 1)
        self.assertEqual(int(liquor["industry_n"].iloc[0]), 0)


if __name__ == "__main__":
    unittest.main()
