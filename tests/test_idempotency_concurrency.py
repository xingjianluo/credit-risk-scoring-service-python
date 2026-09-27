"""并发重复提交：只产生一份最终结论和一条风险记录，冲突被明确识别。"""
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.domain import FeatureSnapshot
from app.errors import ErrorCode, ServiceError

from conftest import GOOD_FEATURES, log_decision, log_error

logger = logging.getLogger("credit_risk.test")


def test_concurrent_duplicate_submit_single_record(service):
    snapshot = FeatureSnapshot("APP-CONC-1", dict(GOOD_FEATURES))
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(
            lambda _: service.submit(snapshot, "v1"), range(64)))
    for r in results:
        log_decision(r)
    # 64 次并发提交 => 结论完全一致，且只有一条风险记录。
    assert len({(r.score, r.decision, r.reasons) for r in results}) == 1
    assert service._store.count() == 1


def test_retry_is_idempotent_no_double_count(service):
    snapshot = FeatureSnapshot("APP-IDEM-1", dict(GOOD_FEATURES))
    first = service.submit(snapshot, "v1")
    for _ in range(10):
        again = service.submit(snapshot, "v1")
        log_decision(again)
        assert (again.score, again.decision) == (first.score, first.decision)
    assert service._store.count() == 1


def test_conflicting_model_version_is_explicit_conflict(service):
    snapshot = FeatureSnapshot("APP-CONFLICT-1", dict(GOOD_FEATURES))
    d1 = service.submit(snapshot, "v1")
    log_decision(d1)
    with pytest.raises(ServiceError) as exc_info:
        service.submit(snapshot, "v2")  # 同一申请换版本重提 => 冲突
    log_error(exc_info.value)
    assert exc_info.value.code == ErrorCode.APPLICATION_CONFLICT
    # 冲突不得造成状态漂移：仍只有一条记录，结论不变。
    assert service._store.count() == 1
    record = service._store.get("APP-CONFLICT-1")
    assert record.model_version == "v1"
    assert record.decision == d1.decision


def test_concurrent_conflicting_versions_single_winner(service):
    snapshot = FeatureSnapshot("APP-CONC-CONFLICT-1", dict(GOOD_FEATURES))
    versions = ["v1", "v2"] * 32
    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(
            lambda v: _submit_catching(service, snapshot, v), versions))
    decisions = [o for o in outcomes if not isinstance(o, ServiceError)]
    conflicts = [o for o in outcomes if isinstance(o, ServiceError)]
    for d in decisions:
        log_decision(d)
    for e in conflicts:
        log_error(e)
    # 只有一个版本胜出；其余全部收到明确的冲突错误，无矛盾结论。
    assert len({d.model_version for d in decisions}) == 1
    assert all(e.code == ErrorCode.APPLICATION_CONFLICT for e in conflicts)
    assert service._store.count() == 1


def _submit_catching(service, snapshot, version):
    try:
        return service.submit(snapshot, version)
    except ServiceError as exc:
        return exc


def test_failed_validation_leaves_no_record(service):
    bad = dict(GOOD_FEATURES)
    del bad["age"]
    with pytest.raises(ServiceError) as exc_info:
        service.submit(FeatureSnapshot("APP-BAD-1", bad), "v1")
    log_error(exc_info.value)
    # 校验失败不落库、不计数，修正后重试可成功。
    assert service._store.get("APP-BAD-1") is None
    fixed = service.submit(FeatureSnapshot("APP-BAD-1", dict(GOOD_FEATURES)), "v1")
    log_decision(fixed)
    assert service._store.count() == 1
