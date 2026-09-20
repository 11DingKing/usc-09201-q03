"""价值计算引擎。

计算是纯函数：相同的（宗地物理量快照 + 市场参数快照 + 公式版本）必然
得到相同价值与明细，使每版结论都可被银行复核复算。

取值口径（``forest-income-v1``，永续经营简化模型）：

* 林木价值      = 活立木蓄积 × 木材单价
* 中药材现值    = 中药材面积 × 亩均年收益 / 折现率 × 道路可达系数
* 康养设施现值  = 康养设施年收益 / 折现率 × 道路可达系数
* 管护义务现值  = 宗地面积 × 亩均年管护成本 / 折现率（扣减项）
* 总价值        = 林木 + 中药材 + 康养 − 管护义务

物理量来自快照内的现场调查证据；收益记录用于交叉核对但不重复计价；
第三方估值仅作为参照值参与争议判定，不并入收益法结果，避免双重计算。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .errors import CalculationInterruptedError, ValidationError
from .models import Evidence, EvidenceKind, MarketParameters

FORMULA_VERSION = "forest-income-v1"

# 与第三方参照值偏差超过该比例即自动标记为争议值
DISPUTE_THRESHOLD = 0.20


class CalculationFailure(Exception):
    """用于测试的可注入中断：模拟计算中途失败。"""


def _latest_by_kind(evidence: list[Evidence], kind: EvidenceKind) -> Evidence | None:
    matches = [e for e in evidence if e.kind is kind]
    return max(matches, key=lambda e: e.collected_on) if matches else None


def _require_number(attributes: dict[str, Any], key: str, label: str) -> float:
    value = attributes.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValidationError(f"现场调查缺少数值字段：{label}", field=key)
    result = float(value)
    if result < 0:
        raise ValidationError(f"{label}不能为负", field=key)
    return result


def calculate_value(
    area_mu: float,
    evidence: list[Evidence],
    params: MarketParameters,
    *,
    interrupter: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """执行价值计算，返回价值、明细、参照值与争议标记。

    :param interrupter: 可选回调，在副作用写入前被调用；抛出
        :class:`CalculationFailure` 时调用方保证版本不落任何结果，
        可安全重试（计算中断不产生半成品结论）。
    """

    survey = _latest_by_kind(evidence, EvidenceKind.FIELD_SURVEY)
    if survey is None:
        raise ValidationError("快照必须包含至少一份现场调查证据")

    timber_volume = _require_number(survey.attributes, "timber_volume_m3", "活立木蓄积")
    herb_area = _require_number(survey.attributes, "herb_area_mu", "中药材面积")
    if herb_area > area_mu + 1e-9:
        raise ValidationError("中药材面积不能超过宗地面积", field="herb_area_mu")

    if not 0.0 <= params.road_accessibility_factor <= 1.0:
        raise ValidationError("道路可达性系数必须位于 0~1 之间")
    if params.discount_rate <= 0:
        raise ValidationError("折现率必须为正")

    def checkpoint() -> None:
        if interrupter is not None:
            interrupter()

    # 各组成部分在中断检查点之后才生成，保证中断时无部分结果外泄
    timber_value = round(timber_volume * params.timber_price, 2)
    checkpoint()

    road = params.road_accessibility_factor
    herb_pv = round(herb_area * params.herb_income_per_mu / params.discount_rate * road, 2)
    checkpoint()

    wellness_pv = round(params.wellness_annual_income / params.discount_rate * road, 2)
    checkpoint()

    management_cost_pv = round(
        area_mu * params.management_cost_per_mu / params.discount_rate, 2
    )
    checkpoint()

    total = round(
        timber_value + herb_pv + wellness_pv - management_cost_pv, 2
    )

    reference: Evidence | None = _latest_by_kind(
        evidence, EvidenceKind.THIRD_PARTY_VALUATION
    )
    reference_value: float | None = None
    disputed = False
    if reference is not None:
        raw = reference.attributes.get("reference_value")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            reference_value = round(float(raw), 2)
            disputed = abs(total - reference_value) / reference_value > DISPUTE_THRESHOLD

    return {
        "value": total,
        "breakdown": {
            "timber_value": timber_value,
            "herb_present_value": herb_pv,
            "wellness_present_value": wellness_pv,
            "management_cost_present_value": management_cost_pv,
        },
        "physical_inputs": {
            "area_mu": area_mu,
            "timber_volume_m3": timber_volume,
            "herb_area_mu": herb_area,
            "road_accessibility_factor": road,
        },
        "reference_value": reference_value,
        "disputed": disputed,
        "survey_evidence_id": survey.evidence_id,
        "survey_season": survey.season.value,
        "survey_collected_on": survey.collected_on.isoformat(),
    }


def run_with_interruption_guard(
    area_mu: float,
    evidence: list[Evidence],
    params: MarketParameters,
    *,
    interrupter: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """包装计算，把注入的中断转换为领域中断错误且不返回部分结果。"""

    try:
        return calculate_value(area_mu, evidence, params, interrupter=interrupter)
    except CalculationFailure as exc:  # 测试/运行期模拟的中断
        raise CalculationInterruptedError("价值计算中断，版本保持可重试状态") from exc
