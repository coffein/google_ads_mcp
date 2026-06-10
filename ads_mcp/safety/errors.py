"""Structured error helpers for Google Ads API exceptions.

LLMs respond much better to structured ``{error_code, message}`` payloads
than to raw stack traces. Use ``wrap_google_ads_error`` inside a try/except
in any tool that calls the Google Ads API.

The wrapped message surfaces, when the API supplies them, the deeper enum
value (e.g. ``field_error/REQUIRED_FIELD_MISSING``), the offending
``operation_index`` inside a batched ``GoogleAdsService.mutate`` call, the
``field_path`` the API blames, and the rejected ``trigger`` value. Without
these, a ``[field_error] The required field was not present.`` is useless
for debugging multi-operation bundles.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf.json_format import MessageToDict

_T = TypeVar("_T")


class GoogleAdsToolError(ToolError):
  """A wrapped Google Ads API exception with a stable error_code attribute."""

  def __init__(
      self, message: str, *, error_code: str | None, request_id: str | None
  ):
    super().__init__(message)
    self.error_code = error_code
    self.request_id = request_id


def _pb(obj):
  """Returns the raw protobuf for a proto-plus or raw message."""
  return getattr(obj, "_pb", obj)


def _which_error_code(error_code) -> str | None:
  """Extracts the active oneof field name across proto-plus and raw protobuf."""
  try:
    return _pb(error_code).WhichOneof("error_code")
  except (AttributeError, ValueError):
    return None


def _deeper_code_name(error_code, oneof_name: str | None) -> str | None:
  """Gets the enum-value name inside the active error_code oneof.

  E.g. ``field_error`` oneof set to ``REQUIRED_FIELD_MISSING`` returns
  ``"REQUIRED_FIELD_MISSING"``. proto-plus exposes the value with a
  ``.name`` attribute; falling back through the raw protobuf handles
  edge cases.
  """
  if not oneof_name:
    return None
  sub = getattr(error_code, oneof_name, None)
  if sub is None:
    return None
  name = getattr(sub, "name", None)
  if name:
    return name
  pb = _pb(error_code)
  try:
    value = getattr(pb, oneof_name)
    descriptor = pb.DESCRIPTOR.fields_by_name[oneof_name].enum_type
    if descriptor is not None:
      return descriptor.values_by_number[value].name
  except (AttributeError, KeyError, ValueError):
    pass
  return None


def _format_field_path(location_pb) -> str | None:
  """Joins ``field_path_elements`` into a dotted path with indices.

  ``operations[2].create.campaign.bidding_strategy_type`` reads infinitely
  better than ``operations index=2 create campaign bidding_strategy_type``.
  """
  parts: list[str] = []
  for el in location_pb.field_path_elements:
    name = el.field_name
    try:
      has_idx = el.HasField("index")
    except (AttributeError, ValueError):
      has_idx = False
    if has_idx:
      parts.append(f"{name}[{el.index}]")
    else:
      parts.append(name)
  return ".".join(parts) if parts else None


def _format_trigger(err_pb) -> str | None:
  """Renders the ``trigger`` ``google.protobuf.Value`` as a short string.

  Returned dicts look like ``{'stringValue': 'foo'}`` or
  ``{'numberValue': 42}``; flatten to just the scalar for brevity.
  """
  try:
    if not err_pb.HasField("trigger"):
      return None
  except (AttributeError, ValueError):
    return None
  try:
    d = MessageToDict(err_pb.trigger, preserving_proto_field_name=False)
  except (AttributeError, ValueError):
    return None
  if isinstance(d, dict) and len(d) == 1:
    return repr(next(iter(d.values())))
  return repr(d)


def _extract_op_index(location_pb) -> int | None:
  """Pulls the operation index out of the first ``operations[N]`` path element.

  v24 of the API encodes the offending operation as the first field-path
  element (``operations`` with ``index=N``), not as a separate
  ``operation_index`` field on ``ErrorLocation``. Surface it for batched
  ``GoogleAdsService.mutate`` errors so the user can find the broken op.
  """
  for el in location_pb.field_path_elements:
    if el.field_name != "operations":
      return None
    try:
      if el.HasField("index"):
        return el.index
    except (AttributeError, ValueError):
      pass
    return None
  return None


def _format_one_error(err) -> str:
  """Builds one human-readable line: ``[code/sub] op#N field=path msg (trigger=...)``."""
  ec = _which_error_code(err.error_code)
  sub = _deeper_code_name(err.error_code, ec)
  code_str = f"{ec}/{sub}" if (ec and sub) else (ec or "")

  loc_bits: list[str] = []
  pb = _pb(err)
  try:
    if pb.HasField("location"):
      loc = pb.location
      op_idx = _extract_op_index(loc)
      if op_idx is not None:
        loc_bits.append(f"op#{op_idx}")
      fp = _format_field_path(loc)
      if fp:
        loc_bits.append(f"field={fp}")
  except (AttributeError, ValueError):
    pass

  trig = _format_trigger(pb)

  out = []
  if code_str:
    out.append(f"[{code_str}]")
  out.extend(loc_bits)
  out.append(err.message)
  if trig is not None:
    out.append(f"(trigger={trig})")
  return " ".join(out)


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
      parts.append(_format_one_error(err))
    raise GoogleAdsToolError(
        "; ".join(parts) or "Google Ads API error",
        error_code=first_code,
        request_id=exc.request_id,
    ) from exc
