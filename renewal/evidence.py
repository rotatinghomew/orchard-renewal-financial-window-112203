"""可查支撑范围的树龄收益证据。

每条证据对应一个 ``(地区, 砧木, 接穗, 树龄)`` 组合，保留：

* 每个贡献果园的逐园值（树龄取该园最接近目标树龄的一行，同一年龄不重复计数）；
* 每亩产量/收入/经营支出的中位与极差，作为不确定性范围而非点估计；
* 证据用到的原始 ``(果园, 年份)`` 与在途观察清单，便于人工复核；
* 是否达到 ``pool.min_analogs``（按不同历史果园个数计数）。

只有 ``final=true`` 的完整年度记录可作支撑；在途观察（``final=false``）只列出、
不参与统计，也不补零。没有任何记录的组合返回 ``supported=False`` 的空证据，
调用方必须把它当作“待补证”，不能用于需要收益保证的签订。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Iterable

# 旧树可比记录允许的树龄差（资料格式.md：相差不超过两年）。
OLD_ANALOG_AGE_TOLERANCE = 2


@dataclass(frozen=True)
class Analog:
    """单个历史果园对某一树龄证据的贡献。"""

    orchard: str
    year: int
    age: int
    kg_per_mu: float
    revenue_per_mu: float
    cost_per_mu: float
    amended: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "orchard": self.orchard,
            "year": self.year,
            "age": self.age,
            "kg_per_mu": round(self.kg_per_mu, 3),
            "revenue_per_mu": round(self.revenue_per_mu, 3),
            "cost_per_mu": round(self.cost_per_mu, 3),
            "amended": self.amended,
        }


@dataclass(frozen=True)
class Observation:
    """在途观察：未收齐的年度，仅供查阅，不进证据统计。"""

    orchard: str
    year: int
    age: int
    kg_per_mu: float
    revenue_per_mu: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "orchard": self.orchard,
            "year": self.year,
            "age": self.age,
            "kg_per_mu": round(self.kg_per_mu, 3),
            "revenue_per_mu": round(self.revenue_per_mu, 3),
        }


@dataclass(frozen=True)
class AgeEvidence:
    region: str
    root: str
    scion: str
    age: int
    analogs: tuple[Analog, ...]
    in_transit: tuple[Observation, ...]
    min_analogs: int

    @property
    def supported(self) -> bool:
        return len({a.orchard for a in self.analogs}) >= self.min_analogs

    @property
    def orchard_count(self) -> int:
        return len({a.orchard for a in self.analogs})

    def _values(self, attr: str) -> tuple[float, ...]:
        return tuple(getattr(a, attr) for a in self.analogs)

    def median(self, attr: str) -> float | None:
        values = self._values(attr)
        return statistics.median(values) if values else None

    def range(self, attr: str) -> tuple[float, float] | None:
        values = self._values(attr)
        return (min(values), max(values)) if values else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "root": self.root,
            "scion": self.scion,
            "age": self.age,
            "supported": self.supported,
            "orchard_count": self.orchard_count,
            "min_analogs": self.min_analogs,
            "kg_per_mu": _summary(self._values("kg_per_mu")),
            "revenue_per_mu": _summary(self._values("revenue_per_mu")),
            "cost_per_mu": _summary(self._values("cost_per_mu")),
            "analogs": [a.as_dict() for a in self.analogs],
            "in_transit": [o.as_dict() for o in self.in_transit],
        }


def _summary(values: tuple[float, ...]) -> dict[str, Any] | None:
    if not values:
        return None
    return {
        "median": round(statistics.median(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


class EvidenceTable:
    """按 (地区, 砧木, 接穗, 树龄) 组织的证据表，支持旧树近龄锚查询。"""

    def __init__(self, history: Iterable[dict[str, Any]], min_analogs: int):
        self._min_analogs = min_analogs
        finals: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for row in history:
            (pending if not row["final"] else finals).append(row)
        self._finals = finals
        self._pending = pending
        self._exact: dict[tuple[str, str, str, int], AgeEvidence] = {}
        self._build_exact()

    def _build_exact(self) -> None:
        grouped: dict[tuple[str, str, str, int], list[dict[str, Any]]] = {}
        for row in self._finals:
            age = row["year"] - row["planted"]
            grouped.setdefault(
                (row["region"], row["root"], row["scion"], age), []
            ).append(row)
        for key, rows in grouped.items():
            # 每个果园只贡献一条：取该园在该组合下树龄最接近 key 的一行。
            best_by_orchard: dict[str, dict[str, Any]] = {}
            for row in rows:
                current = best_by_orchard.get(row["orchard"])
                if current is None or _age_gap(row, key[3]) < _age_gap(
                    current, key[3]
                ):
                    best_by_orchard[row["orchard"]] = row
            analogs = tuple(
                Analog(
                    orchard=row["orchard"],
                    year=row["year"],
                    age=row["year"] - row["planted"],
                    kg_per_mu=row["kg"] / row["mu"],
                    revenue_per_mu=row["revenue_yuan"] / row["mu"],
                    cost_per_mu=row["cost"] / row["mu"],
                    amended=bool(row.get("amended")),
                )
                for row in sorted(
                    best_by_orchard.values(), key=lambda r: (r["orchard"], r["year"])
                )
            )
            in_transit = tuple(
                Observation(
                    orchard=row["orchard"],
                    year=row["year"],
                    age=row["year"] - row["planted"],
                    kg_per_mu=row["kg"] / row["mu"],
                    revenue_per_mu=row["revenue_yuan"] / row["mu"],
                )
                for row in self._pending
                if (row["region"], row["root"], row["scion"]) == key[:3]
                and row["year"] - row["planted"] == key[3]
            )
            self._exact[key] = AgeEvidence(
                region=key[0],
                root=key[1],
                scion=key[2],
                age=key[3],
                analogs=analogs,
                in_transit=in_transit,
                min_analogs=self._min_analogs,
            )

    def lookup(
        self, region: str, root: str, scion: str, age: int
    ) -> AgeEvidence:
        """精确树龄证据；无记录时返回空的待补证证据（不补零）。"""
        if age < 0:
            raise ValueError(f"树龄不能为负：{age}")
        cached = self._exact.get((region, root, scion, age))
        if cached is not None:
            return cached
        return AgeEvidence(
            region=region,
            root=root,
            scion=scion,
            age=age,
            analogs=(),
            in_transit=(),
            min_analogs=self._min_analogs,
        )

    def old_tree_anchor(
        self, region: str, scion: str, age: int
    ) -> AgeEvidence:
        """旧树可比锚：同区同接穗、树龄相差不超过两年的完整经营记录。

        合并近龄各精确证据的果园（同一果园只保留树龄最接近的一行），
        重新按 ``min_analogs`` 判定是否够证。
        """
        candidate_rows: dict[str, dict[str, Any]] = {}
        for row in self._finals:
            if (
                row["region"] == region
                and row["root"] == "old"
                and row["scion"] == scion
                and abs((row["year"] - row["planted"]) - age)
                <= OLD_ANALOG_AGE_TOLERANCE
            ):
                current = candidate_rows.get(row["orchard"])
                if current is None or abs(
                    (row["year"] - row["planted"]) - age
                ) < abs((current["year"] - current["planted"]) - age):
                    candidate_rows[row["orchard"]] = row
        analogs = tuple(
            Analog(
                orchard=row["orchard"],
                year=row["year"],
                age=row["year"] - row["planted"],
                kg_per_mu=row["kg"] / row["mu"],
                revenue_per_mu=row["revenue_yuan"] / row["mu"],
                cost_per_mu=row["cost"] / row["mu"],
                amended=bool(row.get("amended")),
            )
            for row in sorted(
                candidate_rows.values(), key=lambda r: (r["orchard"], r["year"])
            )
        )
        return AgeEvidence(
            region=region,
            root="old",
            scion=scion,
            age=age,
            analogs=analogs,
            in_transit=(),
            min_analogs=self._min_analogs,
        )

    def all_evidence(self) -> tuple[AgeEvidence, ...]:
        return tuple(self._exact[key] for key in sorted(self._exact))

    def combination_supported_ages(
        self, region: str, root: str, scion: str
    ) -> tuple[int, ...]:
        return tuple(
            age
            for (rg, rt, sc, age), ev in sorted(self._exact.items())
            if (rg, rt, sc) == (region, root, scion) and ev.supported
        )


def _age_gap(row: dict[str, Any], target_age: int) -> int:
    return abs((row["year"] - row["planted"]) - target_age)
