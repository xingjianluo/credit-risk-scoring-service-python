"""申请提交服务：幂等 + 并发一致，同一申请只产生一份最终结论。"""
from __future__ import annotations

from .domain import FeatureSnapshot, RiskRecord, ScoreDecision
from .errors import ErrorCode, ServiceError
from .scoring import ModelRegistry
from .store import RiskRecordStore


class ApplicationService:
    """同一 application_id 只产生一份最终结论和一条风险记录。

    - 重复/并发提交（同模型版本）：返回已存结论，不重复计数、不重复占用。
    - 同一 application_id 换模型版本提交：视为并发/重试冲突，
      抛出 APPLICATION_CONFLICT，绝不返回互相矛盾的结论。
    - 历史结论重放：用 record 中保存的模型版本重新评分即可复现。
    """

    def __init__(self, registry: ModelRegistry, store: RiskRecordStore) -> None:
        self._registry = registry
        self._store = store

    def submit(self, snapshot: FeatureSnapshot, model_version: str) -> ScoreDecision:
        # 快路径：已有最终结论，直接幂等返回（不重新计算、不重复计数）。
        existing = self._store.get(snapshot.application_id)
        if existing is not None:
            self._check_version(existing, model_version)
            return self._to_decision(existing)

        # 计算结论（特征异常在此抛出可区分的 ServiceError，不落库）。
        decision = self._registry.score(snapshot, model_version)
        record = RiskRecord(
            application_id=decision.application_id,
            model_version=decision.model_version,
            decision=decision.decision,
            score=decision.score,
            reasons=list(decision.reasons),
        )
        inserted, stored = self._store.insert_if_absent(record)
        if not inserted:
            # 并发竞争失败：已有其他请求先写入，以其为唯一最终结论。
            self._check_version(stored, model_version)
            return self._to_decision(stored)
        return decision

    def replay(self, application_id: str, snapshot: FeatureSnapshot) -> ScoreDecision:
        """按风险记录中保存的原模型版本重放历史结论。"""
        record = self._store.get(application_id)
        if record is None:
            raise ServiceError(
                ErrorCode.APPLICATION_CONFLICT,
                f"application={application_id} 不存在风险记录，无法重放",
            )
        return self._registry.score(snapshot, record.model_version)

    @staticmethod
    def _check_version(record: RiskRecord, requested_version: str) -> None:
        if record.model_version != requested_version:
            raise ServiceError(
                ErrorCode.APPLICATION_CONFLICT,
                f"application={record.application_id} 已按模型版本 "
                f"{record.model_version} 定结论，与本次请求版本 "
                f"{requested_version} 冲突",
            )

    @staticmethod
    def _to_decision(record: RiskRecord) -> ScoreDecision:
        return ScoreDecision(
            application_id=record.application_id,
            model_version=record.model_version,
            score=record.score,
            decision=record.decision,
            reasons=tuple(record.reasons),
        )
