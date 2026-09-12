"""Arithmetic checks for the research calculator, with no RPC or signing."""

import json
import struct
import unittest

from scripts.estimate_solana_storage import (
    SNAPSHOT,
    estimate,
    light_v2_cost,
    rent,
    research_summary,
    tree_bytes,
)


class SolanaResearchCostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = json.loads(SNAPSHOT.read_text())

    def test_first_deploy_does_not_double_count_buffer_but_upgrade_needs_liquidity(self):
        case = estimate(self.snapshot, program_kib=64, active_markets=0, live_disputes=0,
                        fee_reserve=0)
        expected = sum(rent(self.snapshot, n) for n in (65581, 36, 256)) + 4 * rent(self.snapshot, 128)
        self.assertEqual(case["initial_budget_lamports"], expected)
        self.assertEqual(case["initial_plus_one_upgrade_buffer_lamports"],
                         expected + rent(self.snapshot, 65581))

    def test_candidate_binary_layout_sums_without_native_padding(self):
        market = "<8s6BH" + "32s" * 8 + "Q" + "I" * 4 + "Q" * 8
        dispute = "<16s" + "32s" * 4 + "QII"
        self.assertEqual(struct.calcsize(market), 360)
        self.assertEqual(struct.calcsize(dispute), 160)
        self.assertEqual(64 + 16 * struct.calcsize(market), 5824)

    def test_pages_charge_full_capacity_and_round_up(self):
        for count, expected_pages in ((0, 0), (1, 1), (16, 1), (17, 2), (500, 32)):
            with self.subTest(count=count):
                case = estimate(self.snapshot, program_kib=64, active_markets=count,
                                live_disputes=0, paged=True)
                self.assertEqual(case["funded_market_accounts"], expected_pages)
                self.assertEqual(case["lamports"]["active_market_accounts"],
                                 expected_pages * rent(self.snapshot, 5824))

    def test_tree_size_includes_changelog_and_canopy(self):
        self.assertEqual(tree_bytes(14, 64, 0), 31800)
        self.assertEqual(tree_bytes(14, 64, 10), 97272)
        for args in ((0, 64, 0), (14, 63, 0), (14, 64, 15), (True, 64, 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                tree_bytes(*args)

    def test_light_cost_includes_base_tree_address_and_each_new_leaf(self):
        self.assertEqual(light_v2_cost(1, 0), 20300)
        self.assertEqual(light_v2_cost(1, 1), 30600)
        self.assertEqual(light_v2_cost(1000, 11), 133600000)
        self.assertEqual(light_v2_cost(0, 11), 0)
        with self.assertRaises(ValueError):
            light_v2_cost(-1, 0)

    def test_missing_quotes_and_invalid_inputs_fail_instead_of_guessing(self):
        with self.assertRaises(ValueError):
            rent(self.snapshot, 999)
        with self.assertRaises(ValueError):
            estimate(self.snapshot, program_kib=65, active_markets=1, live_disputes=0)
        with self.assertRaises(ValueError):
            estimate(self.snapshot, program_kib=64, active_markets=True, live_disputes=0)

    def test_budget_reports_initial_and_upgrade_separately(self):
        cases = research_summary(self.snapshot)["scenarios"]
        packed64 = next(c for c in cases if c["layout"] == "16_per_page"
                        and c["hypothetical_program_kib"] == 64)
        self.assertTrue(packed64["initial_within_two_sol"])
        self.assertFalse(packed64["including_upgrade_buffer_within_two_sol"])
        packed48 = next(c for c in cases if c["layout"] == "16_per_page"
                        and c["hypothetical_program_kib"] == 48)
        self.assertTrue(packed48["including_upgrade_buffer_within_two_sol"])

    def test_dispute_surge_can_exceed_budget_without_premature_rent_refunds(self):
        normal = estimate(self.snapshot, program_kib=64, active_markets=300, live_disputes=15)
        surge = estimate(self.snapshot, program_kib=64, active_markets=300, live_disputes=256)
        self.assertTrue(normal["including_upgrade_buffer_within_two_sol"])
        self.assertFalse(surge["including_upgrade_buffer_within_two_sol"])
        self.assertEqual(surge["initial_budget_lamports"] - normal["initial_budget_lamports"],
                         (256 - 15) * rent(self.snapshot, 160))


if __name__ == "__main__":
    unittest.main()
