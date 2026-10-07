"""评分服务：严格特征校验 + 确定性打分 + 拒绝原因输出。

约定：
- 缺失 / 类型异常 / 越界 分别映射到不同的 RejectReasonCode，不得用默认值静默通过；
- 类别与判定阈值固定（见 README），同一快照 + 同一版本结论完全一致。
"""
from __future__ import annotations

import hashlib
import json
import math

from app.model_registry import FEATURE_SPECS, latest_version, score_features
from app.models import (
    Decision,
    RejectReason,
    RejectReasonCode,
    ScoreCategory,
    ScoreResult,
)

# 阈值语义：score >= APPROVE_MIN 通过；[REVIEW_MIN, APPROVE_MIN) 人工复核；否则拒绝
APPROVE_MIN = 650
REVIEW_MIN = 500

_CATEGORY_BANDS: tuple[tuple[int, ScoreCategory], ...] = (
    (750, ScoreCategory.EXCELLENT),
    (650, ScoreCategory.GOOD),
    (500, ScoreCategory.FAIR),
    (0, ScoreCategory.POOR),
)


def snapshot_hash(features: dict) -> str:
    """特征快照的确定性哈希：键排序 + 类型标注，同一份快照哈希必须一致。"""
    canonical = json.dumps(
        {k: [type(v).__name__, v] for k, v in sorted(features.items())},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_type_ok(value: object, dtype: type | tuple) -> bool:
    # bool 是 int 的子类，但业务上不接受布尔充当数值
    if isinstance(value, bool):
        return False
    return isinstance(value, dtype)


def validate_features(features: dict) -> list[RejectReason]:
    """严格校验特征，返回全部可区分的拒绝原因（空列表表示校验通过）。"""
    reasons: list[RejectReason] = []
    for spec in FEATURE_SPECS:
        if spec.name not in features or features[spec.name] is None:
            if spec.required:
                reasons.append(
                    RejectReason(RejectReasonCode.MISSING_FEATURE, spec.name, "关键特征缺失")
                )
            continue
        value = features[spec.name]
        # bool 业务上不接受；非有限数值（nan/inf/-inf，通常是 float）单独识别，
        # 优先于类型与越界检查，避免被当成普通低分或漏过比较
        if isinstance(value, bool):
            reasons.append(
                RejectReason(
                    RejectReasonCode.INVALID_FEATURE_TYPE,
                    spec.name,
                    "类型异常: 布尔值不得充当数值",
                )
            )
            continue
        if isinstance(value, float) and not math.isfinite(value):
            reasons.append(
                RejectReason(
                    RejectReasonCode.NON_FINITE_VALUE,
                    spec.name,
                    f"非有限数值: {value!r} 不是合法的有限数值",
                )
            )
            continue
        if not _is_type_ok(value, spec.dtype):
            reasons.append(
                RejectReason(
                    RejectReasonCode.INVALID_FEATURE_TYPE,
                    spec.name,
                    f"类型异常: 期望 {spec.dtype}，实际 {type(value).__name__}",
                )
            )
            continue
        numeric = float(value)
        if (spec.minimum is not None and numeric < spec.minimum) or (
            spec.maximum is not None and numeric > spec.maximum
        ):
            reasons.append(
                RejectReason(
                    RejectReasonCode.FEATURE_OUT_OF_RANGE,
                    spec.name,
                    f"取值越界: {value} 不在 [{spec.minimum}, {spec.maximum}]",
                )
            )
    return reasons


def categorize(score: int) -> ScoreCategory:
    for threshold, category in _CATEGORY_BANDS:
        if score >= threshold:
            return category
    return ScoreCategory.POOR


def decide(score: int) -> Decision:
    if score >= APPROVE_MIN:
        return Decision.APPROVE
    if score >= REVIEW_MIN:
        return Decision.REVIEW
    return Decision.DECLINE


def score_application(
    application_id: str,
    customer_id: str,
    features: dict,
    model_version: str | None = None,
) -> ScoreResult:
    """对申请打分。特征校验失败时返回带可区分拒绝原因的 DECLINE 结论，不抛底层异常。"""
    version = model_version or latest_version()
    reasons = validate_features(features)
    if reasons:
        return ScoreResult(
            application_id=application_id,
            customer_id=customer_id,
            model_version=version,
            score=0,
            category=ScoreCategory.POOR,
            decision=Decision.DECLINE,
            reasons=tuple(reasons),
            feature_snapshot_hash=snapshot_hash(features),
        )
    score, fired = score_features(version, features)
    score_reasons: tuple[RejectReason, ...] = ()
    if decide(score) is Decision.DECLINE:
        score_reasons = (
            RejectReason(
                RejectReasonCode.LOW_SCORE,
                None,
                f"分数 {score} 低于复核阈值 {REVIEW_MIN}；规则: {'; '.join(fired)}",
            ),
        )
    return ScoreResult(
        application_id=application_id,
        customer_id=customer_id,
        model_version=version,
        score=score,
        category=categorize(score),
        decision=decide(score),
        reasons=score_reasons,
        feature_snapshot_hash=snapshot_hash(features),
    )
