"""证据表与修订规则测试。"""
import unittest

from renewal.evidence import EvidenceTable
from renewal.packet import build_packet, read_packet
from pathlib import Path

PACKET_PATH = Path("资料/演练.json")


class EvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packet = read_packet(PACKET_PATH)
        cls.table = EvidenceTable(
            cls.packet.history, cls.packet.pool["min_analogs"]
        )

    def test_min_analogs_counts_distinct_orchards_not_rows(self):
        # east/R2/S1 树龄3 有 6 个不同果园贡献。
        ev = self.table.lookup("east", "R2", "S1", 3)
        self.assertTrue(ev.supported)
        self.assertEqual(ev.orchard_count, 6)
        self.assertEqual(
            len({a.orchard for a in ev.analogs}), len(ev.analogs)
        )

    def test_missing_combination_is_empty_pending_evidence_not_zero(self):
        # hill/R3/S2 树龄0 没有任何记录：不得补零，不得借别处高产园。
        ev = self.table.lookup("hill", "R3", "S2", 0)
        self.assertFalse(ev.supported)
        self.assertEqual(ev.analogs, ())
        self.assertIsNone(ev.median("revenue_per_mu"))

    def test_nonfinal_observations_never_enter_evidence(self):
        # 2026 年全部 final=false，在途观察只列出不统计。
        ev = self.table.lookup("east", "R1", "S1", 7)
        self.assertFalse(ev.supported)  # 只有 2 个完整果园
        self.assertGreaterEqual(len(ev.in_transit), 1)
        analog_years = {a.year for a in ev.analogs}
        self.assertNotIn(2026, analog_years)

    def test_amendments_are_applied_and_flagged(self):
        raw = {
            "history": [
                {
                    "orchard": "O1", "block": "b", "region": "east",
                    "planted": 2017, "year": 2025, "root": "R2",
                    "scion": "S1", "mu": 2, "kg": 100, "revenue_yuan": 500,
                    "cost": 200, "final": True, "know": "2025-11-15",
                }
            ],
            "parcels": [], "scenarios": [], "stock": [],
            "commitments": [], "support": [],
            "pool": {"horizon": [2027, 2033], "plant_years": [2027],
                     "min_analogs": 1, "annual_guarantee_limit": 1},
            "amendments": [
                {"orchard": "O1", "year": 2025, "field": "kg",
                 "new_value": 860, "known": "2026-12-03"}
            ],
        }
        packet = build_packet(raw)
        row = next(iter(packet.history))
        self.assertEqual(row["kg"], 860)
        self.assertTrue(row["amended"])
        table = EvidenceTable(packet.history, 1)
        ev = table.lookup("east", "R2", "S1", 8)
        self.assertTrue(ev.analogs[0].amended)

    def test_old_tree_anchor_uses_same_region_scion_and_age_window(self):
        # H1 旧树 2027 年 17 岁，允许 15~19 岁同区同接穗记录。
        anchor = self.table.old_tree_anchor("east", "S1", 17)
        self.assertTrue(anchor.supported)
        self.assertTrue(all(a.age in range(15, 20) for a in anchor.analogs))
        self.assertTrue(all(a.orchard.startswith(("O1", "O2", "X-east"))
                            for a in anchor.analogs))
        # 山区旧树不能借东区锚。
        hill = self.table.old_tree_anchor("hill", "S1", 17)
        self.assertTrue(all(a.orchard.startswith(("O1", "O2", "X-hill"))
                            for a in hill.analogs))

    def test_signable_combinations_match_horizon_needs(self):
        # 2027 种的树到 2033 年为 6 龄：东区 R1/R2-S1 全程够证。
        for root in ("R1", "R2"):
            ages = self.table.combination_supported_ages("east", root, "S1")
            self.assertIn(6, ages)
        # 山区除 R2-S2 外的新组合在 6 龄前就缺证。
        self.assertIn(
            6, self.table.combination_supported_ages("hill", "R2", "S2")
        )
        self.assertNotIn(
            4, self.table.combination_supported_ages("hill", "R3", "S1")
        )

    def test_evidence_reports_uncertainty_range_and_sources(self):
        ev = self.table.lookup("east", "R1", "S1", 4)
        summary = ev.as_dict()
        self.assertLess(
            summary["revenue_per_mu"]["min"],
            summary["revenue_per_mu"]["max"],
        )
        sources = {a["orchard"] for a in summary["analogs"]}
        self.assertEqual(len(sources), ev.orchard_count)


if __name__ == "__main__":
    unittest.main()
