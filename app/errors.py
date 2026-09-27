"""归一化错误定义：底层异常不得直接透出到对外结果。"""
from __future__ import annotations

import enum


class ErrorCode(enum.StrEnum):
    """对外可区分的归一化错误码。"""

    FEATURE_MISSING = "FEATURE_MISSING"
    FEATURE_OUT_OF_RANGE = "FEATURE_OUT_OF_RANGE"
    FEATURE_TYPE_INVALID = "FEATURE_TYPE_INVALID"
    MODEL_VERSION_UNKNOWN = "MODEL_VERSION_UNKNOWN"
    APPLICATION_CONFLICT = "APPLICATION_CONFLICT"
    UPSTREAM_FAILURE = "UPSTREAM_FAILURE"
    COMPUTE_FAILURE = "COMPUTE_FAILURE"
    PERSISTENCE_FAILURE = "PERSISTENCE_FAILURE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ServiceError(Exception):
    """归一化服务错误：code 对外可见，cause 仅用于内部日志。"""

    def __init__(self, code: ErrorCode, message: str, *, cause: BaseException | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.cause = cause

    def to_dict(self) -> dict:
        return {"error_code": str(self.code), "message": self.message}
