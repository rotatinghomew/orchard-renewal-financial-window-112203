"""方案枚举与农户解释测试。"""
import unittest

from renewal.allocate import allocate
from renewal.evidence import EvidenceTable
from renewal.explanations import explain_household
from renewal.packet import read_packet
from renewal.plans import KEEP, PLANT, enumerate_plans
from pathlib import Path

PACKET_PATH = Path("资料/演练.json")


class PlanEnumerationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packet = read_packet(PACKET_PATH)
        cls.table = EvidenceTable(
            cls.packet.history, cls.packet.pool["min_analogs"]
        )
        cls.plans = enumerate_plans(
            list(cls.packet.parcels),
            list(cls.packet.stock),
            cls.table,
            cls.packet.pool,
        )

    def test_cleared_block_cannot_keep_old_trees(self):
        # H1-1 已清园，任何方案里它都必须是种植动作，不能退回旧树。
        for plan in self.plans["H1"]:
            action = plan.block_action("H1-1")
            self.assertEqual(action.kind, PLANT)
        # 未清园地块可以保留旧树。
        self.assertTrue(
            any(
                plan.block_action("H2-1").kind == KEEP
                for plan in self.plans["H2"]
            )
        )

    def test_plant_year_starts_age_zero(self):
        for house, plans in self.plans.items():
            for plan in plans:
                for action in plan.actions:
                    if action.kind == PLANT:
                        self.assertIn(
                            action.plant_year, self.packet.pool["plant_years"]
                        )

    def test_staggered_plans_exist_alongside_immediate_and_keep(self):
        labels = [p.label for p in self.plans["H3"]]
        self.assertTrue(any("旧树" in label for label in labels))
        self.assertTrue(
            any(label.startswith("立即更新") for label in labels)
        )
        self.assertTrue(
            any(label.startswith("分年更新") for label in labels)
        )

    def test_pending_evidence_blocks_guarantee_but_plan_still_listed(self):
        # 山区 R3 组合缺证：方案仍列出供参考，但 signable=False。
        pending = [
            p
            for p in self.plans["H4"]
            if any(
                a.kind == PLANT and a.root == "R3" for a in p.actions
            )
        ]
        self.assertTrue(pending)
        self.assertTrue(all(not p.signable for p in pending))


class ExplanationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packet = read_packet(PACKET_PATH)
        cls.table = EvidenceTable(
            cls.packet.history, cls.packet.pool["min_analogs"]
        )
        cls.result = allocate(packet=cls.packet, evidence=cls.table)

    def test_every_house_gets_reasons_and_support_levers(self):
        for house in {p["house"] for p in self.packet.parcels}:
            doc = explain_household(
                house,
                all_outcomes=self.result.evaluated[house],
                allocation=self.result,
                loan_limit=6500,
            )
            self.assertIsNotNone(doc["chosen_plan"])
            self.assertTrue(doc["why_higher_revenue_not_bookable"])
            levers = doc["support_that_changes_feasible_year"]
            self.assertIn("cash_injection_needed", levers)
            self.assertIn("evidence_to_collect", levers)

    def test_levers_point_to_concrete_evidence_gaps_and_alt_lots(self):
        doc = explain_household(
            "H4",
            all_outcomes=self.result.evaluated["H4"],
            allocation=self.result,
            loan_limit=6500,
        )
        levers = doc["support_that_changes_feasible_year"]
        # H4 是山区户，待补证提示里应出现具体地区/砧穗/树龄。
        self.assertTrue(
            any("hill" in item for item in levers["evidence_to_collect"])
        )
        # 替代批次必须是资料里真实存在的批次。
        real_lots = {row["lot"] for row in self.packet.stock}
        self.assertTrue(
            set(levers["guaranteed_lot_alternatives"]) <= real_lots
        )


if __name__ == "__main__":
    unittest.main()
