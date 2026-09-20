"""领域模型：证据、林地、估值版本与复核记录。

设计要点：

* 证据一旦登记即 **不可变**（immutable）。补测数据不能覆盖旧证据，只能
  登记新证据并由新版本引用；每条证据记录采集期（季节），使林下作物、
  道路可达性、管护义务等随季节变化的要素可以被解释。
* 估值版本按"估值目的 + 编制人"维度冻结一份输入快照（证据指纹集合 +
  市场参数 + 公式版本）。版本一经冻结，其引用不可改变；参数修订只能
  产生下一版本。
* 版本状态机为：``DRAFT → FROZEN → CALCULATING → CALCULATED →
  IN_REVIEW（争议时）→ PUBLISHED``；发布失败（资格过期等）保持
  ``CALCULATED``，绝不产生半成品正式结论。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any

# 证据内容指纹允许覆盖的键（稳定序列化时使用）
_CONTENT_KEYS = (
    "title",
    "collected_on",
    "season",
    "attributes",
)


class EvidenceKind(str, Enum):
    """证据材料类别，对应证据仓接收的四类输入。"""

    FIELD_SURVEY = "field_survey"  # 现场调查
    MANAGEMENT_PLAN = "management_plan"  # 经营方案
    INCOME_RECORD = "income_record"  # 收益记录
    THIRD_PARTY_VALUATION = "third_party_valuation"  # 第三方估值


class Season(str, Enum):
    """采集季节。季节性要素必须能追溯到具体采集期。"""

    SPRING = "spring"
    SUMMER = "summer"
    AUTUMN = "autumn"
    WINTER = "winter"


class VersionStatus(str, Enum):
    """估值版本生命周期状态。"""

    DRAFT = "draft"
    FROZEN = "frozen"  # 输入快照已冻结，尚未计算
    CALCULATING = "calculating"
    CALCULATED = "calculated"
    IN_REVIEW = "in_review"  # 争议值双人复核中
    REVIEW_REJECTED = "review_rejected"  # 复核否决，只能新建版本
    PUBLISHED = "published"


class ReviewResult(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


def evidence_fingerprint(kind: str, source_doc: str, content: dict[str, Any]) -> str:
    """计算证据业务指纹。

    同一材料（同类别、同来源文号、同规范化内容）无论上传多少次、
    是否离线补传，指纹都相同 —— 仓储据此去重，保证不会出现重复证据。
    """

    normalized = {key: content.get(key) for key in _CONTENT_KEYS}
    if isinstance(normalized.get("attributes"), dict):
        normalized["attributes"] = {
            str(k): normalized["attributes"][k]
            for k in sorted(normalized["attributes"], key=str)
        }
    payload = json.dumps(
        {"kind": kind, "source_doc": source_doc.strip(), "content": normalized},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Evidence:
    """不可变证据记录。"""

    evidence_id: str
    parcel_id: str
    kind: EvidenceKind
    source_doc: str
    title: str
    collected_on: date
    season: Season
    attributes: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    uploaded_by: str = ""
    uploaded_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "parcel_id": self.parcel_id,
            "kind": self.kind.value,
            "source_doc": self.source_doc,
            "title": self.title,
            "collected_on": self.collected_on.isoformat(),
            "season": self.season.value,
            "attributes": self.attributes,
            "fingerprint": self.fingerprint,
            "uploaded_by": self.uploaded_by,
            "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else None,
        }


@dataclass(frozen=True)
class MarketParameters:
    """一版市场参数。参数连续修订形成版本序列，旧值永不被覆盖。"""

    params_id: str
    timber_price: float  # 木材单价（元/立方米）
    herb_income_per_mu: float  # 林下中药材年收益（元/亩）
    wellness_annual_income: float  # 康养设施年收益（元）
    road_accessibility_factor: float  # 道路可达性系数（0~1）
    management_cost_per_mu: float  # 年管护成本（元/亩）
    discount_rate: float  # 资本化折现率（如 0.045）
    effective_from: date
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "params_id": self.params_id,
            "timber_price": self.timber_price,
            "herb_income_per_mu": self.herb_income_per_mu,
            "wellness_annual_income": self.wellness_annual_income,
            "road_accessibility_factor": self.road_accessibility_factor,
            "management_cost_per_mu": self.management_cost_per_mu,
            "discount_rate": self.discount_rate,
            "effective_from": self.effective_from.isoformat(),
            "note": self.note,
        }


@dataclass
class ReviewRecord:
    """争议值双人复核记录（两名相互独立的复核人）。"""

    version_id: str
    requested_by: str
    requested_at: datetime
    result: ReviewResult = ReviewResult.PENDING
    opinions: dict[str, str] = field(default_factory=dict)  # reviewer_id -> 意见
    decided_at: datetime | None = None

    @property
    def approvers(self) -> list[str]:
        return sorted(self.opinions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "requested_by": self.requested_by,
            "requested_at": self.requested_at.isoformat(),
            "result": self.result.value,
            "opinions": dict(self.opinions),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }


@dataclass
class ValuationVersion:
    """一份冻结输入快照并产出可追溯结论的估值版本。"""

    version_id: str
    parcel_id: str
    purpose: str  # 估值目的（如 bank_financing_2026_autumn）
    purpose_label: str
    version_no: int  # 同一目的下的连续序号（从 1 开始）
    preparer_id: str  # 编制人（评估师）
    qualification_id: str
    qualification_expires_on: date
    evidence_ids: list[str]  # 冻结快照中的证据（有序、不可变）
    evidence_fingerprints: list[str]
    params_snapshot: MarketParameters
    formula_version: str
    status: VersionStatus = VersionStatus.DRAFT
    disputed: bool = False
    value: float | None = None
    value_breakdown: dict[str, float] = field(default_factory=dict)
    physical_inputs: dict[str, Any] = field(default_factory=dict)
    reference_value: float | None = None
    season: Season | None = None
    calc_error: str | None = None
    calculated_at: datetime | None = None
    published_at: datetime | None = None
    published_by: str | None = None
    conclusion_no: str | None = None  # 正式结论编号
    review: ReviewRecord | None = None
    supersedes_version_id: str | None = None  # 本版修订自哪一版
    created_at: datetime | None = None

    def is_published(self) -> bool:
        return self.status is VersionStatus.PUBLISHED

    def evidence_snapshot_hash(self) -> str:
        """快照指纹：证据集合（含顺序）+ 参数 + 公式 + 资格。

        使每版价值都能解释"用了哪版证据、哪版参数"。
        """

        payload = json.dumps(
            {
                "parcel_id": self.parcel_id,
                "purpose": self.purpose,
                "version_no": self.version_no,
                "evidence": self.evidence_fingerprints,
                "params": self.params_snapshot.to_dict(),
                "formula_version": self.formula_version,
                "qualification_id": self.qualification_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "parcel_id": self.parcel_id,
            "purpose": self.purpose,
            "purpose_label": self.purpose_label,
            "version_no": self.version_no,
            "preparer_id": self.preparer_id,
            "qualification_id": self.qualification_id,
            "qualification_expires_on": self.qualification_expires_on.isoformat(),
            "evidence_ids": list(self.evidence_ids),
            "evidence_fingerprints": list(self.evidence_fingerprints),
            "snapshot_hash": self.evidence_snapshot_hash(),
            "params_snapshot": self.params_snapshot.to_dict(),
            "formula_version": self.formula_version,
            "status": self.status.value,
            "disputed": self.disputed,
            "value": self.value,
            "value_breakdown": dict(self.value_breakdown),
            "physical_inputs": dict(self.physical_inputs),
            "reference_value": self.reference_value,
            "season": self.season.value if self.season else None,
            "calc_error": self.calc_error,
            "calculated_at": self.calculated_at.isoformat() if self.calculated_at else None,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "published_by": self.published_by,
            "conclusion_no": self.conclusion_no,
            "review": self.review.to_dict() if self.review else None,
            "supersedes_version_id": self.supersedes_version_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
