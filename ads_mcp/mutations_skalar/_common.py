"""Internal helpers shared by all Skalar mutation tools.

Centralises three concerns so each tool can stay declarative:
  * building a Google Ads request with ``validate_only`` flipped from dry_run,
  * resolving enums case-insensitively (re-uses upstream helper),
  * shaping the structured return contract every tool must honour.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from ads_mcp.tools._utils import get_ads_client
from fastmcp.exceptions import ToolError

_DIGITS_RE = re.compile(r"\D+")


def get_client(login_customer_id: str | None = None):
  client = get_ads_client()
  if login_customer_id:
    client.login_customer_id = login_customer_id
  return client


def normalise_id(value: str | int) -> str:
  return _DIGITS_RE.sub("", str(value))


def resolve_enum(enum_type, value: str, param_name: str):
  """Case-insensitive proto-enum resolver.

  Inlined (not imported from ads_mcp.tools.mutations.common) because
  importing from there would transitively trigger registration of every
  upstream @mcp.tool, defeating the SKALAR_MCP_ENABLE_MUTATIONS gate.
  """
  try:
    return enum_type[value.upper()]
  except (KeyError, AttributeError) as exc:
    valid = [
        n for n in enum_type.__members__ if n not in ("UNSPECIFIED", "UNKNOWN")
    ]
    raise ToolError(
        f"Invalid {param_name}: {value!r}. Valid values: {', '.join(valid)}."
    ) from exc


def execute_mutation(
    *,
    client,
    service_name: str,
    method_name: str,
    request_type_name: str,
    customer_id: str,
    operations: list,
    dry_run: bool,
) -> Any:
  """Dispatches a mutate call with ``validate_only=dry_run``.

  Returns the raw response from the Google Ads API.
  """
  request = client.get_type(request_type_name)
  request.customer_id = customer_id
  request.operations.extend(operations)
  request.validate_only = bool(dry_run)
  service = client.get_service(service_name)
  method: Callable = getattr(service, method_name)
  return method(request=request)


def build_result(
    *,
    dry_run: bool,
    expected_changes: list[dict[str, Any]],
    response: Any | None,
) -> dict[str, Any]:
  """Standard structured return shape for every Skalar mutation tool.

  ``audit_id`` is added by the @audit decorator outside this helper.
  """
  if dry_run:
    return {
        "dry_run": True,
        "expected_changes": expected_changes,
        "resource_names": None,
    }
  results = list(getattr(response, "results", []) or [])
  return {
      "dry_run": False,
      "expected_changes": expected_changes,
      "resource_names": [r.resource_name for r in results],
  }
