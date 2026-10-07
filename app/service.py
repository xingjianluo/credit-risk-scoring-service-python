"""编排层：申请提交幂等、告警联动收紧、异常检测、错误归一化。

一致性约定：
- 同一 application_id 重复/并发提交：内容一致则返回同一份最终结论（幂等成功）；
  内容冲突（特征快照或模型版本不同）则抛 CONCURRENT_CONFLICT，绝不返回矛盾结论；
- 提交时原子抢占该客户所有未关联（OPEN）告警并收紧判定，告警状态随即变为
  LINKED_TO_DECISION；已落库申请的结论与关联永久固定，后续新告警不改写历史；
- 所有底层异常归一化为 RiskServiceError，不向外透出原始异常。
"""
from __future__ import annotations

import logging
import threading

from app import anomaly, scoring
from app.errors import ErrorKind, RiskServiceError
from app.linkage import LINK_POLICY_VERSION, apply_tightening
from app.models import (
    Alert,
    AlertSnapshot,
    ApplicationRecord,
    RejectReasonCode,
    ScoreResult,
    Transaction,
)
from app.repository import Repository

# 这些原因表示输入本身不合法：结论是拒绝，但不形成“采用了交易告警”的风险决策，
# 因而不得抢占/关联 OPEN 告警（留给后续正常申请），也不做告警收紧。
_VALIDATION_FAILURE_CODES = frozenset(
    {
        RejectReasonCode.MISSING_FEATURE,
        RejectReasonCode.INVALID_FEATURE_TYPE,
        RejectReasonCode.FEATURE_OUT_OF_RANGE,
        RejectReasonCode.NON_FINITE_NUMERIC,
    }
)

logger = logging.getLogger("risk.service")


class RiskService:
    def __init__(self, repository: Repository | None = None) -> None:
        self._repo = repository or Repository()
        self._history: dict[str, list[Transaction]] = {}
        self._history_lock = threading.Lock()

    # ---- 申请提交（幂等 + 并发一致 + 告警联动收紧） ----

    def submit_application(
        self,
        application_id: str,
        customer_id: str,
        features: dict,
        model_version: str | None = None,
    ) -> ApplicationRecord:
        # 1) 幂等：已落库则只做内容一致性核对，绝不重算、不改关联、不被新告警影响
        existing = self._repo.get_application(application_id)
        if existing is not None:
            self._check_replay_consistency(existing, features, model_version)
            logger.info(
                "application=%s model=%s decision=%s idempotent=true linked_alerts=%s",
                application_id, existing.result.model_version,
                existing.result.decision.value, existing.alert_ids,
            )
            return existing

        # 2) 非法输入（NaN / Infinity 等非有限数值）单独识别，不当成正常低分放过，不落库
        bad_features = scoring.find_non_finite_features(features)
        if bad_features:
            logger.warning(
                "application=%s invalid_input non_finite_features=%s",
                application_id, bad_features,
            )
            raise RiskServiceError(
                ErrorKind.INVALID_INPUT,
                f"申请 {application_id} 含非有限数值特征: {bad_features}",
            )

        # 3) 基础评分（锁外纯计算）：严格校验 + 确定性打分，失败也归一化
        try:
            base = scoring.score_application(application_id, customer_id, features, model_version)
        except RiskServiceError:
            raise
        except Exception as exc:
            logger.exception("score compute failed: application_id=%s", application_id)
            raise RiskServiceError(ErrorKind.COMPUTE_FAILURE, "评分计算失败", cause=exc) from exc

        # 4) 锁内原子：（合法决策才）抢占该客户全部 OPEN 告警 -> 收紧 -> 落库
        validation_failed = any(
            r.code in _VALIDATION_FAILURE_CODES for r in base.reasons
        )

        def builder(claimed: tuple[Alert, ...]) -> ApplicationRecord:
            if validation_failed:
                final, impacts = base, ()
                snapshots: list[AlertSnapshot] = []
            else:
                final, impacts = apply_tightening(base, claimed)
                snapshots = [
                    AlertSnapshot(
                        alert_id=a.alert_id,
                        kind=a.kind,
                        tx_ids=a.tx_ids,
                        window_start=a.window_start,
                        window_end=a.window_end,
                    )
                    for a in claimed
                ]
            return ApplicationRecord(
                application_id=application_id,
                customer_id=customer_id,
                result=final,
                risk_record_id="",
                features=dict(features),
                alert_ids=[],  # 由 repository 在真正写入时回写
                link_policy_version=LINK_POLICY_VERSION,
                base_result=base,
                linked_impacts=list(impacts),
                linked_alert_snapshots=snapshots,
            )

        try:
            record, created = self._repo.create_application(
                application_id, customer_id, builder, claim_alerts=not validation_failed
            )
        except RiskServiceError:
            raise
        except Exception as exc:  # 持久化/构造失败归一化，底层细节不透出
            logger.exception("persist failed: application_id=%s", application_id)
            raise RiskServiceError(ErrorKind.PERSISTENCE_FAILURE, "风险记录持久化失败", cause=exc) from exc

        if not created:
            # 并发竞争失败：以既有结论为准并核对一致性，不允许返回矛盾结论
            self._check_replay_consistency(record, features, model_version)
            logger.info(
                "application=%s model=%s decision=%s idempotent=true linked_alerts=%s",
                application_id, record.result.model_version,
                record.result.decision.value, record.alert_ids,
            )
            return record

        linked = [i.alert_id for i in record.linked_impacts]
        logger.info(
            "application=%s model=%s base_decision=%s base_score=%d "
            "decision=%s score=%d linked_alerts=%s reasons=%s",
            application_id, record.result.model_version,
            base.decision.value, base.score,
            record.result.decision.value, record.result.score,
            linked,
            [r.code.value for r in record.result.reasons],
        )
        for impact in record.linked_impacts:
            logger.info(
                "application=%s linked_alert=%s kind=%s severity=%s penalty=%d steps=%d",
                application_id, impact.alert_id, impact.kind.value,
                impact.severity, impact.score_penalty, impact.ladder_steps,
            )
        return record

    @staticmethod
    def _check_replay_consistency(
        existing: ApplicationRecord, features: dict, model_version: str | None
    ) -> None:
        snap = scoring.snapshot_hash(features)
        version = model_version or existing.result.model_version
        if snap != existing.result.feature_snapshot_hash or version != existing.result.model_version:
            logger.warning(
                "application=%s concurrent_conflict existing_model=%s request_model=%s",
                existing.application_id, existing.result.model_version, version,
            )
            raise RiskServiceError(
                ErrorKind.CONCURRENT_CONFLICT,
                f"申请 {existing.application_id} 已存在且内容冲突 "
                f"(既有版本={existing.result.model_version}, 请求版本={version})",
            )

    # ---- 双向追溯 ----

    def get_application_detail(self, application_id: str) -> ApplicationRecord:
        record = self._repo.get_application(application_id)
        if record is None:
            raise RiskServiceError(ErrorKind.UPSTREAM_FAILURE, f"申请不存在: {application_id}")
        return record

    def get_alert_link(self, alert_id: str) -> Alert:
        alert = self._repo.get_alert(alert_id)
        if alert is None:
            raise RiskServiceError(ErrorKind.UPSTREAM_FAILURE, f"告警不存在: {alert_id}")
        return alert

    # ---- 交易接入（异常检测 + 告警去重） ----

    def ingest_transaction(self, tx: Transaction) -> list[Alert]:
        try:
            with self._history_lock:
                history = list(self._history.get(tx.customer_id, []))
            candidates = anomaly.detect(tx, history)
            saved: list[Alert] = []
            for alert in candidates:
                dedup_key = (alert.customer_id, alert.kind.value, alert.window_start)
                final, created = self._repo.save_alert_if_absent(dedup_key, alert)
                if created:
                    logger.info(
                        "alert=%s kind=%s customer=%s window=%s status=%s",
                        final.alert_id, final.kind.value, final.customer_id,
                        final.window_start, final.status.value,
                    )
                saved.append(final)
            with self._history_lock:
                self._history.setdefault(tx.customer_id, []).append(tx)
            return saved
        except RiskServiceError:
            raise
        except Exception as exc:
            logger.exception("ingest failed: tx_id=%s", tx.tx_id)
            raise RiskServiceError(ErrorKind.COMPUTE_FAILURE, "交易异常检测失败", cause=exc) from exc

    # ---- 历史结论按原版本 + 当时联动快照重放（逐位一致，不漂移） ----

    def replay(self, application_id: str) -> ScoreResult:
        record = self._repo.get_application(application_id)
        if record is None:
            raise RiskServiceError(ErrorKind.UPSTREAM_FAILURE, f"申请不存在: {application_id}")
        try:
            # 始终按当时的模型版本重算基础分
            base = scoring.score_application(
                record.application_id,
                record.customer_id,
                record.features,
                record.result.model_version,
            )
            if record.base_result is not None and base != record.base_result:
                raise RiskServiceError(
                    ErrorKind.COMPUTE_FAILURE,
                    f"重放基础结论与历史不一致: application_id={application_id}",
                )
            # 按当时关联的告警影响重建收紧结论；无联动的历史申请直接比对基础结论
            if record.linked_impacts:
                replayed = self._rebuild_tightened(record, base)
            else:
                replayed = base
            if replayed != record.result:
                raise RiskServiceError(
                    ErrorKind.COMPUTE_FAILURE,
                    f"重放结论与历史不一致: application_id={application_id}",
                )
        except RiskServiceError:
            raise
        except Exception as exc:
            logger.exception("replay failed: application_id=%s", application_id)
            raise RiskServiceError(ErrorKind.COMPUTE_FAILURE, "历史结论重放失败", cause=exc) from exc
        logger.info(
            "application=%s model=%s replay=ok decision=%s linked_alerts=%s",
            application_id, replayed.model_version,
            replayed.decision.value, record.alert_ids,
        )
        return replayed

    def _rebuild_tightened(
        self, record: ApplicationRecord, base: ScoreResult
    ) -> ScoreResult:
        """按决策当时保存的告警快照重建收紧结论（不依赖告警当前状态）。"""
        snapshot_alerts = tuple(
            Alert(
                alert_id=s.alert_id,
                customer_id=record.customer_id,
                kind=s.kind,
                tx_ids=s.tx_ids,
                window_start=s.window_start,
                window_end=s.window_end,
            )
            for s in record.linked_alert_snapshots
        )
        replayed, impacts = apply_tightening(base, snapshot_alerts)
        # 影响明细也必须逐位一致
        expected = [(i.alert_id, i.severity, i.score_penalty, i.ladder_steps)
                    for i in record.linked_impacts]
        actual = [(i.alert_id, i.severity, i.score_penalty, i.ladder_steps) for i in impacts]
        if actual != expected:
            raise RiskServiceError(
                ErrorKind.COMPUTE_FAILURE,
                f"重放联动影响与历史不一致: application_id={record.application_id}",
            )
        return replayed
