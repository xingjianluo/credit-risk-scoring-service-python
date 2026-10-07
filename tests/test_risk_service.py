"""覆盖：并发重复提交、模型版本重放、特征异常、告警去重、错误归一化。"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import ErrorKind, RiskServiceError
from app.models import Decision, Transaction
from app.repository import Repository
from app.scoring import score_application
from app.service import RiskService

GOOD = {
    "age": 40,
    "annual_income": 500000,
    "debt_ratio": 0.1,
    "credit_history_months": 200,
    "delinquency_count": 0,
}


@pytest.fixture()
def service() -> RiskService:
    return RiskService(Repository())


# ---- 并发重复提交 ----

def test_concurrent_duplicate_submit_single_record(service, caplog):
    caplog.set_level(logging.INFO, logger="risk.service")
    with ThreadPoolExecutor(max_workers=16) as pool:
        records = list(
            pool.map(lambda _: service.submit_application("app-1", "c1", GOOD), range(64))
        )
    first = records[0]
    assert all(r is first for r in records), "并发提交必须返回同一份最终结论"
    assert service._repo.count_applications() == 1
    assert service._repo.count_risk_records() == 1, "重试不得造成重复计数/额度占用"
    for rec in caplog.records:
        if "application=" in rec.getMessage():
            assert "model=" in rec.getMessage() and "decision=" in rec.getMessage()


def test_concurrent_conflicting_submit_raises(service):
    def submit(i):
        try:
            return service.submit_application("app-conflict", "c1", dict(GOOD, age=40 + i))
        except RiskServiceError as e:
            return e

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(16)))
    # 恰好一个成功落库，其余必须被识别为冲突，而不是返回矛盾结论
    successes = [r for r in results if not isinstance(r, RiskServiceError)]
    conflicts = [r for r in results if isinstance(r, RiskServiceError)]
    assert len(successes) == 1
    assert all(e.kind is ErrorKind.CONCURRENT_CONFLICT for e in conflicts)
    assert service._repo.count_applications() == 1
    assert service._repo.count_risk_records() == 1


def test_conflicting_resubmit_detected(service):
    service.submit_application("app-2", "c1", GOOD)
    with pytest.raises(RiskServiceError) as ei:
        service.submit_application("app-2", "c1", dict(GOOD, age=41))
    assert ei.value.kind is ErrorKind.CONCURRENT_CONFLICT


# ---- 模型版本重放 ----

def test_model_version_replay(service, caplog):
    caplog.set_level(logging.INFO, logger="risk.service")
    r_v1 = service.submit_application("app-v1", "c1", GOOD, model_version="v1")
    r_v2 = service.submit_application("app-v2", "c1", GOOD, model_version="v2")
    assert r_v1.result.model_version == "v1"
    assert r_v2.result.model_version == "v2"
    # 版本差异 => 结论可不同
    assert r_v1.result.score != r_v2.result.score
    # 模型升级后，历史结论仍按原版本重放且完全一致
    replayed = service.replay("app-v1")
    assert replayed == r_v1.result
    assert replayed.model_version == "v1"
    # 同一版本同一快照 => 逐位一致
    again = score_application("x", "c1", GOOD, "v1")
    assert again.score == r_v1.result.score
    assert again.feature_snapshot_hash == r_v1.result.feature_snapshot_hash


# ---- 特征异常：拒绝原因可区分 ----

def test_feature_anomalies_distinguishable(service):
    missing = dict(GOOD)
    del missing["age"]
    r = service.submit_application("app-miss", "c1", missing)
    assert r.result.decision is Decision.DECLINE
    assert [x.code.value for x in r.result.reasons] == ["MISSING_FEATURE"]

    bad_type = service.submit_application("app-type", "c1", dict(GOOD, age="40"))
    assert [x.code.value for x in bad_type.result.reasons] == ["INVALID_FEATURE_TYPE"]

    out_of_range = service.submit_application("app-range", "c1", dict(GOOD, debt_ratio=1.5))
    assert [x.code.value for x in out_of_range.result.reasons] == ["FEATURE_OUT_OF_RANGE"]

    bool_age = service.submit_application("app-bool", "c1", dict(GOOD, age=True))
    assert [x.code.value for x in bool_age.result.reasons] == ["INVALID_FEATURE_TYPE"]


def test_unknown_model_version(service, caplog):
    caplog.set_level(logging.INFO, logger="risk.service")
    with pytest.raises(RiskServiceError) as ei:
        service.submit_application("app-v9", "c1", GOOD, model_version="v9")
    assert ei.value.kind is ErrorKind.UNKNOWN_MODEL_VERSION


# ---- 告警去重与状态一致 ----

def _tx(i, ts, amount=100.0, merchant="m1"):
    return Transaction(f"tx-{i}", "c1", amount, ts, merchant)


def test_alert_dedup_within_window(service):
    service.ingest_transaction(_tx(1, 0.0))
    a1 = service.ingest_transaction(_tx(2, 10.0))  # 同窗口同商户同金额 -> 重复告警
    a2 = service.ingest_transaction(_tx(3, 20.0))  # 同窗口再次重复 -> 不重复告警
    dup1 = [a for a in a1 if a.kind.value == "DUPLICATE_TRANSACTION"]
    dup2 = [a for a in a2 if a.kind.value == "DUPLICATE_TRANSACTION"]
    assert len(dup1) == 1 and len(dup2) == 1
    assert dup1[0].alert_id == dup2[0].alert_id, "同窗口同类告警必须去重为同一条"
    alerts = service._repo.list_alerts("c1")
    dup_alerts = [a for a in alerts if a.kind.value == "DUPLICATE_TRANSACTION"]
    assert len(dup_alerts) == 1


def test_burst_and_deviation(service):
    for i in range(6):
        alerts = service.ingest_transaction(_tx(i, float(i * 10), amount=50.0 + i, merchant=f"m{i}"))
    assert any(a.kind.value == "BURST" for a in alerts)
    big = service.ingest_transaction(_tx(99, 700.0, amount=100000.0, merchant="mx"))
    assert any(a.kind.value == "BEHAVIOR_DEVIATION" for a in big)


# ---- 错误归一化：底层异常不透出 ----

def test_persistence_failure_normalized(service, caplog):
    caplog.set_level(logging.ERROR, logger="risk.service")

    class BrokenRepo(Repository):
        def create_application_with_alerts(self, application_id, customer_id, features, build_record):
            raise RuntimeError("disk full: /var/lib/db/seg-07")  # 底层细节

    svc = RiskService(BrokenRepo())
    with pytest.raises(RiskServiceError) as ei:
        svc.submit_application("app-persist", "c1", GOOD)
    err = ei.value
    assert err.kind is ErrorKind.PERSISTENCE_FAILURE
    assert "disk full" not in str(err), "底层异常细节不得透出到对外结果"
    assert isinstance(err.cause, RuntimeError)
    assert any("app-persist" in r.getMessage() for r in caplog.records), "日志需含申请标识"


def test_compute_failure_normalized(service):
    class BrokenRepo(Repository):
        pass

    svc = RiskService(BrokenRepo())

    import app.service as svc_mod

    orig = svc_mod.scoring.score_application
    svc_mod.scoring.score_application = lambda *a, **k: 1 / 0
    try:
        with pytest.raises(RiskServiceError) as ei:
            svc.submit_application("app-compute", "c1", GOOD)
    finally:
        svc_mod.scoring.score_application = orig
    assert ei.value.kind is ErrorKind.COMPUTE_FAILURE
    assert "division" not in str(ei.value)
