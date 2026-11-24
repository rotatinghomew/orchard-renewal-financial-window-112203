"""资料包口径校验的错误路径测试。"""
import copy
import json
import unittest
from pathlib import Path

from renewal.packet import PacketError, build_packet, read_packet

PACKET_PATH = Path("资料/演练.json")


class PacketValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = json.loads(PACKET_PATH.read_text(encoding="utf-8"))

    def _build(self, mutate):
        data = copy.deepcopy(self.raw)
        mutate(data)
        return build_packet(data)

    def test_missing_section_rejected(self):
        data = copy.deepcopy(self.raw)
        del data["stock"]
        with self.assertRaises(PacketError):
            build_packet(data)

    def test_scenario_coverage_gap_rejected(self):
        def mutate(data):
            data["scenarios"] = [
                row
                for row in data["scenarios"]
                if not (
                    row["id"] == "base"
                    and row["year"] == 2033
                    and row["region"] == "east"
                )
            ]
        with self.assertRaises(PacketError):
            self._build(mutate)

    def test_unknown_amendment_target_rejected(self):
        def mutate(data):
            data["amendments"][0]["orchard"] = "NOPE"
        with self.assertRaises(PacketError):
            self._build(mutate)

    def test_amendment_unknown_field_rejected(self):
        def mutate(data):
            data["amendments"][0]["field"] = "mu"
        with self.assertRaises(PacketError):
            self._build(mutate)

    def test_dangling_commitment_reference_rejected(self):
        def mutate(data):
            data["commitments"][0]["lot"] = "R9-S9-2099"
        with self.assertRaises(PacketError):
            self._build(mutate)

    def test_duplicate_history_key_rejected(self):
        def mutate(data):
            data["history"].append(dict(data["history"][0]))
        with self.assertRaises(PacketError):
            self._build(mutate)

    def test_support_year_length_must_match_horizon(self):
        def mutate(data):
            data["support"][0]["cost_by_year"] = [1000]
        with self.assertRaises(PacketError):
            self._build(mutate)

    def test_non_dict_container_rejected(self):
        with self.assertRaises(PacketError):
            build_packet({"history": 3})

    def test_amendments_apply_before_evidence_is_built(self):
        packet = read_packet(PACKET_PATH)
        amended = [
            row
            for row in packet.history
            if row.get("amended")
        ]
        self.assertTrue(amended)
        # O1 2025 的 kg 已被修订为 860。
        o1 = next(
            row
            for row in packet.history
            if row["orchard"] == "O1" and row["year"] == 2025
        )
        self.assertEqual(o1["kg"], 860)


if __name__ == "__main__":
    unittest.main()
