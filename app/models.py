"""领域模型与类型定义。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Decision(str, Enum):
    APPROVE = "APPROVE"
    REVIEW = "REVIEW"
    DECLINE = "DECLINE"


class ScoreCategory(str, Enum):
    EXCELLENT = "EXCELLENT"
    GOOD = "GOOD"
    FAIR = "FAIR"
    POOR = "POOR"


class RejectReasonCode(str, Enum):
    MISSING_FEATURE = "MISSING_FEATURE"
    INVALID_FEATURE_TYPE = "INVALID_FEATURE_TYPE"
    FEATURE_OUT_OF_RANGE = "FEATURE_OUT_OF_RANGE"
    NON_FINITE_VALUE = "NON_FINITE_VALUE"
    LOW_SCORE = "LOW_SCORE"
    ALERT_LINKAGE = "ALERT_LINKAGE"


class AlertKind(str, Enum):
    DUPLICATE_TRANSACTION = "DUPLICATE_TRANSACTION"
    BURST = "BURST"
    BEHAVIOR_DEVIATION = "BEHAVIOR_DEVIATION"


class AlertStatus(str, Enum):
    OPEN = "OPEN"
    LINKED_TO_DECISION = "LINKED_TO_DECISION"


@dataclass(frozen=True)
class FeatureSpec:
    """单个特征的校验规格：类型、取值范围、是否必填。"""

    name: str
    dtype: type
    minimum: float | None = None
    maximum: float | None = None
    required: bool = True


@dataclass(frozen=True)
class RejectReason:
    code: RejectReasonCode
    feature: str | None
    detail: str


@dataclass(frozen=True)
class ScoreResult:
    application_id: str
    customer_id: str
    model_version: str
    score: int
    category: ScoreCategory
    decision: Decision
    reasons: tuple[RejectReason, ...]
    feature_snapshot_hash: str


@dataclass(frozen=True)
class Transaction:
    tx_id: str
    customer_id: str
    amount: float
    timestamp: float
    merchant: str


@dataclass(frozen=True)
class AlertLink:
    """一条告警被某次决策采用时的冻结快照：联动规则的依据与影响程度都固定下来。

    历史申请重放时只依赖该快照，不回读当前告警状态或最新联动策略，
    因此后续策略升级、告警状态变化都不会让历史结论漂移。
    """

    alert_id: str
    kind: AlertKind
    tx_ids: tuple[str, ...]
    window_start: float
    window_end: float
    score_penalty: int
    decision_floor: Decision
    reason_detail: str


@dataclass
class Alert:
    alert_id: str
    customer_id: str
    kind: AlertKind
    tx_ids: tuple[str, ...]
    window_start: float
    window_end: float
    status: AlertStatus = AlertStatus.OPEN
    linked_application_id: str | None = None


@dataclass
class ApplicationRecord:
    """一份申请的唯一最终结论与关联风险记录。"""

    application_id: str
    customer_id: str
    result: ScoreResult
    risk_record_id: str
    features: dict = field(default_factory=dict)  # 原始特征快照，用于按原版本重放
    alert_ids: list[str] = field(default_factory=list)
    alert_links: tuple[AlertLink, ...] = ()  # 决策时冻结的联动快照（历史不可变）
