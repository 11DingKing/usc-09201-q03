"""林地价值证据仓领域模型。

所有实体只追加、不改写：补测数据形成证据新版本，市场参数修订形成参数新版本，
每次估值按目的冻结一份输入快照并生成可追溯的估值版本，正式发布后生成结论。
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


class EvidenceKind(str, enum.Enum):
    """证据材料类型。"""

    FIELD_SURVEY = "field_survey"                 # 现场调查
    MANAGEMENT_PLAN = "management_plan"           # 经营方案（含管护义务）
    INCOME_RECORD = "income_record"               # 收益记录（中药材、康养等）
    THIRD_PARTY_VALUATION = "third_party_valuation"  # 第三方估值


class ValuationStatus(str, enum.Enum):
    """估值版本状态。"""

    FROZEN = "frozen"            # 快照已冻结，尚未计算
    INTERRUPTED = "interrupted"  # 计算中断，可重试且不会产生结论
    READY = "ready"              # 价值已算出，等待发布（可能已通过双人复核）
    IN_REVIEW = "in_review"      # 争议值，双人复核中
    REJECTED = "rejected"        # 复核未通过
    PUBLISHED = "published"      # 已生成正式结论


class ReviewDecision(str, enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ConclusionStatus(str, enum.Enum):
    OFFICIAL = "official"        # 当前正式结论
    SUPERSEDED = "superseded"    # 被新版本结论替代，但保留可追溯


class DomainError(Exception):
    """业务规则违反，基类。"""

    status = 422
    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self)}


class BadRequestError(DomainError):
    status = 400
    code = "bad_request"


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"


def canonical_hash(payload: Any) -> str:
    """对任意 JSON 兼容内容计算稳定指纹（字典按键排序）。"""

    body = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


@dataclass
class Appraiser:
    """评估师（评估资格可能到期）。"""

    id: str
    name: str
    license_no: str
    qualification_expires_on: str  # ISO 日期，发布当日必须仍有效
    registered_at: str


@dataclass
class Parcel:
    """一宗林地，可含中药材种植与康养设施等多种经营成分。"""

    id: str
    name: str
    location: str
    components: list[str]
    registered_at: str


@dataclass
class Evidence:
    """证据材料。

    同一 material_key 的补测只追加新版本；content_hash 用于识别完全重复的上传。
    recorded_on 可早于 submitted_at，以支持离线补传。
    """

    id: str
    parcel_id: str
    material_key: str
    version_no: int
    kind: str
    content: dict[str, Any]
    content_hash: str
    season: str
    observed_on: str
    recorded_on: str
    submitted_at: str
    idempotency_key: str


@dataclass
class MarketParams:
    """市场参数，按林地追加版本（如药材价格指数、折现率连续修订）。"""

    id: str
    parcel_id: str
    version_no: int
    values: dict[str, Any]
    effective_on: str
    note: str
    created_at: str


@dataclass
class EvidenceRef:
    """快照内冻结的证据引用，含内容副本，证据后续追加版本不影响快照。"""

    evidence_id: str
    material_key: str
    version_no: int
    kind: str
    content_hash: str
    season: str
    observed_on: str
    content: dict[str, Any]


@dataclass
class Snapshot:
    """按估值目的冻结的输入快照。"""

    id: str
    valuation_id: str
    parcel_id: str
    purpose: str
    season: str
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    params_version: int = 0
    params_values: dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    created_at: str = ""


@dataclass
class Review:
    """双人复核记录。"""

    reviewer_id: str
    decision: str
    reason: str
    reviewed_at: str


@dataclass
class Dispute:
    reason: str
    created_at: str


@dataclass
class Valuation:
    """估值版本：每版价值、引用证据与发布权限均可核对。"""

    id: str
    parcel_id: str
    purpose: str
    season: str
    version_no: int
    appraiser_id: str
    snapshot_id: str
    fingerprint: str
    status: str
    idempotency_key: str
    created_at: str
    value: float | None = None
    breakdown: dict[str, Any] = field(default_factory=dict)
    dispute: Dispute | None = None
    reviews: list[Review] = field(default_factory=list)
    computed_at: str | None = None
    interruption_count: int = 0


@dataclass
class Conclusion:
    """正式估值结论。每个估值版本至多一份，新版本发布后旧结论标记为 superseded。"""

    id: str
    valuation_id: str
    parcel_id: str
    purpose: str
    version_no: int
    value: float
    snapshot_id: str
    appraiser_id: str
    appraiser_license_no: str
    publisher_id: str
    published_at: str
    status: str = ConclusionStatus.OFFICIAL.value
    superseded_by: str | None = None
    reviews: list[Review] = field(default_factory=list)
