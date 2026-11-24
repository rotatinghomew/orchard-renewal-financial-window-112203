"""把证据、方案比较与订位结果整理成果农和经办人可读的报告。"""
from __future__ import annotations

import json
from typing import Any, TextIO

from .allocate import AllocationResult
from .engine import HouseholdSimulation
from .evidence import EvidenceTable
from .explanations import explain_household
from .plans import Plan


def evidence_report(table: EvidenceTable) -> dict[str, Any]:
    """全部树龄证据，含支撑果园与在途观察，供人工复核。"""
    return {
        "min_analogs": table._min_analogs,
        "evidence": [ev.as_dict() for ev in table.all_evidence()],
    }


def plan_comparison(
    plans_by_house: dict[str, tuple[Plan, ...]],
    simulations: dict[str, dict[str, dict[str, HouseholdSimulation]]],
) -> dict[str, Any]:
    """方案 × 情景的逐年现金与可行性对照。

    simulations[house][plan_label][scenario_id] -> HouseholdSimulation
    """
    result: dict[str, Any] = {"houses": {}}
    for house, plans in sorted(plans_by_house.items()):
        house_doc: dict[str, Any] = {"plans": []}
        for plan in plans:
            sims = simulations[house][plan.label]
            house_doc["plans"].append(
                {
                    **plan.as_dict(),
                    "scenarios": {
                        sid: _sim_summary(sim) for sid, sim in sorted(sims.items())
                    },
                }
            )
        result["houses"][house] = house_doc
    return result


def _sim_summary(sim: HouseholdSimulation) -> dict[str, Any]:
    return {
        "feasible": sim.feasible,
        "min_end_cash": round(sim.min_end_cash, 2),
        "terminal_cash": round(sim.terminal_cash, 2),
        "max_guarantee_used": round(sim.max_guarantee_used, 2),
        "total_borrowed": round(sim.total_borrowed, 2),
        "cumulative_operating": round(sim.cumulative_operating, 2),
        "yearly": [
            {
                "year": row.year,
                "operating_cash": round(row.operating_cash, 2),
                "seedling_outlay": round(row.seedling_outlay, 2),
                "pre_loan_cash": round(row.pre_loan_cash, 2),
                "borrow": round(row.borrow, 2),
                "repay": round(row.repay, 2),
                "end_cash": round(row.end_cash, 2),
                "debt_outstanding": round(row.debt_outstanding, 2),
                "shortfall": round(row.shortfall, 2),
            }
            for row in sim.years
        ],
    }


def allocation_report(result: AllocationResult) -> dict[str, Any]:
    return result.as_dict()


def household_explanations(
    result: AllocationResult, support_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """每户一份“为什么不能订 / 什么支持能改年份”的解释。"""
    loan_limits = {row["house"]: float(row["loan_limit"]) for row in support_rows}
    docs = {}
    for house, outcomes in sorted(result.evaluated.items()):
        docs[house] = explain_household(
            house,
            all_outcomes=outcomes,
            allocation=result,
            loan_limit=loan_limits[house],
        )
    return {"houses": docs}
    return result.as_dict()


def write_json(doc: dict[str, Any], handle: TextIO) -> None:
    json.dump(doc, handle, ensure_ascii=False, indent=2, sort_keys=False)
    handle.write("\n")


def render_text(
    *,
    table: EvidenceTable,
    plans_by_house,
    simulations,
    allocation: AllocationResult,
) -> str:
    """经办人用的纯文字摘要：证据范围、方案排序与订位解释。"""
    lines: list[str] = []
    lines.append("== 树龄收益证据支撑范围 ==")
    for ev in table.all_evidence():
        flag = "够证" if ev.supported else "待补证"
        rev = ev.range("revenue_per_mu")
        rev_text = (
            f"每亩收入 {rev[0]:.0f}~{rev[1]:.0f} 元" if rev else "无完整年度记录"
        )
        lines.append(
            f"[{flag}] {ev.region}/{ev.root}/{ev.scion} 树龄{ev.age}: "
            f"{ev.orchard_count} 个果园，{rev_text}"
            + (f"，另有 {len(ev.in_transit)} 条在途观察" if ev.in_transit else "")
        )

    lines.append("")
    lines.append("== 农户方案比较（同一年景并排） ==")
    for house, plans in sorted(plans_by_house.items()):
        lines.append(f"-- {house} --")
        for plan in plans:
            sims = simulations[house][plan.label]
            cells = []
            for sid, sim in sorted(sims.items()):
                cells.append(
                    f"{sid}: {'可行' if sim.feasible else '缺口'}"
                    f"/最低现金{sim.min_end_cash:.0f}"
                    f"/峰值联保{sim.max_guarantee_used:.0f}"
                )
            tag = "可签订" if plan.signable else "待补证"
            lines.append(
                f"  {plan.label} [{tag}] " + "；".join(cells)
            )

    lines.append("")
    lines.append("== 苗木订位分配 ==")
    for entry in allocation.entries:
        lock = "不可撤销" if entry.locked else "可调整"
        lines.append(
            f"{entry.house} {entry.block} -> {entry.lot} {entry.count}株 "
            f"起苗截止{entry.deadline} ({entry.commitment_kind}/{lock})"
        )
    lines.append("")
    lines.append("批次占用 / 库存：")
    for lot, used in sorted(allocation.lot_usage.items()):
        lines.append(f"  {lot}: {used}")
    if allocation.warnings:
        lines.append("")
        lines.append("== 提示 ==")
        for warning in allocation.warnings:
            lines.append("  " + warning)
    return "\n".join(lines)
