"""交易告警与申请决策的联动收紧策略（link-v1）。

纯函数、可复现：同一份基础评分结论 + 同一批告警快照 => 完全一致的收紧后结论
（分数、类别、判定、逐条联动理由）。策略本身带版本号，历史申请按当时版本重放。

收紧手段（两者取更严）：
1. 分数扣减：按严重度扣分，扣分后按固定阈值重新归类与判定；
2. 判定阶梯升级：按严重度抬升判定下限（APPROVE -> REVIEW -> DECLINE），
   保证“原本能放过”的申请在有告警时必然至少进入人工复核或拒绝。
"""
from __future__ import annotations

from app.models import (
    Alert,
    AlertImpact,
    AlertKind,
    Decision,
    RejectReason,
    RejectReasonCode,
    ScoreResult,
)
from app.scoring import categorize, decide

LINK_POLICY_VERSION = "link-v1"

_SCORE_MIN, _SCORE_MAX = 300, 900

# 每类告警 -> (严重度, 扣分, 阶梯升级步数)；一步 = APPROVE→REVIEW，两步 = APPROVE→DECLINE
_SEVERITY_TABLE: dict[AlertKind, tuple[str, int, int]] = {
    AlertKind.DUPLICATE_TRANSACTION: ("LOW", 80, 1),
    AlertKind.BURST: ("MEDIUM", 160, 2),
    AlertKind.BEHAVIOR_DEVIATION: ("HIGH", 250, 3),
}

_SEVERITY_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
_LADDER: tuple[Decision, ...] = (Decision.APPROVE, Decision.REVIEW, Decision.DECLINE)


def alert_severity(kind: AlertKind) -> str:
    """告警类别 -> 严重度（LOW / MEDIUM / HIGH）。"""
    return _SEVERITY_TABLE[kind][0]


def build_impact(alert: Alert) -> AlertImpact:
    """单条告警 -> 确定性的联动影响（扣分、阶梯升级、可读理由）。"""
    severity, penalty, steps = _SEVERITY_TABLE[alert.kind]
    detail = (
        f"告警 {alert.alert_id}（{alert.kind.value}，严重度 {severity}）"
        f"来自窗口 [{alert.window_start:.0f}, {alert.window_end:.0f})、"
        f"关联交易 {len(alert.tx_ids)} 笔：扣 {penalty} 分、"
        f"判定阶梯抬升 {steps} 级"
    )
    return AlertImpact(
        alert_id=alert.alert_id,
        kind=alert.kind,
        severity=severity,
        score_penalty=penalty,
        ladder_steps=steps,
        detail=detail,
    )


def _ladder_floor(steps: int) -> Decision:
    return _LADDER[min(steps, len(_LADDER) - 1)]


def _stricter(a: Decision, b: Decision) -> Decision:
    return a if _LADDER.index(a) >= _LADDER.index(b) else b


def apply_tightening(
    base: ScoreResult, alerts: tuple[Alert, ...]
) -> tuple[ScoreResult, tuple[AlertImpact, ...]]:
    """把未关联告警纳入基础结论并收紧，返回 (最终结论, 逐条影响)。

    无告警（或基础结论本身因特征校验失败而不参与联动）时原样返回。
    """
    if not alerts:
        return base, ()

    ordered = sorted(alerts, key=lambda a: a.alert_id)
    impacts = tuple(build_impact(a) for a in ordered)

    total_penalty = sum(i.score_penalty for i in impacts)
    total_steps = sum(i.ladder_steps for i in impacts)
    worst_severity = max((i.severity for i in impacts), key=lambda s: _SEVERITY_ORDER[s])

    adjusted_score = min(_SCORE_MAX, max(_SCORE_MIN, base.score - total_penalty))
    score_decision = decide(adjusted_score)
    ladder_decision = _ladder_floor(total_steps)
    final_decision = _stricter(score_decision, ladder_decision)

    alert_reasons = tuple(
        RejectReason(RejectReasonCode.ALERT_TIGHTENED, None, impact.detail)
        for impact in impacts
    )
    summary = RejectReason(
        RejectReasonCode.ALERT_TIGHTENED,
        None,
        f"命中 {len(impacts)} 条未关联交易告警（最高严重度 {worst_severity}）："
        f"基础判定 {base.decision.value}（{base.score} 分）经合计扣 {total_penalty} 分、"
        f"阶梯抬升 {total_steps} 级后收紧为 {final_decision.value}（{adjusted_score} 分）",
    )

    tightened = ScoreResult(
        application_id=base.application_id,
        customer_id=base.customer_id,
        model_version=base.model_version,
        score=adjusted_score,
        category=categorize(adjusted_score),
        decision=final_decision,
        reasons=base.reasons + alert_reasons + (summary,),
        feature_snapshot_hash=base.feature_snapshot_hash,
    )
    return tightened, impacts
