"""确定性估值计算引擎。

价值只依赖冻结快照：林下作物收益、道路可达性、管护义务均随季节变化，
因此快照按估值季节选取证据版本。相同快照必产生相同价值与同口径分解，
计算中断时不落任何价值，重试仍是同一个估值版本，不会制造第二份结论。
"""

from __future__ import annotations

from typing import Any

from .models import Snapshot

DEFAULT_CAP_RATE = 0.08
DEFAULT_DISCOUNT_RATE = 0.05
DEFAULT_REMAINING_YEARS = 30


class CalculationInterrupted(Exception):
    """计算过程中断（例如离线端崩溃），调用方应把版本标记为 interrupted。"""


class IncompleteSnapshotError(Exception):
    """快照缺少必要输入，无法计算。"""


def _streams(content: dict[str, Any]) -> list[dict[str, Any]]:
    return list(content.get("streams", []))


def _annuity_present_value(cost: float, rate: float, years: int) -> float:
    if rate <= 0:
        return cost * years
    return cost * (1 - (1 + rate) ** -years) / rate


def calculate(snapshot: Snapshot, *, simulate_failure: bool = False) -> dict[str, Any]:
    """依据快照计算价值，返回 {value, breakdown}。

    口径（全部写入 breakdown 以便复核每一分钱来源）：
    - 收益记录中各经营成分（如中药材、康养设施）年净收益，乘以市场参数中
      该成分的价格指数后，按资本化率还原为价值；
    - 现场调查给出道路可达性系数，对经营价值整体调整；
    - 经营方案中的年度管护义务按剩余年限、折现率折现后扣减；
    - 第三方估值仅作为交叉核对参考，不并入计算值。
    """

    if simulate_failure:
        # 中断发生在任何结果写入之前：幂等重试即可，不产生半成品结论。
        raise CalculationInterrupted("估值计算中断")

    params = snapshot.params_values or {}
    cap_rate = float(params.get("cap_rate", DEFAULT_CAP_RATE))
    discount_rate = float(params.get("discount_rate", DEFAULT_DISCOUNT_RATE))
    remaining_years = int(params.get("remaining_years", DEFAULT_REMAINING_YEARS))
    price_index: dict[str, float] = {
        str(k): float(v) for k, v in (params.get("price_index") or {}).items()
    }
    if cap_rate <= 0:
        raise IncompleteSnapshotError("资本化率必须为正数")

    income_items: list[dict[str, Any]] = []
    annual_income = 0.0
    maintenance_cost = 0.0
    road_factor = 1.0
    third_party: list[dict[str, Any]] = []

    # 证据按 id 排序后处理，保证同快照结果逐位一致。
    for ref in sorted(snapshot.evidence_refs, key=lambda r: r.evidence_id):
        if ref.kind == "income_record":
            for stream in _streams(ref.content):
                component = str(stream.get("component", "未命名成分"))
                base = float(stream.get("annual_net_income", 0.0))
                factor = price_index.get(component, 1.0)
                adjusted = round(base * factor, 2)
                annual_income += adjusted
                income_items.append(
                    {
                        "component": component,
                        "evidence_id": ref.evidence_id,
                        "base_annual_net_income": base,
                        "price_factor": factor,
                        "adjusted_annual_net_income": adjusted,
                    }
                )
        elif ref.kind == "management_plan":
            maintenance_cost += float(ref.content.get("annual_maintenance_cost", 0.0))
        elif ref.kind == "field_survey":
            # 多条调查时取最保守（最低）可达性系数。
            factor = float(ref.content.get("road_accessibility_factor", 1.0))
            road_factor = min(road_factor, factor)
        elif ref.kind == "third_party_valuation":
            third_party.append(
                {
                    "evidence_id": ref.evidence_id,
                    "reference_value": ref.content.get("reference_value"),
                    "method": ref.content.get("method", ""),
                }
            )

    if not income_items:
        raise IncompleteSnapshotError("快照中缺少收益记录，无法形成估值")

    capitalized_income = round(annual_income / cap_rate, 2)
    road_adjusted = round(capitalized_income * road_factor, 2)
    maintenance_pv = round(
        _annuity_present_value(maintenance_cost, discount_rate, remaining_years), 2
    )
    value = round(road_adjusted - maintenance_pv, 2)

    breakdown = {
        "formula": (
            "年净收益(价格指数调整后) / 资本化率 × 道路系数 "
            "- 管护义务年金现值"
        ),
        "params_version": snapshot.params_version,
        "season": snapshot.season,
        "income_items": income_items,
        "annual_net_income": round(annual_income, 2),
        "cap_rate": cap_rate,
        "capitalized_income": capitalized_income,
        "road_accessibility_factor": road_factor,
        "road_adjusted_value": road_adjusted,
        "annual_maintenance_cost": maintenance_cost,
        "maintenance_pv": maintenance_pv,
        "discount_rate": discount_rate,
        "remaining_years": remaining_years,
        "third_party_references": third_party,
        "evidence_count": len(snapshot.evidence_refs),
    }
    return {"value": value, "breakdown": breakdown}
