"""Tests for the Skalar whitelist decorator."""

from __future__ import annotations

import pytest

from ads_mcp.safety.whitelist import (
    AccountNotAllowedError,
    allowed_accounts,
    require_allowed_account,
)


@require_allowed_account
def _stub(customer_id: str, value: int = 0) -> dict:
  return {"customer_id": customer_id, "value": value}


def test_empty_env_rejects_everything(monkeypatch):
  monkeypatch.delenv("GOOGLE_ADS_ALLOWED_ACCOUNTS", raising=False)
  with pytest.raises(AccountNotAllowedError, match="is empty"):
    _stub(customer_id="3332619762")


def test_allowed_id_passes(monkeypatch):
  monkeypatch.setenv("GOOGLE_ADS_ALLOWED_ACCOUNTS", "3332619762,1234567890")
  assert _stub(customer_id="3332619762")["value"] == 0


def test_not_whitelisted_rejects(monkeypatch):
  monkeypatch.setenv("GOOGLE_ADS_ALLOWED_ACCOUNTS", "3332619762")
  with pytest.raises(AccountNotAllowedError, match="not whitelisted"):
    _stub(customer_id="8427925949")


def test_id_normalization_strips_dashes(monkeypatch):
  monkeypatch.setenv("GOOGLE_ADS_ALLOWED_ACCOUNTS", "3332619762")
  assert _stub(customer_id="333-261-9762")["customer_id"] == "333-261-9762"


def test_missing_customer_id_rejects(monkeypatch):
  monkeypatch.setenv("GOOGLE_ADS_ALLOWED_ACCOUNTS", "3332619762")
  with pytest.raises(AccountNotAllowedError, match="required but missing"):
    _stub(customer_id=None)


def test_allowed_accounts_parses_whitespace(monkeypatch):
  monkeypatch.setenv(
      "GOOGLE_ADS_ALLOWED_ACCOUNTS", " 333-261-9762 , 842-792-5949 "
  )
  assert allowed_accounts() == {"3332619762", "8427925949"}


def test_decorator_rejects_function_without_customer_id():
  with pytest.raises(TypeError, match="no customer_id param"):

    @require_allowed_account
    def _bad(other_param: str) -> None:
      del other_param
