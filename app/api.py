"""对外 HTTP 接口：底层异常归一化后返回，不直接透出。"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .anomaly import AnomalyDetector
from .applications import ApplicationService
from .domain import FeatureSnapshot, Transaction
from .errors import ErrorCode, ServiceError
from .scoring import default_registry
from .store import RiskRecordStore

logger = logging.getLogger("credit_risk")


class ApplicationRequest(BaseModel):
    application_id: str
    model_version: str
    features: dict


class ReplayRequest(BaseModel):
    features: dict


class TransactionRequest(BaseModel):
    transaction_id: str
    customer_id: str
    amount: float
    timestamp: float


def _decision_dict(d) -> dict:
    return {
        "application_id": d.application_id,
        "model_version": d.model_version,
        "score": d.score,
        "decision": str(d.decision),
        "reasons": [{"code": r.code, "detail": r.detail} for r in d.reasons],
    }


def create_app() -> FastAPI:
    app = FastAPI(title="credit-risk-scoring-service")
    registry = default_registry()
    store = RiskRecordStore()
    service = ApplicationService(registry, store)
    detector = AnomalyDetector()

    @app.exception_handler(ServiceError)
    async def service_error_handler(_: Request, exc: ServiceError) -> JSONResponse:
        # 归一化错误：对外只暴露 code 与 message，cause 仅记日志。
        logger.warning("service error: %s", exc.to_dict(),
                       exc_info=exc.cause)
        status = 409 if exc.code == ErrorCode.APPLICATION_CONFLICT else 422
        return JSONResponse(status_code=status, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def unknown_error_handler(_: Request, exc: Exception) -> JSONResponse:
        normalized = ServiceError(ErrorCode.INTERNAL_ERROR,
                                  "内部错误", cause=exc)
        logger.error("unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content=normalized.to_dict())

    @app.post("/applications", status_code=200)
    def submit_application(req: ApplicationRequest) -> dict:
        snapshot = FeatureSnapshot(req.application_id, dict(req.features))
        decision = service.submit(snapshot, req.model_version)
        detector.link_decision(req.application_id, decision.decision)
        return _decision_dict(decision)

    @app.post("/applications/{application_id}/replay")
    def replay_application(application_id: str, req: ReplayRequest) -> dict:
        snapshot = FeatureSnapshot(application_id, dict(req.features))
        return _decision_dict(service.replay(application_id, snapshot))

    @app.get("/risk-records/{application_id}")
    def get_risk_record(application_id: str) -> dict:
        record = store.get(application_id)
        if record is None:
            raise ServiceError(ErrorCode.APPLICATION_CONFLICT,
                               f"application={application_id} 无风险记录")
        return {
            "application_id": record.application_id,
            "model_version": record.model_version,
            "decision": str(record.decision),
            "score": record.score,
            "reasons": [{"code": r.code, "detail": r.detail}
                        for r in record.reasons],
        }

    @app.post("/transactions", status_code=200)
    def ingest_transaction(req: TransactionRequest) -> dict:
        alerts = detector.ingest(Transaction(
            transaction_id=req.transaction_id,
            customer_id=req.customer_id,
            amount=req.amount,
            timestamp=req.timestamp,
        ))
        return {"new_alerts": [
            {"alert_id": a.alert_id, "customer_id": a.customer_id,
             "rule": a.rule, "transaction_ids": list(a.transaction_ids),
             "status": str(a.status)} for a in alerts]}

    @app.get("/alerts")
    def list_alerts() -> dict:
        return {"alerts": [
            {"alert_id": a.alert_id, "customer_id": a.customer_id,
             "rule": a.rule, "transaction_ids": list(a.transaction_ids),
             "status": str(a.status)} for a in detector.alerts()]}

    return app
