"""存储层：幂等键唯一约束 + 并发冲突识别（内存实现，接口可替换）。"""
from __future__ import annotations

import threading

from .domain import RiskRecord
from .errors import ErrorCode, ServiceError


class RiskRecordStore:
    """按 application_id 唯一约束存储风险记录。

    insert_if_absent 在单把锁内完成"检查 + 写入"，
    并发重复提交时只有一个调用者能插入成功。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, RiskRecord] = {}

    def insert_if_absent(self, record: RiskRecord) -> tuple[bool, RiskRecord]:
        """原子插入；已存在时返回 (False, 已存在记录)，不抛异常、不覆盖。"""
        try:
            with self._lock:
                existing = self._records.get(record.application_id)
                if existing is not None:
                    return False, existing
                self._records[record.application_id] = record
                return True, record
        except ServiceError:
            raise
        except Exception as exc:  # 底层异常归一化，不直接透出
            raise ServiceError(
                ErrorCode.PERSISTENCE_FAILURE,
                f"写入风险记录失败: application={record.application_id}",
                cause=exc,
            ) from exc

    def get(self, application_id: str) -> RiskRecord | None:
        with self._lock:
            return self._records.get(application_id)

    def count(self) -> int:
        with self._lock:
            return len(self._records)
