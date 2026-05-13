"""Structured error helpers for Google Ads API exceptions.

LLMs respond much better to structured ``{error_code, message}`` payloads
than to raw stack traces. Use ``wrap_google_ads_error`` inside a try/except
in any tool that calls the Google Ads API.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException

_T = TypeVar("_T")


class GoogleAdsToolError(ToolError):
  """A wrapped Google Ads API exception with a stable error_code attribute."""

  def __init__(
      self, message: str, *, error_code: str | None, request_id: str | None
  ):
    super().__init__(message)
    self.error_code = error_code
    self.request_id = request_id


def _which_error_code(error_code) -> str | None:
  """Extracts the active oneof field name across proto-plus and raw protobuf."""
  pb = getattr(error_code, "_pb", error_code)
  try:
    return pb.WhichOneof("error_code")
  except (AttributeError, ValueError):
    return None


def wrap_google_ads_error(call: Callable[[], _T]) -> _T:
  """Runs ``call()`` and converts GoogleAdsException to GoogleAdsToolError."""
  try:
    return call()
  except GoogleAdsException as exc:
    parts: list[str] = []
    first_code: str | None = None
    for err in exc.failure.errors:
      ec = _which_error_code(err.error_code)
      if first_code is None and ec is not None:
        first_code = ec
      parts.append(f"[{ec}] {err.message}" if ec else err.message)
    raise GoogleAdsToolError(
        "; ".join(parts) or "Google Ads API error",
        error_code=first_code,
        request_id=exc.request_id,
    ) from exc
