"""线程安全的内存仓储与幂等记录。

同一份材料重复上传、离线补传、客户端重试都依赖幂等键与内容指纹去重；
估值创建以 (parcel, purpose, idempotency_key) 收敛为同一版本，
从而保证同一批输入不会产生两份正式结论。
"""

from __future__ import annotations

import threading
from dataclasses import asdict
from typing import Any, Callable, TypeVar

from .models import (
    Appraiser,
    Conclusion,
    Evidence,
    MarketParams,
    Parcel,
    Snapshot,
    Valuation,
)

T = TypeVar("T")


class Store:
    """按实体分表的内存仓储，所有写操作在同一把锁内串行化。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.appraisers: dict[str, Appraiser] = {}
        self.parcels: dict[str, Parcel] = {}
        self.evidences: dict[str, Evidence] = {}
        self.params: dict[str, MarketParams] = {}
        self.snapshots: dict[str, Snapshot] = {}
        self.valuations: dict[str, Valuation] = {}
        self.conclusions: dict[str, Conclusion] = {}
        # 幂等键 -> 已生成的资源 id
        self.evidence_idem: dict[str, str] = {}
        self.valuation_idem: dict[tuple[str, str, str], str] = {}
        # 全局唯一的正式结论：(parcel_id, purpose) -> conclusion_id
        self.official_index: dict[tuple[str, str], str] = {}
        # 估值创建串行点：并发同键补传/重试在此排队
        self.create_gates: dict[tuple[str, str, str], threading.Lock] = {}
        self.publish_gates: dict[str, threading.Lock] = {}

    def lock(self) -> threading.RLock:
        return self._lock

    def gate(self, table: dict[Any, threading.Lock], key: Any) -> threading.Lock:
        """按需创建串行锁（调用方需在主锁内调用）。"""

        gate = table.get(key)
        if gate is None:
            gate = threading.Lock()
            table[key] = gate
        return gate

    def tx(self, fn: Callable[[], T]) -> T:
        with self._lock:
            return fn()

    def reset(self) -> None:
        with self._lock:
            self.__init__()  # type: ignore[misc]


def to_dict(obj: Any) -> Any:
    """dataclass（含嵌套与枚举）转 JSON 兼容字典。"""

    return asdict(obj)
