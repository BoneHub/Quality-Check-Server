"""The reliability statistics of a study: % agreement, Krippendorff's alpha and Gwet's AC1.

Checked against the worked examples they are published with, and against the closed forms
they reduce to for two coders, and their bootstrap intervals for what an interval must do.
"""

from __future__ import annotations

import unittest

import numpy as np

from qc_server.study import reliability
from qc_server.study.reliability import counts_of, gwet_ac1, krippendorff_alpha, percent_agreement

#: Krippendorff (2011), "Computing Krippendorff's Alpha-Reliability", example C: four observers,
#: twelve units, nominal values 1-5, with missing values. Gwet's Handbook of Inter-Rater
#: Reliability uses the same data (in the irrCAC package, ``cac.raw4raters``).
N = None
FOUR_OBSERVERS = {
    "A": [1, 2, 3, 3, 2, 1, 4, 1, 2, N, N, N],
    "B": [1, 2, 3, 3, 2, 2, 4, 1, 2, 5, N, 3],
    "C": [N, 3, 3, 3, 2, 3, 4, 2, 2, 5, 1, N],
    "D": [1, 2, 3, 3, 2, 4, 4, 1, 2, 5, 1, N],
}
FOUR_OBSERVER_UNITS = [[FOUR_OBSERVERS[o][u] for o in "ABCD"] for u in range(12)]
FIVE_CATEGORIES = [1, 2, 3, 4, 5]

#: Krippendorff (2011), example A: binary data, two observers, no missing values.
TWO_OBSERVER_UNITS = list(zip([0, 1, 0, 0, 0, 0, 0, 0, 1, 0], [1, 1, 1, 0, 0, 1, 0, 0, 0, 0]))


class PublishedExampleTests(unittest.TestCase):
    def test_alpha_of_binary_data_from_two_observers(self):
        self.assertAlmostEqual(krippendorff_alpha(TWO_OBSERVER_UNITS, [0, 1]), 0.095, places=3)

    def test_alpha_of_nominal_data_with_missing_values(self):
        self.assertAlmostEqual(krippendorff_alpha(FOUR_OBSERVER_UNITS, FIVE_CATEGORIES), 0.743, places=3)

    def test_gwets_percent_agreement_on_the_same_data(self):
        """p_a = 0.8182 in Gwet's handbook, over the eleven units with two ratings or more."""
        self.assertAlmostEqual(percent_agreement(FOUR_OBSERVER_UNITS, FIVE_CATEGORIES), 0.8182, places=4)

    def test_gwets_ac1_on_the_same_data(self):
        """AC1 = 0.7754, with p_e = 0.1903, in Gwet's handbook."""
        self.assertAlmostEqual(gwet_ac1(FOUR_OBSERVER_UNITS, FIVE_CATEGORIES), 0.7754, places=4)


class TwoCoderTests(unittest.TestCase):
    """With two coders and two categories, the formulas reduce to well-known closed forms."""

    def units(self, both_reject: int, first_only: int, second_only: int, neither: int) -> list:
        return [(1, 1)] * both_reject + [(1, 0)] * first_only + [(0, 1)] * second_only + [(0, 0)] * neither

    def test_percent_agreement_is_the_share_of_units_agreed_on(self):
        units = self.units(15, 35, 35, 915)
        self.assertAlmostEqual(percent_agreement(units, [0, 1]), 0.93)

    def test_ac1_is_gwets_two_rater_formula(self):
        """AC1 = (p_a - p_e) / (1 - p_e), p_e = 2 q (1 - q), q the mean share of category 1."""
        units = self.units(15, 35, 35, 915)
        q = (15 + 35 + 15 + 35) / 2000
        expected_chance = 2 * q * (1 - q)
        self.assertAlmostEqual(gwet_ac1(units, [0, 1]), (0.93 - expected_chance) / (1 - expected_chance))

    def test_with_rare_rejects_alpha_is_low_and_ac1_high(self):
        """The case the study reports AC1 for: 5% rejects each, 93% agreement."""
        units = self.units(15, 35, 35, 915)
        self.assertAlmostEqual(krippendorff_alpha(units, [0, 1]), 0.26, places=2)
        self.assertAlmostEqual(gwet_ac1(units, [0, 1]), 0.92, places=2)

    def test_perfect_agreement(self):
        units = self.units(10, 0, 0, 90)
        self.assertEqual(percent_agreement(units, [0, 1]), 1.0)
        self.assertAlmostEqual(krippendorff_alpha(units, [0, 1]), 1.0)
        self.assertAlmostEqual(gwet_ac1(units, [0, 1]), 1.0)

    def test_alpha_is_undefined_when_every_verdict_is_the_same(self):
        """Nothing then tells agreement from chance; AC1 still reads it as full agreement."""
        units = self.units(0, 0, 0, 50)
        self.assertIsNone(krippendorff_alpha(units, [0, 1]))
        self.assertEqual(gwet_ac1(units, [0, 1]), 1.0)

    def test_a_unit_with_one_value_shows_no_agreement(self):
        self.assertIsNone(percent_agreement([(1, None), (None, 0)], [0, 1]))

    def test_a_value_outside_the_categories_is_refused(self):
        with self.assertRaises(ValueError):
            counts_of([(0, 2)], [0, 1])


class IntervalTests(unittest.TestCase):
    """The 95% intervals, from resampling whole subjects."""

    def data(self, n_subjects=40, items=6, flip=0.1, seed=3):
        """Two coders who each get an item's true verdict wrong with probability ``flip``."""
        rng = np.random.default_rng(seed)
        truth = rng.random((n_subjects, items)) < 0.2
        coded = [truth ^ (rng.random(truth.shape) < flip) for _ in range(2)]
        units = [(int(coded[0][s, i]), int(coded[1][s, i])) for s in range(n_subjects) for i in range(items)]
        subjects = np.repeat(np.arange(n_subjects), items)
        return counts_of(units, [0, 1]), subjects

    def test_the_interval_surrounds_the_estimate(self):
        counts, subjects = self.data()
        result = reliability.agreement(counts, subjects, 1000, np.random.default_rng(1))
        for estimate in (result.agreement, result.alpha, result.ac1):
            self.assertLessEqual(estimate.low, estimate.value)
            self.assertGreaterEqual(estimate.high, estimate.value)
            self.assertLess(estimate.low, estimate.high)

    def test_the_same_seed_gives_the_same_interval(self):
        counts, subjects = self.data()
        first = reliability.agreement(counts, subjects, 500, np.random.default_rng(7))
        second = reliability.agreement(counts, subjects, 500, np.random.default_rng(7))
        self.assertEqual(first, second)

    def test_more_subjects_give_a_narrower_interval(self):
        few = reliability.agreement(*self.data(n_subjects=15), 1000, np.random.default_rng(1))
        many = reliability.agreement(*self.data(n_subjects=150), 1000, np.random.default_rng(1))
        self.assertLess(many.alpha.high - many.alpha.low, few.alpha.high - few.alpha.low)

    def test_the_counts_say_how_much_was_compared(self):
        counts, subjects = self.data(n_subjects=10, items=6)
        result = reliability.agreement(counts, subjects, 200, np.random.default_rng(1))
        self.assertEqual((result.subjects, result.items, result.values), (10, 60, 120))
        self.assertAlmostEqual(sum(result.shares), 1.0)

    def test_one_subject_has_no_interval(self):
        counts, subjects = self.data(n_subjects=1)
        result = reliability.agreement(counts, subjects, 200, np.random.default_rng(1))
        self.assertIsNotNone(result.agreement.value)
        self.assertIsNone(result.agreement.low)

    def test_nothing_to_compare_gives_no_numbers(self):
        result = reliability.agreement(np.zeros((4, 2)), np.arange(4), 200, np.random.default_rng(1))
        self.assertEqual((result.items, result.shares), (0, None))
        self.assertIsNone(result.alpha.value)


if __name__ == "__main__":
    unittest.main()
