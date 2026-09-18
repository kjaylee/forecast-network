"""Shamir splitting: any K shares reconstruct, and fewer than K do not."""

from __future__ import annotations

import base64
import itertools
import unittest

from scripts.recovery_key_shares import (
    FORMAT,
    combine,
    decode_share,
    encode_share,
    fingerprint,
    split,
)

SECRET = bytes(range(64)) + b"forecast-network recovery key"


class SplitTests(unittest.TestCase):
    def test_any_threshold_subset_reconstructs_the_secret(self):
        shares = split(SECRET, shares=5, threshold=3)
        self.assertEqual(len(shares), 5)
        for subset in itertools.combinations(shares, 3):
            with self.subTest(indices=[index for index, _ in subset]):
                self.assertEqual(combine(list(subset)), SECRET)

    def test_every_superset_of_the_threshold_also_reconstructs(self):
        shares = split(SECRET, shares=5, threshold=3)
        for size in (4, 5):
            for subset in itertools.combinations(shares, size):
                with self.subTest(size=size, indices=[index for index, _ in subset]):
                    self.assertEqual(combine(list(subset)), SECRET)

    def test_fewer_than_the_threshold_never_reconstructs(self):
        shares = split(SECRET, shares=5, threshold=3)
        for subset in itertools.combinations(shares, 2):
            with self.subTest(indices=[index for index, _ in subset]):
                self.assertNotEqual(combine(list(subset)), SECRET)

    def test_a_threshold_of_two_works_and_one_share_is_never_enough(self):
        shares = split(SECRET, shares=3, threshold=2)
        self.assertEqual(combine(shares[:2]), SECRET)
        self.assertEqual(combine([shares[0], shares[2]]), SECRET)
        self.assertNotEqual(shares[0][1], SECRET)

    def test_each_share_is_the_length_of_the_secret(self):
        shares = split(SECRET, shares=4, threshold=2)
        for _, values in shares:
            self.assertEqual(len(values), len(SECRET))

    def test_the_same_secret_splits_differently_every_time(self):
        first = split(SECRET, shares=3, threshold=2)
        second = split(SECRET, shares=3, threshold=2)
        self.assertNotEqual([v for _, v in first], [v for _, v in second],
                            "a reused polynomial would let one share leak across runs")

    def test_indices_are_one_based_and_distinct(self):
        shares = split(SECRET, shares=6, threshold=3)
        self.assertEqual([index for index, _ in shares], [1, 2, 3, 4, 5, 6])


class ValidationTests(unittest.TestCase):
    def test_impossible_parameters_are_refused(self):
        for shares, threshold in ((1, 1), (3, 1), (3, 4), (0, 0), (300, 3), (3, 0)):
            with self.subTest(shares=shares, threshold=threshold):
                with self.assertRaises(ValueError):
                    split(SECRET, shares=shares, threshold=threshold)

    def test_an_empty_secret_is_refused(self):
        with self.assertRaises(ValueError):
            split(b"", shares=3, threshold=2)

    def test_combining_rejects_a_repeated_share(self):
        shares = split(SECRET, shares=3, threshold=2)
        with self.assertRaises(ValueError):
            combine([shares[0], shares[0]])

    def test_combining_rejects_shares_of_different_lengths(self):
        with self.assertRaises(ValueError):
            combine([(1, b"abcd"), (2, b"ab")])

    def test_combining_needs_at_least_two_shares(self):
        with self.assertRaises(ValueError):
            combine(split(SECRET, shares=2, threshold=2)[:1])

    def test_combining_rejects_indices_outside_the_field(self):
        for index in (0, 256, -1):
            with self.subTest(index=index):
                with self.assertRaises(ValueError):
                    combine([(index, b"ab"), (2, b"cd")])


class EncodingTests(unittest.TestCase):
    def test_a_share_survives_a_round_trip_through_text(self):
        digest = fingerprint(SECRET)
        index, values = split(SECRET, shares=3, threshold=2)[0]
        text = encode_share(index, values, shares=3, threshold=2, key_fingerprint=digest)
        self.assertIn(FORMAT, text)
        decoded_index, decoded_values, meta = decode_share(text)
        self.assertEqual((decoded_index, decoded_values), (index, values))
        self.assertEqual(meta["threshold"], 2)
        self.assertEqual(meta["fingerprint"], digest)

    def test_text_that_is_not_a_share_is_refused(self):
        for text in ("{}", "not json", '{"format": "something-else"}',
                     '{"format": "%s", "index": 0, "value": "AA=="}' % FORMAT,
                     '{"format": "%s", "index": 300, "value": "AA=="}' % FORMAT,
                     '{"format": "%s", "index": 1}' % FORMAT,
                     '{"format": "%s", "index": 1, "value": "not base64!"}' % FORMAT):
            with self.subTest(text=text[:40]):
                with self.assertRaises((ValueError, TypeError)):
                    decode_share(text)

    def test_a_tampered_share_is_caught_by_the_fingerprint_rather_than_accepted(self):
        digest = fingerprint(SECRET)
        index, values, _ = decode_share(encode_share(1, b"x" * 8, shares=3, threshold=2,
                                                     key_fingerprint=digest))
        tampered = combine([(index, values), (2, bytes(8)), (3, bytes(8))])
        self.assertNotEqual(fingerprint(tampered), digest)


class FingerprintTests(unittest.TestCase):
    def test_the_fingerprint_is_stable_and_different_for_different_keys(self):
        self.assertEqual(fingerprint(SECRET), fingerprint(bytes(SECRET)))
        self.assertNotEqual(fingerprint(SECRET), fingerprint(SECRET + b"!"))

    def test_the_fingerprint_is_not_the_key(self):
        digest = fingerprint(SECRET)
        self.assertEqual(len(digest), 64)
        self.assertNotIn(base64.b64encode(SECRET).decode()[:16], digest)


if __name__ == "__main__":
    unittest.main()
