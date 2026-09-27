"""交易流异常检测：重复、突发、行为偏离。

确定性约定：
- 窗口按 WINDOW_SECONDS 对齐（window_start = ts // W * W），同一窗口内同类告警去重；
- 检测只依赖 (transaction, history) 的确定性规则，不引入随机或时钟漂移。
"""
from __future__ import annotations

import hashlib

from app.models import Alert, AlertKind, Transaction

WINDOW_SECONDS = 600.0
BURST_THRESHOLD = 5          # 窗口内交易数达到该值视为突发
DEVIATION_FACTOR = 3.0       # 金额超过历史均值该倍数视为偏离
DEVIATION_MIN_HISTORY = 3    # 至少这么多历史交易才做偏离判定


def window_start(ts: float) -> float:
    return ts - (ts % WINDOW_SECONDS)


def _alert_id(customer_id: str, kind: AlertKind, wstart: float) -> str:
    raw = f"{customer_id}|{kind.value}|{wstart}"
    return "AL-" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def detect(transaction: Transaction, history: list[Transaction]) -> list[Alert]:
    """对单笔交易结合历史做异常检测，返回（可能为空的）告警列表。"""
    wstart = window_start(transaction.timestamp)
    wend = wstart + WINDOW_SECONDS
    in_window = [t for t in history if wstart <= t.timestamp < wend]
    alerts: list[Alert] = []

    # 1) 重复交易：同窗口内同商户同金额
    dups = [
        t for t in in_window
        if t.merchant == transaction.merchant and t.amount == transaction.amount
    ]
    if dups:
        alerts.append(Alert(
            alert_id=_alert_id(transaction.customer_id, AlertKind.DUPLICATE_TRANSACTION, wstart),
            customer_id=transaction.customer_id,
            kind=AlertKind.DUPLICATE_TRANSACTION,
            tx_ids=tuple(sorted(t.tx_id for t in dups) + [transaction.tx_id]),
            window_start=wstart,
            window_end=wend,
        ))

    # 2) 突发：窗口内交易数（含当前）达到阈值
    if len(in_window) + 1 >= BURST_THRESHOLD:
        alerts.append(Alert(
            alert_id=_alert_id(transaction.customer_id, AlertKind.BURST, wstart),
            customer_id=transaction.customer_id,
            kind=AlertKind.BURST,
            tx_ids=tuple(sorted(t.tx_id for t in in_window) + [transaction.tx_id]),
            window_start=wstart,
            window_end=wend,
        ))

    # 3) 行为偏离：金额显著高于历史均值
    if len(history) >= DEVIATION_MIN_HISTORY:
        avg = sum(t.amount for t in history) / len(history)
        if avg > 0 and transaction.amount > DEVIATION_FACTOR * avg:
            alerts.append(Alert(
                alert_id=_alert_id(transaction.customer_id, AlertKind.BEHAVIOR_DEVIATION, wstart),
                customer_id=transaction.customer_id,
                kind=AlertKind.BEHAVIOR_DEVIATION,
                tx_ids=(transaction.tx_id,),
                window_start=wstart,
                window_end=wend,
            ))
    return alerts
