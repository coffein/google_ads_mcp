"""Tests for ``create_search_campaign_bundle`` proto-construction.

These tests intercept the ``MutateGoogleAdsRequest`` the tool would send
and assert that every required field is populated correctly. They run
without API credentials and pin down latent bugs like ``start_date``
vs ``start_date_time`` (the v23 rename) and the proto-plus oneof set
on MANUAL_CPC bidding.
"""

from __future__ import annotations

import pytest

from ads_mcp.mutations_skalar.search_campaign import (
    create_search_campaign_bundle,
)


_BASE_KWARGS = dict(
    customer_id="5532877199",
    budget_name="Brand Leistenhammer DE 2026-06-10",
    budget_amount_micros=20_000_000,
    budget_delivery_method="STANDARD",
    campaign_name="Search Brand Leistenhammer DE 2026-06-10",
    campaign_status="PAUSED",
    bidding_strategy_type="MANUAL_CPC",
    network_search=True,
    network_search_partners=False,
    network_content=False,
    geo_target_constant_ids=[],
    language_constant_ids=[],
    ad_group_name="Leistenhammer Brand",
    ad_group_status="ENABLED",
    ad_group_cpc_bid_micros=150_000,
    keywords=[
        {"text": "leistenhammer", "match_type": "EXACT"},
        {"text": "leistenhammer kaufen", "match_type": "PHRASE"},
    ],
    rsa_headlines=[
        "Leistenhammer Brand",
        "Original Leistenhammer",
        "Werkzeug kaufen",
        "Schnelle Lieferung",
        "Top Service",
    ],
    rsa_descriptions=[
        "Leistenhammer direkt vom Hersteller bestellen.",
        "Hochwertige Werkzeuge zu fairen Preisen.",
    ],
    rsa_final_urls=["https://example.com/leistenhammer"],
    rsa_status="PAUSED",
    enhanced_cpc=False,
    dry_run=True,
)


def _ops_by_kind(request):
  out: dict[str, list] = {}
  for op in request.mutate_operations:
    which = op._pb.WhichOneof("operation")
    out.setdefault(which, []).append(getattr(op, which).create)
  return out


def test_bundle_reproduces_user_payload(fake_client, captured):
  result = create_search_campaign_bundle(**_BASE_KWARGS)
  assert result["dry_run"] is True
  req = captured["mutate_request"]
  ops = _ops_by_kind(req)

  budget = ops["campaign_budget_operation"][0]
  assert budget.name == "Brand Leistenhammer DE 2026-06-10"
  assert budget.amount_micros == 20_000_000

  campaign = ops["campaign_operation"][0]
  assert campaign.name == "Search Brand Leistenhammer DE 2026-06-10"
  # SEARCH ad-channel set, budget wired up, network=search-only.
  assert campaign.network_settings.target_google_search is True
  assert campaign.network_settings.target_search_network is False
  assert campaign.network_settings.target_content_network is False
  # MANUAL_CPC oneof actually selected — this is the bit that historically
  # tripped proto-plus when ``enhanced_cpc_enabled`` defaulted to False.
  assert campaign._pb.WhichOneof("campaign_bidding_strategy") == "manual_cpc"
  assert campaign._pb.HasField("manual_cpc")
  assert campaign.manual_cpc._pb.HasField("enhanced_cpc_enabled")
  # EU TTPA declaration must be set; missing this triggers
  # [field_error/REQUIRED] field=…contains_eu_political_advertising.
  assert (
      campaign.contains_eu_political_advertising.name
      == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
  )

  ad_group = ops["ad_group_operation"][0]
  assert ad_group.name == "Leistenhammer Brand"
  assert ad_group.cpc_bid_micros == 150_000

  crits = ops["ad_group_criterion_operation"]
  assert len(crits) == 2
  assert {c.keyword.text for c in crits} == {
      "leistenhammer",
      "leistenhammer kaufen",
  }

  rsa = ops["ad_group_ad_operation"][0]
  assert len(rsa.ad.responsive_search_ad.headlines) == 5
  assert len(rsa.ad.responsive_search_ad.descriptions) == 2
  assert list(rsa.ad.final_urls) == ["https://example.com/leistenhammer"]


def test_bundle_accepts_start_and_end_dates(fake_client, captured):
  """Regression: in v23+ the Campaign field is start_date_time, not
  start_date. The bundle used to set the old name and would raise
  AttributeError before ever reaching the API.
  """
  kwargs = {**_BASE_KWARGS, "start_date": "2026-06-10", "end_date": "2026-12-31"}
  create_search_campaign_bundle(**kwargs)
  campaign = _ops_by_kind(captured["mutate_request"])["campaign_operation"][0]
  assert campaign.start_date_time == "2026-06-10"
  assert campaign.end_date_time == "2026-12-31"


def test_bundle_target_cpa_strategy(fake_client, captured):
  kwargs = {
      **_BASE_KWARGS,
      "bidding_strategy_type": "TARGET_CPA",
      "target_cpa_micros": 25_000_000,
      "enhanced_cpc": None,
  }
  create_search_campaign_bundle(**kwargs)
  campaign = _ops_by_kind(captured["mutate_request"])["campaign_operation"][0]
  assert campaign._pb.WhichOneof("campaign_bidding_strategy") == "target_cpa"
  assert campaign.target_cpa.target_cpa_micros == 25_000_000


def test_bundle_target_cpa_missing_value_errors(fake_client):
  kwargs = {
      **_BASE_KWARGS,
      "bidding_strategy_type": "TARGET_CPA",
      "target_cpa_micros": None,
  }
  with pytest.raises(Exception, match="target_cpa_micros"):
    create_search_campaign_bundle(**kwargs)


def test_bundle_geo_and_language_criteria_added(fake_client, captured):
  kwargs = {
      **_BASE_KWARGS,
      "geo_target_constant_ids": ["2276"],
      "language_constant_ids": ["1001"],
  }
  create_search_campaign_bundle(**kwargs)
  ops = _ops_by_kind(captured["mutate_request"])
  cc_ops = ops.get("campaign_criterion_operation", [])
  assert len(cc_ops) == 2
  geos = [c for c in cc_ops if c._pb.HasField("location")]
  langs = [c for c in cc_ops if c._pb.HasField("language")]
  assert geos[0].location.geo_target_constant == "geoTargetConstants/2276"
  assert langs[0].language.language_constant == "languageConstants/1001"
