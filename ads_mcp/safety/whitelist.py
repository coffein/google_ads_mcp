"""Customer-ID whitelist enforced at decorator level.

Reads ``GOOGLE_ADS_ALLOWED_ACCOUNTS`` (comma-separated customer IDs, digits only)
on every call so a hot-reload of the env file takes effect without a restart.

Fail-closed: if the env var is missing/empty, every mutation is rejected.
"""

from __future__ import annotations

import functools
import inspect
import os
import re
from typing import Callable

from fastmcp.exceptions import ToolError


class AccountNotAllowedError(ToolError):
  """Raised when a mutation targets a customer_id outside the whitelist."""


_DIGITS_RE = re.compile(r"\D+")


def _normalize(cid: str | int | None) -> str | None:
  if cid is None:
    return None
  return _DIGITS_RE.sub("", str(cid)) or None


def allowed_accounts() -> set[str]:
  """Returns the current whitelist (re-read on every call)."""
  raw = os.getenv("GOOGLE_ADS_ALLOWED_ACCOUNTS", "")
  return {n for n in (_normalize(p) for p in raw.split(",")) if n}


def require_allowed_account(func: Callable) -> Callable:
  """Reject calls whose ``customer_id`` is not in the whitelist.

  The wrapped function MUST take ``customer_id`` as one of its parameters
  (positional or keyword).
  """
  sig = inspect.signature(func)
  if "customer_id" not in sig.parameters:
    raise TypeError(
        f"@require_allowed_account: {func.__name__} has no customer_id param"
    )

  @functools.wraps(func)
  def wrapper(*args, **kwargs):
    bound = sig.bind_partial(*args, **kwargs)
    raw_cid = bound.arguments.get("customer_id")
    cid = _normalize(raw_cid)
    if cid is None:
      raise AccountNotAllowedError(
          f"{func.__name__}: customer_id is required but missing"
      )
    allowed = allowed_accounts()
    if not allowed:
      raise AccountNotAllowedError(
          f"{func.__name__}: GOOGLE_ADS_ALLOWED_ACCOUNTS is empty — "
          "every mutation is refused. Set the env var to a comma-separated "
          "list of customer IDs."
      )
    if cid not in allowed:
      raise AccountNotAllowedError(
          f"{func.__name__}: customer_id {cid} is not whitelisted. "
          f"Allowed: {sorted(allowed)}"
      )
    return func(*args, **kwargs)

  return wrapper
