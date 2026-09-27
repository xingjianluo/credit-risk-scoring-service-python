"""确定性评分引擎：同一快照 + 同一模型版本 => 完全一致结论；历史版本可重放。"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .domain import Decision, FeatureSnapshot, RejectReason, ScoreDecision
from .errors import ErrorCode, ServiceError
from .features import FEATURE_SPECS, validate_features


@dataclass(frozen=True)
class _Model:
    """不可变模型定义：注册后参数永不修改，保证历史结论可重放。"""

    version: str
    weights: tuple[tuple[str, float], ...]  # 固定顺序，保证浮点求和顺序确定
    bias: float
    approve_threshold: float  # score >= 该值 => APPROVE
    review_threshold: float   # score >= 该值 => REVIEW，否则 REJECT


class ModelRegistry:
    """模型版本注册表：版本不可变，注册后永久可重放。"""

    def __init__(self) -> None:
        self._models: dict[str, _Model] = {}

    def register(self, version: str, weights: dict[str, float], bias: float,
                 approve_threshold: float, review_threshold: float) -> None:
        if not (0.0 <= review_threshold <= approve_threshold <= 1.0):
            raise ServiceError(
                ErrorCode.INTERNAL_ERROR,
                f"阈值语义非法: 需满足 0 <= review({review_threshold}) "
                f"<= approve({approve_threshold}) <= 1",
            )
        known = {spec.name for spec in FEATURE_SPECS}
        unknown = set(weights) - known
        if unknown:
            raise ServiceError(
                ErrorCode.INTERNAL_ERROR,
                f"模型 {version} 引用了未知特征: {sorted(unknown)}",
            )
        existing = self._models.get(version)
        model = _Model(
            version=version,
            weights=tuple((name, float(weights.get(name, 0.0)))
                          for name in (s.name for s in FEATURE_SPECS)),
            bias=float(bias),
            approve_threshold=float(approve_threshold),
            review_threshold=float(review_threshold),
        )
        if existing is not None and existing != model:
            raise ServiceError(
                ErrorCode.MODEL_VERSION_UNKNOWN,
                f"模型版本 {version} 已注册且参数不同，版本不可变，禁止覆盖",
            )
        self._models[version] = model

    def versions(self) -> tuple[str, ...]:
        return tuple(sorted(self._models))

    def score(self, snapshot: FeatureSnapshot, version: str) -> ScoreDecision:
        """确定性评分：先校验特征（异常可区分），再按固定顺序求和。

        同一特征快照 + 同一模型版本，在任何请求/并发下结论完全一致。
        """
        model = self._models.get(version)
        if model is None:
            raise ServiceError(
                ErrorCode.MODEL_VERSION_UNKNOWN,
                f"未知模型版本: {version}（可用版本: {list(self.versions())}）",
            )
        validate_features(snapshot)
        try:
            linear = model.bias
            for name, weight in model.weights:  # 固定顺序 => 浮点结果确定
                linear += weight * float(snapshot.features[name])
            score = 1.0 / (1.0 + math.exp(-linear))
        except (OverflowError, ValueError, TypeError) as exc:
            raise ServiceError(
                ErrorCode.COMPUTE_FAILURE,
                f"application={snapshot.application_id} 评分计算失败",
                cause=exc,
            ) from exc

        reasons: list[RejectReason] = []
        for name, weight in model.weights:
            contribution = weight * float(snapshot.features[name])
            if contribution < 0:  # 负向贡献特征作为拒绝/复核依据
                reasons.append(RejectReason(
                    code="NEGATIVE_FACTOR",
                    detail=f"{name}={snapshot.features[name]} 贡献 {contribution:+.6f}",
                ))
        reasons.sort(key=lambda r: r.detail)  # 排序保证输出顺序确定

        if score >= model.approve_threshold:
            decision = Decision.APPROVE
        elif score >= model.review_threshold:
            decision = Decision.REVIEW
        else:
            decision = Decision.REJECT
        return ScoreDecision(
            application_id=snapshot.application_id,
            model_version=version,
            score=score,
            decision=decision,
            reasons=tuple(reasons),
        )


def default_registry() -> ModelRegistry:
    """内置模型版本：v1 与 v2 并存，历史结论可按 v1 重放。"""
    registry = ModelRegistry()
    registry.register(
        "v1",
        weights={
            "age": 0.01,
            "annual_income": 0.00002,
            "debt_ratio": -3.0,
            "credit_history_months": 0.005,
            "delinquency_count": -0.8,
            "existing_loans": -0.2,
        },
        bias=-1.0,
        approve_threshold=0.65,
        review_threshold=0.40,
    )
    registry.register(
        "v2",  # 模型升级：更看重历史逾期与收入
        weights={
            "age": 0.005,
            "annual_income": 0.00003,
            "debt_ratio": -2.5,
            "credit_history_months": 0.004,
            "delinquency_count": -1.2,
            "existing_loans": -0.15,
        },
        bias=-1.2,
        approve_threshold=0.70,
        review_threshold=0.45,
    )
    return registry
