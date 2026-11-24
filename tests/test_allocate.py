"""订位分配、库存超卖、联保池与跨轮锁定测试。"""
import unittest

from renewal.allocate import allocate
from renewal.evidence import EvidenceTable
from renewal.packet import build_packet, read_packet
from pathlib import Path

PACKET_PATH = Path("资料/演练.json")


def _table(packet):
    return EvidenceTable(packet.history, packet.pool["min_analogs"])


class AllocationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packet = read_packet(PACKET_PATH)
        cls.result = allocate(packet=cls.packet, evidence=_table(cls.packet))

    def test_paid_commitment_is_locked_and_never_moved(self):
        paid_entries = [e for e in self.result.entries if e.commitment_kind == "paid"]
        self.assertEqual(len(paid_entries), 1)
        entry = paid_entries[0]
        self.assertEqual((entry.block, entry.lot), ("H1-1", "R1-S1-2027"))
        self.assertTrue(entry.locked)
        # 条目带起苗期限，已付订位不能被移到下一年。
        self.assertEqual(entry.deadline, "2026-12-10")
        self.assertFalse(
            any(
                e.block == "H1-1" and e.plant_year != 2027
                for e in self.result.entries
            )
        )

    def test_quotes_are_revocable_and_do_not_oversell_lots(self):
        # 资料中 R2-S1-2027 的 quote 总量（400）超过库存（300）。
        capacity = {row["lot"]: row["count"] for row in self.packet.stock}
        for lot, used in self.result.lot_usage.items():
            self.assertLessEqual(used, capacity[lot])
        r2_2027 = [
            e for e in self.result.entries if e.lot == "R2-S1-2027"
        ]
        self.assertLessEqual(
            sum(e.count for e in r2_2027), capacity["R2-S1-2027"]
        )
        # H2-1 的 R1-S1-2027 quote 与 paid 冲突，不得同时占用。
        h2_entries = [e for e in self.result.entries if e.house == "H2"]
        self.assertFalse(
            any(e.lot == "R1-S1-2027" for e in h2_entries)
        )
        # quote 是可撤回申请，未中签时不得算作已售库存。
        quote_lots_in_data = {
            (c["block"], c["lot"])
            for c in self.packet.commitments
            if c["kind"] == "quote"
        }
        placed = {(e.block, e.lot) for e in self.result.entries}
        self.assertTrue(quote_lots_in_data - placed)  # 至少一个 quote 未中签

    def test_unsupported_combinations_never_occupy_guaranteed_slots(self):
        # 山区 R1/R3 组合待补证，分配结果里不能出现这些保证性苗位。
        hill_parcels = {
            p["block"] for p in self.packet.parcels if p["region"] == "hill"
        }
        for entry in self.result.entries:
            if entry.block in hill_parcels and entry.commitment_kind != "paid":
                self.assertEqual(
                    (entry.root, entry.scion), ("R2", "S2"),
                    f"待补证组合被签订：{entry}",
                )

    def test_joint_guarantee_pool_respected_in_every_scenario_year(self):
        limit = self.packet.pool["annual_guarantee_limit"]
        for sid, values in self.result.guarantee_demand.items():
            for value in values:
                self.assertLessEqual(value, limit + 0.01)

    def test_reported_guarantee_demand_matches_accepted_plans(self):
        # 联保占用必须能从接受方案的逐年模拟重新加总出来。
        years = range(
            self.packet.pool["horizon"][0],
            self.packet.pool["horizon"][1] + 1,
        )
        for sid in {r["id"] for r in self.packet.scenarios}:
            recomputed = [0.0] * len(list(years))
            for outcome in self.result.accepted.values():
                sim = next(s for s in outcome.simulations if s.scenario == sid)
                for offset, row in enumerate(sim.years):
                    recomputed[offset] += row.guarantee_used
            reported = self.result.guarantee_demand.get(sid, [0.0] * 7)
            for got, want in zip(reported, recomputed):
                self.assertAlmostEqual(got, want, places=2)

    def test_higher_expected_revenue_still_rejected_with_explanation(self):
        # H2 双块“立即更新”基准累计现金更高，但坏年景现金破底线 -> 不订；
        # 系统只给它保留旧树或单块分年更新。
        h2 = self.result.accepted.get("H2")
        self.assertIsNotNone(h2)
        planted_blocks = [
            a.block for a in h2.plan.actions if a.kind == "plant"
        ]
        self.assertLessEqual(len(planted_blocks), 1)
        # 每个被拒/无位家庭都带可读原因。
        for outcome in self.result.rejected.values():
            self.assertTrue(outcome.rejection_reasons)
        for reasons in self.result.unplaceable.values():
            self.assertTrue(reasons)

    def test_each_household_plan_feasible_in_all_scenarios_when_accepted(self):
        for outcome in self.result.accepted.values():
            self.assertTrue(
                all(sim.feasible for sim in outcome.simulations),
                f"{outcome.house} 存在破底线情景",
            )

    def test_staggering_relieves_pool_tightness(self):
        # 接受的种植年应当错开（同一坏年景一起吃紧时的分散要求）。
        plant_years = [
            e.plant_year for e in self.result.entries if not e.locked
        ]
        self.assertGreater(len(set(plant_years)), 1)


class LockAndRerunTest(unittest.TestCase):
    def test_planted_blocks_enter_next_round_as_hard_constraints(self):
        packet = read_packet(PACKET_PATH)
        # 假设新一轮时 H2-2 已按 R2 在 2029 种上（2029 批次尚无竞争）。
        result = allocate(
            packet=packet,
            evidence=_table(packet),
            planted={"H2-2": ("R2", 2029)},
        )
        h2 = result.accepted["H2"]
        action = h2.plan.block_action("H2-2")
        self.assertEqual((action.root, action.plant_year), ("R2", 2029))
        entry = next(e for e in result.entries if e.block == "H2-2")
        self.assertTrue(entry.locked)

    def test_locked_plans_that_break_raise_instead_of_silently_replacing(self):
        packet = read_packet(PACKET_PATH)
        # H1-1 已有 paid R1@2027，再强制 R2@2028 属于冲突约束。
        with self.assertRaises(ValueError):
            allocate(
                packet=packet,
                evidence=_table(packet),
                planted={"H1-1": ("R2", 2028)},
            )

    def test_new_round_keeps_unrelated_signed_lots_when_estimates_change(self):
        packet = read_packet(PACKET_PATH)
        first = allocate(packet=packet, evidence=_table(packet))
        # 用新增修订模拟“新一季资料推翻旧估计”，重比时把已种地块
        # 与不可撤销订位作为约束带入；不相关组合的已签苗位不能被挤掉。
        rebuilt = build_packet(
            _raw_with_extra_amendment(), as_of="2027-11-20"
        )
        second = allocate(
            packet=rebuilt,
            evidence=_table(rebuilt),
            planted={"H3-1": ("R2", 2027)},
        )
        first_locked_lots = {
            (e.block, e.lot) for e in first.entries if e.locked
        }
        second_lots = {(e.block, e.lot) for e in second.entries}
        self.assertTrue(first_locked_lots <= second_lots)


def _raw_with_extra_amendment():
    import json
    raw = json.loads(Path(PACKET_PATH).read_text(encoding="utf-8"))
    raw["amendments"].append(
        {
            "orchard": "O3", "year": 2023, "field": "kg",
            "new_value": 900, "known": "2027-11-20",
        }
    )
    return raw


class OversellTest(unittest.TestCase):
    def test_guarantee_pool_blocks_combination_that_passes_per_household_checks(self):
        """逐户都能过、但联保池合计超限的组合必须被拒绝。"""
        # 3 个新树果园各提供树龄 0~6 的完整记录，保证 2027 种植全程够证；
        # 3 个旧树果园各覆盖 7 个年度，支撑规划年内逐年长龄的旧树锚。
        new_history = [
            {
                "orchard": f"N{i}", "block": f"nb{i}", "region": "east",
                "planted": 2020, "year": 2020 + age,
                "root": "R1", "scion": "S1", "mu": 2,
                "kg": 800 + 900 * age, "revenue_yuan": 4000 + 4500 * age,
                "cost": 1200 + 60 * age, "final": True,
                "know": f"{2020 + age}-11-15",
            }
            for i in range(3)
            for age in range(7)
        ]
        old_history = [
            {
                "orchard": f"D{i}", "block": f"db{i}", "region": "east",
                "planted": 2008, "year": 2027 + offset, "root": "old",
                "scion": "S1", "mu": 2, "kg": 2400, "revenue_yuan": 20000,
                "cost": 3000, "final": True,
                "know": f"{2027 + offset}-11-15",
            }
            for i in range(3)
            for offset in range(7)
        ]
        raw = {
            "history": new_history + old_history,
            "parcels": [
                {
                    "house": f"H{i}", "block": f"H{i}-1", "region": "east",
                    "mu": 2, "old_age": 19, "root_choices": ["R1"],
                    "scion": "S1", "cleared": False, "nursery_count": 200,
                    "old_scion": "S1",
                }
                for i in range(3)
            ],
            "scenarios": [
                {
                    "id": sid, "year": year, "region": "east",
                    "price_factor": 0.5 if sid == "frost" else 1.0,
                    "yield_factor": 0.5 if sid == "frost" else 1.0,
                }
                for sid in ("base", "frost")
                for year in range(2027, 2034)
            ],
            "stock": [
                {
                    "lot": f"R1-S1-{year}", "root": "R1", "scion": "S1",
                    "year": year, "count": 1000, "deadline": "2026-12-10",
                    "unit_price": 6,
                }
                for year in (2027, 2028, 2029)
            ],
            "commitments": [],
            "support": [
                {
                    "house": f"H{i}", "cash_start": 5500, "floor": 4000,
                    "cost_by_year": [2600] * 7, "loan_limit": 6000,
                }
                for i in range(3)
            ],
            "pool": {
                # 池额小于三户同年借款之和：必须靠错年份或部分保留旧树满足。
                "annual_guarantee_limit": 5000,
                "plant_years": [2027, 2028, 2029],
                "horizon": [2027, 2033],
                "min_analogs": 2,
            },
            "amendments": [],
        }
        packet = build_packet(raw)
        table = _table(packet)
        # 前置确认：新树证据在 0~6 龄、旧树锚在规划年内都够证。
        self.assertTrue(
            all(
                table.lookup("east", "R1", "S1", age).supported
                for age in range(7)
            )
        )
        self.assertTrue(
            all(
                table.old_tree_anchor("east", "S1", 19 + offset).supported
                for offset in range(7)
            )
        )
        result = allocate(packet=packet, evidence=table)
        for sid, values in result.guarantee_demand.items():
            for value in values:
                self.assertLessEqual(
                    value, packet.pool["annual_guarantee_limit"] + 0.01
                )
        # 至少有一户被错开或保留旧树，而不是三户同年一起更新。
        plant_years = [e.plant_year for e in result.entries]
        self.assertTrue(plant_years)
        self.assertLessEqual(
            max(
                sum(1 for y in plant_years if y == year)
                for year in set(plant_years)
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
