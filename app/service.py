"""编排层：申请提交幂等、告警联动收紧、错误归一化。

一致性约定：
- 同一 application_id 重复/并发提交：内容一致则返回同一份最终结论（幂等成功）；
  内容冲突（特征快照或模型版本不同）则抛 CONCURRENT_CONFLICT，绝不返回矛盾结论；
- 申请落库与「认领该客户当前未关联告警」在存储层同一把锁内原子完成：
  不漏关联、不重复关联、不会把告警挂到别的申请；
- 每条申请在决策时冻结所采用告警的联动快照（AlertLink），历史结论与关联关系
  从此固定；后续新告警不会回头改写，重放按原版本 + 冻结快照逐位一致；
- 所有底层异常归一化为 RiskServiceError，不向外透出原始异常。
"""
from __future__ import annotations

import logging
import threading

from app import anomaly, linkage, scoring
from app.errors import ErrorKind, RiskServiceError
from app.models import Alert, ApplicationRecord, ScoreResult, Transaction
from app.repository import Repository

logger = logging.getLogger("risk.service")


class RiskService:
    def __init__(self, repository: Repository | None = None) -> None:
        self._repo = repository or Repository()
        self._history: dict[str, list[Transaction]] = {}
        self._history_lock = threading.Lock()

    # ---- 申请提交（幂等 + 并发一致） ----

    def submit_application(
        self,
        application_id: str,
        customer_id: str,
        features: dict,
        model_version: str | None = None,
    ) -> ApplicationRecord:
        existing = self._repo.get_application(application_id)
        if existing is not None:
            self._check_replay_consistency(existing, features, model_version)
            return existing
        try:
            base = scoring.score_application(application_id, customer_id, features, model_version)
        except RiskServiceError:
            raise
        except Exception as exc:  # 计算失败归一化
            logger.exception("score compute failed: application_id=%s", application_id)
            raise RiskServiceError(ErrorKind.COMPUTE_FAILURE, "评分计算失败", cause=exc) from exc

        features_snapshot = dict(features)

        def build_record(open_alerts: list[Alert]) -> ApplicationRecord:
            # 在存储锁内、依据锁内快照的 OPEN 告警构建最终结论
            links = linkage.build_links(open_alerts)
            final_result = linkage.apply(base, links)
            return ApplicationRecord(
                application_id=application_id,
                customer_id=customer_id,
                result=final_result,
                risk_record_id="",
                features=features_snapshot,
                alert_links=links,
            )

        try:
            final, created = self._repo.create_application_with_alerts(
                application_id, customer_id, features_snapshot, build_record
            )
        except RiskServiceError:
            raise
        except Exception as exc:  # 持久化/联动失败归一化
            logger.exception("persist failed: application_id=%s", application_id)
            raise RiskServiceError(ErrorKind.PERSISTENCE_FAILURE, "风险记录持久化失败", cause=exc) from exc
        if not created:
            # 并发竞争失败：核对既有结论与本请求是否一致
            self._check_replay_consistency(final, features, model_version)
        else:
            logger.info(
                "application=%s customer=%s model=%s decision=%s score=%d "
                "linked_alerts=%s reasons=%s",
                application_id,
                customer_id,
                final.result.model_version,
                final.result.decision.value,
                final.result.score,
                ",".join(final.alert_ids) or "-",
                "; ".join(r.code.value for r in final.result.reasons) or "-",
            )
        return final

    @staticmethod
    def _check_replay_consistency(
        existing: ApplicationRecord, features: dict, model_version: str | None
    ) -> None:
        snap = scoring.snapshot_hash(features)
        version = model_version or existing.result.model_version
        if snap != existing.result.feature_snapshot_hash or version != existing.result.model_version:
            raise RiskServiceError(
                ErrorKind.CONCURRENT_CONFLICT,
                f"申请 {existing.application_id} 已存在且内容冲突 "
                f"(既有版本={existing.result.model_version}, 请求版本={version})",
            )

    # ---- 交易接入（异常检测 + 告警去重 + 状态联动） ----

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
                        "alert=%s kind=%s customer=%s window=%s",
                        final.alert_id, final.kind.value, final.customer_id, final.window_start,
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

    # ---- 双向追溯 ----

    def get_application_alerts(self, application_id: str) -> list[Alert]:
        """申请 -> 关联了哪些告警。"""
        record = self._repo.get_application(application_id)
        if record is None:
            raise RiskServiceError(ErrorKind.UPSTREAM_FAILURE, f"申请不存在: {application_id}")
        return self._repo.list_application_alerts(application_id)

    def get_alert_linked_application(self, alert_id: str) -> ApplicationRecord | None:
        """告警 -> 关联了哪一次决策；未关联返回 None。"""
        alert = self._repo.get_alert(alert_id)
        if alert is None:
            raise RiskServiceError(ErrorKind.UPSTREAM_FAILURE, f"告警不存在: {alert_id}")
        if alert.linked_application_id is None:
            return None
        return self._repo.get_application(alert.linked_application_id)

    # ---- 历史结论按原版本 + 冻结联动快照重放 ----

    def replay(self, application_id: str) -> ScoreResult:
        record = self._repo.get_application(application_id)
        if record is None:
            raise RiskServiceError(ErrorKind.UPSTREAM_FAILURE, f"申请不存在: {application_id}")
        # 基线按原版本重算；联动部分只叠加决策时冻结的快照，
        # 不查询当前告警状态与最新策略，因此后续任何变化都不会令历史漂移
        base = scoring.score_application(
            record.application_id,
            record.customer_id,
            record.features,
            record.result.model_version,
        )
        result = linkage.apply(base, record.alert_links)
        if result != record.result:
            logger.error(
                "replay drift: application=%s model=%s",
                application_id,
                record.result.model_version,
            )
            raise RiskServiceError(
                ErrorKind.COMPUTE_FAILURE,
                f"重放结论与历史不一致: application_id={application_id}",
            )
        logger.info(
            "replay application=%s model=%s decision=%s linked_alerts=%s",
            application_id,
            result.model_version,
            result.decision.value,
            ",".join(a.alert_id for a in record.alert_links) or "-",
        )
        return result
