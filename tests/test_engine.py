"""逐年现金、借款还款与联保占用规则测试。"""
import unittest

from renewal.engine import lot_needs, simulate_household
from renewal.evidence import EvidenceTable
from renewal.packet import read_packet
from renewal.plans import enumerate_plans
from pathlib import Path

PACKET_PATH = Path("资料/演练.json")


class EngineTest(unittest.TestCase):
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
        cls.support = {row["house"]: row for row in cls.packet.support}
        cls.paid = {
            (row["block"], row["lot"]): row
            for row in cls.packet.commitments
            if row["kind"] == "paid"
        }

    def _sim(self, house, plan_label, scenario):
        parcels = [p for p in self.packet.parcels if p["house"] == house]
        plan = next(p for p in self.plans[house] if p.label == plan_label)
        return plan, simulate_household(
            plan,
            parcels=parcels,
            support=self.support[house],
            scenarios=list(self.packet.scenarios),
            evidence=self.table,
            stock=list(self.packet.stock),
            paid_lots=self.paid,
            scenario_id=scenario,
        )

    def test_paid_seedling_fee_not_charged_twice(self):
        # H1-1 的 R1-S1-2027 已付定金：种植年（2027）不得再扣苗款。
        plan, sim = self._sim(
            "H1",
            next(
                p.label
                for p in self.plans["H1"]
                if "H1-1:R1@2027" in p.label and "H1-2:旧树" in p.label
            ),
            "base",
        )
        needs = lot_needs(
            plan,
            [p for p in self.packet.parcels if p["house"] == "H1"],
            list(self.packet.stock),
            self.paid,
        )
        paid_need = next(n for n in needs if n.block == "H1-1")
        self.assertTrue(paid_need.already_paid)
        row_2027 = next(r for r in sim.years if r.year == 2027)
        self.assertEqual(row_2027.seedling_outlay, 0.0)

    def test_unpaid_seedling_fee_hits_planting_year_once(self):
        # 未付订位的苗款 = 株数 × 单价，只在种植年出现一次。
        plan, sim = self._sim(
            "H2",
            next(
                p.label
                for p in self.plans["H2"]
                if "H2-2:R2@2027" in p.label and "H2-1:旧树" in p.label
            ),
            "base",
        )
        rows = {r.year: r for r in sim.years}
        self.assertEqual(rows[2027].seedling_outlay, 300 * 9)
        self.assertEqual(rows[2028].seedling_outlay, 0.0)

    def test_cleared_idle_block_has_no_revenue_only_committed_cost(self):
        # H1-1 已清园；若方案 2028 才种，2027 年该地块无收入无经营支出。
        plan = next(
            p
            for p in self.plans["H1"]
            if "H1-1:R1@2028" in p.label and "H1-2:旧树" in p.label
        )
        parcels = [p for p in self.packet.parcels if p["house"] == "H1"]
        sim = simulate_household(
            plan,
            parcels=parcels,
            support=self.support["H1"],
            scenarios=list(self.packet.scenarios),
            evidence=self.table,
            stock=list(self.packet.stock),
            paid_lots=self.paid,
            scenario_id="base",
        )
        detail = next(
            b
            for r in sim.years
            if r.year == 2027
            for b in r.block_detail
            if b["block"] == "H1-1"
        )
        self.assertEqual(detail["state"], "cleared_idle")
        self.assertNotIn("revenue", detail)

    def test_borrow_lifts_to_floor_and_repay_returns_surplus(self):
        # 选一个会触发借款的紧现金方案，检查借/还与底线。
        plan = next(
            p
            for p in self.plans["H2"]
            if "H2-1:R1@2027" in p.label and "H2-2:R2@2027" in p.label
        )
        parcels = [p for p in self.packet.parcels if p["house"] == "H2"]
        sim = simulate_household(
            plan,
            parcels=parcels,
            support=self.support["H2"],
            scenarios=list(self.packet.scenarios),
            evidence=self.table,
            stock=list(self.packet.stock),
            paid_lots={},
            scenario_id="base",
        )
        floor = self.support["H2"]["floor"]
        for row in sim.years:
            self.assertGreaterEqual(row.end_cash, floor - 0.01)
            if row.borrow > 0:
                self.assertAlmostEqual(
                    row.pre_loan_cash + row.borrow,
                    floor if row.repay == 0 else row.pre_loan_cash + row.borrow,
                    places=2,
                )
        # 有还款年时债务必须下降。
        debts = [r.debt_outstanding for r in sim.years]
        self.assertIn(0.0, debts)

    def test_loan_limit_breach_is_reported_as_shortfall(self):
        plan = next(
            p
            for p in self.plans["H2"]
            if "H2-1:R1@2027" in p.label and "H2-2:R2@2027" in p.label
        )
        parcels = [p for p in self.packet.parcels if p["house"] == "H2"]
        sim = simulate_household(
            plan,
            parcels=parcels,
            support=self.support["H2"],
            scenarios=list(self.packet.scenarios),
            evidence=self.table,
            stock=list(self.packet.stock),
            paid_lots={},
            scenario_id="shared_frost",
        )
        self.assertFalse(sim.feasible)
        self.assertTrue(any(r.shortfall > 0 for r in sim.years))
        # 借款余额任何一年都不能突破 loan_limit。
        for row in sim.years:
            self.assertLessEqual(
                row.debt_outstanding,
                self.support["H2"]["loan_limit"] + 0.01,
            )

    def test_scenarios_are_independent_and_same_year_shocks_apply_together(self):
        plan = next(iter(self.plans["H3"]))
        parcels = [p for p in self.packet.parcels if p["house"] == "H3"]
        sims = {
            sid: simulate_household(
                plan,
                parcels=parcels,
                support=self.support["H3"],
                scenarios=list(self.packet.scenarios),
                evidence=self.table,
                stock=list(self.packet.stock),
                paid_lots={},
                scenario_id=sid,
            )
            for sid in ("base", "low_price", "shared_frost")
        }
        # 同一方案在不同情景下走独立现金轨迹，不能互抵。
        self.assertNotEqual(
            sims["base"].min_end_cash, sims["shared_frost"].min_end_cash
        )
        frost_row = next(
            r for r in sims["shared_frost"].years if r.year == 2027
        )
        # 同一果园的多块地在同年景一起受冲击，不能按互相独立消掉风险。
        producing = [b for b in frost_row.block_detail if "revenue" in b]
        self.assertEqual(len(producing), 2)
        for block in producing:
            self.assertEqual(block["yield_factor"], 0.56)
            self.assertEqual(block["price_factor"], 0.9)
        # 对照基准年，两块地的系数同样同时为 1。
        base_row = next(r for r in sims["base"].years if r.year == 2027)
        for block in base_row.block_detail:
            if "revenue" in block:
                self.assertEqual(block["yield_factor"], 1.0)

    def test_debt_carries_over_and_reduces_next_year_headroom(self):
        plan = next(
            p
            for p in self.plans["H2"]
            if "H2-1:R1@2027" in p.label and "H2-2:R3@2028" in p.label
        )
        parcels = [p for p in self.packet.parcels if p["house"] == "H2"]
        sim = simulate_household(
            plan,
            parcels=parcels,
            support=self.support["H2"],
            scenarios=list(self.packet.scenarios),
            evidence=self.table,
            stock=list(self.packet.stock),
            paid_lots={},
            scenario_id="low_price",
        )
        # 债务滚动：次年年初未还余额 = 上年年末余额。
        for earlier, later in zip(sim.years, sim.years[1:]):
            self.assertAlmostEqual(
                later.pre_loan_cash,
                earlier.end_cash
                + later.operating_cash
                - later.seedling_outlay,
                places=2,
            )

    def test_guarantee_demand_is_summed_per_scenario_without_netting(self):
        # 联保占用按情景各自求和：base 没借款不能拿来抵 frost 的占用。
        from renewal.allocate import allocate

        result = allocate(packet=self.packet, evidence=self.table)
        scenario_ids = {s for s in result.guarantee_demand}
        # base 情景若没有任何借款，联保需求里就不该出现 base 的非零占用。
        base_demand = result.guarantee_demand.get("base", [])
        self.assertTrue(all(value == 0 for value in base_demand))
        # frost 有真实占用，且等于各接受户同年债务之和。
        frost = result.guarantee_demand.get("shared_frost", [])
        for offset, total in enumerate(frost):
            recomputed = sum(
                next(
                    sim for sim in outcome.simulations
                    if sim.scenario == "shared_frost"
                ).years[offset].guarantee_used
                for outcome in result.accepted.values()
            )
            self.assertAlmostEqual(total, recomputed, places=2)


if __name__ == "__main__":
    unittest.main()
