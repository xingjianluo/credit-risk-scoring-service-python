"""线程安全的内存存储：幂等键、唯一风险记录、告警去重、原子联动认领。

一致性约定：
- application_id 是幂等键：create_application_with_alerts 在锁内检查并写入，
  重复/并发提交永远返回同一份最终记录，不产生第二条风险记录；
- 告警按 (customer_id, kind, window_start) 去重，同窗口同类告警只保留一条；
- 申请落库与告警认领（OPEN -> LINKED_TO_DECISION）在同一把锁内原子完成：
  并发申请不会漏关联、重复关联或把告警挂到别的申请上。
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from app.models import Alert, AlertStatus, ApplicationRecord


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

    def get_alert(self, alert_id: str) -> Alert | None:
        with self._lock:
            return self._alerts.get(alert_id)

    def create_application_with_alerts(
        self,
        application_id: str,
        customer_id: str,
        features: dict,
        build_record: Callable[[list[Alert]], ApplicationRecord],
    ) -> tuple[ApplicationRecord, bool]:
        """原子地「创建申请 + 认领该客户当前所有未关联告警」。

        全程持锁：
        1. 申请已存在 -> 直接返回既有记录与 False（幂等/竞争失败）；
        2. 在锁内快照该客户的 OPEN 告警，回调据此构建记录（纯计算，不回写存储）；
        3. 风险记录号仅在真正写入时分配；告警翻转为 LINKED_TO_DECISION 并回填
           linked_application_id，申请记录保存冻结快照与 alert_ids；
           任一步骤与写入都在锁内，杜绝漏关联/重复关联/错挂。
        """
        with self._lock:
            existing = self._applications.get(application_id)
            if existing is not None:
                return existing, False
            open_alerts = [
                a
                for a in self._alerts.values()
                if a.customer_id == customer_id and a.status is AlertStatus.OPEN
            ]
            record = build_record(open_alerts)
            self._risk_record_counter += 1
            record.risk_record_id = f"RR-{self._risk_record_counter:06d}"
            claimed_ids: list[str] = []
            for alert in open_alerts:
                alert.status = AlertStatus.LINKED_TO_DECISION
                alert.linked_application_id = application_id
                claimed_ids.append(alert.alert_id)
            record.alert_ids = claimed_ids
            self._applications[application_id] = record
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

    def list_alerts(self, customer_id: str) -> list[Alert]:
        with self._lock:
            return [a for a in self._alerts.values() if a.customer_id == customer_id]

    def list_application_alerts(self, application_id: str) -> list[Alert]:
        """正向追溯：申请 -> 它关联了哪些告警（读冻结 id 列表回查当前实体）。"""
        with self._lock:
            record = self._applications.get(application_id)
            if record is None:
                return []
            return [self._alerts[i] for i in record.alert_ids if i in self._alerts]

    def count_applications(self) -> int:
        with self._lock:
            return len(self._applications)

    def count_risk_records(self) -> int:
        with self._lock:
            return self._risk_record_counter
