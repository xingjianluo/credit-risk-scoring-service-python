"""覆盖：并发重复提交、模型版本重放、特征异常、告警去重、错误归一化。"""
from __future__ import annotations

import logging
import threading
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
        def create_application(self, application_id, customer_id, builder, claim_alerts=True):
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


# ---- 本轮新增：交易告警联动收紧 ----

def _make_open_alert(svc, customer_id="c1", i=1, amount=100.0, merchant="m1", wstart=0.0):
    """造一笔重复交易告警并返回该告警。

    wstart 指定 600s 对齐窗口起点：同窗口同类告警会按既有规则去重，
    需要两条独立告警时把它们放到不同窗口。
    """
    svc.ingest_transaction(Transaction(f"dup-{i}-a", customer_id, amount, wstart, merchant))
    alerts = svc.ingest_transaction(
        Transaction(f"dup-{i}-b", customer_id, amount, wstart + 10.0, merchant)
    )
    return next(a for a in alerts if a.kind.value == "DUPLICATE_TRANSACTION")


def test_alert_tightens_approve_to_review(service, caplog):
    caplog.set_level(logging.INFO, logger="risk.service")
    alert = _make_open_alert(service, i=1)
    assert alert.status.value == "OPEN"

    # 无告警时 GOOD 特征本应 APPROVE（v1 基础分 873）
    clean = RiskService(Repository())
    baseline = clean.submit_application("app-base", "c1", GOOD, model_version="v1")
    assert baseline.result.decision is Decision.APPROVE

    record = service.submit_application("app-link", "c1", GOOD, model_version="v1")
    # 有一条 LOW(重复) 告警：扣 80 分（873->793，分数侧仍通过），阶梯 1 级兜底 => 人工复核
    assert record.result.decision is Decision.REVIEW
    assert record.base_result is not None
    assert record.base_result.decision is Decision.APPROVE
    # 结论里必须给出可分辨的联动理由
    codes = [r.code.value for r in record.result.reasons]
    assert "ALERT_TIGHTENED" in codes
    impact = record.linked_impacts[0]
    assert impact.alert_id == alert.alert_id
    assert impact.kind.value == "DUPLICATE_TRANSACTION"
    assert impact.severity == "LOW"
    assert impact.score_penalty == 80 and impact.ladder_steps == 1
    assert alert.alert_id in impact.detail and "DUPLICATE_TRANSACTION" in impact.detail
    # 日志能看出申请标识、模型版本、判定理由与用到的告警
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "app-link" in text and "model=v1" in text
    assert "linked_alert=" + alert.alert_id in text


def test_burst_alert_declines(service):
    # 同窗口 5 笔 => BURST（MEDIUM，阶梯 2 级 => APPROVE 直接 DECLINE）
    for i in range(5):
        alerts = service.ingest_transaction(_tx(i, float(i), amount=100.0, merchant=f"mm{i}"))
    burst = next(a for a in alerts if a.kind.value == "BURST")
    record = service.submit_application("app-burst", "c1", GOOD, model_version="v1")
    assert record.result.decision is Decision.DECLINE
    impact = next(i for i in record.linked_impacts if i.alert_id == burst.alert_id)
    assert impact.severity == "MEDIUM" and impact.ladder_steps == 2


def test_multiple_alerts_accumulate(service):
    a1 = _make_open_alert(service, i=1, merchant="m1", wstart=0.0)
    a2 = _make_open_alert(service, i=2, merchant="m2", wstart=600.0)
    record = service.submit_application("app-multi", "c1", GOOD, model_version="v1")
    # 两条 LOW：基础分 873 扣 160 => 713（分数侧仍可通过），阶梯累计 2 级 => DECLINE，取更严
    assert record.result.decision is Decision.DECLINE
    assert record.result.score == 713
    assert [i.alert_id for i in record.linked_impacts] == sorted([a1.alert_id, a2.alert_id])


def test_only_unlinked_alerts_are_used(service):
    _make_open_alert(service, i=1)
    first = service.submit_application("app-first", "c1", GOOD, model_version="v1")
    assert first.result.decision is Decision.REVIEW
    # 同客户再来一单：上一批告警已关联，不应再次影响；无新 OPEN 告警 => 回到 APPROVE
    second = service.submit_application("app-second", "c1", GOOD, model_version="v1")
    assert second.result.decision is Decision.APPROVE
    assert second.alert_ids == []
    assert second.linked_impacts == []


# ---- 双向追溯 ----

def test_bidirectional_traceability(service):
    alert = _make_open_alert(service, i=1)
    service.submit_application("app-trace", "c1", GOOD, model_version="v1")

    # 正向：申请 -> 告警
    detail = service.get_application_detail("app-trace")
    assert detail.alert_ids == [alert.alert_id]
    # 反向：告警 -> 申请，状态变为已关联决策
    linked = service.get_alert_link(alert.alert_id)
    assert linked.status.value == "LINKED_TO_DECISION"
    assert linked.linked_application_id == "app-trace"

    # HTTP 层两个方向都可查
    from fastapi.testclient import TestClient

    from app.api import app
    client = TestClient(app)
    # 复用同一个内存服务：把全局 service 指向当前 repository 支撑的实例
    import app.api as api_mod
    api_mod.service = service
    resp = client.get("/applications/app-trace")
    assert resp.status_code == 200
    body = resp.json()
    assert body["linked_alerts"][0]["alert_id"] == alert.alert_id
    resp2 = client.get(f"/alerts/{alert.alert_id}/decision")
    assert resp2.status_code == 200
    assert resp2.json()["linked_application_id"] == "app-trace"


# ---- 历史不可变 + 重放逐位一致 ----

def test_history_immutable_to_later_alerts(service):
    _make_open_alert(service, i=1)
    record = service.submit_application("app-hist", "c1", GOOD, model_version="v1")
    assert record.result.decision is Decision.REVIEW
    fixed_result = record.result
    fixed_alerts = list(record.alert_ids)

    # 之后同客户再冒出新告警（下一个对齐窗口）
    _make_open_alert(service, i=2, wstart=600.0)
    for _ in range(5):
        service.ingest_transaction(_tx(900 + _, 900.0 + _, amount=10.0, merchant=f"q{_}"))

    # 历史申请的结论与关联关系原样固定，没有被新告警改写
    again = service.submit_application("app-hist", "c1", GOOD, model_version="v1")
    assert again is record
    assert again.result == fixed_result
    assert again.alert_ids == fixed_alerts
    # 新告警仍处于 OPEN，没有被回挂到历史申请
    new_alert = next(
        a for a in service._repo.list_alerts("c1")
        if a.alert_id not in fixed_alerts and a.kind.value == "DUPLICATE_TRANSACTION"
    )
    assert new_alert.status.value == "OPEN"
    assert new_alert.linked_application_id is None


def test_replay_linked_application_bit_identical(service):
    _make_open_alert(service, i=1)
    record = service.submit_application("app-replay", "c1", GOOD, model_version="v1")
    tightened = record.result
    # 之后再来新告警，重放历史必须仍是当时那条收紧结论
    _make_open_alert(service, i=2, wstart=600.0)
    replayed = service.replay("app-replay")
    assert replayed == tightened
    # 逐条理由（含窗口/笔数的 detail）也要逐位一致
    assert tuple(tightened.reasons) == tuple(record.result.reasons)


def test_replay_unlinked_application_unchanged(service):
    record = service.submit_application("app-clean", "c1", GOOD, model_version="v1")
    assert record.linked_impacts == []
    assert service.replay("app-clean") == record.result


def test_replay_respects_historical_model_version(service):
    _make_open_alert(service, i=1)
    r1 = service.submit_application("app-rv1", "c1", GOOD, model_version="v1")
    _make_open_alert(service, i=2, wstart=600.0)
    r2 = service.submit_application("app-rv2", "c1", GOOD, model_version="v2")
    assert service.replay("app-rv1") == r1.result
    assert service.replay("app-rv2") == r2.result
    assert r1.result.model_version == "v1" and r2.result.model_version == "v2"


# ---- 并发下的关联一致性 ----

def test_concurrent_application_and_alerts_no_mislink(service):
    # 预置 3 条不同窗口的 OPEN 告警
    alert_ids = [
        _make_open_alert(service, i=k, wstart=k * 600.0).alert_id for k in range(3)
    ]

    def submit(_):
        return service.submit_application("app-conn", "c1", GOOD, model_version="v1")

    with ThreadPoolExecutor(max_workers=16) as pool:
        records = list(pool.map(submit, range(32)))

    # 只有一份最终结论、一条风险记录；所有返回是同一对象，绝不互相矛盾
    assert all(r is records[0] for r in records)
    assert service._repo.count_applications() == 1
    assert service._repo.count_risk_records() == 1
    rec = records[0]
    # 3 条告警恰好都挂在这一次申请上，不重不漏
    assert sorted(rec.alert_ids) == sorted(alert_ids)
    assert len(rec.linked_impacts) == 3
    for aid in alert_ids:
        alert = service._repo.get_alert(aid)
        assert alert.status.value == "LINKED_TO_DECISION"
        assert alert.linked_application_id == "app-conn"


def test_concurrent_two_applications_partition_alerts(service):
    # 两个不同申请并发提交，同时交易侧继续产生告警：
    # 任何一条 OPEN 告警要么归 app-A 要么归 app-B 或仍 OPEN，绝不重复挂、挂错
    initial = [
        _make_open_alert(service, i=k, wstart=k * 600.0).alert_id for k in range(2)
    ]

    barrier = threading.Barrier(2)

    def submit_a(_):
        barrier.wait()
        return service.submit_application("app-A", "c1", GOOD, model_version="v1")

    def submit_b(_):
        barrier.wait()
        return service.submit_application("app-B", "c1", GOOD, model_version="v1")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(submit_a, 0), pool.submit(submit_b, 0)]
        ra, rb = [f.result() for f in futs]

    # 两个申请都成功且结论确定；初始告警不重不漏地恰好归属其中一个
    assert {ra.application_id, rb.application_id} == {"app-A", "app-B"}
    assigned = ra.alert_ids + rb.alert_ids
    assert sorted(assigned) == sorted(initial)
    assert len(set(assigned)) == len(assigned), "告警不得重复关联到两个申请"
    for aid in initial:
        owner = service._repo.get_alert(aid).linked_application_id
        assert owner in {"app-A", "app-B"}


def test_concurrent_ingest_then_application_consistency(service):
    # 告警与申请几乎同时：一半线程造告警、一半线程提交，最终状态必须自洽
    def worker(idx):
        if idx % 2 == 0:
            w = (idx // 2) * 600.0
            return ("alert", _make_open_alert(service, i=idx, wstart=w).alert_id)
        return ("app", service.submit_application("app-race", "c1", GOOD, model_version="v1"))

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(worker, range(16)))  # 并发跑完，结果通过存储层状态校验

    rec = service._repo.get_application("app-race")
    assert rec is not None
    linked = set(rec.alert_ids)
    # 申请记录的每条关联都与告警反向指针一致
    for aid in linked:
        assert service._repo.get_alert(aid).linked_application_id == "app-race"
    # 被申请关联过的告警不会被第二个申请再关联
    rec2 = service.submit_application("app-race-2", "c1", GOOD, model_version="v1")
    assert set(rec2.alert_ids).isdisjoint(linked)


# ---- 非法数值边界：非有限值单独识别，不当正常低分放过 ----

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numeric_rejected(service, bad):
    features = dict(GOOD, annual_income=bad)
    with pytest.raises(RiskServiceError) as ei:
        service.submit_application("app-nan", "c1", features)
    assert ei.value.kind is ErrorKind.INVALID_INPUT
    # 非法输入不得落库、不得占用风险记录号
    assert service._repo.count_applications() == 0
    assert service._repo.count_risk_records() == 0


def test_non_finite_does_not_link_alerts(service, caplog):
    caplog.set_level(logging.WARNING, logger="risk.service")
    _make_open_alert(service, i=1)
    with pytest.raises(RiskServiceError) as ei:
        service.submit_application("app-nan2", "c1", dict(GOOD, debt_ratio=float("nan")))
    assert ei.value.kind is ErrorKind.INVALID_INPUT
    # 告警必须仍是 OPEN，未被非法申请吞掉
    alert = service._repo.list_alerts("c1")[0]
    assert alert.status.value == "OPEN" and alert.linked_application_id is None
    assert any("invalid_input" in r.getMessage() for r in caplog.records)


def test_non_finite_scoring_reason_distinguishable():
    r = score_application("x", "c1", dict(GOOD, annual_income=float("inf")))
    assert [x.code.value for x in r.reasons] == ["NON_FINITE_NUMERIC"]


def test_validation_failure_does_not_consume_alerts(service):
    alert = _make_open_alert(service, i=1)
    # 特征越界的畸形申请：拒绝落库，但不形成“采用告警”的决策
    bad = service.submit_application("app-bad-feature", "c1", dict(GOOD, debt_ratio=1.5))
    assert bad.result.decision is Decision.DECLINE
    assert bad.alert_ids == [] and bad.linked_impacts == []
    # 告警仍是 OPEN，未被畸形申请吞掉
    still_open = service._repo.get_alert(alert.alert_id)
    assert still_open.status.value == "OPEN" and still_open.linked_application_id is None
    # 随后正常申请仍能看到这条告警并被收紧
    good = service.submit_application("app-good", "c1", GOOD, model_version="v1")
    assert good.alert_ids == [alert.alert_id]
    assert good.result.decision is Decision.REVIEW
