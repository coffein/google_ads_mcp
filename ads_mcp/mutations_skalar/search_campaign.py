"""Skalar Search-campaign creation.

Building a Search campaign from scratch requires creating ~5 resources in
the right order with cross-references: a CampaignBudget, the Campaign, an
AdGroup, an AdGroupAd (Responsive Search Ad), keywords, and optional
geo/language CampaignCriteria. The Google Ads API supports this in a
single GoogleAdsService.mutate call using temporary resource names
(negative IDs), giving atomicity — either everything lands or nothing
does. See
https://developers.google.com/google-ads/api/docs/mutating/best-practices#temporary_resource_names
for the temp-id convention.

This module exposes:
  * ``create_search_campaign_bundle`` — atomic bundle for new campaigns
  * ``create_ad_group``               — standalone, under existing campaign
  * ``create_responsive_search_ad``   — standalone, under existing ad group
"""

from __future__ import annotations

from ads_mcp.coordinator import mcp_server as mcp
from ads_mcp.mutations_skalar._common import (
    build_result,
    execute_mutation,
    get_client,
    normalise_id,
    resolve_enum,
)
from ads_mcp.mutations_skalar.keyword import Keyword
from ads_mcp.safety import audit, require_allowed_account, wrap_google_ads_error
from ads_mcp.tools._ads_api import enum_types
from ads_mcp.tools._ads_api import resource_types
from ads_mcp.tools._ads_api import service_types
from fastmcp.exceptions import ToolError

# Temp resource IDs for the bundled create. Negative integers tell Google
# "this resource doesn't exist yet — bind it to the result of an earlier
# operation in the same batch".
_BUDGET_TEMP_ID = "-1"
_CAMPAIGN_TEMP_ID = "-2"
_AD_GROUP_TEMP_ID = "-3"

# RSA limits per Google Ads API.
_RSA_HEADLINE_MIN, _RSA_HEADLINE_MAX, _RSA_HEADLINE_LEN = 3, 15, 30
_RSA_DESCRIPTION_MIN, _RSA_DESCRIPTION_MAX, _RSA_DESCRIPTION_LEN = 2, 4, 90
_RSA_PATH_LEN = 15


def _validate_rsa(
    headlines: list[str],
    descriptions: list[str],
    final_urls: list[str],
    path1: str | None,
    path2: str | None,
) -> None:
  problems: list[str] = []
  if not (_RSA_HEADLINE_MIN <= len(headlines) <= _RSA_HEADLINE_MAX):
    problems.append(
        f"headlines must have {_RSA_HEADLINE_MIN}..{_RSA_HEADLINE_MAX} entries,"
        f" got {len(headlines)}"
    )
  for i, h in enumerate(headlines):
    if not isinstance(h, str) or not h.strip():
      problems.append(f"headlines[{i}] must be non-empty string")
    elif len(h) > _RSA_HEADLINE_LEN:
      problems.append(
          f"headlines[{i}] too long ({len(h)} > {_RSA_HEADLINE_LEN} chars):"
          f" {h!r}"
      )
  if not (_RSA_DESCRIPTION_MIN <= len(descriptions) <= _RSA_DESCRIPTION_MAX):
    problems.append(
        f"descriptions must have {_RSA_DESCRIPTION_MIN}.."
        f"{_RSA_DESCRIPTION_MAX} entries, got {len(descriptions)}"
    )
  for i, d in enumerate(descriptions):
    if not isinstance(d, str) or not d.strip():
      problems.append(f"descriptions[{i}] must be non-empty string")
    elif len(d) > _RSA_DESCRIPTION_LEN:
      problems.append(
          f"descriptions[{i}] too long ({len(d)} > {_RSA_DESCRIPTION_LEN}"
          f" chars): {d!r}"
      )
  if not final_urls:
    problems.append("final_urls must contain at least one URL")
  for label, p in (("path1", path1), ("path2", path2)):
    if p is not None and len(p) > _RSA_PATH_LEN:
      problems.append(
          f"{label} too long ({len(p)} > {_RSA_PATH_LEN} chars): {p!r}"
      )
  if problems:
    raise ToolError("RSA validation failed: " + "; ".join(problems))


def _apply_bidding_strategy(
    *,
    campaign,
    client,
    bidding_strategy_type: str,
    target_cpa_micros: int | None,
    target_roas: float | None,
    enhanced_cpc: bool | None,
) -> str:
  """Materialises the oneof bidding strategy on campaign.

  Mirrors the pattern used in ``bidding.update_campaign_bidding_strategy``:
  oneof fields require explicit ``client.get_type()`` assignment for
  proto-plus to register them as set.
  """
  st = bidding_strategy_type.upper()
  if st == "MANUAL_CPC":
    campaign.manual_cpc = client.get_type("ManualCpc")
    if enhanced_cpc is not None:
      campaign.manual_cpc.enhanced_cpc_enabled = bool(enhanced_cpc)
  elif st == "TARGET_CPA":
    if target_cpa_micros is None:
      raise ToolError("target_cpa_micros is required for TARGET_CPA")
    campaign.target_cpa = client.get_type("TargetCpa")
    campaign.target_cpa.target_cpa_micros = int(target_cpa_micros)
  elif st == "TARGET_ROAS":
    if target_roas is None:
      raise ToolError("target_roas is required for TARGET_ROAS")
    campaign.target_roas = client.get_type("TargetRoas")
    campaign.target_roas.target_roas = float(target_roas)
  elif st == "MAXIMIZE_CONVERSIONS":
    campaign.maximize_conversions = client.get_type("MaximizeConversions")
    if target_cpa_micros is not None:
      campaign.maximize_conversions.target_cpa_micros = int(target_cpa_micros)
  elif st == "MAXIMIZE_CONVERSION_VALUE":
    campaign.maximize_conversion_value = client.get_type(
        "MaximizeConversionValue"
    )
    if target_roas is not None:
      campaign.maximize_conversion_value.target_roas = float(target_roas)
  else:
    raise ToolError(
        f"Invalid bidding_strategy_type {bidding_strategy_type!r}. Valid:"
        " MANUAL_CPC, TARGET_CPA, TARGET_ROAS, MAXIMIZE_CONVERSIONS,"
        " MAXIMIZE_CONVERSION_VALUE"
    )
  return st


@mcp.tool()
@audit()
@require_allowed_account
def create_search_campaign_bundle(
    customer_id: str,
    budget_name: str,
    budget_amount_micros: int,
    budget_delivery_method: str,
    campaign_name: str,
    campaign_status: str,
    bidding_strategy_type: str,
    network_search: bool,
    network_search_partners: bool,
    network_content: bool,
    geo_target_constant_ids: list[str],
    language_constant_ids: list[str],
    ad_group_name: str,
    ad_group_status: str,
    ad_group_cpc_bid_micros: int,
    keywords: list[Keyword],
    rsa_headlines: list[str],
    rsa_descriptions: list[str],
    rsa_final_urls: list[str],
    rsa_status: str,
    target_cpa_micros: int | None = None,
    target_roas: float | None = None,
    enhanced_cpc: bool | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    rsa_path1: str | None = None,
    rsa_path2: str | None = None,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Creates a complete Search campaign in one atomic mutate call.

  Bundles ~5 resource creates: CampaignBudget → Campaign → AdGroup → RSA
  (AdGroupAd) → Keywords (AdGroupCriterion) → optional Geo/Language
  CampaignCriteria. The Google Ads API processes them in one transaction —
  either everything lands or nothing does.

  Bidding strategy types:
    MANUAL_CPC                — manual; enhanced_cpc optional.
    TARGET_CPA                — requires target_cpa_micros.
    TARGET_ROAS               — requires target_roas (e.g. 2.5 = 250%).
    MAXIMIZE_CONVERSIONS      — target_cpa_micros is optional soft cap.
    MAXIMIZE_CONVERSION_VALUE — target_roas is optional soft cap.

  Networks: at least one of network_search / network_search_partners /
  network_content must be True. "Search only" is search=True, others False.

  Args:
      customer_id: Google Ads customer ID (digits only).
      budget_name: CampaignBudget name. Must be unique in the account.
      budget_amount_micros: Daily budget in micros (1 EUR = 1_000_000).
      budget_delivery_method: STANDARD (recommended) or ACCELERATED
          (deprecated by Google for most channels).
      campaign_name: Campaign name. Must be unique in the account.
      campaign_status: ENABLED | PAUSED. PAUSED is safer for initial setup.
      bidding_strategy_type: See list above.
      network_search: Serve on google.com search.
      network_search_partners: Serve on Google search partners.
      network_content: Serve on the Display Network.
      geo_target_constant_ids: List of geo target IDs (e.g. ["2276"] = DE).
          Pass [] for no geo restriction. Look up via the
          geo_target_constant reporting view or GeoTargetConstantService.
      language_constant_ids: List of language IDs (e.g. ["1001"] = German).
          Pass [] for all languages.
      ad_group_name: AdGroup name. Unique within the campaign.
      ad_group_status: ENABLED | PAUSED | REMOVED.
      ad_group_cpc_bid_micros: Default CPC bid in micros. Required by the
          API even when the campaign uses a smart-bidding strategy
          (treated as a fallback).
      keywords: List of {text, match_type, optional cpc_bid_micros,
          optional status (default ENABLED)}.
      rsa_headlines: 3..15 headlines, each <= 30 chars.
      rsa_descriptions: 2..4 descriptions, each <= 90 chars.
      rsa_final_urls: List of landing-page URLs (>= 1).
      rsa_status: ENABLED | PAUSED.
      target_cpa_micros: For TARGET_CPA (required) or MAXIMIZE_CONVERSIONS
          (optional soft cap).
      target_roas: For TARGET_ROAS (required) or
          MAXIMIZE_CONVERSION_VALUE (optional soft cap). 2.5 = 250%.
      enhanced_cpc: For MANUAL_CPC, enables auto-bid tweaks on top.
      start_date: YYYY-MM-DD. API default = today.
      end_date: YYYY-MM-DD. API default = far future.
      rsa_path1: Display-URL path segment 1 (<= 15 chars).
      rsa_path2: Display-URL path segment 2 (<= 15 chars).
      dry_run: When True (default) the batch is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names,
      campaign_resource_name, ad_group_resource_name, audit_id}``.
      Resource names are None in dry_run mode (validate_only doesn't
      assign IDs).
  """
  if not budget_name:
    raise ToolError("budget_name must be non-empty")
  if budget_amount_micros <= 0:
    raise ToolError(
        f"budget_amount_micros must be > 0, got {budget_amount_micros}"
    )
  if not campaign_name:
    raise ToolError("campaign_name must be non-empty")
  if not ad_group_name:
    raise ToolError("ad_group_name must be non-empty")
  if ad_group_cpc_bid_micros < 0:
    raise ToolError(
        f"ad_group_cpc_bid_micros must be >= 0, got {ad_group_cpc_bid_micros}"
    )
  if not (network_search or network_search_partners or network_content):
    raise ToolError(
        "At least one of network_search / network_search_partners /"
        " network_content must be True"
    )
  if not keywords:
    raise ToolError("keywords must not be empty")
  _validate_rsa(rsa_headlines, rsa_descriptions, rsa_final_urls, rsa_path1, rsa_path2)

  customer_id = normalise_id(customer_id)
  client = get_client(login_customer_id)

  budget_service = client.get_service("CampaignBudgetService")
  campaign_service = client.get_service("CampaignService")
  ad_group_service = client.get_service("AdGroupService")
  googleads_service = client.get_service("GoogleAdsService")
  geo_service = client.get_service("GeoTargetConstantService")

  temp_budget_path = budget_service.campaign_budget_path(
      customer_id, _BUDGET_TEMP_ID
  )
  temp_campaign_path = campaign_service.campaign_path(
      customer_id, _CAMPAIGN_TEMP_ID
  )
  temp_ad_group_path = ad_group_service.ad_group_path(
      customer_id, _AD_GROUP_TEMP_ID
  )

  operations: list = []

  # 1) CampaignBudget.
  op = client.get_type("MutateOperation")
  budget = op.campaign_budget_operation.create
  budget.resource_name = temp_budget_path
  budget.name = budget_name
  budget.amount_micros = int(budget_amount_micros)
  budget.delivery_method = resolve_enum(
      enum_types.BudgetDeliveryMethodEnum.BudgetDeliveryMethod,
      budget_delivery_method,
      "budget_delivery_method",
  )
  operations.append(op)

  # 2) Campaign.
  op = client.get_type("MutateOperation")
  campaign = op.campaign_operation.create
  campaign.resource_name = temp_campaign_path
  campaign.name = campaign_name
  campaign.status = resolve_enum(
      enum_types.CampaignStatusEnum.CampaignStatus,
      campaign_status,
      "campaign_status",
  )
  campaign.advertising_channel_type = (
      enum_types.AdvertisingChannelTypeEnum.AdvertisingChannelType.SEARCH
  )
  campaign.campaign_budget = temp_budget_path
  campaign.network_settings.target_google_search = bool(network_search)
  campaign.network_settings.target_search_network = bool(network_search_partners)
  campaign.network_settings.target_content_network = bool(network_content)
  campaign.network_settings.target_partner_search_network = False
  # EU TTPA (Reg. 2024/900) requires every new campaign to declare whether it
  # contains political advertising. Skalar is e-commerce; hardcode the
  # negative declaration. Expose a parameter if a political-advertising
  # use case ever appears.
  campaign.contains_eu_political_advertising = (
      enum_types.EuPoliticalAdvertisingStatusEnum
      .EuPoliticalAdvertisingStatus
      .DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
  )
  # Field name moved from start_date/end_date to start_date_time/end_date_time
  # in v23+ of the Google Ads API. Setting the legacy name raises
  # AttributeError before the request even reaches Google.
  if start_date:
    campaign.start_date_time = start_date
  if end_date:
    campaign.end_date_time = end_date
  _apply_bidding_strategy(
      campaign=campaign,
      client=client,
      bidding_strategy_type=bidding_strategy_type,
      target_cpa_micros=target_cpa_micros,
      target_roas=target_roas,
      enhanced_cpc=enhanced_cpc,
  )
  operations.append(op)

  # 3) Geo CampaignCriteria.
  for gid in geo_target_constant_ids:
    op = client.get_type("MutateOperation")
    cc = op.campaign_criterion_operation.create
    cc.campaign = temp_campaign_path
    cc.location.geo_target_constant = geo_service.geo_target_constant_path(
        normalise_id(gid)
    )
    operations.append(op)

  # 4) Language CampaignCriteria.
  for lid in language_constant_ids:
    op = client.get_type("MutateOperation")
    cc = op.campaign_criterion_operation.create
    cc.campaign = temp_campaign_path
    cc.language.language_constant = (
        f"languageConstants/{normalise_id(lid)}"
    )
    operations.append(op)

  # 5) AdGroup.
  op = client.get_type("MutateOperation")
  ag = op.ad_group_operation.create
  ag.resource_name = temp_ad_group_path
  ag.name = ad_group_name
  ag.campaign = temp_campaign_path
  ag.status = resolve_enum(
      enum_types.AdGroupStatusEnum.AdGroupStatus,
      ad_group_status,
      "ad_group_status",
  )
  ag.type_ = enum_types.AdGroupTypeEnum.AdGroupType.SEARCH_STANDARD
  ag.cpc_bid_micros = int(ad_group_cpc_bid_micros)
  operations.append(op)

  # 6) Keywords (AdGroupCriterion).
  match_type_enum = enum_types.KeywordMatchTypeEnum.KeywordMatchType
  crit_status_enum = enum_types.AdGroupCriterionStatusEnum.AdGroupCriterionStatus
  for idx, kw in enumerate(keywords):
    text = kw.get("text")
    match_type = kw.get("match_type")
    if not text or not match_type:
      raise ToolError(
          f"keywords[{idx}]: 'text' and 'match_type' are required"
      )
    op = client.get_type("MutateOperation")
    crit = op.ad_group_criterion_operation.create
    crit.ad_group = temp_ad_group_path
    crit.status = resolve_enum(
        crit_status_enum,
        kw.get("status", "ENABLED"),
        f"keywords[{idx}].status",
    )
    crit.keyword.text = text
    crit.keyword.match_type = resolve_enum(
        match_type_enum, match_type, f"keywords[{idx}].match_type"
    )
    if "cpc_bid_micros" in kw:
      crit.cpc_bid_micros = int(kw["cpc_bid_micros"])
    operations.append(op)

  # 7) RSA (AdGroupAd).
  op = client.get_type("MutateOperation")
  aga = op.ad_group_ad_operation.create
  aga.ad_group = temp_ad_group_path
  aga.status = resolve_enum(
      enum_types.AdGroupAdStatusEnum.AdGroupAdStatus, rsa_status, "rsa_status"
  )
  for url in rsa_final_urls:
    aga.ad.final_urls.append(url)
  rsa = aga.ad.responsive_search_ad
  for h in rsa_headlines:
    asset_text = client.get_type("AdTextAsset")
    asset_text.text = h
    rsa.headlines.append(asset_text)
  for d in rsa_descriptions:
    asset_text = client.get_type("AdTextAsset")
    asset_text.text = d
    rsa.descriptions.append(asset_text)
  if rsa_path1 is not None:
    rsa.path1 = rsa_path1
  if rsa_path2 is not None:
    rsa.path2 = rsa_path2
  operations.append(op)

  expected_changes = [{
      "stage": "create_search_campaign_bundle",
      "budget": {
          "name": budget_name,
          "amount_micros": budget_amount_micros,
          "delivery_method": budget_delivery_method.upper(),
      },
      "campaign": {
          "name": campaign_name,
          "status": campaign_status.upper(),
          "bidding_strategy": bidding_strategy_type.upper(),
          "target_cpa_micros": target_cpa_micros,
          "target_roas": target_roas,
          "enhanced_cpc": enhanced_cpc,
          "networks": {
              "search": network_search,
              "search_partners": network_search_partners,
              "content": network_content,
          },
          "geo_target_constant_ids": list(geo_target_constant_ids),
          "language_constant_ids": list(language_constant_ids),
          "start_date": start_date,
          "end_date": end_date,
      },
      "ad_group": {
          "name": ad_group_name,
          "status": ad_group_status.upper(),
          "cpc_bid_micros": ad_group_cpc_bid_micros,
      },
      "keywords_count": len(keywords),
      "rsa": {
          "headlines_count": len(rsa_headlines),
          "descriptions_count": len(rsa_descriptions),
          "final_urls": list(rsa_final_urls),
          "path1": rsa_path1,
          "path2": rsa_path2,
          "status": rsa_status.upper(),
      },
      "operations_count": len(operations),
  }]

  request = client.get_type("MutateGoogleAdsRequest")
  request.customer_id = customer_id
  request.mutate_operations.extend(operations)
  request.validate_only = bool(dry_run)

  response = wrap_google_ads_error(
      lambda: googleads_service.mutate(request=request)
  )

  if dry_run:
    return {
        "dry_run": True,
        "expected_changes": expected_changes,
        "resource_names": None,
        "campaign_resource_name": None,
        "ad_group_resource_name": None,
    }

  resource_names: list[str] = []
  campaign_rn: str | None = None
  ad_group_rn: str | None = None
  for r in response.mutate_operation_responses:
    which = r._pb.WhichOneof("response")
    if which == "campaign_budget_result":
      resource_names.append(r.campaign_budget_result.resource_name)
    elif which == "campaign_result":
      campaign_rn = r.campaign_result.resource_name
      resource_names.append(campaign_rn)
    elif which == "ad_group_result":
      ad_group_rn = r.ad_group_result.resource_name
      resource_names.append(ad_group_rn)
    elif which == "ad_group_ad_result":
      resource_names.append(r.ad_group_ad_result.resource_name)
    elif which == "ad_group_criterion_result":
      resource_names.append(r.ad_group_criterion_result.resource_name)
    elif which == "campaign_criterion_result":
      resource_names.append(r.campaign_criterion_result.resource_name)

  return {
      "dry_run": False,
      "expected_changes": expected_changes,
      "resource_names": resource_names,
      "campaign_resource_name": campaign_rn,
      "ad_group_resource_name": ad_group_rn,
  }


@mcp.tool()
@audit()
@require_allowed_account
def create_ad_group(
    customer_id: str,
    campaign_id: str,
    name: str,
    status: str,
    cpc_bid_micros: int,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Creates a SEARCH_STANDARD AdGroup under an existing campaign.

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_id: Existing campaign ID (digits only).
      name: AdGroup name. Unique within the campaign.
      status: ENABLED | PAUSED | REMOVED.
      cpc_bid_micros: Default CPC bid in micros (1 EUR = 1_000_000).
          Required even when the campaign uses a smart-bidding strategy.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  if not name:
    raise ToolError("name must be non-empty")
  if cpc_bid_micros < 0:
    raise ToolError(f"cpc_bid_micros must be >= 0, got {cpc_bid_micros}")
  customer_id = normalise_id(customer_id)
  campaign_id = normalise_id(campaign_id)
  client = get_client(login_customer_id)
  campaign_path = client.get_service("CampaignService").campaign_path(
      customer_id, campaign_id
  )

  ag = resource_types.AdGroup()
  ag.name = name
  ag.campaign = campaign_path
  ag.status = resolve_enum(
      enum_types.AdGroupStatusEnum.AdGroupStatus, status, "status"
  )
  ag.type_ = enum_types.AdGroupTypeEnum.AdGroupType.SEARCH_STANDARD
  ag.cpc_bid_micros = int(cpc_bid_micros)

  op = service_types.AdGroupOperation(create=ag)
  expected_changes = [{
      "campaign": campaign_path,
      "name": name,
      "status": status.upper(),
      "cpc_bid_micros": cpc_bid_micros,
      "type": "SEARCH_STANDARD",
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AdGroupService",
      method_name="mutate_ad_groups",
      request_type_name="MutateAdGroupsRequest",
      customer_id=customer_id,
      operations=[op],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )


@mcp.tool()
@audit()
@require_allowed_account
def create_responsive_search_ad(
    customer_id: str,
    ad_group_id: str,
    headlines: list[str],
    descriptions: list[str],
    final_urls: list[str],
    status: str,
    path1: str | None = None,
    path2: str | None = None,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Creates a Responsive Search Ad (RSA) under an existing ad group.

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_group_id: Existing ad group ID (digits only).
      headlines: 3..15 headlines, each <= 30 chars.
      descriptions: 2..4 descriptions, each <= 90 chars.
      final_urls: List of landing-page URLs (>= 1).
      status: ENABLED | PAUSED.
      path1: Optional display-URL path segment 1 (<= 15 chars).
      path2: Optional display-URL path segment 2 (<= 15 chars).
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  _validate_rsa(headlines, descriptions, final_urls, path1, path2)
  customer_id = normalise_id(customer_id)
  ad_group_id = normalise_id(ad_group_id)
  client = get_client(login_customer_id)
  ad_group_path = client.get_service("AdGroupService").ad_group_path(
      customer_id, ad_group_id
  )

  aga = resource_types.AdGroupAd()
  aga.ad_group = ad_group_path
  aga.status = resolve_enum(
      enum_types.AdGroupAdStatusEnum.AdGroupAdStatus, status, "status"
  )
  for url in final_urls:
    aga.ad.final_urls.append(url)
  rsa = aga.ad.responsive_search_ad
  for h in headlines:
    asset_text = client.get_type("AdTextAsset")
    asset_text.text = h
    rsa.headlines.append(asset_text)
  for d in descriptions:
    asset_text = client.get_type("AdTextAsset")
    asset_text.text = d
    rsa.descriptions.append(asset_text)
  if path1 is not None:
    rsa.path1 = path1
  if path2 is not None:
    rsa.path2 = path2

  op = service_types.AdGroupAdOperation(create=aga)
  expected_changes = [{
      "ad_group": ad_group_path,
      "status": status.upper(),
      "headlines_count": len(headlines),
      "descriptions_count": len(descriptions),
      "final_urls": list(final_urls),
      "path1": path1,
      "path2": path2,
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AdGroupAdService",
      method_name="mutate_ad_group_ads",
      request_type_name="MutateAdGroupAdsRequest",
      customer_id=customer_id,
      operations=[op],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
