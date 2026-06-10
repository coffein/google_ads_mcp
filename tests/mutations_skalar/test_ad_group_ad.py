"""Tests for ``update_ad_group_ad_status`` and ``update_responsive_search_ad``.

These pin down the two parts the user found missing in the MCP:
  * pausing a single ad without touching the others — needed for the
    "create new RSA, pause old" rotation workflow,
  * actually-mutable RSA text/URLs via ``AdService`` (not the
    AdGroupAdService, which only owns the link's status).
"""

from __future__ import annotations

import pytest

from ads_mcp.mutations_skalar.ad_group_ad import (
    update_ad_group_ad_status,
    update_responsive_search_ad,
)


def test_update_ad_group_ad_status_pauses_specific_ad(fake_client, captured):
  result = update_ad_group_ad_status(
      customer_id="5532877199",
      ad_group_id="123",
      ad_id="456",
      status="PAUSED",
      dry_run=True,
  )
  assert result["dry_run"] is True
  req = captured["mutate_ad_group_ads_request"]
  assert req.validate_only is True
  assert len(req.operations) == 1
  op = req.operations[0]
  assert (
      op.update.resource_name
      == "customers/5532877199/adGroupAds/123~456"
  )
  # AdGroupAdStatus.PAUSED = 3
  assert op.update.status.name == "PAUSED"
  assert list(op.update_mask.paths) == ["status"]


def test_update_ad_group_ad_status_rejects_invalid_status(fake_client):
  with pytest.raises(Exception, match="Invalid status"):
    update_ad_group_ad_status(
        customer_id="5532877199",
        ad_group_id="123",
        ad_id="456",
        status="MAYBE",
        dry_run=True,
    )


def test_update_rsa_headlines_only(fake_client, captured):
  result = update_responsive_search_ad(
      customer_id="5532877199",
      ad_id="999",
      headlines=[
          {"text": "Headline 1", "pinned_field": "HEADLINE_1"},
          "Headline 2",
          "Headline 3",
      ],
      dry_run=True,
  )
  assert result["dry_run"] is True
  req = captured["mutate_ads_request"]
  assert req.validate_only is True
  op = req.operations[0]
  ad = op.update
  assert ad.resource_name == "customers/5532877199/ads/999"
  hs = ad.responsive_search_ad.headlines
  assert len(hs) == 3
  assert hs[0].text == "Headline 1"
  assert hs[0].pinned_field.name == "HEADLINE_1"
  assert hs[1].text == "Headline 2"
  # update_mask was auto-derived: it should mention responsive_search_ad
  # and NOT mention descriptions/final_urls/path1/path2.
  paths = list(op.update_mask.paths)
  assert any("responsive_search_ad" in p for p in paths)
  assert all("descriptions" not in p for p in paths)
  assert all("final_urls" not in p for p in paths)


def test_update_rsa_all_fields_at_once(fake_client, captured):
  update_responsive_search_ad(
      customer_id="5532877199",
      ad_id="999",
      headlines=["H1", "H2", "H3"],
      descriptions=["D1", "D2"],
      final_urls=["https://example.com"],
      path1="info",
      path2="extra",
      dry_run=True,
  )
  req = captured["mutate_ads_request"]
  ad = req.operations[0].update
  assert [a.text for a in ad.responsive_search_ad.headlines] == ["H1", "H2", "H3"]
  assert [a.text for a in ad.responsive_search_ad.descriptions] == ["D1", "D2"]
  assert list(ad.final_urls) == ["https://example.com"]
  assert ad.responsive_search_ad.path1 == "info"
  assert ad.responsive_search_ad.path2 == "extra"


def test_update_rsa_requires_at_least_one_field(fake_client):
  with pytest.raises(Exception, match="At least one of"):
    update_responsive_search_ad(
        customer_id="5532877199",
        ad_id="999",
        dry_run=True,
    )


def test_update_rsa_rejects_empty_final_urls(fake_client):
  with pytest.raises(Exception, match="final_urls"):
    update_responsive_search_ad(
        customer_id="5532877199",
        ad_id="999",
        final_urls=[],
        dry_run=True,
    )


def test_update_rsa_rejects_too_long_headline(fake_client):
  with pytest.raises(Exception, match="headlines"):
    update_responsive_search_ad(
        customer_id="5532877199",
        ad_id="999",
        headlines=["a" * 31, "b", "c"],
        dry_run=True,
    )


def test_update_rsa_rejects_invalid_pinned_field_for_description(fake_client):
  with pytest.raises(Exception, match="pinned_field"):
    update_responsive_search_ad(
        customer_id="5532877199",
        ad_id="999",
        descriptions=[
            {"text": "D1", "pinned_field": "HEADLINE_1"},
            "D2",
        ],
        dry_run=True,
    )
