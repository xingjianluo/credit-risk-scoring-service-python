"""线程安全的内存存储：幂等键、唯一风险记录、告警去重与原子关联。

一致性约定：
- application_id 是幂等键：create_application 在同一把锁内完成
  “查重 -> 抢占该客户全部 OPEN 告警 -> 构造结论 -> 分配风险记录号 -> 落库”，
  重复/并发提交永远只有一份最终结论，告警不会漏关联、重复关联或挂到别的申请；
- 风险记录号仅在真正写入时分配，竞争失败不消耗序号、不重复计数；
- 告警按 (customer_id, kind, window_start) 去重，同窗口同类告警只保留一条；
- 告警与申请双向可追溯：Alert.linked_application_id 与 ApplicationRecord.alert_ids 同时维护。
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

    def create_application(
        self,
        application_id: str,
        customer_id: str,
        builder: Callable[[tuple[Alert, ...]], ApplicationRecord],
        claim_alerts: bool = True,
    ) -> tuple[ApplicationRecord, bool]:
        """原子地创建申请，并（可选）关联该客户当前所有未关联（OPEN）告警。

        在同一把锁内：
        1. application_id 已存在 -> 直接返回 (既有记录, False)，不动任何告警；
        2. claim_alerts=False（特征校验失败等非正常决策）-> 不抢占任何告警；
           否则抓取该客户全部 OPEN 告警并置为 LINKED_TO_DECISION（预占）；
        3. 调 builder(claimed_alerts) 在锁内构造最终记录（收紧结论依赖这批告警快照）；
        4. 分配风险记录号并落库，回写双向关联。
        builder 抛错时预占全部回滚为 OPEN，不产生半成品关联。
        """
        with self._lock:
            existing = self._applications.get(application_id)
            if existing is not None:
                return existing, False

            claimed: list[Alert] = []
            if claim_alerts:
                claimed = sorted(
                    (a for a in self._alerts.values()
                     if a.customer_id == customer_id and a.status is AlertStatus.OPEN),
                    key=lambda a: a.alert_id,
                )
            for alert in claimed:
                alert.status = AlertStatus.LINKED_TO_DECISION
                alert.linked_application_id = application_id
            try:
                record = builder(tuple(claimed))
            except BaseException:
                # 构造失败：回滚告警预占，保证状态机仍在 OPEN -> LINKED 的合法轨道上
                for alert in claimed:
                    alert.status = AlertStatus.OPEN
                    alert.linked_application_id = None
                raise

            self._risk_record_counter += 1
            record.risk_record_id = f"RR-{self._risk_record_counter:06d}"
            record.alert_ids = [a.alert_id for a in claimed]
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

    def get_alert(self, alert_id: str) -> Alert | None:
        with self._lock:
            return self._alerts.get(alert_id)

    def list_alerts(self, customer_id: str) -> list[Alert]:
        with self._lock:
            return [a for a in self._alerts.values() if a.customer_id == customer_id]

    def count_applications(self) -> int:
        with self._lock:
            return len(self._applications)

    def count_risk_records(self) -> int:
        with self._lock:
            return self._risk_record_counter
