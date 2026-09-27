"""API 层：对外只暴露归一化错误，并发提交经由 HTTP 也只有一条记录。"""
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.api import create_app

from conftest import GOOD_FEATURES

logger = logging.getLogger("credit_risk.test")


def _client():
    return TestClient(create_app(), raise_server_exceptions=False)


def test_feature_error_normalized_over_http():
    client = _client()
    bad = dict(GOOD_FEATURES)
    del bad["age"]
    r = client.post("/applications", json={
        "application_id": "API-1", "model_version": "v1", "features": bad})
    logger.info("http=%s body=%s", r.status_code, r.json())
    assert r.status_code == 422
    body = r.json()
    assert body["error_code"] == "FEATURE_MISSING"
    assert "Traceback" not in str(body) and "KeyError" not in str(body)


def test_concurrent_submit_over_http_single_record():
    client = _client()
    payload = {"application_id": "API-CONC-1", "model_version": "v1",
               "features": GOOD_FEATURES}
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(
            lambda _: client.post("/applications", json=payload), range(32)))
    bodies = [r.json() for r in responses]
    for b in bodies:
        logger.info("http decision application=%s model_version=%s decision=%s",
                    b["application_id"], b["model_version"], b["decision"])
    assert len({(b["score"], b["decision"]) for b in bodies}) == 1
    record = client.get("/risk-records/API-CONC-1").json()
    assert record["application_id"] == "API-CONC-1"


def test_conflict_over_http_is_409():
    client = _client()
    payload = {"application_id": "API-CONFLICT-1", "model_version": "v1",
               "features": GOOD_FEATURES}
    client.post("/applications", json=payload)
    r = client.post("/applications",
                    json={**payload, "model_version": "v2"})
    logger.info("http=%s body=%s", r.status_code, r.json())
    assert r.status_code == 409
    assert r.json()["error_code"] == "APPLICATION_CONFLICT"


def test_transaction_and_alert_flow_over_http():
    client = _client()
    txn = {"transaction_id": "T1", "customer_id": "C1",
           "amount": 100.0, "timestamp": 1000.0}
    assert client.post("/transactions", json=txn).json()["new_alerts"] == []
    dup = client.post("/transactions", json=txn).json()["new_alerts"]
    logger.info("alerts=%s", dup)
    assert len(dup) == 1 and dup[0]["rule"] == "DUPLICATE_TRANSACTION"
    again = client.post("/transactions", json=txn).json()["new_alerts"]
    assert all(a["rule"] != "DUPLICATE_TRANSACTION" for a in again)
