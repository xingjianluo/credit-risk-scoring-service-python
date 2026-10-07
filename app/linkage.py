"""交易告警 -> 申请决策的联动收紧策略（纯函数）。

确定性约定：
- 策略只依赖 (基线评分结果, 决策时刻的 OPEN 告警集合)，同一输入必得同一收紧结论；
- 每条被采用告警的影响（扣分、判定下限、理由）在决策时冻结进 AlertLink，
  历史申请重放只读冻结快照，不回读本模块的当前策略，策略升级不会改写历史；
- 收紧只可能让判定更严格（APPROVE -> REVIEW/DECLINE），绝不放松。
"""
from __future__ import annotations

from app.model_registry import SCORE_MIN
from app.models import (
    Alert,
    AlertKind,
    AlertLink,
    Decision,
    RejectReason,
    RejectReasonCode,
    ScoreResult,
)
from app.scoring import categorize, decide

# 每类告警：扣分幅度 + 判定下限（最终判定不得宽松于该下限）
_POLICY: dict[AlertKind, tuple[int, Decision, str]] = {
    AlertKind.DUPLICATE_TRANSACTION: (
        40,
        Decision.REVIEW,
        "重复交易告警：存在同窗口同商户同金额交易，疑似重复扣款或盗刷",
    ),
    AlertKind.BURST: (
        80,
        Decision.REVIEW,
        "交易突发告警：短时间内交易频次异常，疑似账户失控",
    ),
    AlertKind.BEHAVIOR_DEVIATION: (
        150,
        Decision.DECLINE,
        "行为偏离告警：单笔金额显著偏离历史水平，欺诈风险高",
    ),
}

# 判定严格度排序，用于多条告警取下限
_SEVERITY: dict[Decision, int] = {
    Decision.APPROVE: 0,
    Decision.REVIEW: 1,
    Decision.DECLINE: 2,
}


def _stricter(a: Decision, b: Decision) -> Decision:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def build_links(alerts: list[Alert]) -> tuple[AlertLink, ...]:
    """把决策时刻采用的告警冻结成联动快照，按 alert_id 排序保证确定性。"""
    links: list[AlertLink] = []
    for alert in sorted(alerts, key=lambda a: a.alert_id):
        penalty, floor, policy_desc = _POLICY[alert.kind]
        links.append(
            AlertLink(
                alert_id=alert.alert_id,
                kind=alert.kind,
                tx_ids=alert.tx_ids,
                window_start=alert.window_start,
                window_end=alert.window_end,
                score_penalty=penalty,
                decision_floor=floor,
                reason_detail=(
                    f"{policy_desc}；影响: 评分扣减 {penalty} 分且判定不宽松于 {floor.value}"
                ),
            )
        )
    return tuple(links)


def linkage_reasons(links: tuple[AlertLink, ...]) -> tuple[RejectReason, ...]:
    return tuple(
        RejectReason(
            code=RejectReasonCode.ALERT_LINKAGE,
            feature=None,
            detail=(
                f"受告警 {link.alert_id}（类别 {link.kind.value}，关联交易 "
                f"{','.join(link.tx_ids)}）联动收紧: {link.reason_detail}"
            ),
        )
        for link in links
    )


def apply(base: ScoreResult, links: tuple[AlertLink, ...]) -> ScoreResult:
    """在基线评分结论上施加告警联动收紧，返回新的最终结论。

    两步收紧取更严者：扣减后分数对应的判定，与所有告警判定下限的最严值。
    无联动告警时原样返回基线结论（保持与原有行为逐位一致）。
    """
    if not links:
        return base
    adjusted_score = max(SCORE_MIN, base.score - sum(link.score_penalty for link in links))
    adjusted_decision = decide(adjusted_score)
    floor = Decision.APPROVE
    for link in links:
        floor = _stricter(floor, link.decision_floor)
    final_decision = _stricter(adjusted_decision, floor)
    reasons = tuple(base.reasons) + linkage_reasons(links)
    return ScoreResult(
        application_id=base.application_id,
        customer_id=base.customer_id,
        model_version=base.model_version,
        score=adjusted_score,
        category=categorize(adjusted_score),
        decision=final_decision,
        reasons=reasons,
        feature_snapshot_hash=base.feature_snapshot_hash,
    )
