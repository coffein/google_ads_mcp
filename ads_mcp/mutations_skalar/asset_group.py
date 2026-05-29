"""Skalar PMAX asset-group mutations.

Performance Max asset groups bundle the assets (texts, images, logo,
business name, ...) that Google's ML uses to assemble served ads. The
Google Ads API rejects creation of an AssetGroup unless the required
minimum assets are linked in the SAME mutate batch, so this module
exposes a bundled ``create_asset_group`` that performs the AssetGroup
create + every required AssetGroupAsset link in one
GoogleAdsService.mutate call (see
https://developers.google.com/google-ads/api/docs/mutating/best-practices#temporary_resource_names
for the temporary-ID convention used for the not-yet-created AssetGroup).

Status updates are exposed as a separate single-operation tool against
AssetGroupService.
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
from ads_mcp.safety import audit, require_allowed_account, wrap_google_ads_error
from ads_mcp.tools._ads_api import enum_types
from ads_mcp.tools._ads_api import resource_types
from ads_mcp.tools._ads_api import service_types
from fastmcp.exceptions import ToolError
from google.protobuf import field_mask_pb2

# Temporary negative ID used for the not-yet-created AssetGroup so the
# AssetGroupAsset link operations in the same batch can reference it.
_ASSET_GROUP_TEMP_ID = "-1"

# PMAX required minimums (single-image variant — brand-guidelines mode,
# which uses BrandAsset for business_name + logo, is NOT modelled here).
# Source: https://developers.google.com/google-ads/api/docs/performance-max/assets
_MIN_HEADLINES = 3
_MIN_LONG_HEADLINES = 1
_MIN_DESCRIPTIONS = 2
_MIN_MARKETING_IMAGES = 1
_MIN_SQUARE_MARKETING_IMAGES = 1
_MIN_LOGOS = 1


def _check_minimums(
    *,
    headline_asset_ids: list[str],
    long_headline_asset_ids: list[str],
    description_asset_ids: list[str],
    business_name_asset_id: str | None,
    marketing_image_asset_ids: list[str],
    square_marketing_image_asset_ids: list[str],
    logo_asset_ids: list[str],
) -> None:
  problems: list[str] = []
  if len(headline_asset_ids) < _MIN_HEADLINES:
    problems.append(
        f">= {_MIN_HEADLINES} headline_asset_ids required, got {len(headline_asset_ids)}"
    )
  if len(long_headline_asset_ids) < _MIN_LONG_HEADLINES:
    problems.append(
        f">= {_MIN_LONG_HEADLINES} long_headline_asset_ids required, got {len(long_headline_asset_ids)}"
    )
  if len(description_asset_ids) < _MIN_DESCRIPTIONS:
    problems.append(
        f">= {_MIN_DESCRIPTIONS} description_asset_ids required, got {len(description_asset_ids)}"
    )
  if not business_name_asset_id:
    problems.append("business_name_asset_id is required")
  if len(marketing_image_asset_ids) < _MIN_MARKETING_IMAGES:
    problems.append(
        f">= {_MIN_MARKETING_IMAGES} marketing_image_asset_ids required, got {len(marketing_image_asset_ids)}"
    )
  if len(square_marketing_image_asset_ids) < _MIN_SQUARE_MARKETING_IMAGES:
    problems.append(
        f">= {_MIN_SQUARE_MARKETING_IMAGES} square_marketing_image_asset_ids required, got {len(square_marketing_image_asset_ids)}"
    )
  if len(logo_asset_ids) < _MIN_LOGOS:
    problems.append(
        f">= {_MIN_LOGOS} logo_asset_ids required, got {len(logo_asset_ids)}"
    )
  if problems:
    raise ToolError(
        "PMAX minimum-assets check failed: " + "; ".join(problems)
    )


@mcp.tool()
@audit()
@require_allowed_account
def create_asset_group(
    customer_id: str,
    campaign_id: str,
    name: str,
    final_urls: list[str],
    headline_asset_ids: list[str],
    long_headline_asset_ids: list[str],
    description_asset_ids: list[str],
    business_name_asset_id: str,
    marketing_image_asset_ids: list[str],
    square_marketing_image_asset_ids: list[str],
    logo_asset_ids: list[str],
    final_mobile_urls: list[str] | None = None,
    path1: str | None = None,
    path2: str | None = None,
    status: str = "PAUSED",
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Creates a PMAX AssetGroup and links its required assets in one batch.

  The Google Ads API rejects asset-group creation unless the required
  minimum assets are present at commit time, so this tool bundles the
  AssetGroup create + every AssetGroupAsset link in a single
  GoogleAdsService.mutate call. All asset IDs passed must already exist
  in the account-level asset library (use ``create_text_asset`` for new
  texts; image and logo assets must be uploaded out of band).

  Required PMAX minimums (single-image variant — brand-guidelines mode
  is NOT supported here):
    * 3+ HEADLINE             (each <= 30 chars)
    * 1+ LONG_HEADLINE        (each <= 90 chars)
    * 2+ DESCRIPTION          (each <= 90 chars)
    * 1  BUSINESS_NAME        (<= 25 chars)
    * 1+ MARKETING_IMAGE
    * 1+ SQUARE_MARKETING_IMAGE
    * 1+ LOGO

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_id: Existing PMAX campaign ID to attach the asset group to.
      name: AssetGroup name. Must be unique within the campaign.
      final_urls: List of landing-page URLs (>= 1).
      headline_asset_ids: Existing HEADLINE text-Asset IDs (>= 3).
      long_headline_asset_ids: Existing LONG_HEADLINE text-Asset IDs (>= 1).
      description_asset_ids: Existing DESCRIPTION text-Asset IDs (>= 2).
      business_name_asset_id: Existing BUSINESS_NAME text-Asset ID.
      marketing_image_asset_ids: Existing MARKETING_IMAGE asset IDs (>= 1).
      square_marketing_image_asset_ids: Existing SQUARE_MARKETING_IMAGE
          asset IDs (>= 1).
      logo_asset_ids: Existing LOGO asset IDs (>= 1).
      final_mobile_urls: Optional mobile-specific landing-page URLs.
      path1: Optional display-URL path segment 1 (<= 15 chars).
      path2: Optional display-URL path segment 2 (<= 15 chars).
      status: ENABLED | PAUSED (default) | REMOVED.
      dry_run: When True (default) the batch is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names,
      asset_group_resource_name, audit_id}``. ``asset_group_resource_name``
      is None in dry_run mode (the API does not assign an ID under
      validate_only).
  """
  if not name:
    raise ToolError("name must be a non-empty string.")
  if not final_urls:
    raise ToolError("final_urls must contain at least one URL.")

  headline_asset_ids = [normalise_id(a) for a in headline_asset_ids]
  long_headline_asset_ids = [normalise_id(a) for a in long_headline_asset_ids]
  description_asset_ids = [normalise_id(a) for a in description_asset_ids]
  business_name_asset_id_n = (
      normalise_id(business_name_asset_id) if business_name_asset_id else None
  )
  marketing_image_asset_ids = [
      normalise_id(a) for a in marketing_image_asset_ids
  ]
  square_marketing_image_asset_ids = [
      normalise_id(a) for a in square_marketing_image_asset_ids
  ]
  logo_asset_ids = [normalise_id(a) for a in logo_asset_ids]

  _check_minimums(
      headline_asset_ids=headline_asset_ids,
      long_headline_asset_ids=long_headline_asset_ids,
      description_asset_ids=description_asset_ids,
      business_name_asset_id=business_name_asset_id_n,
      marketing_image_asset_ids=marketing_image_asset_ids,
      square_marketing_image_asset_ids=square_marketing_image_asset_ids,
      logo_asset_ids=logo_asset_ids,
  )

  customer_id = normalise_id(customer_id)
  campaign_id = normalise_id(campaign_id)
  client = get_client(login_customer_id)

  asset_group_service = client.get_service("AssetGroupService")
  campaign_service = client.get_service("CampaignService")
  asset_service = client.get_service("AssetService")
  googleads_service = client.get_service("GoogleAdsService")

  temp_asset_group_path = asset_group_service.asset_group_path(
      customer_id, _ASSET_GROUP_TEMP_ID
  )
  campaign_path = campaign_service.campaign_path(customer_id, campaign_id)
  resolved_status = resolve_enum(
      enum_types.AssetGroupStatusEnum.AssetGroupStatus, status, "status"
  )

  operations: list = []

  # 1) AssetGroup create with temp resource name.
  op_create_ag = client.get_type("MutateOperation")
  ag = op_create_ag.asset_group_operation.create
  ag.resource_name = temp_asset_group_path
  ag.name = name
  ag.campaign = campaign_path
  for url in final_urls:
    ag.final_urls.append(url)
  if final_mobile_urls:
    for url in final_mobile_urls:
      ag.final_mobile_urls.append(url)
  if path1 is not None:
    ag.path1 = path1
  if path2 is not None:
    ag.path2 = path2
  ag.status = resolved_status
  operations.append(op_create_ag)

  # 2..N) AssetGroupAsset link creates referencing the temp AssetGroup.
  def _add_link(asset_id: str, field_type_name: str) -> None:
    op = client.get_type("MutateOperation")
    link = op.asset_group_asset_operation.create
    link.asset_group = temp_asset_group_path
    link.asset = asset_service.asset_path(customer_id, asset_id)
    link.field_type = resolve_enum(
        enum_types.AssetFieldTypeEnum.AssetFieldType,
        field_type_name,
        "field_type",
    )
    operations.append(op)

  for aid in headline_asset_ids:
    _add_link(aid, "HEADLINE")
  for aid in long_headline_asset_ids:
    _add_link(aid, "LONG_HEADLINE")
  for aid in description_asset_ids:
    _add_link(aid, "DESCRIPTION")
  _add_link(business_name_asset_id_n, "BUSINESS_NAME")
  for aid in marketing_image_asset_ids:
    _add_link(aid, "MARKETING_IMAGE")
  for aid in square_marketing_image_asset_ids:
    _add_link(aid, "SQUARE_MARKETING_IMAGE")
  for aid in logo_asset_ids:
    _add_link(aid, "LOGO")

  expected_changes = [
      {
          "stage": "create_asset_group",
          "campaign": campaign_path,
          "name": name,
          "status": status.upper(),
          "final_urls": list(final_urls),
          "final_mobile_urls": list(final_mobile_urls or []),
          "path1": path1,
          "path2": path2,
      },
      {
          "stage": "link_assets",
          "by_field_type": {
              "HEADLINE": len(headline_asset_ids),
              "LONG_HEADLINE": len(long_headline_asset_ids),
              "DESCRIPTION": len(description_asset_ids),
              "BUSINESS_NAME": 1,
              "MARKETING_IMAGE": len(marketing_image_asset_ids),
              "SQUARE_MARKETING_IMAGE": len(square_marketing_image_asset_ids),
              "LOGO": len(logo_asset_ids),
          },
          "count": len(operations) - 1,
      },
  ]

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
        "asset_group_resource_name": None,
    }

  resource_names: list[str] = []
  asset_group_rn: str | None = None
  for r in response.mutate_operation_responses:
    which = r._pb.WhichOneof("response")
    if which == "asset_group_result":
      asset_group_rn = r.asset_group_result.resource_name
      resource_names.append(asset_group_rn)
    elif which == "asset_group_asset_result":
      resource_names.append(r.asset_group_asset_result.resource_name)

  return {
      "dry_run": False,
      "expected_changes": expected_changes,
      "resource_names": resource_names,
      "asset_group_resource_name": asset_group_rn,
  }


@mcp.tool()
@audit()
@require_allowed_account
def update_asset_group_status(
    customer_id: str,
    asset_group_id: str,
    status: str,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Pauses, enables, or removes a PMAX AssetGroup.

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_group_id: AssetGroup ID (digits only).
      status: ENABLED | PAUSED | REMOVED.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  asset_group_id = normalise_id(asset_group_id)
  client = get_client(login_customer_id)
  resource_name = client.get_service("AssetGroupService").asset_group_path(
      customer_id, asset_group_id
  )

  ag = resource_types.AssetGroup(
      resource_name=resource_name,
      status=resolve_enum(
          enum_types.AssetGroupStatusEnum.AssetGroupStatus, status, "status"
      ),
  )
  op = service_types.AssetGroupOperation(update=ag)
  op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))

  expected_changes = [{
      "resource_name": resource_name,
      "field": "status",
      "new_value": status.upper(),
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AssetGroupService",
      method_name="mutate_asset_groups",
      request_type_name="MutateAssetGroupsRequest",
      customer_id=customer_id,
      operations=[op],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
