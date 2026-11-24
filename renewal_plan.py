"""命令行入口：证据查询、方案比较、订位分配与跨轮复议。

用法（从项目根目录）：

    python3 renewal_plan.py evidence                 # 树龄证据与支撑范围
    python3 renewal_plan.py compare                  # 三方案同年景现金对照
    python3 renewal_plan.py allocate                 # 库存+联保约束下订位
    python3 renewal_plan.py all                      # 全部输出到 输出/
    python3 renewal_plan.py allocate --planted PLANTED.json

``--planted`` 文件示例（已种地块作为新一轮硬约束）：

    {"H1-1": ["R1", 2027]}

仅使用标准库；读取资料包时以隔离方式运行：python3 -I renewal_plan.py ...
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from renewal.allocate import allocate
from renewal.engine import simulate_household
from renewal.evidence import EvidenceTable
from renewal.packet import read_packet
from renewal.plans import enumerate_plans
from renewal.report import (
    allocation_report,
    evidence_report,
    household_explanations,
    plan_comparison,
    render_text,
    write_json,
)

DEFAULT_PACKET = Path("资料/演练.json")
DEFAULT_OUT = Path("输出")


def build_artifacts(packet, *, planted=None):
    table = EvidenceTable(packet.history, packet.pool["min_analogs"])
    plans_by_house = enumerate_plans(
        list(packet.parcels), list(packet.stock), table, packet.pool
    )
    support_index = {row["house"]: row for row in packet.support}
    scenario_ids = sorted({row["id"] for row in packet.scenarios})
    simulations: dict = {}
    for house, plans in plans_by_house.items():
        house_parcels = [row for row in packet.parcels if row["house"] == house]
        paid_lots = {
            (row["block"], row["lot"]): row
            for row in packet.commitments
            if row["kind"] == "paid"
        }
        simulations[house] = {}
        for plan in plans:
            simulations[house][plan.label] = {
                sid: simulate_household(
                    plan,
                    parcels=house_parcels,
                    support=support_index[house],
                    scenarios=list(packet.scenarios),
                    evidence=table,
                    stock=list(packet.stock),
                    paid_lots=paid_lots,
                    scenario_id=sid,
                )
                for sid in scenario_ids
            }
    allocation = allocate(packet=packet, evidence=table, planted=planted)
    return table, plans_by_house, simulations, allocation


def load_planted(path: str | None) -> dict[str, tuple[str, int]] | None:
    if path is None:
        return None
    with Path(path).open(encoding="utf-8") as handle:
        raw = json.load(handle)
    return {block: (value[0], int(value[1])) for block, value in raw.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="老果园更新比较与苗木订位")
    parser.add_argument("command", choices=("evidence", "compare", "allocate", "all"))
    parser.add_argument("packet", type=Path, nargs="?", default=DEFAULT_PACKET)
    parser.add_argument("--planted", type=str, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    try:
        packet = read_packet(args.packet)
        planted = load_planted(args.planted)
        table, plans_by_house, simulations, allocation = build_artifacts(
            packet, planted=planted
        )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"无法完成分析：{exc}\n")
    args.out.mkdir(parents=True, exist_ok=True)

    if args.command in ("evidence", "all"):
        with (args.out / "树龄证据.json").open("w", encoding="utf-8") as handle:
            write_json(evidence_report(table), handle)
    if args.command in ("compare", "all"):
        with (args.out / "方案比较.json").open("w", encoding="utf-8") as handle:
            write_json(plan_comparison(plans_by_house, simulations), handle)
    if args.command in ("allocate", "all"):
        with (args.out / "订位结果.json").open("w", encoding="utf-8") as handle:
            write_json(allocation_report(allocation), handle)
        with (args.out / "农户解释.json").open("w", encoding="utf-8") as handle:
            write_json(
                household_explanations(
                    allocation, list(packet.support)
                ),
                handle,
            )

    text = render_text(
        table=table,
        plans_by_house=plans_by_house,
        simulations=simulations,
        allocation=allocation,
    )
    if args.command == "all":
        with (args.out / "经办摘要.txt").open("w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
