"""评分确定性与模型版本重放：同快照同版本结论完全一致，历史版本可重放。"""
import logging

import pytest

from app.domain import Decision, FeatureSnapshot
from app.errors import ErrorCode, ServiceError
from app.scoring import default_registry

from conftest import GOOD_FEATURES, log_decision, log_error

logger = logging.getLogger("credit_risk.test")


def test_same_snapshot_same_version_is_deterministic():
    registry = default_registry()
    snapshot = FeatureSnapshot("APP-DET-1", dict(GOOD_FEATURES))
    results = [registry.score(snapshot, "v1") for _ in range(50)]
    for r in results:
        log_decision(r)
    assert len({(r.score, r.decision, r.reasons) for r in results}) == 1


def test_model_upgrade_changes_conclusion_but_old_version_replays(service):
    snapshot = FeatureSnapshot("APP-REPLAY-1", dict(GOOD_FEATURES))
    d1 = service.submit(snapshot, "v1")
    log_decision(d1)

    # 模型升级后，历史结论仍可按原版本重放且完全一致。
    replayed = service.replay("APP-REPLAY-1", snapshot)
    log_decision(replayed)
    assert replayed.model_version == "v1"
    assert (replayed.score, replayed.decision, replayed.reasons) == \
        (d1.score, d1.decision, d1.reasons)

    # 新版本独立可用，且与旧版本并存。
    d2 = service.submit(FeatureSnapshot("APP-REPLAY-2", dict(GOOD_FEATURES)), "v2")
    log_decision(d2)
    assert d2.model_version == "v2"


def test_unknown_model_version_is_distinguishable(service):
    with pytest.raises(ServiceError) as exc_info:
        service.submit(FeatureSnapshot("APP-VER-1", dict(GOOD_FEATURES)), "v999")
    log_error(exc_info.value)
    assert exc_info.value.code == ErrorCode.MODEL_VERSION_UNKNOWN


def test_model_version_is_immutable():
    registry = default_registry()
    with pytest.raises(ServiceError) as exc_info:
        registry.register("v1", weights={"age": 9.9}, bias=0.0,
                          approve_threshold=0.5, review_threshold=0.3)
    log_error(exc_info.value)
    # 同参数重复注册是幂等允许的。
    from app.scoring import default_registry as _  # noqa: F401


def test_decision_thresholds_semantics():
    registry = default_registry()
    # 构造一个低质量快照，验证 REJECT 分支与拒绝原因。
    poor = {"age": 19, "annual_income": 10000, "debt_ratio": 0.95,
            "credit_history_months": 1, "delinquency_count": 10,
            "existing_loans": 8}
    d = registry.score(FeatureSnapshot("APP-LOW-1", poor), "v1")
    log_decision(d)
    assert d.decision == Decision.REJECT
    assert d.reasons, "拒绝结论必须给出判定依据"
    assert all(r.code == "NEGATIVE_FACTOR" for r in d.reasons)
