"""Tests for the GoogleAdsException → GoogleAdsToolError wrapper.

Use the real protobuf message types so the field accessors, ``HasField``
quirks, and proto-plus → raw protobuf round-trip behave exactly like in
production. Mocking these would let the wrapper silently regress against
the actual API surface — the wrapper exists precisely to handle that
surface's edge cases.
"""

from __future__ import annotations

import pytest
from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.v24.errors.types.errors import (
    ErrorCode,
    ErrorLocation,
    GoogleAdsError,
    GoogleAdsFailure,
)
from google.ads.googleads.v24.errors.types.field_error import FieldErrorEnum

from ads_mcp.safety.errors import GoogleAdsToolError, wrap_google_ads_error


def _make_exc(errors: list[GoogleAdsError]) -> GoogleAdsException:
  failure = GoogleAdsFailure(errors=errors)
  return GoogleAdsException(
      error=None, call=None, failure=failure, request_id="req-123"
  )


def _raise(exc):
  raise exc


def test_minimal_error_keeps_old_shape():
  err = GoogleAdsError(
      error_code=ErrorCode(
          field_error=FieldErrorEnum.FieldError.REQUIRED
      ),
      message="The required field was not present.",
  )
  with pytest.raises(GoogleAdsToolError) as info:
    wrap_google_ads_error(lambda: _raise(_make_exc([err])))
  msg = str(info.value)
  assert "[field_error/REQUIRED]" in msg
  assert "The required field was not present." in msg
  assert info.value.error_code == "field_error"
  assert info.value.request_id == "req-123"


def test_error_with_location_surfaces_op_index_and_field_path():
  err = GoogleAdsError(
      error_code=ErrorCode(
          field_error=FieldErrorEnum.FieldError.REQUIRED
      ),
      message="The required field was not present.",
      location=ErrorLocation(
          field_path_elements=[
              ErrorLocation.FieldPathElement(field_name="operations", index=2),
              ErrorLocation.FieldPathElement(field_name="create"),
              ErrorLocation.FieldPathElement(field_name="campaign"),
              ErrorLocation.FieldPathElement(
                  field_name="bidding_strategy_type"
              ),
          ],
      ),
  )
  with pytest.raises(GoogleAdsToolError) as info:
    wrap_google_ads_error(lambda: _raise(_make_exc([err])))
  msg = str(info.value)
  assert "op#2" in msg
  assert (
      "field=operations[2].create.campaign.bidding_strategy_type" in msg
  )


def test_error_with_trigger_value_is_included():
  err = GoogleAdsError(
      error_code=ErrorCode(
          field_error=FieldErrorEnum.FieldError.INVALID_VALUE
      ),
      message="A value is invalid.",
  )
  err.trigger.string_value = "MANUAL_CPC"
  with pytest.raises(GoogleAdsToolError) as info:
    wrap_google_ads_error(lambda: _raise(_make_exc([err])))
  msg = str(info.value)
  assert "trigger=" in msg
  assert "MANUAL_CPC" in msg


def test_multiple_errors_are_joined_with_semicolons():
  err1 = GoogleAdsError(
      error_code=ErrorCode(
          field_error=FieldErrorEnum.FieldError.REQUIRED
      ),
      message="missing 1",
      location=ErrorLocation(
          field_path_elements=[
              ErrorLocation.FieldPathElement(field_name="operations", index=0)
          ]
      ),
  )
  err2 = GoogleAdsError(
      error_code=ErrorCode(
          field_error=FieldErrorEnum.FieldError.VALUE_MUST_BE_UNSET
      ),
      message="invalid 2",
      location=ErrorLocation(
          field_path_elements=[
              ErrorLocation.FieldPathElement(field_name="operations", index=1)
          ]
      ),
  )
  with pytest.raises(GoogleAdsToolError) as info:
    wrap_google_ads_error(lambda: _raise(_make_exc([err1, err2])))
  msg = str(info.value)
  assert "; " in msg
  assert "op#0" in msg
  assert "op#1" in msg
  # first error's code wins for the structured attribute
  assert info.value.error_code == "field_error"


def test_non_googleads_exception_propagates():
  with pytest.raises(RuntimeError, match="boom"):
    wrap_google_ads_error(lambda: _raise(RuntimeError("boom")))


def test_success_returns_value():
  assert wrap_google_ads_error(lambda: 42) == 42
