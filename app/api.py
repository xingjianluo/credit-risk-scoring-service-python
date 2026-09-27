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
    r = record.result
    return {
        "application_id": record.application_id,
        "risk_record_id": record.risk_record_id,
        "model_version": r.model_version,
        "score": r.score,
        "category": r.category.value,
        "decision": r.decision.value,
        "reasons": [
            {"code": x.code.value, "feature": x.feature, "detail": x.detail} for x in r.reasons
        ],
        "feature_snapshot_hash": r.feature_snapshot_hash,
    }


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
