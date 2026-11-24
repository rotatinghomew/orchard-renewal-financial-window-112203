"""苗木订位分配、联保池约束与跨轮复议。

分配不是按收益排名把苗全给第一名，而是在以下硬约束下做可回溯搜索：

1. ``paid`` 订位不可撤销、不可移年，直接占用批次库存与对应苗款；
2. 已种地块（外部传入的 ``planted`` 状态）作为新一轮硬约束，方案必须沿用；
3. 批次库存按 ``count`` 占用，起苗期限随批次固定，超卖直接拒绝；
4. 每户每个情景都要能借到自己的现金底线（``loan_limit`` 内）；
5. 联保池按情景、按当年所有户未还占用求和，超过
   ``annual_guarantee_limit`` 的组合拒绝——同一坏年景必须一起吃紧；
6. 证据不足（``signable=False``）的方案只能列为待补证，不能签订
   需要收益保证的名额。

新一轮资料推翻旧估计时，已种地块与不可撤销订位作为约束进入重比；
无法维持保底的家庭单独提示，不挤掉不相关组合的已签苗位。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .engine import HouseholdSimulation, LotNeed, lot_needs, simulate_household
from .evidence import EvidenceTable
from .plans import PLANT, Plan, enumerate_plans


@dataclass(frozen=True)
class AllocationEntry:
    house: str
    block: str
    lot: str
    root: str
    scion: str
    plant_year: int
    count: int
    unit_price: float
    deadline: str
    commitment_kind: str          # paid | quote | planted | none
    locked: bool                  # paid 或已种地块 -> 不可撤销
    signable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "house": self.house,
            "block": self.block,
            "lot": self.lot,
            "root": self.root,
            "scion": self.scion,
            "plant_year": self.plant_year,
            "count": self.count,
            "unit_price": self.unit_price,
            "deadline": self.deadline,
            "commitment_kind": self.commitment_kind,
            "locked": self.locked,
            "signable": self.signable,
        }


@dataclass(frozen=True)
class HouseholdOutcome:
    house: str
    plan: Plan
    simulations: tuple[HouseholdSimulation, ...]
    signable: bool
    feasible: bool
    rejection_reasons: tuple[str, ...]
    needs: tuple[LotNeed, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "house": self.house,
            "plan": self.plan.as_dict(),
            "signable": self.signable,
            "feasible": self.feasible,
            "rejection_reasons": list(self.rejection_reasons),
            "simulations": [sim.as_dict() for sim in self.simulations],
            "lot_needs": [
                {
                    "block": n.block,
                    "lot": n.lot,
                    "count": n.count,
                    "unit_price": n.unit_price,
                    "already_paid": n.already_paid,
                }
                for n in self.needs
            ],
        }


@dataclass
class AllocationResult:
    accepted: dict[str, HouseholdOutcome]
    rejected: dict[str, HouseholdOutcome]
    unplaceable: dict[str, tuple[str, ...]]   # 无任何可行方案的家庭及原因
    entries: tuple[AllocationEntry, ...]
    lot_usage: dict[str, int]
    guarantee_demand: dict[str, list[float]]  # scenario -> 逐年联保占用
    warnings: tuple[str, ...]
    evaluated: dict[str, list[HouseholdOutcome]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": {h: o.as_dict() for h, o in sorted(self.accepted.items())},
            "rejected": {h: o.as_dict() for h, o in sorted(self.rejected.items())},
            "unplaceable": {h: list(reasons) for h, reasons in sorted(self.unplaceable.items())},
            "entries": [
                {
                    "house": e.house,
                    "block": e.block,
                    "lot": e.lot,
                    "root": e.root,
                    "scion": e.scion,
                    "plant_year": e.plant_year,
                    "count": e.count,
                    "unit_price": e.unit_price,
                    "deadline": e.deadline,
                    "commitment_kind": e.commitment_kind,
                    "locked": e.locked,
                    "signable": e.signable,
                }
                for e in self.entries
            ],
            "lot_usage": dict(sorted(self.lot_usage.items())),
            "guarantee_demand": {
                sid: [round(v, 2) for v in vals]
                for sid, vals in sorted(self.guarantee_demand.items())
            },
            "warnings": list(self.warnings),
        }


def allocate(
    *,
    packet,
    evidence: EvidenceTable,
    planted: dict[str, tuple[str, int]] | None = None,
) -> AllocationResult:
    """执行一轮完整分配。

    ``planted`` 为跨轮锁定：``block -> (root, plant_year)``，
    已种地块只能沿用该砧木与种植年。
    """
    planted = planted or {}
    parcels = list(packet.parcels)
    stock = list(packet.stock)
    commitments = list(packet.commitments)
    support_index = {row["house"]: row for row in packet.support}
    parcel_index = {row["block"]: row for row in parcels}
    scenario_ids = sorted({row["id"] for row in packet.scenarios})
    horizon_years = list(
        range(packet.pool["horizon"][0], packet.pool["horizon"][1] + 1)
    )

    paid_lots = {
        (row["block"], row["lot"]): row
        for row in commitments
        if row["kind"] == "paid"
    }
    quote_lots = {
        (row["block"], row["lot"]): row
        for row in commitments
        if row["kind"] == "quote"
    }
    stock_capacity = {row["lot"]: row["count"] for row in stock}
    stock_row = {row["lot"]: row for row in stock}

    plans_by_house = enumerate_plans(
        parcels, stock, evidence, packet.pool
    )
    plans_by_house = _apply_locks(
        plans_by_house, parcels, paid_lots, planted
    )

    # 先固定不可撤销条目，占用库存。
    lot_usage: dict[str, int] = {lot: 0 for lot in stock_capacity}
    locked_entries: list[AllocationEntry] = []
    warnings: list[str] = []
    for (block, lot_id), commitment in sorted(paid_lots.items()):
        parcel = parcel_index[block]
        lot = stock_row[lot_id]
        if lot_usage[lot_id] + commitment["count"] > stock_capacity[lot_id]:
            raise ValueError(f"已付订位超出批次库存：{lot_id}")
        lot_usage[lot_id] += commitment["count"]
        locked_entries.append(
            AllocationEntry(
                house=parcel["house"],
                block=block,
                lot=lot_id,
                root=lot["root"],
                scion=lot["scion"],
                plant_year=lot["year"],
                count=commitment["count"],
                unit_price=lot["unit_price"],
                deadline=lot["deadline"],
                commitment_kind="paid",
                locked=True,
                signable=True,
            )
        )
    for block, (root, plant_year) in sorted(planted.items()):
        parcel = parcel_index[block]
        lot_id = _lot_id(root, parcel["scion"], plant_year)
        paid_lock = next(
            (lot for (blk, lot) in paid_lots if blk == block), None
        )
        if paid_lock is not None and paid_lock != lot_id:
            raise ValueError(
                f"已种约束 {block}->{lot_id} 与不可撤销订位 {paid_lock} 冲突"
            )
        lot = stock_row[lot_id]
        count = parcel["nursery_count"]
        if lot_usage[lot_id] + count > stock_capacity[lot_id]:
            raise ValueError(f"已种地块与库存冲突：{block} -> {lot_id}")
        lot_usage[lot_id] += count
        locked_entries.append(
            AllocationEntry(
                house=parcel["house"],
                block=block,
                lot=lot_id,
                root=root,
                scion=parcel["scion"],
                plant_year=plant_year,
                count=count,
                unit_price=lot["unit_price"],
                deadline=lot["deadline"],
                commitment_kind="planted",
                locked=True,
                signable=True,
            )
        )

    # 逐户评估方案：现金可行性、证据、批次库存与联保池。
    evaluated: dict[str, list[HouseholdOutcome]] = {}
    for house, plans in sorted(plans_by_house.items()):
        house_parcels = [row for row in parcels if row["house"] == house]
        outcomes: list[HouseholdOutcome] = []
        for plan in plans:
            sims = tuple(
                simulate_household(
                    plan,
                    parcels=house_parcels,
                    support=support_index[house],
                    scenarios=list(packet.scenarios),
                    evidence=evidence,
                    stock=stock,
                    paid_lots=paid_lots,
                    scenario_id=sid,
                )
                for sid in scenario_ids
            )
            reasons: list[str] = []
            if not plan.signable:
                reasons.append(
                    "待补证：" + "、".join(plan.pending_blocks) + " 证据不足"
                )
            cash_fail = [sim.scenario for sim in sims if not sim.feasible]
            if cash_fail:
                reasons.append("现金底线无法满足：" + "、".join(cash_fail))
            needs = lot_needs(plan, house_parcels, stock, paid_lots)
            outcomes.append(
                HouseholdOutcome(
                    house=house,
                    plan=plan,
                    simulations=sims,
                    signable=plan.signable,
                    feasible=not cash_fail,
                    rejection_reasons=tuple(reasons),
                    needs=needs,
                )
            )
        evaluated[house] = outcomes

    search = _SearchState(
        evaluated=evaluated,
        parcel_index=parcel_index,
        stock_row=stock_row,
        stock_capacity=stock_capacity,
        lot_usage=dict(lot_usage),
        scenario_ids=scenario_ids,
        horizon_years=horizon_years,
        guarantee_limit=float(packet.pool["annual_guarantee_limit"]),
        quote_lots=quote_lots,
        paid_lots=paid_lots,
        planted=planted,
    )
    chosen, rejected, unplaceable, guarantee_demand = search.run()

    entries = list(locked_entries)
    locked_keys = {(entry.block, entry.lot) for entry in locked_entries}
    for outcome in chosen.values():
        for entry in _entry_for_outcome(
            outcome, parcel_index, stock_row, quote_lots, paid_lots, planted
        ):
            # 锁定条目已经登记过，避免同一地块批次重复出现。
            if (entry.block, entry.lot) not in locked_keys:
                entries.append(entry)
    entries = tuple(entries)
    # 无法维持保底的家庭单独提示。
    for house, reasons in sorted(unplaceable.items()):
        warnings.append(f"{house} 没有可签订方案：{'；'.join(reasons)}")
    for house, outcome in sorted(rejected.items()):
        warnings.append(
            f"{house} 预计收益更高但本轮无法订位：{'；'.join(outcome.rejection_reasons) or '库存/联保约束'}"
        )
    # 已接受方案里若含待补证地块，单独提示不能据此签收益保证。
    for house, outcome in sorted(chosen.items()):
        if not outcome.signable:
            warnings.append(
                f"{house} 已接受方案含待补证地块（"
                + "、".join(outcome.plan.pending_blocks)
                + "），只能先安排不签收益保证的部分"
            )
    return AllocationResult(
        accepted=chosen,
        rejected=rejected,
        unplaceable=unplaceable,
        entries=entries,
        lot_usage=search.lot_usage,
        guarantee_demand=guarantee_demand,
        warnings=tuple(warnings),
        evaluated=evaluated,
    )


class _SearchState:
    """按户回溯搜索：库存、每户现金与联保池共同约束。"""

    def __init__(
        self,
        *,
        evaluated,
        parcel_index,
        stock_row,
        stock_capacity,
        lot_usage,
        scenario_ids,
        horizon_years,
        guarantee_limit,
        quote_lots,
        paid_lots,
        planted,
    ):
        self.evaluated = evaluated
        self.parcel_index = parcel_index
        self.stock_row = stock_row
        self.stock_capacity = stock_capacity
        self.lot_usage = lot_usage
        self.scenario_ids = scenario_ids
        self.horizon_years = horizon_years
        self.guarantee_limit = guarantee_limit
        self.quote_lots = quote_lots
        self.paid_lots = paid_lots
        self.planted = planted
        self.houses = sorted(evaluated)

    def run(self):
        # 每户方案排序：现金可行优先；其次可签订苗块数、承接 quote、
        # 基准累计经营现金、错开年份。所有分配都在回溯里完成，
        # 搜索结束后的联保/库存状态与最终选中的方案严格一致。
        ranked = {
            house: sorted(outcomes, key=self._rank_key)
            for house, outcomes in self.evaluated.items()
        }
        guarantee = {
            sid: [0.0] * len(self.horizon_years) for sid in self.scenario_ids
        }
        chosen: dict[str, HouseholdOutcome] = {}
        self._best(0, ranked, guarantee, chosen)

        rejected: dict[str, HouseholdOutcome] = {}
        unplaceable: dict[str, tuple[str, ...]] = {}
        for house in self.houses:
            if house in chosen:
                continue
            outcomes = ranked[house]
            own_feasible = [o for o in outcomes if o.feasible]
            if not own_feasible:
                reasons = sorted(
                    {
                        reason
                        for outcome in outcomes
                        for reason in outcome.rejection_reasons
                    }
                )
                unplaceable[house] = tuple(reasons) or ("无满足现金底线的方案",)
            else:
                # 有自身可行的方案，但所有带新苗位的选择都被库存/联保挤掉，
                # 记录最优方案与具体原因，便于向农户解释“为什么不能订”。
                rejected[house] = self._with_constraint_reason(
                    own_feasible[0], guarantee
                )
        guarantee_demand = {
            sid: values for sid, values in guarantee.items() if any(values)
        }
        return chosen, rejected, unplaceable, guarantee_demand

    def _best(self, index, ranked, guarantee, chosen):
        """回溯：最大化 (安置户数, 可签订苗块数, 基准情景累计经营现金)。

        每户都可以落到零新苗位的可行方案（保留旧树/只剩锁定订位），
        因此搜索不会因单户拿不到苗而中断；库存与联保占用随分支
        同步增减，返回时状态与最优分支完全一致。
        """
        if index == len(self.houses):
            return self._score(chosen)
        house = self.houses[index]
        best_score = None
        best_state = None
        for outcome in ranked[house]:
            if not outcome.feasible:
                continue
            if not self._fits(outcome, guarantee):
                continue
            lot_snapshot = dict(self.lot_usage)
            guarantee_snapshot = {
                sid: values[:] for sid, values in guarantee.items()
            }
            self._consume(outcome, guarantee)
            chosen[house] = outcome
            score = self._best(index + 1, ranked, guarantee, chosen)
            if best_score is None or score > best_score:
                best_score = score
                best_state = (
                    dict(self.lot_usage),
                    {sid: values[:] for sid, values in guarantee.items()},
                    dict(chosen),
                )
            self.lot_usage = lot_snapshot
            for sid, values in guarantee_snapshot.items():
                guarantee[sid] = values
            chosen.pop(house, None)
        # 兜底：该户选一个不占新库存的可行方案（保留旧树/只剩锁定订位）。
        # 联保池仍是硬约束：连兜底都超池时该户本轮不分配，由 run() 单独提示。
        fallback = self._fallback_outcome(house, ranked)
        if fallback is not None and not self._pool_blockers(
            fallback, guarantee
        ):
            guarantee_snapshot = {
                sid: values[:] for sid, values in guarantee.items()
            }
            self._consume(fallback, guarantee)
            chosen[house] = fallback
            score = self._best(index + 1, ranked, guarantee, chosen)
            if best_score is None or score > best_score:
                best_score = score
                best_state = (
                    dict(self.lot_usage),
                    {sid: values[:] for sid, values in guarantee.items()},
                    dict(chosen),
                )
            for sid, values in guarantee_snapshot.items():
                guarantee[sid] = values
            chosen.pop(house, None)
        else:
            # 该户没有任何能在当前池占用下放下的方案：留空继续其余户。
            score = self._best(index + 1, ranked, guarantee, chosen)
            if best_score is None or score > best_score:
                best_score = score
                best_state = (
                    dict(self.lot_usage),
                    {sid: values[:] for sid, values in guarantee.items()},
                    dict(chosen),
                )
        if best_state is not None:
            self.lot_usage, guarantee_state, chosen_state = best_state
            for sid, values in guarantee_state.items():
                guarantee[sid] = values
            chosen.clear()
            chosen.update(chosen_state)
        return best_score

    def _fallback_outcome(self, house, ranked):
        """无新增库存冲突的可行方案：零新苗位优先，其次只含已锁定苗位。"""
        candidates = [o for o in ranked[house] if o.feasible]
        no_open = [
            o
            for o in candidates
            if all(need.already_paid for need in o.needs)
        ]
        if no_open:
            return no_open[0]
        locked_only = [
            o
            for o in candidates
            if all(
                need.already_paid
                or need.block in self.planted
                for need in o.needs
            )
        ]
        return locked_only[0] if locked_only else None

    def _score(self, chosen) -> tuple[int, int, float]:
        households = len(chosen)
        supported_blocks = 0
        base_operating = 0.0
        for outcome in chosen.values():
            supported_blocks += sum(
                1
                for need in outcome.needs
                if self._need_contract_supported(outcome, need)
            )
            base_sim = next(
                sim for sim in outcome.simulations if sim.scenario == "base"
            )
            base_operating += base_sim.cumulative_operating
        return households, supported_blocks, round(base_operating, 2)

    def _rank_key(self, outcome: HouseholdOutcome):
        plan = outcome.plan
        quote_match = self._quote_match_count(outcome)
        base_sim = next(
            sim for sim in outcome.simulations if sim.scenario == "base"
        )
        plant_years = [
            action.plant_year
            for action in plan.actions
            if action.kind == PLANT and action.plant_year is not None
        ]
        # 只统计证据够证、能签收益保证的订苗块数（待补证块不计入）。
        supported_needs = sum(
            1
            for need in outcome.needs
            if self._need_contract_supported(outcome, need)
        )
        spread = (max(plant_years) - min(plant_years)) if plant_years else 0
        return (
            0 if outcome.feasible else 1,
            -supported_needs,              # 先比可签订苗块数
            0 if outcome.signable else 1,
            -quote_match,                  # 再优先承接本户已提 quote
            -round(base_sim.cumulative_operating, 2),
            -spread,                       # 同等条件下偏好错开年份
            plan.label,
            tuple(
                (a.block, a.kind, str(a.plant_year), str(a.root))
                for a in plan.actions
            ),
        )

    def _quote_match_count(self, outcome: HouseholdOutcome) -> int:
        count = 0
        for need in outcome.needs:
            if (need.block, need.lot) in self.quote_lots or (
                need.block,
                need.lot,
            ) in self.paid_lots:
                count += 1
        return count

    def _with_constraint_reason(self, outcome, guarantee) -> HouseholdOutcome:
        reasons = list(outcome.rejection_reasons)
        # paid 订位在搜索前已锁库存，不能把它自己的批次算作库存阻塞。
        open_needs = [need for need in outcome.needs if not need.already_paid]
        lot_blockers = [
            need.lot
            for need in open_needs
            if self.lot_usage.get(need.lot, 0) + need.count
            > self.stock_capacity[need.lot]
        ]
        if lot_blockers:
            reasons.append("批次库存不足：" + "、".join(sorted(set(lot_blockers))))
        pool_blockers = self._pool_blockers(outcome, guarantee)
        if pool_blockers:
            reasons.append("联保池在坏年景超限：" + "、".join(pool_blockers))
        if not reasons:
            reasons.append("受其他家庭已占苗位/联保额度约束")
        return HouseholdOutcome(
            house=outcome.house,
            plan=outcome.plan,
            simulations=outcome.simulations,
            signable=outcome.signable,
            feasible=outcome.feasible,
            rejection_reasons=tuple(reasons),
            needs=outcome.needs,
        )

    def _fits(self, outcome: HouseholdOutcome, guarantee) -> bool:
        """现金底线是硬约束；证据只约束需要收益保证的新订苗位。

        * 任何情景下现金不可行 -> 直接排除；
        * 方案里有待补证地块时，该地块不能占用保证性苗位，只允许
          “不订苗”（保留旧树或清园闲置）的动作通过；
        * 库存与联保池为共享硬约束。
        """
        if not outcome.feasible:
            return False
        for need in outcome.needs:
            if not self._need_contract_supported(outcome, need):
                return False
            if (
                self.lot_usage.get(need.lot, 0) + need.count
                > self.stock_capacity[need.lot]
            ):
                return False
        if self._pool_blockers(outcome, guarantee):
            return False
        return True

    def _need_contract_supported(self, outcome: HouseholdOutcome, need) -> bool:
        """该苗位对应的地块在种植年及以后是否全程够证。"""
        status = next(
            status
            for status in outcome.plan.evidence_status
            if status.block == need.block
        )
        return status.signable

    def _pool_blockers(self, outcome, guarantee) -> list[str]:
        blockers: list[str] = []
        for sim in outcome.simulations:
            for offset, row in enumerate(sim.years):
                if guarantee[sim.scenario][offset] + row.guarantee_used > (
                    self.guarantee_limit + 0.01
                ):
                    blockers.append(f"{sim.scenario}/{row.year}")
        return sorted(set(blockers))

    def _consume(self, outcome: HouseholdOutcome, guarantee) -> None:
        for need in outcome.needs:
            # paid 已在进入搜索前占用，这里不重复加。
            if not need.already_paid:
                self.lot_usage[need.lot] = (
                    self.lot_usage.get(need.lot, 0) + need.count
                )
        for sim in outcome.simulations:
            for offset, row in enumerate(sim.years):
                guarantee[sim.scenario][offset] += row.guarantee_used


def _apply_locks(plans_by_house, parcels, paid_lots, planted):
    """把 paid 订位与已种地块变成方案硬约束。"""
    parcel_index = {row["block"]: row for row in parcels}
    locked_block_roots: dict[str, tuple[str, int]] = {}
    for (block, lot_id), commitment in paid_lots.items():
        # lot 名形如 R1-S1-2027
        root, _scion, year_text = lot_id.split("-")
        locked_block_roots[block] = (root, int(year_text))
    for block, value in planted.items():
        locked_block_roots[block] = value
    if not locked_block_roots:
        return plans_by_house
    filtered: dict[str, tuple[Plan, ...]] = {}
    for house, plans in plans_by_house.items():
        kept: list[Plan] = []
        for plan in plans:
            ok = True
            for action in plan.actions:
                lock = locked_block_roots.get(action.block)
                if lock is None:
                    continue
                root, plant_year = lock
                if (
                    action.kind != PLANT
                    or action.root != root
                    or action.plant_year != plant_year
                ):
                    ok = False
                    break
            if ok:
                kept.append(plan)
        if not kept:
            raise ValueError(
                f"{house} 的不可撤销约束没有任何候选方案可满足"
            )
        filtered[house] = tuple(kept)
    return filtered


def _lot_id(root: str, scion: str, year: int) -> str:
    return f"{root}-{scion}-{year}"


def _entry_for_outcome(outcome, parcel_index, stock_row, quote_lots, paid_lots, planted):
    entries = []
    for need in outcome.needs:
        kind = "none"
        locked = False
        if need.already_paid or (need.block, need.lot) in paid_lots:
            kind = "paid"
            locked = True
        elif (need.block, need.lot) in quote_lots:
            kind = "quote"
        if need.block in planted:
            locked = True
            kind = "planted"
        entries.append(
            AllocationEntry(
                house=outcome.house,
                block=need.block,
                lot=need.lot,
                root=need.root,
                scion=need.scion,
                plant_year=need.plant_year,
                count=need.count,
                unit_price=need.unit_price,
                deadline=stock_row[need.lot]["deadline"],
                commitment_kind=kind,
                locked=locked,
                signable=outcome.signable,
            )
        )
    return tuple(entries)
