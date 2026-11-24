"""更新方案枚举：保留旧树、立即更新与分年更新。

一个方案是农户名下各地块动作的组合。地块动作只有两类：

* ``keep``      当年继续按旧树计收入（旧树随规划年逐年长一岁）；
* ``plant``     在 ``plant_year`` 清园并种指定砧穗组合，种植当年记树龄零。

已清园地块（``cleared=true``）不能退回旧树，只能在允许年份内种植；
未清园地块可以保留，也可以在任意允许年份更新。分年更新即同一农户的
两块地选不同种植年。证据不足的组合仍会列出，但带 ``待补证`` 标记，
供苗分配时不得据此签订需要收益保证的名额。
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Any

from .evidence import EvidenceTable

KEEP = "keep"
PLANT = "plant"


@dataclass(frozen=True)
class BlockAction:
    block: str
    kind: str  # keep | plant
    plant_year: int | None = None
    root: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "kind": self.kind,
            "plant_year": self.plant_year,
            "root": self.root,
        }


@dataclass(frozen=True)
class BlockEvidenceStatus:
    """地块在整个规划年内用到的证据是否够证。"""

    block: str
    signable: bool
    missing: tuple[dict[str, Any], ...]
    in_transit_note: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "block": self.block,
            "signable": self.signable,
            "missing": list(self.missing),
            "in_transit": list(self.in_transit_note),
        }


@dataclass(frozen=True)
class Plan:
    house: str
    actions: tuple[BlockAction, ...]
    horizon: tuple[int, int]
    evidence_status: tuple[BlockEvidenceStatus, ...]
    label: str

    @property
    def signable(self) -> bool:
        """方案内所有地块在所有规划年都有达 analog 门槛的证据。"""
        return all(status.signable for status in self.evidence_status)

    @property
    def pending_blocks(self) -> tuple[str, ...]:
        return tuple(
            status.block for status in self.evidence_status if not status.signable
        )

    def block_action(self, block: str) -> BlockAction:
        for action in self.actions:
            if action.block == block:
                return action
        raise KeyError(block)

    def as_dict(self) -> dict[str, Any]:
        return {
            "house": self.house,
            "label": self.label,
            "signable": self.signable,
            "actions": [action.as_dict() for action in self.actions],
            "evidence": [status.as_dict() for status in self.evidence_status],
        }


def enumerate_plans(
    parcels: list[dict[str, Any]],
    stock: list[dict[str, Any]],
    evidence: EvidenceTable,
    pool: dict[str, Any],
) -> dict[str, tuple[Plan, ...]]:
    """按农户枚举全部候选方案，并逐块核对规划年证据。"""
    start, end = pool["horizon"]
    years = list(range(start, end + 1))
    plant_years = list(pool["plant_years"])
    lots = _lot_index(stock)
    result: dict[str, tuple[Plan, ...]] = {}
    houses = sorted({row["house"] for row in parcels})
    for house in houses:
        house_parcels = [row for row in parcels if row["house"] == house]
        per_block: list[tuple[BlockAction, ...]] = []
        for parcel in house_parcels:
            per_block.append(
                _block_actions(parcel, plant_years, lots)
            )
        plans: list[Plan] = []
        for combo in product(*per_block):
            statuses = tuple(
                _evidence_status(parcel, action, evidence, years)
                for parcel, action in zip(house_parcels, combo)
            )
            plans.append(
                Plan(
                    house=house,
                    actions=tuple(combo),
                    horizon=(start, end),
                    evidence_status=statuses,
                    label=_label(combo, start),
                )
            )
        # 去重（枚举本身无重复，保险）并保证顺序稳定。
        unique = _deduplicate(plans)
        result[house] = tuple(unique)
    return result


def _lot_index(stock: list[dict[str, Any]]) -> dict[tuple[str, str, int], dict]:
    return {(row["root"], row["scion"], row["year"]): row for row in stock}


def _block_actions(parcel, plant_years, lots) -> tuple[BlockAction, ...]:
    actions: list[BlockAction] = []
    if not parcel["cleared"]:
        actions.append(BlockAction(block=parcel["block"], kind=KEEP))
    for year in plant_years:
        for root in parcel["root_choices"]:
            if (root, parcel["scion"], year) in lots:
                actions.append(
                    BlockAction(
                        block=parcel["block"],
                        kind=PLANT,
                        plant_year=year,
                        root=root,
                    )
                )
    if not actions:
        raise ValueError(f"地块 {parcel['block']} 没有任何可行动作")
    return tuple(actions)


def _evidence_status(parcel, action: BlockAction, table: EvidenceTable, years):
    missing: list[dict[str, Any]] = []
    transit: list[dict[str, Any]] = []
    if action.kind == KEEP:
        for year in years:
            age = parcel["old_age"] + (year - years[0])
            ev = table.old_tree_anchor(parcel["region"], parcel["old_scion"], age)
            if not ev.supported:
                missing.append(
                    {
                        "year": year,
                        "age": age,
                        "region": parcel["region"],
                        "root": "old",
                        "scion": parcel["old_scion"],
                        "orchard_count": ev.orchard_count,
                    }
                )
    else:
        for year in years:
            age = year - action.plant_year  # 种植当年为 0；清园未种的年份不查
            if year < action.plant_year:
                continue
            ev = table.lookup(
                parcel["region"], action.root, parcel["scion"], age
            )
            if not ev.supported:
                missing.append(
                    {
                        "year": year,
                        "age": age,
                        "region": parcel["region"],
                        "root": action.root,
                        "scion": parcel["scion"],
                        "orchard_count": ev.orchard_count,
                    }
                )
            elif ev.in_transit:
                transit.append(
                    {
                        "year": year,
                        "age": age,
                        "root": action.root,
                        "scion": parcel["scion"],
                        "observations": len(ev.in_transit),
                    }
                )
    return BlockEvidenceStatus(
        block=parcel["block"],
        signable=not missing,
        missing=tuple(missing),
        in_transit_note=tuple(transit),
    )


def _label(actions: tuple[BlockAction, ...], start_year: int) -> str:
    plant_years = sorted(
        action.plant_year
        for action in actions
        if action.kind == PLANT and action.plant_year is not None
    )
    detail = ",".join(
        (
            f"{action.block}:旧树"
            if action.kind == KEEP
            else f"{action.block}:{action.root}@{action.plant_year}"
        )
        for action in actions
    )
    if not plant_years:
        return f"保留旧树({detail})"
    if len(plant_years) == len(actions) and len(set(plant_years)) == 1:
        head = (
            "立即更新" if plant_years[0] == start_year else f"{plant_years[0]}年更新"
        )
        return f"{head}({detail})"
    return f"分年更新({detail})"


def _deduplicate(plans: list[Plan]) -> list[Plan]:
    seen: set[tuple[tuple[str, str, int | None, str | None], ...]] = set()
    unique: list[Plan] = []
    for plan in plans:
        key = tuple(
            (action.block, action.kind, action.plant_year, action.root)
            for action in plan.actions
        )
        if key not in seen:
            seen.add(key)
            unique.append(plan)
    return unique
