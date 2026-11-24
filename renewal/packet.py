"""资料包读取、修订与输入口径校验。

规则（见 ``资料格式.md``）：

* ``amendments`` 只修订明确指明的一条历史字段，且修订在所有分析之前生效；
* ``history`` 中 ``final=false`` 的在途观察不进入支撑证据，只保留供查阅；
* 情景按 ``id`` 分组，同一情景在每个 (年份, 地区) 上恰好有一条系数；
* 各引用（订位->地块/批次、农户->支持参数）必须闭合。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIRED_SECTIONS = (
    "history",
    "parcels",
    "scenarios",
    "stock",
    "commitments",
    "support",
    "pool",
    "amendments",
)
HISTORY_FIELDS = frozenset(
    {
        "orchard",
        "block",
        "region",
        "planted",
        "year",
        "root",
        "scion",
        "mu",
        "kg",
        "revenue_yuan",
        "cost",
        "final",
        "know",
    }
)
AMENDABLE_FIELDS = frozenset({"kg", "revenue_yuan", "cost"})


class PacketError(ValueError):
    """资料包不满足约定口径时抛出。"""


@dataclass(frozen=True)
class Packet:
    """通过校验、已套用修订的资料包。"""

    history: tuple[dict[str, Any], ...]
    parcels: tuple[dict[str, Any], ...]
    scenarios: tuple[dict[str, Any], ...]
    stock: tuple[dict[str, Any], ...]
    commitments: tuple[dict[str, Any], ...]
    support: tuple[dict[str, Any], ...]
    pool: dict[str, Any]
    amendments: tuple[dict[str, Any], ...]
    as_of: str

    def section(self, name: str):
        return getattr(self, name)


def read_packet(path: str | Path, *, as_of: str | None = None) -> Packet:
    """读取资料包，套用 amendments，并做引用与口径校验。"""
    with Path(path).open(encoding="utf-8") as handle:
        raw = json.load(handle)
    return build_packet(raw, as_of=as_of)


def build_packet(raw: dict[str, Any], *, as_of: str | None = None) -> Packet:
    if not isinstance(raw, dict) or any(
        not isinstance(value, (list, dict)) for value in raw.values()
    ):
        raise PacketError("资料包必须是由记录列表或参数对象组成的 JSON 对象")
    missing = [name for name in REQUIRED_SECTIONS if name not in raw]
    if missing:
        raise PacketError(f"资料包缺少章节：{', '.join(missing)}")

    history = [dict(row) for row in raw["history"]]
    amendments = tuple(dict(row) for row in raw["amendments"])
    _apply_amendments(history, amendments)
    packet = Packet(
        history=tuple(history),
        parcels=tuple(dict(row) for row in raw["parcels"]),
        scenarios=tuple(dict(row) for row in raw["scenarios"]),
        stock=tuple(dict(row) for row in raw["stock"]),
        commitments=tuple(dict(row) for row in raw["commitments"]),
        support=tuple(dict(row) for row in raw["support"]),
        pool=dict(raw["pool"]),
        amendments=amendments,
        as_of=as_of or _latest_know(history),
    )
    _validate(packet)
    return packet


def _apply_amendments(history: list[dict[str, Any]], amendments) -> None:
    index = {(row["orchard"], row["year"]): row for row in history}
    for amendment in amendments:
        key = (amendment["orchard"], amendment["year"])
        if key not in index:
            raise PacketError(
                f"修订找不到历史记录：{key[0]} {key[1]}"
            )
        field = amendment["field"]
        if field not in AMENDABLE_FIELDS:
            raise PacketError(f"修订字段不在允许范围：{field}")
        index[key][field] = amendment["new_value"]
        index[key]["amended"] = True


def _latest_know(history) -> str:
    return max(row["know"] for row in history)


def _validate(packet: Packet) -> None:
    # 历史记录键唯一，字段齐全且非负。
    seen: set[tuple[str, int]] = set()
    for row in packet.history:
        unknown = set(row) - HISTORY_FIELDS - {"amended"}
        if unknown:
            raise PacketError(f"历史记录出现未约定字段：{sorted(unknown)}")
        key = (row["orchard"], row["year"])
        if key in seen:
            raise PacketError(f"历史记录重复：{key}")
        seen.add(key)
        if row["year"] < row["planted"]:
            raise PacketError(f"历史年份早于栽植年：{key}")
        for field in ("mu", "kg", "revenue_yuan", "cost"):
            if not isinstance(row[field], (int, float)) or row[field] < 0:
                raise PacketError(f"历史字段 {field} 必须是非负数：{key}")

    # 情景：同一 id 下每个 (年份, 地区) 唯一，系数为正。
    scenario_keys: dict[str, set[tuple[int, str]]] = {}
    for row in packet.scenarios:
        bucket = scenario_keys.setdefault(row["id"], set())
        key = (row["year"], row["region"])
        if key in bucket:
            raise PacketError(f"情景 {row['id']} 在 {key} 上重复")
        bucket.add(key)
        if row["price_factor"] <= 0 or row["yield_factor"] <= 0:
            raise PacketError(f"情景系数必须为正：{row}")
    horizon_years = list(
        range(packet.pool["horizon"][0], packet.pool["horizon"][1] + 1)
    )
    regions = {row["region"] for row in packet.parcels}
    for sid, keys in scenario_keys.items():
        expect = {(year, region) for year in horizon_years for region in regions}
        if keys != expect:
            raise PacketError(f"情景 {sid} 未覆盖规划年×地区全集")

    # 引用闭合。
    blocks = {row["block"] for row in packet.parcels}
    lots = {row["lot"] for row in packet.stock}
    houses = {row["house"] for row in packet.support}
    if {row["house"] for row in packet.parcels} != houses:
        raise PacketError("地块农户与 support 农户不一致")
    for row in packet.commitments:
        if row["block"] not in blocks:
            raise PacketError(f"订位引用了未知地块：{row['block']}")
        if row["lot"] not in lots:
            raise PacketError(f"订位引用了未知批次：{row['lot']}")
        if row["kind"] not in {"paid", "quote"}:
            raise PacketError(f"订位 kind 非法：{row['kind']}")

    # 库存批次与地块可选砧木/接穗/年份口径。
    plant_years = set(packet.pool["plant_years"])
    for row in packet.stock:
        if row["year"] not in plant_years:
            raise PacketError(f"批次年份不在 plant_years 内：{row['lot']}")
        if row["count"] < 0 or row["unit_price"] < 0:
            raise PacketError(f"批次数量/单价非法：{row['lot']}")
    for row in packet.parcels:
        if not row["root_choices"]:
            raise PacketError(f"地块没有可选砧木：{row['block']}")
        if row["nursery_count"] <= 0 or row["mu"] <= 0:
            raise PacketError(f"地块面积/用苗量非法：{row['block']}")

    # 托管支出长度对齐规划年。
    for row in packet.support:
        if len(row["cost_by_year"]) != len(horizon_years):
            raise PacketError(f"托管支出年度数与 horizon 不一致：{row['house']}")
        if row["cash_start"] < 0 or row["floor"] < 0 or row["loan_limit"] < 0:
            raise PacketError(f"支持参数非法：{row['house']}")
