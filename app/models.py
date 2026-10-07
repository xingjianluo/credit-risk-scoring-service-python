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
    NON_FINITE_NUMERIC = "NON_FINITE_NUMERIC"
    LOW_SCORE = "LOW_SCORE"
    ALERT_TIGHTENED = "ALERT_TIGHTENED"


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
class AlertImpact:
    """单条告警对本次决策的联动影响（决策当时快照的一部分，重放逐位一致）。"""

    alert_id: str
    kind: AlertKind
    severity: str          # LOW / MEDIUM / HIGH
    score_penalty: int     # 扣分
    ladder_steps: int      # 判定阶梯抬升级数
    detail: str            # 人类可读的联动理由


@dataclass
class Alert:
    alert_id: str
    customer_id: str
    kind: AlertKind
    tx_ids: tuple[str, ...]
    window_start: float
    window_end: float
    status: AlertStatus = AlertStatus.OPEN
    linked_application_id: str | None = None  # 反查：该告警关联到了哪一次决策


@dataclass(frozen=True)
class AlertSnapshot:
    """决策当时采用的告警不可变快照：重放只依赖它，告警后续变化不影响历史。"""

    alert_id: str
    kind: AlertKind
    tx_ids: tuple[str, ...]
    window_start: float
    window_end: float


@dataclass
class ApplicationRecord:
    """一份申请的唯一最终结论与关联风险记录。"""

    application_id: str
    customer_id: str
    result: ScoreResult
    risk_record_id: str
    features: dict = field(default_factory=dict)  # 原始特征快照，用于按原版本重放
    alert_ids: list[str] = field(default_factory=list)
    # 联动策略版本：历史申请按当时版本重放，后续策略升级不影响历史结论
    link_policy_version: str = "link-v1"
    # 未联动告警收紧前的原始打分结论（决策当时快照）
    base_result: ScoreResult | None = None
    # 本次实际采用的告警及其影响明细，顺序确定（按 alert_id 排序）
    linked_impacts: list[AlertImpact] = field(default_factory=list)
    # 决策当时的告警快照，重放只用它重建逐条联动理由（逐位一致、不随现状漂移）
    linked_alert_snapshots: list[AlertSnapshot] = field(default_factory=list)
