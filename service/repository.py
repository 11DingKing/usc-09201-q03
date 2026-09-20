"""线程安全的内存仓储。

承担三类一致性保证：

1. **证据去重**：同一宗地内，业务指纹相同的材料（重复上传、离线补传）
   只登记一次，第二次返回原记录并标记 ``deduplicated=True``。
2. **正式结论唯一**：同一估值目的至多一个 ``PUBLISHED`` 版本；结论编号
   全局唯一。并发发布会被行锁串行化，第二个请求得到冲突错误而不是
   产出第二份正式结论。
3. **幂等请求**：写入类接口支持 ``Idempotency-Key``，同一键的重试
   （离线补传、网络抖动重发）返回首次结果，绝不重复落库。

生产环境可将本类替换为数据库实现，接口保持不变。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Callable, TypeVar

from .errors import ConflictError, NotFoundError
from .models import Evidence, MarketParameters, ValuationVersion, VersionStatus

T = TypeVar("T")


class Repository:
    """聚合所有聚合根的内存仓储（单库锁 + 每版本细锁）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._parcels: dict[str, dict[str, object]] = {}
        self._evidence: dict[str, Evidence] = {}
        self._fingerprint_index: dict[tuple[str, str], str] = {}
        self._params: dict[str, MarketParameters] = {}
        self._versions: dict[str, ValuationVersion] = {}
        self._versions_by_purpose: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._version_locks: dict[str, threading.Lock] = {}
        self._idempotent: dict[str, object] = {}
        self._idem_locks: dict[str, threading.Lock] = {}
        self._conclusion_nos: set[str] = set()

    # ---- 通用幂等支持 -------------------------------------------------

    def idempotent(self, key: str | None, factory: Callable[[], T]) -> T:
        """按幂等键执行 ``factory``；键命中时直接返回首次结果。

        同一键的并发请求在键锁上串行：``factory``（含落库副作用）对
        每个键至多执行一次，因此离线补传/网络重试不会写入两份数据。
        无键时每次都执行（业务唯一索引仍兜底）。
        """

        if key is None:
            return factory()
        with self._lock:
            slot = self._idem_locks.setdefault(key, threading.Lock())
        with slot:
            with self._lock:
                if key in self._idempotent:
                    return self._idempotent[key]  # type: ignore[return-value]
            result = factory()
            with self._lock:
                if key in self._idempotent:
                    return self._idempotent[key]  # type: ignore[return-value]
                self._idempotent[key] = result
                return result

    # ---- 宗地 ---------------------------------------------------------

    def create_parcel(self, parcel_id: str, name: str, area_mu: float) -> dict[str, object]:
        with self._lock:
            if parcel_id in self._parcels:
                raise ConflictError("宗地已存在", parcel_id=parcel_id)
            parcel = {"parcel_id": parcel_id, "name": name, "area_mu": area_mu}
            self._parcels[parcel_id] = parcel
            return dict(parcel)

    def get_parcel(self, parcel_id: str) -> dict[str, object]:
        with self._lock:
            try:
                return dict(self._parcels[parcel_id])
            except KeyError:
                raise NotFoundError("宗地不存在", parcel_id=parcel_id) from None

    # ---- 证据 ---------------------------------------------------------

    def add_evidence(self, evidence: Evidence) -> tuple[Evidence, bool]:
        """登记证据；命中指纹索引时返回既有记录。

        :returns: (证据记录, 是否为新建)
        """

        index_key = (evidence.parcel_id, evidence.fingerprint)
        with self._lock:
            existing_id = self._fingerprint_index.get(index_key)
            if existing_id is not None:
                return self._evidence[existing_id], False
            self._evidence[evidence.evidence_id] = evidence
            self._fingerprint_index[index_key] = evidence.evidence_id
            return evidence, True

    def get_evidence(self, evidence_id: str) -> Evidence:
        with self._lock:
            try:
                return self._evidence[evidence_id]
            except KeyError:
                raise NotFoundError("证据不存在", evidence_id=evidence_id) from None

    def list_evidence(self, parcel_id: str) -> list[Evidence]:
        with self._lock:
            return [e for e in self._evidence.values() if e.parcel_id == parcel_id]

    def require_evidence(self, parcel_id: str, evidence_ids: list[str]) -> list[Evidence]:
        """解析版本快照引用的证据，校验归属与去重。"""

        if not evidence_ids:
            from .errors import ValidationError

            raise ValidationError("估值版本至少引用一条证据")
        if len(set(evidence_ids)) != len(evidence_ids):
            from .errors import ValidationError

            raise ValidationError("快照中证据不可重复引用")
        with self._lock:
            result: list[Evidence] = []
            for evidence_id in evidence_ids:
                evidence = self._evidence.get(evidence_id)
                if evidence is None:
                    raise NotFoundError("证据不存在", evidence_id=evidence_id)
                if evidence.parcel_id != parcel_id:
                    from .errors import ValidationError

                    raise ValidationError(
                        "证据不属于该宗地",
                        evidence_id=evidence_id,
                        parcel_id=parcel_id,
                    )
                result.append(evidence)
            return result

    # ---- 市场参数 -----------------------------------------------------

    def add_params(self, params: MarketParameters) -> MarketParameters:
        with self._lock:
            if params.params_id in self._params:
                raise ConflictError("市场参数版本已存在", params_id=params.params_id)
            self._params[params.params_id] = params
            return params

    def get_params(self, params_id: str) -> MarketParameters:
        with self._lock:
            try:
                return self._params[params_id]
            except KeyError:
                raise NotFoundError("市场参数版本不存在", params_id=params_id) from None

    # ---- 估值版本 -----------------------------------------------------

    def version_lock(self, version_id: str) -> threading.Lock:
        with self._lock:
            try:
                return self._version_locks[version_id]
            except KeyError:
                lock = threading.Lock()
                self._version_locks[version_id] = lock
                return lock

    def next_version_no(self, parcel_id: str, purpose: str) -> int:
        """分配同一目的下的版本序号；调用须保证最终落库。"""

        with self._lock:
            return len(self._versions_by_purpose[(parcel_id, purpose)]) + 1

    def add_version(self, version: ValuationVersion) -> ValuationVersion:
        with self._lock:
            if version.version_id in self._versions:
                raise ConflictError("估值版本已存在", version_id=version.version_id)
            key = (version.parcel_id, version.purpose)
            stored = self._versions_by_purpose[key]
            version.version_no = len(stored) + 1
            self._versions[version.version_id] = version
            stored.append(version.version_id)
            self._version_locks[version.version_id] = threading.Lock()
            return version

    def get_version(self, version_id: str) -> ValuationVersion:
        with self._lock:
            try:
                return self._versions[version_id]
            except KeyError:
                raise NotFoundError("估值版本不存在", version_id=version_id) from None

    def list_versions(self, parcel_id: str, purpose: str) -> list[ValuationVersion]:
        with self._lock:
            ids = self._versions_by_purpose.get((parcel_id, purpose), [])
            return [self._versions[vid] for vid in ids]

    def latest_version(self, parcel_id: str, purpose: str) -> ValuationVersion | None:
        versions = self.list_versions(parcel_id, purpose)
        return versions[-1] if versions else None

    def published_version(self, parcel_id: str, purpose: str) -> ValuationVersion | None:
        for version in self.list_versions(parcel_id, purpose):
            if version.status is VersionStatus.PUBLISHED:
                return version
        return None

    # ---- 发布结论编号 -------------------------------------------------

    def reserve_conclusion_no(self, conclusion_no: str) -> None:
        """占用正式结论编号；已被占用时冲突。"""

        with self._lock:
            if conclusion_no in self._conclusion_nos:
                raise ConflictError("正式结论编号已存在", conclusion_no=conclusion_no)
            self._conclusion_nos.add(conclusion_no)

    def publish_if_allowed(
        self,
        version: ValuationVersion,
        *,
        can_publish: Callable[[ValuationVersion], None],
        conclusion_no: str,
        publisher_id: str,
        published_at: object,
    ) -> ValuationVersion:
        """在版本细锁 + 库锁内原子地完成"检查闸门 → 占位结论号 → 置发布态"。

        并发发布同一目的的两个版本时，只有一个能通过"目的唯一"检查；
        失败方保持原状态，不会形成第二份正式结论。
        """

        lock = self.version_lock(version.version_id)
        with lock, self._lock:
            current = self._versions[version.version_id]
            can_publish(current)  # 资格/计算/复核/状态闸门，抛 PublishBlockedError
            existing = self.published_version(current.parcel_id, current.purpose)
            if existing is not None and existing.version_id != current.version_id:
                raise ConflictError(
                    "该估值目的已有正式结论；修订须先经新版本流程",
                    existing_conclusion_no=existing.conclusion_no,
                )
            if current.conclusion_no:
                # 同一版本重试：结论号已占位即视为成功（幂等发布）
                return current
            if conclusion_no in self._conclusion_nos:
                raise ConflictError("正式结论编号已存在", conclusion_no=conclusion_no)
            self._conclusion_nos.add(conclusion_no)
            current.status = VersionStatus.PUBLISHED
            current.conclusion_no = conclusion_no
            current.published_by = publisher_id
            current.published_at = published_at  # type: ignore[assignment]
            return current
