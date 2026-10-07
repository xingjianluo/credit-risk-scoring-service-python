"""本轮新增：告警-申请联动收紧、双向追溯、历史不可变与重放、并发关联一致、非法数值边界。"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import ErrorKind, RiskServiceError
from app.models import AlertStatus, Decision, Transaction
from app.repository import Repository
from app.service import RiskService

# 高分特征：基线评分 APPROVE，且扣分后分数仍高，用于验证「判定下限」在起作用
HIGH = {
    "age": 40,
    "annual_income": 500000,
    "debt_ratio": 0.1,
    "credit_history_months": 200,
    "delinquency_count": 0,
}
# 中分特征：基线 REVIEW（548），被重复告警扣 40 后落入 DECLINE（508 仍 REVIEW），
# 再叠一条突发告警共扣 120 -> 428 DECLINE
MID = {
    "age": 30,
    "annual_income": 100000,
    "debt_ratio": 0.5,
    "credit_history_months": 60,
    "delinquency_count": 0,
}


@pytest.fixture()
def service() -> RiskService:
    return RiskService(Repository())


def _dup_alerts(svc: RiskService, customer: str = "c1", ts0: float = 0.0) -> list[str]:
    """喂两笔同窗口同商户同金额交易，制造一条重复交易告警。"""
    ids: list[str] = []
    for i in range(2):
        alerts = svc.ingest_transaction(
            Transaction(f"tx-{customer}-{i}", customer, 100.0, ts0 + i * 10.0, "m1")
        )
        ids.extend(a.alert_id for a in alerts)
    return ids


def _burst_alert(svc: RiskService, customer: str, ts0: float = 0.0) -> str:
    """喂 5 笔同窗口不同商户交易，制造一条突发告警。"""
    alert_id = ""
    for i in range(5):
        alerts = svc.ingest_transaction(
            Transaction(f"tx-{customer}-b{i}", customer, 50.0 + i, ts0 + i * 10.0, f"m{i}")
        )
        for a in alerts:
            if a.kind.value == "BURST":
                alert_id = a.alert_id
    assert alert_id
    return alert_id


# ---- 联动升级 ----

def test_no_alert_keeps_baseline(service):
    rec = service.submit_application("app-1", "c1", HIGH)
    assert rec.result.decision is Decision.APPROVE
    assert rec.alert_ids == []
    assert rec.alert_links == ()


def test_duplicate_alert_tightens_approve_to_review(service, caplog):
    caplog.set_level(logging.INFO, logger="risk.service")
    alert_id = _dup_alerts(service)[0]
    rec = service.submit_application("app-1", "c1", HIGH)
    # 分数扣了 40 但仍在高分区间，是「判定下限 REVIEW」把 APPROVE 收紧成 REVIEW
    assert rec.result.decision is Decision.REVIEW
    assert rec.result.score == 810
    assert rec.alert_ids == [alert_id]
    link = rec.alert_links[0]
    assert link.alert_id == alert_id
    assert link.score_penalty == 40
    assert link.decision_floor is Decision.REVIEW
    reasons = [r for r in rec.result.reasons if r.code.value == "ALERT_LINKAGE"]
    assert len(reasons) == 1
    detail = reasons[0].detail
    assert alert_id in detail and "DUPLICATE_TRANSACTION" in detail
    assert "扣减 40" in detail and "REVIEW" in detail
    # 告警状态已翻转
    alert = service._repo.get_alert(alert_id)
    assert alert.status is AlertStatus.LINKED_TO_DECISION
    assert alert.linked_application_id == "app-1"
    # 日志含申请标识、模型版本、判定理由、所用告警
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "app-1" in logs and "model=v2" in logs and "linked_alerts=" + alert_id in logs
    assert "ALERT_LINKAGE" in logs


def test_burst_alert_floor_review_and_multi_penalty(service):
    # 突发窗口（不同商户、金额平缓，避免触发重复/偏离）
    bid = _burst_alert(service, "c1")
    # 同窗口补两笔同商户同金额，追加一条重复告警
    service.ingest_transaction(Transaction("dup1", "c1", 150.0, 550.0, "md"))
    did_alerts = service.ingest_transaction(Transaction("dup2", "c1", 150.0, 560.0, "md"))
    did = next(a.alert_id for a in did_alerts if a.kind.value == "DUPLICATE_TRANSACTION")

    rec = service.submit_application("app-m", "c1", HIGH)
    assert rec.result.decision is Decision.REVIEW
    assert set(rec.alert_ids) == {bid, did}
    assert rec.result.score == 850 - 40 - 80  # 40(重复) + 80(突发)
    kinds = {link.kind.value for link in rec.alert_links}
    assert kinds == {"DUPLICATE_TRANSACTION", "BURST"}


def test_deviation_alert_declines(service):
    # 4 笔小额历史（不同商户防突发，跨窗口防重复）-> 第 5 笔巨额触发行为偏离（DECLINE 下限）
    for i in range(4):
        service.ingest_transaction(
            Transaction(f"h{i}", "c1", 100.0, 600.0 * i, f"m{i}")
        )
    alerts = service.ingest_transaction(
        Transaction("big", "c1", 100_000.0, 3000.0, "mx")
    )
    kinds = {a.kind.value for a in alerts}
    assert kinds == {"BEHAVIOR_DEVIATION"}
    dev = next(a.alert_id for a in alerts if a.kind.value == "BEHAVIOR_DEVIATION")
    rec = service.submit_application("app-d", "c1", HIGH)
    assert rec.result.decision is Decision.DECLINE
    assert rec.alert_ids == [dev]
    assert rec.alert_links[0].decision_floor is Decision.DECLINE


def test_alert_penalty_pushes_mid_score_to_decline(service):
    # 基线 REVIEW(548)：重复+突发共扣 120 -> 428，分数本身跌破拒绝线
    bid = _burst_alert(service, "c1")
    service.ingest_transaction(Transaction("dup1", "c1", 150.0, 550.0, "md"))
    did_alerts = service.ingest_transaction(Transaction("dup2", "c1", 150.0, 560.0, "md"))
    did = next(a.alert_id for a in did_alerts if a.kind.value == "DUPLICATE_TRANSACTION")
    rec = service.submit_application("app-mid", "c1", MID)
    assert rec.result.decision is Decision.DECLINE
    assert rec.result.score == 428
    assert set(rec.alert_ids) == {bid, did}


def test_already_declined_baseline_collects_alerts_anyway(service):
    # 校验失败本就 DECLINE，告警依旧被认领并挂接、理由可分辨
    _dup_alerts(service)
    bad = dict(HIGH)
    del bad["age"]
    rec = service.submit_application("app-bad", "c1", bad)
    assert rec.result.decision is Decision.DECLINE
    assert len(rec.alert_ids) == 1
    codes = [r.code.value for r in rec.result.reasons]
    assert "MISSING_FEATURE" in codes and "ALERT_LINKAGE" in codes


# ---- 双向追溯 ----

def test_bidirectional_trace(service):
    alert_id = _dup_alerts(service)[0]
    rec = service.submit_application("app-t", "c1", HIGH)

    # 申请 -> 告警
    alerts = service.get_application_alerts("app-t")
    assert [a.alert_id for a in alerts] == [alert_id]
    assert alerts[0].status is AlertStatus.LINKED_TO_DECISION

    # 告警 -> 申请
    linked = service.get_alert_linked_application(alert_id)
    assert linked is rec

    service.ingest_transaction(Transaction("u1", "c1", 7.0, 700.0, "zz"))
    ua = service.ingest_transaction(Transaction("u2", "c1", 7.0, 701.0, "zz"))
    uid = next(a.alert_id for a in ua if a.kind.value == "DUPLICATE_TRANSACTION")
    assert service.get_alert_linked_application(uid) is None


def test_trace_missing_entities_normalized(service):
    with pytest.raises(RiskServiceError) as ei:
        service.get_application_alerts("nope")
    assert ei.value.kind is ErrorKind.UPSTREAM_FAILURE
    with pytest.raises(RiskServiceError) as ei:
        service.get_alert_linked_application("nope")
    assert ei.value.kind is ErrorKind.UPSTREAM_FAILURE

# ---- 历史不可变与重放一致 ----

def test_history_immutable_to_later_alerts(service):
    alert_id = _dup_alerts(service, "c1")[0]
    rec = service.submit_application("app-h", "c1", HIGH, model_version="v1")
    frozen_result = rec.result
    assert frozen_result.decision is Decision.REVIEW
    assert rec.alert_ids == [alert_id]

    # 之后同客户再冒出新告警（新窗口重复 + 跨窗口），不得回头改写历史申请
    service.ingest_transaction(Transaction("n1", "c1", 5.0, 600.0, "zn"))
    later = service.ingest_transaction(Transaction("n2", "c1", 5.0, 610.0, "zn"))
    new_id = next(a.alert_id for a in later if a.kind.value == "DUPLICATE_TRANSACTION")
    assert new_id != alert_id

    again = service.submit_application("app-h", "c1", HIGH, model_version="v1")
    assert again is rec
    assert again.result == frozen_result
    assert again.alert_ids == [alert_id]  # 关联关系固定，不含新告警
    # 新告警仍处于 OPEN，等待下一次申请
    assert service._repo.get_alert(new_id).status is AlertStatus.OPEN


def test_replay_bit_identical_with_frozen_links(service, caplog):
    caplog.set_level(logging.INFO, logger="risk.service")
    _dup_alerts(service, "c1")
    rec = service.submit_application("app-r", "c1", HIGH, model_version="v1")
    replayed = service.replay("app-r")
    assert replayed == rec.result  # 逐位一致（含联动理由、分数、快照哈希）
    # 重放后再制造新告警并升级策略视角，重放依旧不变
    service.ingest_transaction(Transaction("x1", "c1", 9.0, 900.0, "zx"))
    service.ingest_transaction(Transaction("x2", "c1", 9.0, 901.0, "zx"))
    assert service.replay("app-r") == rec.result
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "app-r" in logs and "model=v1" in logs


def test_replay_drift_detected_if_tampered(service):
    _dup_alerts(service, "c1")
    rec = service.submit_application("app-tamper", "c1", HIGH)
    # 人为篡改历史冻结快照，重放必须识别漂移而不是静默返回新结论
    object.__setattr__(rec, "alert_links", ())
    with pytest.raises(RiskServiceError) as ei:
        service.replay("app-tamper")
    assert ei.value.kind is ErrorKind.COMPUTE_FAILURE


# ---- 并发下的关联一致性 ----

def test_concurrent_duplicate_application_links_once(service):
    alert_id = _dup_alerts(service, "c1")[0]
    with ThreadPoolExecutor(max_workers=16) as pool:
        records = list(
            pool.map(lambda _: service.submit_application("app-c", "c1", HIGH), range(64))
        )
    first = records[0]
    assert all(r is first for r in records), "并发重复提交必须返回同一份结论"
    assert service._repo.count_applications() == 1
    assert service._repo.count_risk_records() == 1, "重试不得重复计数"
    # 告警只被关联一次、只挂在这一次申请上
    alert = service._repo.get_alert(alert_id)
    assert alert.status is AlertStatus.LINKED_TO_DECISION
    assert alert.linked_application_id == "app-c"
    assert first.alert_ids == [alert_id]


def test_concurrent_distinct_applications_split_alerts(service):
    # 两个不同申请并发提交，但客户只有一条 OPEN 告警：恰好一个申请认领，
    # 另一个拿不到，绝不重复关联或错挂
    _dup_alerts(service, "c1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [
            pool.submit(service.submit_application, "app-A", "c1", HIGH),
            pool.submit(service.submit_application, "app-B", "c1", HIGH),
        ]
        ra, rb = [f.result() for f in futs]
    winners = [r for r in (ra, rb) if r.alert_ids]
    losers = [r for r in (ra, rb) if not r.alert_ids]
    assert len(winners) == 1 and len(losers) == 1
    winner = winners[0]
    loser = losers[0]
    assert winner.result.decision is Decision.REVIEW
    assert loser.result.decision is Decision.APPROVE
    # 双向指针一致：告警只指向认领方
    all_open = [a for a in service._repo.list_alerts("c1") if a.status is AlertStatus.OPEN]
    assert all_open == []
    linked_id = winner.alert_ids[0]
    alert = service._repo.get_alert(linked_id)
    assert alert.linked_application_id == winner.application_id
    assert service.get_alert_linked_application(linked_id).application_id == winner.application_id
    # 两个申请各自只有一条风险记录
    assert service._repo.count_risk_records() == 2


def test_concurrent_application_with_new_ingest_no_miss(service):
    # 申请与告警几乎同时：先占位申请拿不到未产生的告警属正常时序；
    # 但"提交时告警已存在"的竞争下，原子认领保证不漏。用屏障对齐两者起跑。
    import threading

    start = threading.Barrier(2)

    def make_alert():
        start.wait()
        _dup_alerts(service, "c1")

    def submit():
        start.wait()
        return service.submit_application("app-race", "c1", HIGH)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_alert = pool.submit(make_alert)
        f_app = pool.submit(submit)
        f_alert.result()
        rec = f_app.result()
    # 两种线性化都自洽：若申请先到则无关联且告警 OPEN；若告警先到则必被关联
    alerts = service._repo.list_alerts("c1")
    if rec.alert_ids:
        assert len(rec.alert_ids) == 1
        assert all(a.status is AlertStatus.LINKED_TO_DECISION for a in alerts)
        assert rec.result.decision is Decision.REVIEW
    else:
        assert all(a.status is AlertStatus.OPEN for a in alerts)
        assert rec.result.decision is Decision.APPROVE
    # 无论哪种顺序，下一次申请都会把残留 OPEN 告警认领，不丢关联
    rec2 = service.submit_application("app-race2", "c1", HIGH)
    if not rec.alert_ids:
        assert len(rec2.alert_ids) == 1
        assert rec2.result.decision is Decision.REVIEW


# ---- 非法数值边界 ----

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_rejected_distinctly(service, bad):
    features = dict(HIGH, age=bad)
    rec = service.submit_application(f"app-nf-{bad}", "c1", features)
    assert rec.result.decision is Decision.DECLINE
    codes = [r.code.value for r in rec.result.reasons]
    assert codes == ["NON_FINITE_VALUE"], "非有限数值必须单独识别，不能当普通低分放过"


def test_non_finite_float_feature_distinct(service):
    rec = service.submit_application(
        "app-nf2", "c1", dict(HIGH, annual_income=float("nan"))
    )
    assert [r.code.value for r in rec.result.reasons] == ["NON_FINITE_VALUE"]


def test_multiple_non_finite_all_reported(service):
    rec = service.submit_application(
        "app-nf3", "c1", dict(HIGH, debt_ratio=float("inf"), delinquency_count=float("-inf"))
    )
    # debt_ratio 是 float 规格 -> NON_FINITE；delinquency_count 是 int 规格，
    # 传入的是 float(-inf)，有限性检查先于类型检查，同样归 NON_FINITE
    assert all(r.code.value == "NON_FINITE_VALUE" for r in rec.result.reasons)
    assert {r.feature for r in rec.result.reasons} == {"debt_ratio", "delinquency_count"}

# ---- 联动链路错误归一化（底层细节不透出，日志可定位） ----

def test_claim_failure_normalized_with_log(caplog):
    class BrokenLinkRepo(Repository):
        def create_application_with_alerts(self, application_id, customer_id, features, build_record):
            raise RuntimeError("segfault in lock manager node=42")

    svc = RiskService(BrokenLinkRepo())
    caplog.set_level(logging.ERROR, logger="risk.service")
    with pytest.raises(RiskServiceError) as ei:
        svc.submit_application("app-broken-link", "c1", HIGH)
    err = ei.value
    assert err.kind is ErrorKind.PERSISTENCE_FAILURE
    assert "segfault" not in str(err) and "node=42" not in str(err)
    assert isinstance(err.cause, RuntimeError)
    assert any("app-broken-link" in r.getMessage() for r in caplog.records)


def test_trace_endpoint_normalized_error():
    from fastapi.testclient import TestClient

    from app.api import app

    client = TestClient(app)
    r = client.get("/applications/missing-id/alerts")
    assert r.status_code == 502
    body = r.json()["error"]
    assert body["kind"] == "UPSTREAM_FAILURE"
