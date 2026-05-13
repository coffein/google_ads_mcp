"""Tests for the SQLite audit decorator."""

from __future__ import annotations

import sqlite3

import pytest

from ads_mcp.safety.audit import audit, init_audit_db


@pytest.fixture
def audit_db(tmp_path, monkeypatch):
  db = tmp_path / "audit.db"
  monkeypatch.setenv("GOOGLE_ADS_AUDIT_DB", str(db))
  # reset module-level init cache so each test gets a fresh init
  from ads_mcp.safety.audit import _reset_init_cache

  _reset_init_cache()
  init_audit_db()
  return db


def _rows(db) -> list[sqlite3.Row]:
  with sqlite3.connect(db) as conn:
    conn.row_factory = sqlite3.Row
    return list(conn.execute("SELECT * FROM mutations ORDER BY id"))


def test_success_writes_row_with_audit_id(audit_db):
  @audit()
  def my_tool(customer_id: str, dry_run: bool = True):
    return {"resource_names": ["customers/123/campaigns/456"]}

  result = my_tool(customer_id="3332619762", dry_run=False)
  assert "audit_id" in result
  rows = _rows(audit_db)
  assert len(rows) == 1
  assert rows[0]["tool_name"] == "my_tool"
  assert rows[0]["customer_id"] == "3332619762"
  assert rows[0]["dry_run"] == 0
  assert rows[0]["success"] == 1
  assert "customers/123/campaigns/456" in rows[0]["resource_names_json"]


def test_dry_run_default_logged(audit_db):
  @audit()
  def my_tool(customer_id: str, dry_run: bool = True):
    return {}

  my_tool(customer_id="3332619762")
  rows = _rows(audit_db)
  assert rows[0]["dry_run"] == 1


def test_exception_logged_and_reraised(audit_db):
  @audit()
  def my_tool(customer_id: str, dry_run: bool = True):
    raise ValueError("boom")

  with pytest.raises(ValueError, match="boom"):
    my_tool(customer_id="3332619762", dry_run=True)
  rows = _rows(audit_db)
  assert rows[0]["success"] == 0
  assert rows[0]["error_code"] == "ValueError"
  assert rows[0]["error_message"] == "boom"


def test_non_dict_return_wrapped(audit_db):
  @audit()
  def my_tool(customer_id: str, dry_run: bool = True):
    return ["a", "b"]

  result = my_tool(customer_id="3332619762")
  assert result["result"] == ["a", "b"]
  assert "audit_id" in result
