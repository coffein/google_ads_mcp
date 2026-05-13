"""SQLite audit log for every mutation tool.

One row per call (success or failure). The DB path is taken from
``GOOGLE_ADS_AUDIT_DB`` (default ``/var/lib/mcp-google-ads/audit.db``) and
the parent directory is created on first write.

The audit row id is returned via the tool result as ``audit_id``; a wrapped
function may either return a dict (the decorator merges ``audit_id`` in) or
return a non-dict value (the decorator wraps it as ``{"result": ..., "audit_id": ...}``).
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException

DEFAULT_AUDIT_DB = "/var/lib/mcp-google-ads/audit.db"

_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()


class AuditError(ToolError):
  """Raised when the audit log cannot be written (refuses the mutation)."""


def _db_path() -> str:
  return os.getenv("GOOGLE_ADS_AUDIT_DB", DEFAULT_AUDIT_DB)


def _reset_init_cache() -> None:
  """Test hook: clears the per-path init memo so tests get a fresh schema."""
  with _LOCK:
    _INITIALIZED.clear()


def init_audit_db(path: str | None = None) -> str:
  """Creates the audit DB schema if missing. Idempotent."""
  resolved = path or _db_path()
  with _LOCK:
    if resolved in _INITIALIZED:
      return resolved
    Path(resolved).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(resolved) as conn:
      conn.execute("""
          CREATE TABLE IF NOT EXISTS mutations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts TEXT NOT NULL,
              tool_name TEXT NOT NULL,
              customer_id TEXT,
              dry_run INTEGER NOT NULL,
              payload_json TEXT,
              success INTEGER NOT NULL,
              resource_names_json TEXT,
              error_code TEXT,
              error_message TEXT
          )
      """)
      conn.execute(
          "CREATE INDEX IF NOT EXISTS idx_mutations_ts ON mutations(ts)"
      )
      conn.execute(
          "CREATE INDEX IF NOT EXISTS idx_mutations_customer_id "
          "ON mutations(customer_id)"
      )
    _INITIALIZED.add(resolved)
    return resolved


def _serializable(value: Any) -> Any:
  try:
    json.dumps(value)
    return value
  except TypeError:
    return repr(value)


def _insert(
    *,
    tool_name: str,
    customer_id: str | None,
    dry_run: bool,
    payload: dict[str, Any],
    success: bool,
    resource_names: list[str] | None,
    error_code: str | None,
    error_message: str | None,
) -> int:
  path = init_audit_db()
  with _LOCK, sqlite3.connect(path) as conn:
    cur = conn.execute(
        "INSERT INTO mutations (ts, tool_name, customer_id, dry_run, "
        "payload_json, success, resource_names_json, error_code, error_message)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            tool_name,
            customer_id,
            1 if dry_run else 0,
            json.dumps({k: _serializable(v) for k, v in payload.items()}),
            1 if success else 0,
            json.dumps(resource_names) if resource_names else None,
            error_code,
            error_message,
        ),
    )
    return int(cur.lastrowid or 0)


def _extract_resource_names(result: Any) -> list[str] | None:
  if isinstance(result, dict):
    rn = result.get("resource_names")
    if isinstance(rn, list):
      return [str(x) for x in rn]
    if isinstance(result.get("resource_name"), str):
      return [result["resource_name"]]
  return None


def _extract_google_ads_error(exc: Exception) -> tuple[str | None, str]:
  # Mirror the upstream-tolerant lookup in safety.errors._which_error_code.
  if isinstance(exc, GoogleAdsException):
    parts: list[str] = []
    code = None
    for err in exc.failure.errors:
      pb = getattr(err.error_code, "_pb", err.error_code)
      try:
        ec = pb.WhichOneof("error_code")
      except (AttributeError, ValueError):
        ec = None
      if ec is not None:
        code = code or ec
      parts.append(err.message)
    return code, "; ".join(parts) or str(exc)
  # The Skalar errors wrapper may have already converted; surface its code.
  ec_attr = getattr(exc, "error_code", None)
  if isinstance(ec_attr, str):
    return ec_attr, str(exc)
  return type(exc).__name__, str(exc)


def audit(func: Callable | None = None) -> Callable:
  """Wraps a mutation tool: writes an audit row and injects ``audit_id``."""

  def decorate(fn: Callable) -> Callable:
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
      bound = sig.bind_partial(*args, **kwargs)
      bound.apply_defaults()
      payload = dict(bound.arguments)
      customer_id = (
          str(payload.get("customer_id"))
          if payload.get("customer_id") is not None
          else None
      )
      dry_run = bool(payload.get("dry_run", True))
      try:
        result = fn(*args, **kwargs)
      except Exception as exc:
        code, msg = _extract_google_ads_error(exc)
        try:
          _insert(
              tool_name=fn.__name__,
              customer_id=customer_id,
              dry_run=dry_run,
              payload=payload,
              success=False,
              resource_names=None,
              error_code=code,
              error_message=msg,
          )
        except sqlite3.Error:
          pass  # never let audit-write failure mask the real exception
        raise
      audit_id = _insert(
          tool_name=fn.__name__,
          customer_id=customer_id,
          dry_run=dry_run,
          payload=payload,
          success=True,
          resource_names=_extract_resource_names(result),
          error_code=None,
          error_message=None,
      )
      if isinstance(result, dict):
        result.setdefault("audit_id", audit_id)
        return result
      return {"result": result, "audit_id": audit_id}

    return wrapper

  return decorate(func) if func else decorate
