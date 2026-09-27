"""归一化错误定义：底层异常不得直接透出，统一映射为可区分的错误类别。"""
from __future__ import annotations

from enum import Enum


class ErrorKind(str, Enum):
    # 特征校验类
    MISSING_FEATURE = "MISSING_FEATURE"
    INVALID_FEATURE_TYPE = "INVALID_FEATURE_TYPE"
    FEATURE_OUT_OF_RANGE = "FEATURE_OUT_OF_RANGE"
    # 模型类
    UNKNOWN_MODEL_VERSION = "UNKNOWN_MODEL_VERSION"
    # 并发类
    CONCURRENT_CONFLICT = "CONCURRENT_CONFLICT"
    # 基础设施类
    UPSTREAM_FAILURE = "UPSTREAM_FAILURE"
    COMPUTE_FAILURE = "COMPUTE_FAILURE"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"


class RiskServiceError(Exception):
    """对外统一暴露的归一化错误。"""

    def __init__(self, kind: ErrorKind, detail: str, cause: Exception | None = None):
        super().__init__(f"{kind.value}: {detail}")
        self.kind = kind
        self.detail = detail
        self.cause = cause
