"""Shared fixtures for skalar mutation tests.

The skalar tools are gated by env vars (mutations enabled + whitelist) and
decorated with @require_allowed_account + @audit. Setting those up
per-test would be noisy; centralise here.

Tests in this package never reach Google — they assert on the proto
operations the tool *would* send. That validates field names, oneof
selection, and resource-path wiring without needing API credentials.
"""

from __future__ import annotations

import os
from typing import Any

import pytest


class _PathBuilder:
  """Stand-in for Google Ads service classes — only their path builders
  are used by the mutation tools (mutate() itself goes through a mock).
  """

  @staticmethod
  def campaign_path(cid, c):
    return f"customers/{cid}/campaigns/{c}"

  @staticmethod
  def ad_group_path(cid, ag):
    return f"customers/{cid}/adGroups/{ag}"

  @staticmethod
  def ad_group_ad_path(cid, ag, ad):
    return f"customers/{cid}/adGroupAds/{ag}~{ad}"

  @staticmethod
  def ad_path(cid, ad):
    return f"customers/{cid}/ads/{ad}"

  @staticmethod
  def campaign_budget_path(cid, b):
    return f"customers/{cid}/campaignBudgets/{b}"

  @staticmethod
  def geo_target_constant_path(gid):
    return f"geoTargetConstants/{gid}"


class _FakeClient:
  """Implements just the surface `client.get_type` / `client.get_service`
  that the mutation tools use. Both produce real proto-plus instances /
  shared path-builder so the tools build *real* operation messages we can
  introspect.
  """

  def __init__(self, captured: dict[str, Any]):
    import google.ads.googleads.v24 as v
    self._v = v
    self._captured = captured

  def get_type(self, name: str):
    for sub in ("services", "common", "resources", "enums"):
      mod = getattr(self._v, sub)
      try:
        return getattr(mod.types, name)()
      except AttributeError:
        continue
    raise AttributeError(name)

  def get_service(self, name: str):  # noqa: ARG002 — same builder for all
    captured = self._captured

    class _Service:
      campaign_path = staticmethod(_PathBuilder.campaign_path)
      ad_group_path = staticmethod(_PathBuilder.ad_group_path)
      ad_group_ad_path = staticmethod(_PathBuilder.ad_group_ad_path)
      ad_path = staticmethod(_PathBuilder.ad_path)
      campaign_budget_path = staticmethod(_PathBuilder.campaign_budget_path)
      geo_target_constant_path = staticmethod(
          _PathBuilder.geo_target_constant_path
      )

      @staticmethod
      def mutate(request=None):
        captured["mutate_request"] = request
        from google.ads.googleads.v24.services.types.google_ads_service import (
            MutateGoogleAdsResponse,
        )
        return MutateGoogleAdsResponse()

      @staticmethod
      def mutate_campaigns(request=None):
        captured["mutate_campaigns_request"] = request
        from google.ads.googleads.v24.services.types.campaign_service import (
            MutateCampaignsResponse,
        )
        return MutateCampaignsResponse()

      @staticmethod
      def mutate_ad_groups(request=None):
        captured["mutate_ad_groups_request"] = request
        from google.ads.googleads.v24.services.types.ad_group_service import (
            MutateAdGroupsResponse,
        )
        return MutateAdGroupsResponse()

      @staticmethod
      def mutate_ad_group_ads(request=None):
        captured["mutate_ad_group_ads_request"] = request
        from google.ads.googleads.v24.services.types.ad_group_ad_service import (
            MutateAdGroupAdsResponse,
        )
        return MutateAdGroupAdsResponse()

      @staticmethod
      def mutate_ads(request=None):
        captured["mutate_ads_request"] = request
        from google.ads.googleads.v24.services.types.ad_service import (
            MutateAdsResponse,
        )
        return MutateAdsResponse()

    return _Service


@pytest.fixture
def captured() -> dict[str, Any]:
  return {}


@pytest.fixture
def fake_client(captured, monkeypatch, tmp_path):
  monkeypatch.setenv("GOOGLE_ADS_ALLOWED_ACCOUNTS", "5532877199,8427925949")
  monkeypatch.setenv("SKALAR_MCP_ENABLE_MUTATIONS", "1")
  monkeypatch.setenv("GOOGLE_ADS_AUDIT_DB", str(tmp_path / "audit.db"))
  client = _FakeClient(captured)
  monkeypatch.setattr(
      "ads_mcp.mutations_skalar._common.get_ads_client", lambda: client
  )
  return client


