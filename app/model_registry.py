"""模型注册表：按版本保存评分模型，支持历史版本重放。

打分函数必须是 (model_version, features) 的纯函数：
同一特征快照 + 同一版本 => 完全一致的分数与命中规则。
"""
from __future__ import annotations

from app.errors import ErrorKind, RiskServiceError
from app.models import FeatureSpec

FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec("age", int, 18, 120),
    FeatureSpec("annual_income", (int, float), 0.0, 100_000_000.0),
    FeatureSpec("debt_ratio", (int, float), 0.0, 1.0),
    FeatureSpec("credit_history_months", int, 0, 1200),
    FeatureSpec("delinquency_count", int, 0, 100),
)

_BASE_SCORE = 600

# 每个版本：特征名 -> (每单位权重, 参考基准)。score += weight * (value - baseline)
_V1_WEIGHTS: dict[str, tuple[float, float]] = {
    "age": (0.5, 30.0),
    "annual_income": (0.0005, 100_000.0),
    "debt_ratio": (-200.0, 0.3),
    "credit_history_months": (0.2, 60.0),
    "delinquency_count": (-40.0, 0.0),
}

# v2 调整权重并加重逾期惩罚 —— 用于验证版本重放语义
_V2_WEIGHTS: dict[str, tuple[float, float]] = {
    "age": (0.3, 30.0),
    "annual_income": (0.0004, 100_000.0),
    "debt_ratio": (-260.0, 0.3),
    "credit_history_months": (0.25, 60.0),
    "delinquency_count": (-70.0, 0.0),
}

_MODELS: dict[str, dict[str, tuple[float, float]]] = {
    "v1": _V1_WEIGHTS,
    "v2": _V2_WEIGHTS,
}

_SCORE_MIN, _SCORE_MAX = 300, 900


def available_versions() -> list[str]:
    return sorted(_MODELS)


def latest_version() -> str:
    return available_versions()[-1]


def score_features(model_version: str, features: dict) -> tuple[int, tuple[str, ...]]:
    """按指定模型版本对特征打分，返回 (分数, 命中规则)。版本未知抛 UNKNOWN_MODEL_VERSION。

    前置条件：features 已通过严格校验。打分过程不做任何隐式默认值填充。
    """
    weights = _MODELS.get(model_version)
    if weights is None:
        raise RiskServiceError(
            ErrorKind.UNKNOWN_MODEL_VERSION,
            f"未知模型版本: {model_version!r}，可用版本: {available_versions()}",
        )
    total = float(_BASE_SCORE)
    fired: list[str] = []
    for name in sorted(weights):
        weight, baseline = weights[name]
        contribution = weight * (float(features[name]) - baseline)
        total += contribution
        fired.append(f"{name}:{'+' if contribution >= 0 else ''}{contribution:.2f}")
    score = int(round(min(_SCORE_MAX, max(_SCORE_MIN, total))))
    return score, tuple(fired)
