"""线程安全的内存存储：幂等键、唯一风险记录、告警去重。

一致性约定：
- application_id 是幂等键：save_application_if_absent 在锁内检查并写入，
  重复/并发提交永远返回同一份最终记录，不产生第二条风险记录；
- 告警按 (customer_id, kind, window_start) 去重，同窗口同类告警只保留一条。
"""
from __future__ import annotations

import threading

from app.models import Alert, ApplicationRecord


class Repository:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._applications: dict[str, ApplicationRecord] = {}
        self._alerts: dict[str, Alert] = {}
        self._alert_dedup_keys: dict[tuple, str] = {}
        self._risk_record_counter = 0

    def get_application(self, application_id: str) -> ApplicationRecord | None:
        with self._lock:
            return self._applications.get(application_id)

    def save_application_if_absent(self, record: ApplicationRecord) -> tuple[ApplicationRecord, bool]:
        """原子写入：已存在则返回既有记录与 False。

        风险记录号在锁内、且仅在真正写入时分配，竞争失败的线程不会消耗序号，
        保证同一申请只有一条风险记录、无重复计数。
        """
        with self._lock:
            existing = self._applications.get(record.application_id)
            if existing is not None:
                return existing, False
            self._risk_record_counter += 1
            record.risk_record_id = f"RR-{self._risk_record_counter:06d}"
            self._applications[record.application_id] = record
            return record, True

    def save_alert_if_absent(self, dedup_key: tuple, alert: Alert) -> tuple[Alert, bool]:
        """按去重键原子写入告警，同一时间窗口内同类告警不重复。"""
        with self._lock:
            existing_id = self._alert_dedup_keys.get(dedup_key)
            if existing_id is not None:
                return self._alerts[existing_id], False
            self._alert_dedup_keys[dedup_key] = alert.alert_id
            self._alerts[alert.alert_id] = alert
            return alert, True

    def link_alert(self, alert_id: str, application_id: str) -> None:
        from app.models import AlertStatus

        with self._lock:
            alert = self._alerts[alert_id]
            alert.status = AlertStatus.LINKED_TO_DECISION
            record = self._applications.get(application_id)
            if record is not None and alert_id not in record.alert_ids:
                record.alert_ids.append(alert_id)

    def list_alerts(self, customer_id: str) -> list[Alert]:
        with self._lock:
            return [a for a in self._alerts.values() if a.customer_id == customer_id]

    def count_applications(self) -> int:
        with self._lock:
            return len(self._applications)

    def count_risk_records(self) -> int:
        with self._lock:
            return self._risk_record_counter
