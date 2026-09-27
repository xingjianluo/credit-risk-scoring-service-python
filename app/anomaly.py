"""交易异常检测：窗口内去重，告警与评分结论状态联动。"""
from __future__ import annotations

import math
import threading

from .domain import Alert, AlertStatus, Decision, Transaction
from .errors import ErrorCode, ServiceError

RULE_DUPLICATE = "DUPLICATE_TRANSACTION"
RULE_BURST = "BURST_TRANSACTIONS"
RULE_DEVIATION = "BEHAVIOR_DEVIATION"


class AnomalyDetector:
    """规则：
    - DUPLICATE_TRANSACTION  同一 transaction_id 在窗口内重复出现
    - BURST_TRANSACTIONS     同一客户在窗口内交易数 >= burst_count
    - BEHAVIOR_DEVIATION     单笔金额 >= 历史均值 * deviation_ratio

    去重：同一 (customer, rule, 窗口) 只告警一次；
    告警与评分结论状态联动：link_decision 后告警置为 LINKED。
    """

    def __init__(self, window_seconds: float = 300.0, burst_count: int = 3,
                 deviation_ratio: float = 5.0) -> None:
        self._window_seconds = float(window_seconds)
        self._burst_count = int(burst_count)
        self._deviation_ratio = float(deviation_ratio)
        self._lock = threading.Lock()
        self._seen_txn: dict[str, int] = {}            # txn_id -> window bucket
        self._customer_txns: dict[str, dict[int, list[Transaction]]] = {}
        self._history: dict[str, list[float]] = {}     # customer -> 历史金额
        self._alerted: set[tuple[str, str, int]] = set()
        self._alert_seq = 0
        self._alerts: list[Alert] = []

    def _bucket(self, timestamp: float) -> int:
        return int(timestamp // self._window_seconds)

    def _validate(self, txn: Transaction) -> None:
        # 上游数据异常归一化为 UPSTREAM_FAILURE，不直接透出底层异常。
        if not txn.transaction_id or not txn.customer_id:
            raise ServiceError(ErrorCode.UPSTREAM_FAILURE,
                               "交易缺少 transaction_id 或 customer_id")
        if not isinstance(txn.amount, (int, float)) or isinstance(txn.amount, bool) \
                or math.isnan(txn.amount) or math.isinf(txn.amount) or txn.amount < 0:
            raise ServiceError(ErrorCode.UPSTREAM_FAILURE,
                               f"交易金额非法: {txn.amount!r}")
        if not isinstance(txn.timestamp, (int, float)) \
                or math.isnan(txn.timestamp) or math.isinf(txn.timestamp):
            raise ServiceError(ErrorCode.UPSTREAM_FAILURE,
                               f"交易时间戳非法: {txn.timestamp!r}")

    def ingest(self, txn: Transaction) -> list[Alert]:
        """摄入交易，返回本次新产生的告警（窗口内已告警的规则不重复告警）。"""
        self._validate(txn)
        try:
            with self._lock:
                bucket = self._bucket(txn.timestamp)
                new_alerts: list[Alert] = []

                # 1) 重复交易
                if txn.transaction_id in self._seen_txn:
                    alert = self._emit(txn.customer_id, RULE_DUPLICATE,
                                       (txn.transaction_id,), bucket)
                    if alert:
                        new_alerts.append(alert)
                else:
                    self._seen_txn[txn.transaction_id] = bucket

                # 2) 突发交易
                window_txns = self._customer_txns.setdefault(
                    txn.customer_id, {}).setdefault(bucket, [])
                window_txns.append(txn)
                if len(window_txns) >= self._burst_count:
                    alert = self._emit(
                        txn.customer_id, RULE_BURST,
                        tuple(t.transaction_id for t in window_txns), bucket)
                    if alert:
                        new_alerts.append(alert)

                # 3) 行为偏离（相对历史均值）
                history = self._history.setdefault(txn.customer_id, [])
                if history:
                    mean = sum(history) / len(history)
                    if mean > 0 and txn.amount >= mean * self._deviation_ratio:
                        alert = self._emit(txn.customer_id, RULE_DEVIATION,
                                           (txn.transaction_id,), bucket)
                        if alert:
                            new_alerts.append(alert)
                history.append(txn.amount)
                return new_alerts
        except ServiceError:
            raise
        except Exception as exc:  # 计算/内部异常归一化
            raise ServiceError(
                ErrorCode.COMPUTE_FAILURE,
                f"异常检测失败: transaction={txn.transaction_id}",
                cause=exc,
            ) from exc

    def _emit(self, customer_id: str, rule: str,
              txn_ids: tuple[str, ...], bucket: int) -> Alert | None:
        key = (customer_id, rule, bucket)
        if key in self._alerted:
            return None  # 同窗口同规则不重复告警
        self._alerted.add(key)
        self._alert_seq += 1
        alert = Alert(
            alert_id=f"ALERT-{self._alert_seq:06d}",
            customer_id=customer_id,
            rule=rule,
            transaction_ids=tuple(txn_ids),
        )
        self._alerts.append(alert)
        return alert

    def link_decision(self, customer_id: str, decision: Decision) -> int:
        """告警与评分结论联动：REJECT/REVIEW 结论下，该客户未联动告警置为 LINKED。

        返回本次联动的告警数，保证告警状态与评分结论状态一致。
        """
        if decision not in (Decision.REJECT, Decision.REVIEW):
            return 0
        linked = 0
        with self._lock:
            for alert in self._alerts:
                if alert.customer_id == customer_id \
                        and alert.status == AlertStatus.OPEN:
                    alert.status = AlertStatus.LINKED
                    linked += 1
        return linked

    def alerts(self) -> list[Alert]:
        with self._lock:
            return list(self._alerts)
