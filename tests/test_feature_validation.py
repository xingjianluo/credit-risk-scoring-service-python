"""特征异常场景：缺失 / 越界 / 类型异常的拒绝原因必须可区分。"""
import logging

import pytest

from app.domain import FeatureSnapshot
from app.errors import ErrorCode, ServiceError
from app.features import validate_features

from conftest import GOOD_FEATURES, log_error

logger = logging.getLogger("credit_risk.test")


def _expect(features, code: ErrorCode) -> ServiceError:
    snapshot = FeatureSnapshot("APP-FEAT-1", features)
    with pytest.raises(ServiceError) as exc_info:
        validate_features(snapshot)
    log_error(exc_info.value)
    assert exc_info.value.code == code
    return exc_info.value


def test_missing_feature_is_distinguishable():
    features = dict(GOOD_FEATURES)
    del features["age"]
    exc = _expect(features, ErrorCode.FEATURE_MISSING)
    assert "age" in exc.message


def test_out_of_range_is_distinguishable_from_missing():
    features = dict(GOOD_FEATURES, debt_ratio=1.5)
    exc = _expect(features, ErrorCode.FEATURE_OUT_OF_RANGE)
    assert "debt_ratio" in exc.message


def test_type_invalid_is_distinguishable():
    features = dict(GOOD_FEATURES, annual_income="三十万")
    exc = _expect(features, ErrorCode.FEATURE_TYPE_INVALID)
    assert "annual_income" in exc.message


def test_bool_is_not_silently_accepted_as_int():
    _expect(dict(GOOD_FEATURES, delinquency_count=True),
            ErrorCode.FEATURE_TYPE_INVALID)


def test_nan_is_not_silently_accepted():
    _expect(dict(GOOD_FEATURES, debt_ratio=float("nan")),
            ErrorCode.FEATURE_TYPE_INVALID)


def test_none_is_not_defaulted():
    _expect(dict(GOOD_FEATURES, age=None), ErrorCode.FEATURE_TYPE_INVALID)


def test_valid_snapshot_passes():
    validate_features(FeatureSnapshot("APP-FEAT-OK", dict(GOOD_FEATURES)))
    logger.info("valid snapshot passed application=APP-FEAT-OK")
