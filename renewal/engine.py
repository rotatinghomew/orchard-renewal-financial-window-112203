"""同年景下的逐年现金、借款与联保占用模拟。

口径（见 ``资料格式.md``）：

* 情景系数同时作用于正常年实物产量和每公斤价格，故地块收入按
  ``基准每亩收入 × 价系数 × 量系数`` 缩放；经营支出不随情景缩放。
* 留着的地按旧树锚计收入；新树从种植当年（树龄 0）按证据中位计；
  已清园而未栽的年份没有收入，只承担实际承诺支出。
* 苗价是种植年一次性每株费用；``paid`` 订位的苗款已从初始现金扣过，
  种植年不再扣第二次。历史经营 ``cost`` 不含苗价。
* 年结净现金低于底线时可借至底线，余额超出时先归还借款；
  每户受 ``loan_limit`` 约束，联保池受 ``annual_guarantee_limit`` 约束，
  且各情景独立结算，不能用其他情景的闲置互抵。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .evidence import EvidenceTable
from .plans import KEEP, PLANT, Plan


@dataclass(frozen=True)
class YearRecord:
    year: int
    operating_cash: float          # 地块经营净现金流（不含借款、不含苗款）
    seedling_outlay: float         # 当年新增苗款支出（paid 订位为 0）
    pre_loan_cash: float           # 借款前年末现金
    borrow: float                  # 当年新借
    repay: float                   # 当年归还
    end_cash: float                # 年结现金
    debt_outstanding: float        # 年末未还借款
    guarantee_used: float          # 本户当年联保占用（= 年末未还余额）
    block_detail: tuple[dict[str, Any], ...]
    feasible: bool
    shortfall: float               # 借满后仍低于底线的缺口

    def as_dict(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "operating_cash": round(self.operating_cash, 2),
            "seedling_outlay": round(self.seedling_outlay, 2),
            "pre_loan_cash": round(self.pre_loan_cash, 2),
            "borrow": round(self.borrow, 2),
            "repay": round(self.repay, 2),
            "end_cash": round(self.end_cash, 2),
            "debt_outstanding": round(self.debt_outstanding, 2),
            "guarantee_used": round(self.guarantee_used, 2),
            "feasible": self.feasible,
            "shortfall": round(self.shortfall, 2),
            "blocks": list(self.block_detail),
        }


@dataclass(frozen=True)
class HouseholdSimulation:
    house: str
    scenario: str
    feasible: bool
    years: tuple[YearRecord, ...]
    max_guarantee_used: float
    total_borrowed: float
    terminal_cash: float
    min_end_cash: float
    cumulative_operating: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "house": self.house,
            "scenario": self.scenario,
            "feasible": self.feasible,
            "max_guarantee_used": round(self.max_guarantee_used, 2),
            "total_borrowed": round(self.total_borrowed, 2),
            "terminal_cash": round(self.terminal_cash, 2),
            "min_end_cash": round(self.min_end_cash, 2),
            "cumulative_operating": round(self.cumulative_operating, 2),
            "years": [row.as_dict() for row in self.years],
        }


@dataclass(frozen=True)
class LotNeed:
    """方案在种植年对某批次的苗量需求。"""

    block: str
    lot: str
    root: str
    scion: str
    plant_year: int
    count: int
    unit_price: float
    already_paid: bool


def lot_needs(
    plan: Plan,
    parcels: list[dict[str, Any]],
    stock: list[dict[str, Any]],
    paid_lots: dict[tuple[str, str], dict[str, Any]],
) -> tuple[LotNeed, ...]:
    """列出方案需要锁定的批次；已付定金的批次标记为 paid，不再计苗款。"""
    parcel_index = {row["block"]: row for row in parcels}
    stock_index = {(row["root"], row["scion"], row["year"]): row for row in stock}
    needs: list[LotNeed] = []
    for action in plan.actions:
        if action.kind != PLANT:
            continue
        parcel = parcel_index[action.block]
        lot_row = stock_index[(action.root, parcel["scion"], action.plant_year)]
        paid = paid_lots.get((action.block, lot_row["lot"]))
        needs.append(
            LotNeed(
                block=action.block,
                lot=lot_row["lot"],
                root=action.root,
                scion=parcel["scion"],
                plant_year=action.plant_year,
                count=parcel["nursery_count"],
                unit_price=lot_row["unit_price"],
                already_paid=bool(paid),
            )
        )
    return tuple(needs)


def simulate_household(
    plan: Plan,
    *,
    parcels: list[dict[str, Any]],
    support: dict[str, Any],
    scenarios: list[dict[str, Any]],
    evidence: EvidenceTable,
    stock: list[dict[str, Any]],
    paid_lots: dict[tuple[str, str], dict[str, Any]],
    scenario_id: str,
) -> HouseholdSimulation:
    """单户、单情景的逐年现金模拟（不含联保池总量约束）。"""
    start, end = plan.horizon
    years = list(range(start, end + 1))
    parcel_index = {row["block"]: row for row in parcels}
    factor_index = {
        (row["year"], row["region"]): row
        for row in scenarios
        if row["id"] == scenario_id
    }
    needs = lot_needs(plan, parcels, stock, paid_lots)
    seedling_by_year: dict[int, float] = {}
    for need in needs:
        if not need.already_paid:
            seedling_by_year[need.plant_year] = (
                seedling_by_year.get(need.plant_year, 0.0)
                + need.count * need.unit_price
            )

    cash = float(support["cash_start"])
    debt = 0.0
    records: list[YearRecord] = []
    total_borrowed = 0.0
    max_guarantee = 0.0
    cumulative_operating = 0.0
    feasible = True
    loan_limit = float(support["loan_limit"])
    floor = float(support["floor"])

    for offset, year in enumerate(years):
        block_rows: list[dict[str, Any]] = []
        operating = 0.0
        for action in plan.actions:
            parcel = parcel_index[action.block]
            factor = factor_index[(year, parcel["region"])]
            block_cash, detail = _block_cash(
                action=action,
                parcel=parcel,
                year=year,
                start=start,
                factor=factor,
                evidence=evidence,
            )
            operating += block_cash
            block_rows.append(detail)
        # 农户级已承诺托管支出（从 2027 起每年一笔）。
        committed = float(support["cost_by_year"][offset])
        operating -= committed
        cumulative_operating += operating

        seedling = seedling_by_year.get(year, 0.0)
        pre_loan = cash + operating - seedling
        need_borrow = max(0.0, floor - pre_loan)
        headroom = max(0.0, loan_limit - debt)
        borrowed = min(need_borrow, headroom)
        shortfall = max(0.0, need_borrow - borrowed)
        after_borrow = pre_loan + borrowed
        repaid = 0.0
        if after_borrow > floor and debt > 0:
            repaid = min(debt, after_borrow - floor)
        debt = debt + borrowed - repaid
        cash = after_borrow - repaid
        if shortfall > 0.01:
            feasible = False
        total_borrowed += borrowed
        max_guarantee = max(max_guarantee, debt)
        records.append(
            YearRecord(
                year=year,
                operating_cash=operating,
                seedling_outlay=seedling,
                pre_loan_cash=pre_loan,
                borrow=borrowed,
                repay=repaid,
                end_cash=cash,
                debt_outstanding=debt,
                guarantee_used=debt,
                block_detail=tuple(block_rows),
                feasible=shortfall <= 0.01,
                shortfall=shortfall,
            )
        )
    return HouseholdSimulation(
        house=plan.house,
        scenario=scenario_id,
        feasible=feasible,
        years=tuple(records),
        max_guarantee_used=max_guarantee,
        total_borrowed=total_borrowed,
        terminal_cash=cash,
        min_end_cash=min(row.end_cash for row in records),
        cumulative_operating=cumulative_operating,
    )


def _block_cash(*, action, parcel, year, start, factor, evidence):
    """返回地块当年经营净现金（收入-经营支出）与明细。苗款在农户层处理。"""
    detail: dict[str, Any] = {
        "block": parcel["block"],
        "year": year,
        "region": parcel["region"],
        "state": None,
        "mu": parcel["mu"],
    }
    if action.kind == KEEP:
        age = parcel["old_age"] + (year - start)
        anchor = evidence.old_tree_anchor(
            parcel["region"], parcel["old_scion"], age
        )
        rev_per_mu = anchor.median("revenue_per_mu") or 0.0
        cost_per_mu = anchor.median("cost_per_mu") or 0.0
        detail.update(
            {
                "state": "old",
                "age": age,
                "evidence_orchards": anchor.orchard_count,
                "base_revenue_per_mu": round(rev_per_mu, 3),
            }
        )
    else:
        if year < action.plant_year:
            # 已清园而未栽：无收入、无地块经营支出（实际承诺在农户层记账）。
            detail.update({"state": "cleared_idle", "age": None})
            return 0.0, detail
        age = year - action.plant_year
        ev = evidence.lookup(parcel["region"], action.root, parcel["scion"], age)
        rev_per_mu = ev.median("revenue_per_mu") or 0.0
        cost_per_mu = ev.median("cost_per_mu") or 0.0
        detail.update(
            {
                "state": "new",
                "root": action.root,
                "scion": parcel["scion"],
                "age": age,
                "evidence_orchards": ev.orchard_count,
                "base_revenue_per_mu": round(rev_per_mu, 3),
            }
        )
    revenue = (
        rev_per_mu
        * parcel["mu"]
        * factor["price_factor"]
        * factor["yield_factor"]
    )
    cost = cost_per_mu * parcel["mu"]
    detail.update(
        {
            "revenue": round(revenue, 2),
            "operating_cost": round(cost, 2),
            "net": round(revenue - cost, 2),
            "price_factor": factor["price_factor"],
            "yield_factor": factor["yield_factor"],
        }
    )
    return revenue - cost, detail
