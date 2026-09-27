"""特征校验：缺失 / 越界 / 类型异常必须可区分，不得被默认值静默通过。"""
from __future__ import annotations

import math

from .domain import FeatureSnapshot, FeatureSpec
from .errors import ErrorCode, ServiceError

# 特征规格：评分所依赖的全部关键特征及其合法取值范围。
FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec("age", int, min_value=18, max_value=100),
    FeatureSpec("annual_income", (int, float), min_value=0.0, max_value=100_000_000.0),
    FeatureSpec("debt_ratio", (int, float), min_value=0.0, max_value=1.0),
    FeatureSpec("credit_history_months", int, min_value=0, max_value=1200),
    FeatureSpec("delinquency_count", int, min_value=0, max_value=100),
    FeatureSpec("existing_loans", int, min_value=0, max_value=50),
)


def _is_type_ok(value: object, spec: FeatureSpec) -> bool:
    # bool 是 int 的子类，但在特征语义里属于类型异常，必须显式拒绝。
    if isinstance(value, bool):
        return False
    return isinstance(value, spec.dtype)


def validate_features(snapshot: FeatureSnapshot) -> None:
    """校验特征快照。

    失败时抛出 ServiceError，ErrorCode 可区分：
    - FEATURE_MISSING       关键特征缺失
    - FEATURE_TYPE_INVALID  类型异常（含 bool 冒充数值、NaN/Inf）
    - FEATURE_OUT_OF_RANGE  取值越界
    任何异常都不得被默认值静默通过。
    """
    for spec in FEATURE_SPECS:
        if spec.name not in snapshot.features:
            raise ServiceError(
                ErrorCode.FEATURE_MISSING,
                f"application={snapshot.application_id} 缺少关键特征: {spec.name}",
            )
        value = snapshot.features[spec.name]
        if not _is_type_ok(value, spec):
            raise ServiceError(
                ErrorCode.FEATURE_TYPE_INVALID,
                f"application={snapshot.application_id} 特征 {spec.name} 类型异常: "
                f"期望 {spec.dtype}，实际 {type(value).__name__}({value!r})",
            )
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise ServiceError(
                ErrorCode.FEATURE_TYPE_INVALID,
                f"application={snapshot.application_id} 特征 {spec.name} 为 NaN/Inf",
            )
        if spec.min_value is not None and value < spec.min_value:
            raise ServiceError(
                ErrorCode.FEATURE_OUT_OF_RANGE,
                f"application={snapshot.application_id} 特征 {spec.name}={value} "
                f"低于下限 {spec.min_value}",
            )
        if spec.max_value is not None and value > spec.max_value:
            raise ServiceError(
                ErrorCode.FEATURE_OUT_OF_RANGE,
                f"application={snapshot.application_id} 特征 {spec.name}={value} "
                f"高于上限 {spec.max_value}",
            )
