"""异常交易检测：窗口内去重告警，告警与评分结论状态联动，错误归一化。"""
import logging

import pytest

from app.anomaly import (AnomalyDetector, RULE_BURST, RULE_DEVIATION,
                         RULE_DUPLICATE)
from app.domain import AlertStatus, Decision, Transaction
from app.errors import ErrorCode, ServiceError

from conftest import log_error

logger = logging.getLogger("credit_risk.test")


def _txn(tid, cid="C1", amount=100.0, ts=1000.0):
    return Transaction(transaction_id=tid, customer_id=cid,
                       amount=amount, timestamp=ts)


def _log_alerts(alerts):
    for a in alerts:
        logger.info("alert id=%s customer=%s rule=%s txns=%s status=%s",
                    a.alert_id, a.customer_id, a.rule,
                    a.transaction_ids, a.status)


def test_duplicate_transaction_alerts_once_per_window():
    detector = AnomalyDetector(window_seconds=300, burst_count=10)
    assert detector.ingest(_txn("T1")) == []
    first = detector.ingest(_txn("T1"))          # 窗口内重复 => 告警
    second = detector.ingest(_txn("T1"))         # 同窗口再重复 => 不重复告警
    _log_alerts(first)
    assert len(first) == 1 and first[0].rule == RULE_DUPLICATE
    assert second == []
    # 新窗口再次出现 => 重新告警（窗口语义）
    later = detector.ingest(_txn("T1", ts=1000.0 + 301))
    _log_alerts(later)
    assert len(later) == 1


def test_burst_alerts_once_per_window():
    detector = AnomalyDetector(window_seconds=300, burst_count=3)
    detector.ingest(_txn("B1"))
    detector.ingest(_txn("B2"))
    burst = detector.ingest(_txn("B3"))          # 达到突发阈值
    extra = detector.ingest(_txn("B4"))          # 同窗口继续 => 不重复告警
    _log_alerts(burst)
    assert [a.rule for a in burst] == [RULE_BURST]
    assert extra == []


def test_deviation_from_history():
    detector = AnomalyDetector(window_seconds=300, deviation_ratio=5.0)
    for i in range(5):
        detector.ingest(_txn(f"H{i}", amount=100.0, ts=1000.0 + i))
    alerts = detector.ingest(_txn("BIG", amount=2000.0, ts=1100.0))
    _log_alerts(alerts)
    assert any(a.rule == RULE_DEVIATION for a in alerts)


def test_alert_status_links_with_scoring_decision():
    detector = AnomalyDetector(window_seconds=300)
    detector.ingest(_txn("L1"))
    detector.ingest(_txn("L1"))                  # 产生 OPEN 告警
    assert detector.alerts()[0].status == AlertStatus.OPEN
    linked = detector.link_decision("C1", Decision.REJECT)
    logger.info("linked alerts=%d decision=%s", linked, Decision.REJECT)
    assert linked == 1
    assert detector.alerts()[0].status == AlertStatus.LINKED
    # APPROVE 结论不联动告警
    assert detector.link_decision("C1", Decision.APPROVE) == 0


def test_upstream_bad_data_is_normalized():
    detector = AnomalyDetector()
    with pytest.raises(ServiceError) as exc_info:
        detector.ingest(_txn("X1", amount=float("nan")))
    log_error(exc_info.value)
    assert exc_info.value.code == ErrorCode.UPSTREAM_FAILURE
    with pytest.raises(ServiceError) as exc_info2:
        detector.ingest(_txn("", amount=1.0))
    log_error(exc_info2.value)
    assert exc_info2.value.code == ErrorCode.UPSTREAM_FAILURE
