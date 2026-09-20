"""证据仓应用服务：编排领域规则与发布闸门。

关键规则（与城口县金融服务中心约定一致）：

1. 证据不可变；补测/季节变化只能登记新证据、形成新版本，旧版本原样保留。
2. 创建版本即冻结输入快照（证据指纹有序集合 + 市场参数快照 + 公式版本 +
   评估资格），此后任何修订都产生新版本，使每版价值可复算、可解释。
3. 发布闸门全部通过才会生成唯一的正式结论：
   - 价值已计算成功（中断的版本停留草稿/计算态，可安全重试）；
   - 发布当日评估资格仍在有效期内（资格过期直接阻止发布）；
   - 争议值须经 **两名相互独立、且均非编制人** 的复核人共同通过；
   - 同一估值目的至多一份 ``PUBLISHED`` 结论（库内原子检查）。
4. 所有写入支持 ``Idempotency-Key``；重复上传、离线补传、计算中断、
   并发发布都不会制造两份正式结论。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Callable

from .calc import FORMULA_VERSION, run_with_interruption_guard
from .errors import (
    AuthorizationError,
    ConflictError,
    NotCalculatedError,
    QualificationExpiredError,
    ReviewRejectedError,
    ReviewRequiredError,
    ValidationError,
)
from .models import (
    Evidence,
    EvidenceKind,
    MarketParameters,
    ReviewRecord,
    ReviewResult,
    Season,
    ValuationVersion,
    VersionStatus,
    evidence_fingerprint,
)
from .repository import Repository

ROLE_APPRAISER = "appraiser"
ROLE_REVIEWER = "reviewer"
ROLE_PUBLISHER = "publisher"

# 双人复核所需独立人数
REVIEW_QUORUM = 2


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class EvidenceVaultService:
    """应用服务门面。时间与标识生成均可注入以便测试。"""

    def __init__(
        self,
        repository: Repository | None = None,
        *,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.repo = repository or Repository()
        self._clock = clock

    # ---- 工具 ---------------------------------------------------------

    def _today(self) -> date:
        return self._clock().date()

    @staticmethod
    def _require_role(actor: dict[str, Any], role: str) -> None:
        if role not in actor.get("roles", []):
            raise AuthorizationError(f"需要{role}角色", required_role=role)

    # ---- 宗地 ---------------------------------------------------------

    def register_parcel(self, actor: dict[str, Any], name: str, area_mu: float) -> dict[str, Any]:
        if not isinstance(area_mu, (int, float)) or area_mu <= 0:
            raise ValidationError("宗地面积必须为正数")
        parcel_id = f"P{_new_id('')}"
        return self.repo.create_parcel(parcel_id, name=name, area_mu=float(area_mu))

    # ---- 证据 ---------------------------------------------------------

    def submit_evidence(
        self,
        actor: dict[str, Any],
        *,
        parcel_id: str,
        kind: str,
        source_doc: str,
        title: str,
        collected_on: str,
        season: str,
        attributes: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """登记证据。同材料重复上传或离线补传返回同一条记录。"""

        parcel = self.repo.get_parcel(parcel_id)  # 宗地须存在
        try:
            evidence_kind = EvidenceKind(kind)
            evidence_season = Season(season)
        except ValueError as exc:
            raise ValidationError("证据类别或季节取值不合法", value=str(exc)) from None
        if not source_doc.strip() or not title.strip():
            raise ValidationError("来源文号与标题不可为空")
        try:
            collected_date = date.fromisoformat(collected_on)
        except ValueError as exc:
            raise ValidationError("采集日期格式应为 YYYY-MM-DD") from None
        attributes = attributes or {}

        def create() -> tuple[Evidence, bool]:
            evidence_id = _new_id("E")
            fingerprint = evidence_fingerprint(
                evidence_kind.value,
                source_doc,
                {
                    "title": title,
                    "collected_on": collected_on,
                    "season": evidence_season.value,
                    "attributes": attributes,
                },
            )
            evidence = Evidence(
                evidence_id=evidence_id,
                parcel_id=parcel_id,
                kind=evidence_kind,
                source_doc=source_doc.strip(),
                title=title.strip(),
                collected_on=collected_date,
                season=evidence_season,
                attributes=dict(attributes),
                fingerprint=fingerprint,
                uploaded_by=actor.get("user_id", "anonymous"),
                uploaded_at=self._clock(),
            )
            return self.repo.add_evidence(evidence)

        if idempotency_key:
            stored, created = self.repo.idempotent(
                f"evidence:{idempotency_key}", create
            )
        else:
            stored, created = create()
        result = stored.to_dict()
        result["parcel_name"] = parcel["name"]
        result["deduplicated"] = not created
        return result

    def list_evidence(self, parcel_id: str) -> list[dict[str, Any]]:
        self.repo.get_parcel(parcel_id)
        return [e.to_dict() for e in self.repo.list_evidence(parcel_id)]

    # ---- 市场参数 -----------------------------------------------------

    def revise_market_parameters(
        self,
        actor: dict[str, Any],
        *,
        parcel_id: str,
        timber_price: float,
        herb_income_per_mu: float,
        wellness_annual_income: float,
        road_accessibility_factor: float,
        management_cost_per_mu: float,
        discount_rate: float,
        effective_from: str,
        note: str = "",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """登记新一版市场参数。连续修订只追加，旧版本永不修改。"""

        self.repo.get_parcel(parcel_id)
        try:
            effective_date = date.fromisoformat(effective_from)
        except ValueError as exc:
            raise ValidationError("生效日期格式应为 YYYY-MM-DD") from None
        for key, value in {
            "timber_price": timber_price,
            "herb_income_per_mu": herb_income_per_mu,
            "wellness_annual_income": wellness_annual_income,
            "management_cost_per_mu": management_cost_per_mu,
            "discount_rate": discount_rate,
        }.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise ValidationError(f"参数必须为非负数值：{key}")
        if discount_rate <= 0:
            raise ValidationError("折现率必须为正")
        if not 0.0 <= road_accessibility_factor <= 1.0:
            raise ValidationError("道路可达性系数必须位于 0~1 之间")

        def create() -> MarketParameters:
            params = MarketParameters(
                params_id=_new_id(f"MP::{parcel_id}"),
                timber_price=float(timber_price),
                herb_income_per_mu=float(herb_income_per_mu),
                wellness_annual_income=float(wellness_annual_income),
                road_accessibility_factor=float(road_accessibility_factor),
                management_cost_per_mu=float(management_cost_per_mu),
                discount_rate=float(discount_rate),
                effective_from=effective_date,
                note=note,
            )
            return self.repo.add_params(params)

        stored = self.repo.idempotent(f"params:{idempotency_key}", create) if idempotency_key else create()
        return stored.to_dict()

    # ---- 版本：冻结快照 ----------------------------------------------

    def create_version(
        self,
        actor: dict[str, Any],
        *,
        parcel_id: str,
        purpose: str,
        purpose_label: str,
        evidence_ids: list[str],
        params_id: str,
        qualification_id: str,
        qualification_expires_on: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """冻结一份输入快照，形成新的估值版本。"""

        self._require_role(actor, ROLE_APPRAISER)
        parcel = self.repo.get_parcel(parcel_id)
        evidence_list = self.repo.require_evidence(parcel_id, list(evidence_ids))
        params = self.repo.get_params(params_id)
        if params.timber_price is None:  # pragma: no cover - 防御性
            raise ValidationError("市场参数不完整")
        try:
            expires = date.fromisoformat(qualification_expires_on)
        except ValueError as exc:
            raise ValidationError("资格到期日格式应为 YYYY-MM-DD") from None
        if not purpose.strip():
            raise ValidationError("估值目的不可为空")

        prior = self.repo.latest_version(parcel_id, purpose)

        def create() -> ValuationVersion:
            # 以最新调查证据的采集季节作为版本季节口径
            surveys = [e for e in evidence_list if e.kind is EvidenceKind.FIELD_SURVEY]
            season = max(surveys, key=lambda e: e.collected_on).season if surveys else None
            version = ValuationVersion(
                version_id=_new_id("V"),
                parcel_id=parcel_id,
                purpose=purpose.strip(),
                purpose_label=purpose_label or purpose,
                version_no=0,  # 仓储在落库时分配
                preparer_id=actor["user_id"],
                qualification_id=qualification_id,
                qualification_expires_on=expires,
                evidence_ids=[e.evidence_id for e in evidence_list],
                evidence_fingerprints=[e.fingerprint for e in evidence_list],
                params_snapshot=params,
                formula_version=FORMULA_VERSION,
                status=VersionStatus.FROZEN,
                season=season,
                supersedes_version_id=prior.version_id if prior else None,
                created_at=self._clock(),
            )
            return self.repo.add_version(version)

        stored = self.repo.idempotent(f"version:{idempotency_key}", create) if idempotency_key else create()
        result = stored.to_dict()
        result["parcel_name"] = parcel["name"]
        return result

    # ---- 版本：计算 ---------------------------------------------------

    def calculate_version(
        self,
        actor: dict[str, Any],
        version_id: str,
        *,
        interrupter: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """对冻结快照执行计算。中断时版本保持可重试，不产生部分结果。"""

        self._require_role(actor, ROLE_APPRAISER)
        version = self.repo.get_version(version_id)
        lock = self.repo.version_lock(version_id)
        with lock:
            if version.status not in (
                VersionStatus.DRAFT,
                VersionStatus.FROZEN,
                VersionStatus.CALCULATING,
            ):
                raise ConflictError(
                    "当前状态不允许重新计算",
                    status=version.status.value,
                )
            if actor["user_id"] != version.preparer_id:
                raise AuthorizationError("仅版本编制人可发起计算")
            version.status = VersionStatus.CALCULATING
            version.calc_error = None

            parcel = self.repo.get_parcel(version.parcel_id)
            evidence_list = self.repo.require_evidence(
                version.parcel_id, version.evidence_ids
            )
            # 计算为纯函数；若中断抛错，下面的赋值不会执行
            outcome = run_with_interruption_guard(
                float(parcel["area_mu"]),
                evidence_list,
                version.params_snapshot,
                interrupter=interrupter,
            )

            version.value = outcome["value"]
            version.value_breakdown = dict(outcome["breakdown"])
            version.physical_inputs = dict(outcome["physical_inputs"])
            version.reference_value = outcome["reference_value"]
            version.disputed = outcome["disputed"]
            version.calculated_at = self._clock()
            version.calc_error = None
            version.status = (
                VersionStatus.IN_REVIEW if outcome["disputed"] else VersionStatus.CALCULATED
            )
            if outcome["disputed"]:
                version.review = ReviewRecord(
                    version_id=version.version_id,
                    requested_by=actor["user_id"],
                    requested_at=self._clock(),
                )
            return version.to_dict()

    # ---- 争议：双人复核 ----------------------------------------------

    def submit_review_opinion(
        self,
        actor: dict[str, Any],
        version_id: str,
        *,
        approved: bool,
        comment: str = "",
    ) -> dict[str, Any]:
        """复核人提交独立意见。两人均批准才通过；任一否决即驳回。"""

        self._require_role(actor, ROLE_REVIEWER)
        version = self.repo.get_version(version_id)
        lock = self.repo.version_lock(version_id)
        with lock:
            if not version.disputed or version.review is None:
                raise ConflictError("该版本不处于争议复核流程")
            if version.status is not VersionStatus.IN_REVIEW:
                raise ConflictError("复核流程已结束", status=version.status.value)
            reviewer_id = actor["user_id"]
            if reviewer_id == version.preparer_id:
                raise AuthorizationError("编制人不能复核本人编制的版本")
            if reviewer_id in version.review.opinions:
                raise ConflictError("该复核人已提交意见，意见不可更改")

            version.review.opinions[reviewer_id] = (
                f"{'approved' if approved else 'rejected'}: {comment}".strip()
            )

            if not approved:
                version.review.result = ReviewResult.REJECTED
                version.review.decided_at = self._clock()
                version.status = VersionStatus.REVIEW_REJECTED
                return version.to_dict()

            if len(version.review.opinions) >= REVIEW_QUORUM:
                version.review.result = ReviewResult.APPROVED
                version.review.decided_at = self._clock()
                version.status = VersionStatus.CALCULATED
            return version.to_dict()

    # ---- 发布闸门 -----------------------------------------------------

    def _check_publish_gate(self, version: ValuationVersion) -> None:
        """发布前全部闸门；任一不过即阻止，且不改变版本状态。"""

        if version.status is VersionStatus.PUBLISHED:
            return  # 幂等发布由仓储处理
        if version.status is VersionStatus.REVIEW_REJECTED or (
            version.review is not None
            and version.review.result is ReviewResult.REJECTED
        ):
            raise ReviewRejectedError(
                "双人复核已否决该争议值，只能依据新材料形成新版本"
            )
        if version.value is None or version.calculated_at is None:
            raise NotCalculatedError("版本尚未完成价值计算，不能发布")
        if self._today() > version.qualification_expires_on:
            raise QualificationExpiredError(
                "评估资格已过期，禁止发布；请续期后以新版本出具结论",
                qualification_id=version.qualification_id,
                expired_on=version.qualification_expires_on.isoformat(),
            )
        if version.disputed:
            review = version.review
            if review is None or review.result is not ReviewResult.APPROVED:
                raise ReviewRequiredError(
                    "争议值须经两名独立复核人共同通过后方可发布"
                )
            if len(set(review.approvers)) < REVIEW_QUORUM:
                raise ReviewRequiredError("双人复核人数不足")
        if version.status is not VersionStatus.CALCULATED:
            raise ConflictError("当前版本状态不允许发布", status=version.status.value)

    def publish_version(
        self,
        actor: dict[str, Any],
        version_id: str,
        *,
        conclusion_no: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """发布正式结论。资格过期/争议未复核都会被阻止。"""

        self._require_role(actor, ROLE_PUBLISHER)
        version = self.repo.get_version(version_id)

        def do_publish() -> ValuationVersion:
            return self.repo.publish_if_allowed(
                version,
                can_publish=self._check_publish_gate,
                conclusion_no=conclusion_no,
                publisher_id=actor["user_id"],
                published_at=self._clock(),
            )

        stored = (
            self.repo.idempotent(f"publish:{idempotency_key}", do_publish)
            if idempotency_key
            else do_publish()
        )
        return stored.to_dict()

    # ---- 查询与追溯 ---------------------------------------------------

    def get_version(self, version_id: str) -> dict[str, Any]:
        return self.repo.get_version(version_id).to_dict()

    def list_versions(self, parcel_id: str, purpose: str) -> list[dict[str, Any]]:
        self.repo.get_parcel(parcel_id)
        return [v.to_dict() for v in self.repo.list_versions(parcel_id, purpose)]

    def get_published_conclusion(self, parcel_id: str, purpose: str) -> dict[str, Any]:
        self.repo.get_parcel(parcel_id)
        version = self.repo.published_version(parcel_id, purpose)
        if version is None:
            from .errors import NotFoundError

            raise NotFoundError("该估值目的尚无正式结论")
        return version.to_dict()
