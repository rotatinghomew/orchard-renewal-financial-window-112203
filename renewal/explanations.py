"""面向农户的可解释输出。

回答两个具体问题：

1. 为什么预计收益更高的方案仍然不能订苗？
2. 哪一笔“确认的支持”会改变它能选择的更新年份？

解释全部基于同一份逐年模拟结果，不引入新的估计：
现金缺口按情景/年份列出，待补证按地块列出，库存与联保按批次列出。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .allocate import AllocationResult, HouseholdOutcome


@dataclass(frozen=True)
class SupportLevers:
    """改变可行年份的确认支持杠杆（只做口径化提示，不自动改方案）。"""

    house: str
    cash_injection_needed: dict[str, float]      # scenario -> 累计缺口
    extra_loan_headroom_needed: dict[str, float]
    guaranteed_lot_alternatives: tuple[str, ...]
    evidence_to_collect: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "house": self.house,
            "cash_injection_needed": {
                k: round(v, 2) for k, v in self.cash_injection_needed.items()
            },
            "extra_loan_headroom_needed": {
                k: round(v, 2) for k, v in self.extra_loan_headroom_needed.items()
            },
            "guaranteed_lot_alternatives": list(self.guaranteed_lot_alternatives),
            "evidence_to_collect": list(self.evidence_to_collect),
        }


def explain_household(
    house: str,
    *,
    all_outcomes: list[HouseholdOutcome],
    allocation: AllocationResult,
    loan_limit: float,
) -> dict[str, Any]:
    """生成单户解释文档：为什么不能订、什么支持能解锁年份。"""
    accepted = allocation.accepted.get(house)
    base_ranked = sorted(all_outcomes, key=lambda o: -_base_operating(o))
    best_by_cash = base_ranked[0]
    chosen = accepted or allocation.rejected.get(house)

    why_not: list[str] = []
    if (
        chosen is not None
        and accepted is None
        and _base_operating(best_by_cash) > _base_operating(chosen) + 0.01
    ):
        why_not.append(
            f"基准年累计经营现金更高的方案「{best_by_cash.plan.label}」"
            f"（{_base_operating(best_by_cash):.0f} 元）不能订，原因："
            + "；".join(best_by_cash.rejection_reasons or ("受库存/联保约束",))
        )
    for outcome in all_outcomes:
        if outcome is chosen:
            continue
        for sim in outcome.simulations:
            if not sim.feasible:
                gaps = [
                    f"{row.year}年缺{row.shortfall:.0f}元"
                    for row in sim.years
                    if row.shortfall > 0.01
                ]
                if gaps:
                    why_not.append(
                        f"「{outcome.plan.label}」在 {sim.scenario} 情景破底线："
                        + "、".join(gaps)
                    )
                    break
        if not outcome.signable:
            pending = "、".join(outcome.plan.pending_blocks)
            why_not.append(
                f"「{outcome.plan.label}」含待补证地块（{pending}），"
                "不能签需要收益保证的名额"
            )

    levers = _levers(house, all_outcomes, allocation, loan_limit)
    return {
        "house": house,
        "chosen_plan": chosen.plan.as_dict() if chosen else None,
        "highest_revenue_plan": best_by_cash.plan.as_dict(),
        "highest_revenue_operating_cash": round(
            _base_operating(best_by_cash), 2
        ),
        "why_higher_revenue_not_bookable": why_not,
        "support_that_changes_feasible_year": levers.as_dict(),
    }


def _levers(
    house,
    all_outcomes,
    allocation: AllocationResult,
    loan_limit: float,
) -> SupportLevers:
    cash_injection: dict[str, float] = {}
    extra_loan: dict[str, float] = {}
    evidence: set[str] = set()
    alternatives: set[str] = set()
    for outcome in all_outcomes:
        for sim in outcome.simulations:
            if not sim.feasible:
                total_gap = sum(row.shortfall for row in sim.years)
                cash_injection[sim.scenario] = max(
                    cash_injection.get(sim.scenario, 0.0), total_gap
                )
                # 借款已顶到 loan_limit 时，额外授信至少要覆盖累计缺口。
                if any(
                    row.debt_outstanding >= loan_limit - 0.01
                    for row in sim.years
                ):
                    extra_loan[sim.scenario] = max(
                        extra_loan.get(sim.scenario, 0.0), total_gap
                    )
        if not outcome.signable:
            for status in outcome.plan.evidence_status:
                for miss in status.missing:
                    evidence.add(
                        f"{miss['region']}/{miss['root']}/{miss['scion']}"
                        f" 树龄{miss['age']}（现有{miss['orchard_count']}个果园）"
                    )
        for need in outcome.needs:
            year = need.plant_year
            for delta in (-1, 1):
                alt = need.lot.replace(str(year), str(year + delta))
                if alt in allocation.lot_usage and alt != need.lot:
                    alternatives.add(alt)
    return SupportLevers(
        house=house,
        cash_injection_needed=cash_injection,
        extra_loan_headroom_needed=extra_loan,
        guaranteed_lot_alternatives=tuple(sorted(alternatives)),
        evidence_to_collect=tuple(sorted(evidence)),
    )


def _base_operating(outcome: HouseholdOutcome) -> float:
    sim = next(s for s in outcome.simulations if s.scenario == "base")
    return sim.cumulative_operating
