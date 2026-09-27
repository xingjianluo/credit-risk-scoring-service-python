import logging

import pytest

from app.applications import ApplicationService
from app.scoring import default_registry
from app.store import RiskRecordStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("credit_risk.test")

GOOD_FEATURES = {
    "age": 35,
    "annual_income": 300000,
    "debt_ratio": 0.2,
    "credit_history_months": 120,
    "delinquency_count": 0,
    "existing_loans": 1,
}


@pytest.fixture()
def service() -> ApplicationService:
    return ApplicationService(default_registry(), RiskRecordStore())


def log_decision(decision) -> None:
    """单测日志：打印申请标识、模型版本、判定依据。"""
    logger.info(
        "decision application=%s model_version=%s decision=%s score=%.6f reasons=%s",
        decision.application_id, decision.model_version, decision.decision,
        decision.score, [r.detail for r in decision.reasons],
    )


def log_error(exc) -> None:
    """单测日志：打印归一化后的错误。"""
    logger.info("normalized error code=%s message=%s", exc.code, exc.message)
