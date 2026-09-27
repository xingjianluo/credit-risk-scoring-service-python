"""领域模型：特征快照、评分结论、风险记录、交易与告警。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Decision(enum.StrEnum):
    APPROVE = "APPROVE"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


class AlertStatus(enum.StrEnum):
    OPEN = "OPEN"
    LINKED = "LINKED"  # 已与评分结论联动


@dataclass(frozen=True)
class FeatureSpec:
    """单个特征的校验规则。"""

    name: str
    dtype: type
    min_value: float | None = None
    max_value: float | None = None


@dataclass(frozen=True)
class FeatureSnapshot:
    """客户特征快照：同一快照 + 同一模型版本 => 结论完全一致。"""

    application_id: str
    features: dict


@dataclass(frozen=True)
class RejectReason:
    code: str
    detail: str


@dataclass(frozen=True)
class ScoreDecision:
    application_id: str
    model_version: str
    score: float
    decision: Decision
    reasons: tuple[RejectReason, ...] = ()


@dataclass
class RiskRecord:
    """一次申请唯一对应一条风险记录。"""

    application_id: str
    model_version: str
    decision: Decision
    score: float
    reasons: list[RejectReason] = field(default_factory=list)


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    customer_id: str
    amount: float
    timestamp: float


@dataclass
class Alert:
    alert_id: str
    customer_id: str
    rule: str
    transaction_ids: tuple[str, ...]
    status: AlertStatus = AlertStatus.OPEN
