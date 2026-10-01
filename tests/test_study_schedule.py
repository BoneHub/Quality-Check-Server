"""How a study is drawn from its seed: the subjects picked, the raters' codes, and each rater's list."""

from __future__ import annotations

import random
import unittest

from qc_server.study.schedule import (
    code_of,
    default_gap,
    gap_problem,
    normalized_subject_key,
    pick_subjects,
    rater_codes,
    reading_order,
    reading_orders,
)

KEYS = [f"001_{n:06d}" for n in range(1, 21)]


def gaps(order: list[tuple[str, int]]) -> list[int]:
    """For each subject read twice in a row of rounds, the readings between its two readings."""
    where: dict[tuple[str, int], int] = {item: index for index, item in enumerate(order)}
    return [where[(key, n + 1)] - where[(key, n)] - 1 for key, n in order if (key, n + 1) in where]


class SubjectKeyTests(unittest.TestCase):
    def test_a_key_is_written_as_the_dataset_writes_it(self):
        self.assertEqual(normalized_subject_key("1_12"), "001_000012")
        self.assertEqual(normalized_subject_key(" 001_000012 "), "001_000012")

    def test_what_is_no_key_is_refused(self):
        for text in ("12", "a_b", "1_2_3", ""):
            with self.subTest(text=text), self.assertRaises(ValueError):
                normalized_subject_key(text)


class PickTests(unittest.TestCase):
    def test_the_same_seed_picks_the_same_subjects(self):
        first, _ = pick_subjects(KEYS, 5, 42, lambda key: None)
        again, _ = pick_subjects(list(reversed(KEYS)), 5, 42, lambda key: None)
        self.assertEqual(first, again, "the order the candidates come in does not matter")
        self.assertEqual(len(first), 5)

    def test_another_seed_picks_others(self):
        picks = {tuple(pick_subjects(KEYS, 5, seed, lambda key: None)[0]) for seed in range(10)}
        self.assertGreater(len(picks), 5)

    def test_a_subject_with_a_problem_is_skipped_with_it(self):
        bad = set(KEYS[::2])
        picked, skipped = pick_subjects(KEYS, 5, 1, lambda key: "no segmentation" if key in bad else None)
        self.assertEqual(len(picked), 5)
        self.assertFalse(bad & set(picked))
        self.assertTrue(all(reason == "no segmentation" for _, reason in skipped))

    def test_fewer_are_picked_when_the_candidates_run_out(self):
        picked, _ = pick_subjects(KEYS[:3], 5, 1, lambda key: None)
        self.assertEqual(len(picked), 3)


class CodeTests(unittest.TestCase):
    def test_codes_run_from_a_to_z_then_aa(self):
        self.assertEqual([code_of(i) for i in (0, 1, 25, 26, 27, 51, 52)], ["A", "B", "Z", "AA", "AB", "AZ", "BA"])

    def test_the_same_seed_gives_the_same_codes_whatever_the_order_of_names(self):
        names = ["alice", "bob", "carol", "dave"]
        self.assertEqual(rater_codes(names, 5), rater_codes(list(reversed(names)), 5))
        self.assertEqual(sorted(rater_codes(names, 5).values()), ["A", "B", "C", "D"])

    def test_codes_do_not_follow_the_alphabet_of_names(self):
        names = [f"rater{n:02d}" for n in range(8)]
        alphabetical = {name: code_of(i) for i, name in enumerate(names)}
        self.assertTrue(any(rater_codes(names, seed) != alphabetical for seed in range(5)))


class ReadingOrderTests(unittest.TestCase):
    def test_every_subject_is_read_once_in_each_round(self):
        order = reading_order(KEYS, 3, 5, random.Random(1))
        self.assertEqual(len(order), 3 * len(KEYS))
        for n in (1, 2, 3):
            round_keys = [key for key, reading in order[(n - 1) * len(KEYS) : n * len(KEYS)]]
            self.assertEqual(sorted(round_keys), KEYS)
            self.assertTrue(all(reading == n for _, reading in order[(n - 1) * len(KEYS) : n * len(KEYS)]))

    def test_the_gap_is_kept_whatever_the_seed(self):
        for gap in (0, 5, 10, 19):
            for seed in range(30):
                with self.subTest(gap=gap, seed=seed):
                    order = reading_order(KEYS, 3, gap, random.Random(seed))
                    self.assertGreaterEqual(min(gaps(order)), gap)

    def test_the_largest_gap_repeats_the_first_round(self):
        order = reading_order(KEYS, 2, len(KEYS) - 1, random.Random(4))
        self.assertEqual([key for key, _ in order[: len(KEYS)]], [key for key, _ in order[len(KEYS) :]])

    def test_a_gap_that_cannot_be_kept_is_refused(self):
        self.assertIsNotNone(gap_problem(len(KEYS), len(KEYS), 2))
        self.assertIsNone(gap_problem(len(KEYS), len(KEYS), 1), "one reading of each subject needs no gap")
        with self.assertRaises(ValueError):
            reading_order(KEYS, 2, len(KEYS), random.Random(1))

    def test_the_default_gap_is_half_the_subjects(self):
        self.assertEqual(default_gap(20), 10)
        self.assertEqual(default_gap(1), 0)

    def test_each_rater_has_an_order_of_their_own_and_the_seed_fixes_it(self):
        codes = {"alice": "B", "bob": "A", "carol": "C"}
        orders = reading_orders(KEYS, codes, 2, 5, seed=9)
        self.assertEqual(orders, reading_orders(KEYS, codes, 2, 5, seed=9))
        self.assertEqual(len({tuple(order) for order in orders.values()}), 3)
        self.assertNotEqual(orders, reading_orders(KEYS, codes, 2, 5, seed=10))

    def test_a_rater_keeps_their_order_when_others_join(self):
        """Each rater's order is drawn from the seed and their own code."""
        alone = reading_orders(KEYS, {"alice": "A"}, 2, 5, seed=3)
        joined = reading_orders(KEYS, {"alice": "A", "bob": "B"}, 2, 5, seed=3)
        self.assertEqual(alone["alice"], joined["alice"])


if __name__ == "__main__":
    unittest.main()
