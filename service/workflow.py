"""证据仓业务工作流。

关键不变量：
1. 只追加：证据补测与市场参数修订都产生新版本，旧版本与旧快照永不改写；
2. 快照冻结：估值创建时复制当时各材料最新版本与参数版本，事后补传不影响已冻结版本；
3. 幂等：同一幂等键的重复上传/离线补传/创建重试收敛为同一资源；
4. 唯一性：每宗林地每个估值目的至多一份正式结论，新版本发布自动作废旧结论；
5. 准入：评估资格过期阻止发布；争议值须两名不同复核人通过才可发布。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from . import engine
from .models import (
    Appraiser,
    BadRequestError,
    Conclusion,
    ConclusionStatus,
    Dispute,
    DomainError,
    Evidence,
    EvidenceKind,
    EvidenceRef,
    MarketParams,
    NotFoundError,
    Parcel,
    Review,
    ReviewDecision,
    Snapshot,
    Valuation,
    ValuationStatus,
    canonical_hash,
)
from .storage import Store

ALL_SEASONS = "all"
REVIEW_APPROVALS_REQUIRED = 2


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class EvidenceVault:
    """林地价值证据仓应用服务。"""

    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()

    # ------------------------------------------------------------------ 基础档案

    def register_appraiser(
        self,
        appraiser_id: str,
        name: str,
        license_no: str,
        qualification_expires_on: str,
    ) -> Appraiser:
        def op() -> Appraiser:
            if appraiser_id in self.store.appraisers:
                raise BadRequestError("评估师已登记", code="appraiser_exists")
            appraiser = Appraiser(
                id=appraiser_id,
                name=name,
                license_no=license_no,
                qualification_expires_on=qualification_expires_on,
                registered_at=_now(),
            )
            self.store.appraisers[appraiser_id] = appraiser
            return appraiser

        return self.store.tx(op)

    def register_parcel(
        self, parcel_id: str, name: str, location: str, components: list[str]
    ) -> Parcel:
        def op() -> Parcel:
            if parcel_id in self.store.parcels:
                raise BadRequestError("林地已登记", code="parcel_exists")
            parcel = Parcel(
                id=parcel_id,
                name=name,
                location=location,
                components=list(components),
                registered_at=_now(),
            )
            self.store.parcels[parcel_id] = parcel
            return parcel

        return self.store.tx(op)

    # ------------------------------------------------------------------ 证据接收

    def submit_evidence(
        self,
        parcel_id: str,
        material_key: str,
        kind: str,
        content: dict[str, Any],
        season: str,
        observed_on: str,
        recorded_on: str,
        idempotency_key: str,
    ) -> Evidence:
        """接收一份证据材料。

        - 幂等键重复（客户端重试、离线补传重放）：返回首次生成的证据，不新增版本；
        - 同材料同内容但无相同幂等键：判定为重复上传，拒绝；
        - 其余（含补测）：为该 material_key 追加新版本。
        """

        try:
            EvidenceKind(kind)
        except ValueError:
            raise BadRequestError(f"未知证据类型：{kind}") from None
        if not isinstance(content, dict):
            raise BadRequestError("证据内容必须为对象")
        if not idempotency_key:
            raise BadRequestError("缺少幂等键 idempotency_key")

        def op() -> Evidence:
            if parcel_id not in self.store.parcels:
                raise NotFoundError("林地不存在")
            existing_id = self.store.evidence_idem.get(idempotency_key)
            if existing_id is not None:
                return self.store.evidences[existing_id]

            content_hash = canonical_hash(content)
            duplicate = next(
                (
                    e
                    for e in self.store.evidences.values()
                    if e.parcel_id == parcel_id
                    and e.material_key == material_key
                    and e.content_hash == content_hash
                ),
                None,
            )
            if duplicate is not None:
                raise DomainError(
                    "同一材料内容已上传，禁止重复建档",
                    code="duplicate_evidence",
                )

            version_no = (
                max(
                    (
                        e.version_no
                        for e in self.store.evidences.values()
                        if e.parcel_id == parcel_id
                        and e.material_key == material_key
                    ),
                    default=0,
                )
                + 1
            )
            evidence = Evidence(
                id=f"EV-{parcel_id}-{len(self.store.evidences) + 1:04d}",
                parcel_id=parcel_id,
                material_key=material_key,
                version_no=version_no,
                kind=kind,
                content=content,
                content_hash=content_hash,
                season=season,
                observed_on=observed_on,
                recorded_on=recorded_on,
                submitted_at=_now(),
                idempotency_key=idempotency_key,
            )
            self.store.evidences[evidence.id] = evidence
            self.store.evidence_idem[idempotency_key] = evidence.id
            return evidence

        return self.store.tx(op)

    def revise_market_params(
        self,
        parcel_id: str,
        values: dict[str, Any],
        effective_on: str,
        note: str = "",
    ) -> MarketParams:
        """修订市场参数，始终追加新版本。"""

        if not isinstance(values, dict):
            raise BadRequestError("市场参数必须为对象")

        def op() -> MarketParams:
            if parcel_id not in self.store.parcels:
                raise NotFoundError("林地不存在")
            version_no = (
                max(
                    (
                        p.version_no
                        for p in self.store.params.values()
                        if p.parcel_id == parcel_id
                    ),
                    default=0,
                )
                + 1
            )
            params = MarketParams(
                id=f"MP-{parcel_id}-{version_no:03d}",
                parcel_id=parcel_id,
                version_no=version_no,
                values=values,
                effective_on=effective_on,
                note=note,
                created_at=_now(),
            )
            self.store.params[params.id] = params
            return params

        return self.store.tx(op)

    # ------------------------------------------------------------------ 估值版本

    def _freeze_snapshot(
        self, valuation_id: str, parcel_id: str, purpose: str, season: str
    ) -> Snapshot:
        """冻结当前输入：各材料取适用季节内的最新版本，参数取最新版本。"""

        latest_by_material: dict[str, Evidence] = {}
        for evidence in self.store.evidences.values():
            if evidence.parcel_id != parcel_id:
                continue
            if evidence.season not in (season, ALL_SEASONS):
                continue
            current = latest_by_material.get(evidence.material_key)
            if current is None or evidence.version_no > current.version_no:
                latest_by_material[evidence.material_key] = evidence

        refs = [
            EvidenceRef(
                evidence_id=e.id,
                material_key=e.material_key,
                version_no=e.version_no,
                kind=e.kind,
                content_hash=e.content_hash,
                season=e.season,
                observed_on=e.observed_on,
                # 复制内容：后续补测追加新版本也无法改变本快照。
                content=dict(e.content),
            )
            for e in sorted(
                latest_by_material.values(), key=lambda x: (x.material_key, x.version_no)
            )
        ]
        params_sorted = sorted(
            (p for p in self.store.params.values() if p.parcel_id == parcel_id),
            key=lambda p: p.version_no,
        )
        latest_params = params_sorted[-1] if params_sorted else None

        fingerprint = canonical_hash(
            {
                "parcel_id": parcel_id,
                "purpose": purpose,
                "season": season,
                "evidence": [
                    {
                        "material_key": r.material_key,
                        "version_no": r.version_no,
                        "content_hash": r.content_hash,
                    }
                    for r in refs
                ],
                "params_version": latest_params.version_no if latest_params else 0,
                "params_values": latest_params.values if latest_params else {},
            }
        )
        snapshot = Snapshot(
            id=f"SN-{len(self.store.snapshots) + 1:05d}",
            valuation_id=valuation_id,
            parcel_id=parcel_id,
            purpose=purpose,
            season=season,
            evidence_refs=refs,
            params_version=latest_params.version_no if latest_params else 0,
            params_values=dict(latest_params.values) if latest_params else {},
            fingerprint=fingerprint,
            created_at=_now(),
        )
        self.store.snapshots[snapshot.id] = snapshot
        return snapshot

    def create_valuation(
        self,
        parcel_id: str,
        purpose: str,
        appraiser_id: str,
        season: str,
        idempotency_key: str,
        *,
        simulate_failure: bool = False,
    ) -> Valuation:
        """创建估值版本并冻结快照；并发/重试同键收敛为同一版本。"""

        if not purpose or not season or not idempotency_key:
            raise BadRequestError("purpose、season、idempotency_key 均不能为空")
        idem_key = (parcel_id, purpose, idempotency_key)

        with self.store.lock():
            gate = self.store.gate(self.store.create_gates, idem_key)
        with gate:
            def op() -> Valuation:
                existing_id = self.store.valuation_idem.get(idem_key)
                if existing_id is not None:
                    # 离线补传重放或客户端重试：返回同一版本，绝不生成第二份结论。
                    return self.store.valuations[existing_id]
                if parcel_id not in self.store.parcels:
                    raise NotFoundError("林地不存在")
                if appraiser_id not in self.store.appraisers:
                    raise NotFoundError("评估师不存在")

                version_no = sum(
                    1
                    for v in self.store.valuations.values()
                    if v.parcel_id == parcel_id and v.purpose == purpose
                ) + 1
                valuation = Valuation(
                    id=f"VAL-{parcel_id}-{version_no:03d}",
                    parcel_id=parcel_id,
                    purpose=purpose,
                    season=season,
                    version_no=version_no,
                    appraiser_id=appraiser_id,
                    snapshot_id="",
                    fingerprint="",
                    status=ValuationStatus.FROZEN.value,
                    idempotency_key=idempotency_key,
                    created_at=_now(),
                )
                snapshot = self._freeze_snapshot(
                    valuation.id, parcel_id, purpose, season
                )
                valuation.snapshot_id = snapshot.id
                valuation.fingerprint = snapshot.fingerprint
                self.store.valuations[valuation.id] = valuation
                self.store.valuation_idem[idem_key] = valuation.id
                return valuation

            valuation = self.store.tx(op)
            # 计算在锁外执行；中断只更新同一版本的状态。
            return self._run_calculation(valuation.id, simulate_failure=simulate_failure)

    def _run_calculation(
        self, valuation_id: str, *, simulate_failure: bool
    ) -> Valuation:
        def mark_interrupted() -> Valuation:
            def op() -> Valuation:
                valuation = self.store.valuations[valuation_id]
                valuation.status = ValuationStatus.INTERRUPTED.value
                valuation.interruption_count += 1
                valuation.value = None
                return valuation

            return self.store.tx(op)

        def snapshot_of() -> Snapshot:
            return self.store.snapshots[self.store.valuations[valuation_id].snapshot_id]

        try:
            result = engine.calculate(snapshot_of(), simulate_failure=simulate_failure)
        except engine.CalculationInterrupted:
            return mark_interrupted()
        except engine.IncompleteSnapshotError as exc:
            raise BadRequestError(str(exc), code="incomplete_snapshot") from None

        def op() -> Valuation:
            valuation = self.store.valuations[valuation_id]
            if valuation.status == ValuationStatus.PUBLISHED.value:
                return valuation
            valuation.value = result["value"]
            valuation.breakdown = result["breakdown"]
            valuation.status = ValuationStatus.READY.value
            valuation.computed_at = _now()
            return valuation

        return self.store.tx(op)

    def retry_calculation(
        self, valuation_id: str, *, simulate_failure: bool = False
    ) -> Valuation:
        """对冻结或中断版本重算；输入仍是同一份快照，版本号不变。"""

        def check() -> None:
            if valuation_id not in self.store.valuations:
                raise NotFoundError("估值版本不存在")
            status = self.store.valuations[valuation_id].status
            if status not in (
                ValuationStatus.FROZEN.value,
                ValuationStatus.INTERRUPTED.value,
            ):
                raise BadRequestError(
                    f"状态 {status} 不允许重算", code="retry_not_allowed"
                )

        self.store.tx(check)
        return self._run_calculation(valuation_id, simulate_failure=simulate_failure)

    # ------------------------------------------------------------------ 争议与复核

    def raise_dispute(self, valuation_id: str, reason: str) -> Valuation:
        def op() -> Valuation:
            valuation = self._get_valuation(valuation_id)
            if valuation.status != ValuationStatus.READY.value:
                raise BadRequestError(
                    "只有已算出、待发布的估值可提出争议",
                    code="dispute_not_allowed",
                )
            valuation.status = ValuationStatus.IN_REVIEW.value
            valuation.dispute = Dispute(reason=reason, created_at=_now())
            return valuation

        return self.store.tx(op)

    def submit_review(
        self,
        valuation_id: str,
        reviewer_id: str,
        decision: str,
        reason: str = "",
    ) -> Valuation:
        """双人复核：两名不同复核人均 APPROVE 才回到 READY；任一 REJECT 即拒绝。"""

        try:
            verdict = ReviewDecision(decision)
        except ValueError:
            raise BadRequestError(f"未知复核决定：{decision}") from None

        def op() -> Valuation:
            valuation = self._get_valuation(valuation_id)
            if valuation.status != ValuationStatus.IN_REVIEW.value:
                raise BadRequestError(
                    "该估值不处于双人复核中", code="not_in_review"
                )
            if any(r.reviewer_id == reviewer_id for r in valuation.reviews):
                raise BadRequestError(
                    "同一复核人不能重复表决", code="reviewer_duplicate"
                )

            valuation.reviews.append(
                Review(
                    reviewer_id=reviewer_id,
                    decision=verdict.value,
                    reason=reason,
                    reviewed_at=_now(),
                )
            )
            approvals = sum(
                1 for r in valuation.reviews if r.decision == ReviewDecision.APPROVE.value
            )
            if verdict == ReviewDecision.REJECT:
                valuation.status = ValuationStatus.REJECTED.value
            elif approvals >= REVIEW_APPROVALS_REQUIRED:
                valuation.status = ValuationStatus.READY.value
            return valuation

        return self.store.tx(op)

    # ------------------------------------------------------------------ 发布

    def publish(self, valuation_id: str, publisher_id: str, *, today: str | None = None) -> Conclusion:
        """发布正式结论。

        - READY（含已通过双人复核）才可发布；
        - 评估资格在发布当日必须仍有效，过期一律阻断；
        - 发布按估值版本串行；重复发布返回已有结论；
        - 同目的旧正式结论自动标记为 superseded，保留可追溯链。
        """

        today = today or _today()
        with self.store.lock():
            gate = self.store.gate(self.store.publish_gates, valuation_id)
        with gate:
            def op() -> Conclusion:
                valuation = self._get_valuation(valuation_id)
                existing = next(
                    (
                        c
                        for c in self.store.conclusions.values()
                        if c.valuation_id == valuation_id
                    ),
                    None,
                )
                if existing is not None:
                    # 重试/重复发布不制造第二份正式结论。
                    return existing
                if valuation.status == ValuationStatus.IN_REVIEW.value:
                    raise DomainError(
                        "争议值尚未完成双人复核，禁止发布",
                        code="review_pending",
                    )
                if valuation.status == ValuationStatus.REJECTED.value:
                    raise DomainError(
                        "复核已拒绝，禁止发布", code="review_rejected"
                    )
                if valuation.status != ValuationStatus.READY.value:
                    raise DomainError(
                        f"估值状态 {valuation.status}，不可发布",
                        code="not_ready",
                    )
                appraiser = self.store.appraisers[valuation.appraiser_id]
                if appraiser.qualification_expires_on < today:
                    raise DomainError(
                        "评估资格已过期，阻止发布",
                        code="qualification_expired",
                    )
                if valuation.value is None:
                    raise BadRequestError("估值尚未算出价值", code="no_value")

                conclusion = Conclusion(
                    id=f"CON-{len(self.store.conclusions) + 1:05d}",
                    valuation_id=valuation.id,
                    parcel_id=valuation.parcel_id,
                    purpose=valuation.purpose,
                    version_no=valuation.version_no,
                    value=valuation.value,
                    snapshot_id=valuation.snapshot_id,
                    appraiser_id=appraiser.id,
                    appraiser_license_no=appraiser.license_no,
                    publisher_id=publisher_id,
                    published_at=_now(),
                    reviews=list(valuation.reviews),
                )
                scope_key = (valuation.parcel_id, valuation.purpose)
                previous_id = self.store.official_index.get(scope_key)
                if previous_id is not None:
                    previous = self.store.conclusions[previous_id]
                    previous.status = ConclusionStatus.SUPERSEDED.value
                    previous.superseded_by = conclusion.id
                self.store.conclusions[conclusion.id] = conclusion
                self.store.official_index[scope_key] = conclusion.id
                valuation.status = ValuationStatus.PUBLISHED.value
                return conclusion

            return self.store.tx(op)

    # ------------------------------------------------------------------ 查询

    def _get_valuation(self, valuation_id: str) -> Valuation:
        valuation = self.store.valuations.get(valuation_id)
        if valuation is None:
            raise NotFoundError("估值版本不存在")
        return valuation

    def get_valuation(self, valuation_id: str) -> Valuation:
        return self.store.tx(lambda: self._get_valuation(valuation_id))

    def get_snapshot(self, snapshot_id: str) -> Snapshot:
        def op() -> Snapshot:
            snapshot = self.store.snapshots.get(snapshot_id)
            if snapshot is None:
                raise NotFoundError("快照不存在")
            return snapshot

        return self.store.tx(op)

    def official_conclusion(self, parcel_id: str, purpose: str) -> Conclusion:
        def op() -> Conclusion:
            conclusion_id = self.store.official_index.get((parcel_id, purpose))
            if conclusion_id is None:
                raise NotFoundError("当前无正式结论")
            return self.store.conclusions[conclusion_id]

        return self.store.tx(op)

    def valuation_trace(self, valuation_id: str) -> dict[str, Any]:
        """返回一版估值的完整追溯链：价值、快照、证据引用、参数、复核、结论。"""

        def op() -> dict[str, Any]:
            valuation = self._get_valuation(valuation_id)
            snapshot = self.store.snapshots[valuation.snapshot_id]
            conclusion = next(
                (
                    c
                    for c in self.store.conclusions.values()
                    if c.valuation_id == valuation_id
                ),
                None,
            )
            return {
                "valuation": valuation,
                "snapshot": snapshot,
                "conclusion": conclusion,
            }

        return self.store.tx(op)

    def ledger(self, parcel_id: str, purpose: str) -> list[Valuation]:
        """某宗林地按目的的全部估值版本，版本号升序。"""

        def op() -> list[Valuation]:
            return sorted(
                (
                    v
                    for v in self.store.valuations.values()
                    if v.parcel_id == parcel_id and v.purpose == purpose
                ),
                key=lambda v: v.version_no,
            )

        return self.store.tx(op)
