"""FastAPI 对外接口：统一错误归一化为 HTTP 响应，底层异常不透出。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.errors import ErrorKind, RiskServiceError
from app.models import Transaction
from app.service import RiskService

app = FastAPI(title="credit-risk-scoring-service")
service = RiskService()

_STATUS_BY_KIND = {
    ErrorKind.MISSING_FEATURE: 422,
    ErrorKind.INVALID_FEATURE_TYPE: 422,
    ErrorKind.FEATURE_OUT_OF_RANGE: 422,
    ErrorKind.INVALID_INPUT: 422,
    ErrorKind.UNKNOWN_MODEL_VERSION: 400,
    ErrorKind.CONCURRENT_CONFLICT: 409,
    ErrorKind.UPSTREAM_FAILURE: 502,
    ErrorKind.COMPUTE_FAILURE: 500,
    ErrorKind.PERSISTENCE_FAILURE: 500,
}


class ApplicationRequest(BaseModel):
    application_id: str
    customer_id: str
    features: dict
    model_version: str | None = None


class TransactionRequest(BaseModel):
    tx_id: str
    customer_id: str
    amount: float
    timestamp: float
    merchant: str


@app.exception_handler(RiskServiceError)
async def risk_error_handler(_: Request, exc: RiskServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=_STATUS_BY_KIND.get(exc.kind, 500),
        content={"error": {"kind": exc.kind.value, "detail": exc.detail}},
    )


@app.exception_handler(Exception)
async def unknown_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # 兜底：任何未预期异常都归一化，不向外透出堆栈
    return JSONResponse(
        status_code=500,
        content={"error": {"kind": ErrorKind.COMPUTE_FAILURE.value, "detail": "内部错误"}},
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/applications")
def submit(req: ApplicationRequest) -> dict:
    record = service.submit_application(
        req.application_id, req.customer_id, req.features, req.model_version
    )
    return _application_payload(record)


def _application_payload(record) -> dict:
    r = record.result
    return {
        "application_id": record.application_id,
        "customer_id": record.customer_id,
        "risk_record_id": record.risk_record_id,
        "model_version": r.model_version,
        "link_policy_version": record.link_policy_version,
        "score": r.score,
        "category": r.category.value,
        "decision": r.decision.value,
        # 联动收紧前的原始打分判定，便于对比“被收紧到了什么程度”
        "base_score": record.base_result.score if record.base_result else None,
        "base_decision": (
            record.base_result.decision.value if record.base_result else None
        ),
        "reasons": [
            {"code": x.code.value, "feature": x.feature, "detail": x.detail} for x in r.reasons
        ],
        "feature_snapshot_hash": r.feature_snapshot_hash,
        # 本次决策关联了哪些告警，以及每条告警的联动影响
        "linked_alerts": [
            {
                "alert_id": impact.alert_id,
                "kind": impact.kind.value,
                "severity": impact.severity,
                "score_penalty": impact.score_penalty,
                "ladder_steps": impact.ladder_steps,
                "detail": impact.detail,
            }
            for impact in record.linked_impacts
        ],
    }


@app.get("/applications/{application_id}")
def application_detail(application_id: str) -> dict:
    """正向追溯：一次申请关联了哪些告警（返回决策当时固定的结论与关联）。"""
    record = service.get_application_detail(application_id)
    payload = _application_payload(record)
    payload["alert_ids"] = list(record.alert_ids)
    return payload


@app.get("/alerts/{alert_id}/decision")
def alert_decision(alert_id: str) -> dict:
    """反向追溯：一条告警关联到了哪一次决策。"""
    alert = service.get_alert_link(alert_id)
    payload = {
        "alert_id": alert.alert_id,
        "customer_id": alert.customer_id,
        "kind": alert.kind.value,
        "status": alert.status.value,
        "linked_application_id": alert.linked_application_id,
    }
    if alert.linked_application_id is not None:
        record = service.get_application_detail(alert.linked_application_id)
        payload["decision"] = record.result.decision.value
        payload["risk_record_id"] = record.risk_record_id
    return payload


@app.get("/applications/{application_id}/replay")
def replay(application_id: str) -> dict:
    r = service.replay(application_id)
    return {
        "application_id": r.application_id,
        "model_version": r.model_version,
        "score": r.score,
        "category": r.category.value,
        "decision": r.decision.value,
    }


@app.post("/transactions")
def ingest(req: TransactionRequest) -> dict:
    alerts = service.ingest_transaction(Transaction(**req.model_dump()))
    return {
        "alerts": [
            {
                "alert_id": a.alert_id,
                "kind": a.kind.value,
                "status": a.status.value,
                "window_start": a.window_start,
                "window_end": a.window_end,
                "tx_ids": list(a.tx_ids),
            }
            for a in alerts
        ]
    }
