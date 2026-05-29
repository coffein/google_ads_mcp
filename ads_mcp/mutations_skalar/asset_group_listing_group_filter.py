"""Skalar PMAX listing-group-filter mutations.

Performance Max can target a subset of products via AssetGroupListingGroupFilter
nodes — conceptually the same as Shopping's listing groups but living on the
AssetGroup (not AdGroupCriterion), using its own enum types and a separate
service.

Resource shape:
  AssetGroupListingGroupFilter
    id                              (assigned by the API)
    asset_group                     parent AssetGroup
    parent_listing_group_filter     another filter under the same asset group;
                                    None on the ROOT node
    type_                           SUBDIVISION | UNIT_INCLUDED | UNIT_EXCLUDED
    listing_source                  SHOPPING | WEBPAGE
    case_value                      ListingGroupFilterDimension (oneof)

API rules to know:
  * The root is a SUBDIVISION with NO parent_listing_group_filter and NO
    case_value.
  * UNIT_INCLUDED leaves include the partition; UNIT_EXCLUDED leaves exclude
    it. There is NO per-unit bid in PMAX.
  * To convert a UNIT to a SUBDIVISION (or vice versa), DELETE and re-CREATE.
  * Deleting a non-leaf SUBDIVISION will fail; remove its children first.
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


def _set_filter_dimension(case_value, dimension: dict | None) -> None:
  """Populates a ListingGroupFilterDimension from a one-key dict.

  Supported dimension keys (one per node):
    product_brand           : str
    product_condition       : NEW | USED | REFURBISHED
    product_channel         : ONLINE | LOCAL
    product_item_id         : str (offer id)
    product_type_l1..l5     : str
    product_category_l1..l5 : int (Google product category id)
    product_custom_attr0..4 : str

  A value of ``None`` (e.g. ``{"product_brand": None}``) marks the node as
  the "other" catch-all sibling for that dimension — the oneof field is
  marked present but carries no value, which is how Google models the
  everything-else partition.

  Pass dimension=None ONLY when creating the ROOT subdivision.
  """
  if dimension is None:
    return
  if not isinstance(dimension, dict) or len(dimension) != 1:
    raise ToolError(
        f"dimension must be a dict with exactly one key, got {dimension!r}"
    )
  key, value = next(iter(dimension.items()))

  if key == "product_brand":
    if value is None:
      case_value.product_brand._pb.SetInParent()
    else:
      case_value.product_brand.value = str(value)
  elif key == "product_condition":
    if value is None:
      case_value.product_condition._pb.SetInParent()
    else:
      case_value.product_condition.condition = resolve_enum(
          enum_types.ListingGroupFilterProductConditionEnum.ListingGroupFilterProductCondition,
          str(value),
          "product_condition",
      )
  elif key == "product_channel":
    if value is None:
      case_value.product_channel._pb.SetInParent()
    else:
      case_value.product_channel.channel = resolve_enum(
          enum_types.ListingGroupFilterProductChannelEnum.ListingGroupFilterProductChannel,
          str(value),
          "product_channel",
      )
  elif key == "product_item_id":
    if value is None:
      case_value.product_item_id._pb.SetInParent()
    else:
      case_value.product_item_id.value = str(value)
  elif key.startswith("product_type_l"):
    level_n = int(key[-1])
    case_value.product_type.level = resolve_enum(
        enum_types.ListingGroupFilterProductTypeLevelEnum.ListingGroupFilterProductTypeLevel,
        f"LEVEL{level_n}",
        "product_type level",
    )
    if value is not None:
      case_value.product_type.value = str(value)
  elif key.startswith("product_category_l"):
    level_n = int(key[-1])
    case_value.product_category.level = resolve_enum(
        enum_types.ListingGroupFilterProductCategoryLevelEnum.ListingGroupFilterProductCategoryLevel,
        f"LEVEL{level_n}",
        "product_category level",
    )
    if value is not None:
      case_value.product_category.category_id = int(value)
  elif key.startswith("product_custom_attr"):
    idx = int(key[-1])
    case_value.product_custom_attribute.index = resolve_enum(
        enum_types.ListingGroupFilterCustomAttributeIndexEnum.ListingGroupFilterCustomAttributeIndex,
        f"INDEX{idx}",
        "custom attribute index",
    )
    if value is not None:
      case_value.product_custom_attribute.value = str(value)
  else:
    raise ToolError(f"Unsupported dimension key: {key!r}")


def _create_filter(
    *,
    filter_type: str,
    customer_id: str,
    asset_group_id: str,
    parent_filter_id: str | None,
    dimension: dict | None,
    listing_source: str,
    dry_run: bool,
    login_customer_id: str | None,
) -> dict:
  customer_id = normalise_id(customer_id)
  asset_group_id = normalise_id(asset_group_id)
  client = get_client(login_customer_id)
  asset_group_path = client.get_service("AssetGroupService").asset_group_path(
      customer_id, asset_group_id
  )

  flt = resource_types.AssetGroupListingGroupFilter(asset_group=asset_group_path)
  flt.type_ = resolve_enum(
      enum_types.ListingGroupFilterTypeEnum.ListingGroupFilterType,
      filter_type,
      "filter_type",
  )
  flt.listing_source = resolve_enum(
      enum_types.ListingGroupFilterListingSourceEnum.ListingGroupFilterListingSource,
      listing_source,
      "listing_source",
  )
  if parent_filter_id is not None:
    flt.parent_listing_group_filter = client.get_service(
        "AssetGroupListingGroupFilterService"
    ).asset_group_listing_group_filter_path(
        customer_id, asset_group_id, normalise_id(parent_filter_id)
    )
  _set_filter_dimension(flt.case_value, dimension)

  op = service_types.AssetGroupListingGroupFilterOperation(create=flt)
  expected_changes = [{
      "asset_group": asset_group_path,
      "type": filter_type.upper(),
      "parent_filter_id": parent_filter_id,
      "dimension": dimension,
      "listing_source": listing_source.upper(),
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AssetGroupListingGroupFilterService",
      method_name="mutate_asset_group_listing_group_filters",
      request_type_name="MutateAssetGroupListingGroupFiltersRequest",
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
def create_asset_group_listing_group_filter_subdivision(
    customer_id: str,
    asset_group_id: str,
    parent_filter_id: str | None = None,
    dimension: dict | None = None,
    listing_source: str = "SHOPPING",
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Creates a SUBDIVISION (inner-tree) listing-group filter on a PMAX asset group.

  Pass parent_filter_id=None AND dimension=None to create the ROOT subdivision
  of an asset group that has no listing-group tree yet.

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_group_id: AssetGroup ID (digits only).
      parent_filter_id: Parent SUBDIVISION's filter_id, or None for root.
      dimension: One-key dict like ``{"product_brand": "Nike"}``; None for root.
          A None value (e.g. ``{"product_brand": None}``) creates the "other"
          catch-all sibling for that dimension. See _set_filter_dimension for
          the full list of supported keys.
      listing_source: SHOPPING (retail PMAX with Merchant Center) | WEBPAGE.
          Default SHOPPING.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  return _create_filter(
      filter_type="SUBDIVISION",
      customer_id=customer_id,
      asset_group_id=asset_group_id,
      parent_filter_id=parent_filter_id,
      dimension=dimension,
      listing_source=listing_source,
      dry_run=dry_run,
      login_customer_id=login_customer_id,
  )


@mcp.tool()
@audit()
@require_allowed_account
def create_asset_group_listing_group_filter_unit(
    customer_id: str,
    asset_group_id: str,
    parent_filter_id: str,
    dimension: dict,
    excluded: bool = False,
    listing_source: str = "SHOPPING",
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Creates a UNIT (leaf) listing-group filter on a PMAX asset group.

  UNIT_INCLUDED partitions are served by the asset group; UNIT_EXCLUDED
  partitions are removed from its targeting. There is no per-unit bid in
  PMAX — the campaign's bidding strategy decides spend.

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_group_id: AssetGroup ID (digits only).
      parent_filter_id: Parent SUBDIVISION's filter_id.
      dimension: One-key dict like ``{"product_brand": "Nike"}``. A None
          value creates the "other" sibling for that dimension. See the
          subdivision tool for the full list of supported keys.
      excluded: When True the node is UNIT_EXCLUDED (these products are
          removed from this asset group). Default False = UNIT_INCLUDED.
      listing_source: SHOPPING | WEBPAGE. Default SHOPPING.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  return _create_filter(
      filter_type="UNIT_EXCLUDED" if excluded else "UNIT_INCLUDED",
      customer_id=customer_id,
      asset_group_id=asset_group_id,
      parent_filter_id=parent_filter_id,
      dimension=dimension,
      listing_source=listing_source,
      dry_run=dry_run,
      login_customer_id=login_customer_id,
  )


@mcp.tool()
@audit()
@require_allowed_account
def delete_asset_group_listing_group_filter(
    customer_id: str,
    asset_group_id: str,
    filter_id: str,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Deletes a listing-group filter node (UNIT or leaf SUBDIVISION).

  Deleting a non-leaf SUBDIVISION fails — remove its children first.

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_group_id: AssetGroup ID containing the filter.
      filter_id: Filter ID to delete (digits only).
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  asset_group_id = normalise_id(asset_group_id)
  filter_id = normalise_id(filter_id)
  client = get_client(login_customer_id)
  resource_name = client.get_service(
      "AssetGroupListingGroupFilterService"
  ).asset_group_listing_group_filter_path(customer_id, asset_group_id, filter_id)

  op = service_types.AssetGroupListingGroupFilterOperation(remove=resource_name)
  expected_changes = [{
      "resource_name": resource_name,
      "operation": "REMOVE",
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AssetGroupListingGroupFilterService",
      method_name="mutate_asset_group_listing_group_filters",
      request_type_name="MutateAssetGroupListingGroupFiltersRequest",
      customer_id=customer_id,
      operations=[op],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
