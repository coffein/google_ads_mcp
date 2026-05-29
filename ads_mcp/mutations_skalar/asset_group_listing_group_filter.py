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


# Temporary negative IDs for the 3-node atomic tree create. Must be negative
# per the API's temp-resource-name convention so the parent_listing_group_filter
# references on the child nodes resolve within the same mutate batch.
# https://developers.google.com/google-ads/api/docs/mutating/best-practices#temporary_resource_names
_ROOT_TEMP_ID = "-1"
_UNIT_VALUE_TEMP_ID = "-2"
_UNIT_OTHER_TEMP_ID = "-3"


@mcp.tool()
@audit()
@require_allowed_account
def create_asset_group_listing_group_two_way_split(
    customer_id: str,
    asset_group_id: str,
    dimension: dict,
    listing_source: str = "SHOPPING",
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Atomically creates a Root + 2-leaf PMAX listing-group tree.

  Builds the minimal "split inventory in two" tree in ONE
  GoogleAdsService.mutate batch — either all three nodes land or none do::

      Root (SUBDIVISION, no dimension)
      ├── UNIT_INCLUDED  (dimension)         ← the carved-out bucket
      └── UNIT_INCLUDED  (same key, value=None) ← everything-else sibling

  Typical use: ``dimension={"product_custom_attr0": "Topseller"}`` splits the
  asset group's inventory into a Topseller bucket and an everything-else
  bucket. Both are INCLUDED, so all products keep serving from this asset
  group — the tree just prepares the structure so one branch can later be
  flipped to EXCLUDED, or so a sibling asset group can target the other
  bucket directly.

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_group_id: AssetGroup ID (digits only). Must NOT already have a
          listing-group tree — the API rejects the batch otherwise.
      dimension: One-key dict for the carved-out bucket. The value MUST NOT
          be None (the "other" sibling is auto-generated with value=None on
          the same key). Common picks:

            * ``{"product_custom_attr0": "Topseller"}``
            * ``{"product_brand": "Nike"}``
            * ``{"product_condition": "NEW"}``
            * ``{"product_type_l1": "Apparel"}``
            * ``{"product_category_l1": 123}``

      listing_source: SHOPPING (retail PMAX) | WEBPAGE. Default SHOPPING.
      dry_run: When True (default) the batch is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, root_resource_name,
      value_unit_resource_name, other_unit_resource_name, audit_id}``.
      The three ``*_resource_name`` fields are None in dry_run mode.
  """
  if not isinstance(dimension, dict) or len(dimension) != 1:
    raise ToolError(
        f"dimension must be a dict with exactly one key, got {dimension!r}"
    )
  key, value = next(iter(dimension.items()))
  if value is None:
    raise ToolError(
        "dimension value must not be None — the other-sibling is auto-generated."
    )

  customer_id = normalise_id(customer_id)
  asset_group_id = normalise_id(asset_group_id)
  client = get_client(login_customer_id)

  asset_group_service = client.get_service("AssetGroupService")
  filter_service = client.get_service("AssetGroupListingGroupFilterService")
  googleads_service = client.get_service("GoogleAdsService")

  asset_group_path = asset_group_service.asset_group_path(
      customer_id, asset_group_id
  )
  root_path = filter_service.asset_group_listing_group_filter_path(
      customer_id, asset_group_id, _ROOT_TEMP_ID
  )
  value_path = filter_service.asset_group_listing_group_filter_path(
      customer_id, asset_group_id, _UNIT_VALUE_TEMP_ID
  )
  other_path = filter_service.asset_group_listing_group_filter_path(
      customer_id, asset_group_id, _UNIT_OTHER_TEMP_ID
  )

  resolved_source = resolve_enum(
      enum_types.ListingGroupFilterListingSourceEnum.ListingGroupFilterListingSource,
      listing_source,
      "listing_source",
  )
  type_subdiv = resolve_enum(
      enum_types.ListingGroupFilterTypeEnum.ListingGroupFilterType,
      "SUBDIVISION",
      "filter_type",
  )
  type_unit = resolve_enum(
      enum_types.ListingGroupFilterTypeEnum.ListingGroupFilterType,
      "UNIT_INCLUDED",
      "filter_type",
  )

  # 1) Root SUBDIVISION
  op_root = client.get_type("MutateOperation")
  root = op_root.asset_group_listing_group_filter_operation.create
  root.resource_name = root_path
  root.asset_group = asset_group_path
  root.type_ = type_subdiv
  root.listing_source = resolved_source

  # 2) UNIT_INCLUDED with the user-supplied dimension value
  op_value = client.get_type("MutateOperation")
  value_unit = op_value.asset_group_listing_group_filter_operation.create
  value_unit.resource_name = value_path
  value_unit.asset_group = asset_group_path
  value_unit.parent_listing_group_filter = root_path
  value_unit.type_ = type_unit
  value_unit.listing_source = resolved_source
  _set_filter_dimension(value_unit.case_value, dimension)

  # 3) UNIT_INCLUDED "other"-sibling — same dimension key, no value
  op_other = client.get_type("MutateOperation")
  other_unit = op_other.asset_group_listing_group_filter_operation.create
  other_unit.resource_name = other_path
  other_unit.asset_group = asset_group_path
  other_unit.parent_listing_group_filter = root_path
  other_unit.type_ = type_unit
  other_unit.listing_source = resolved_source
  _set_filter_dimension(other_unit.case_value, {key: None})

  operations = [op_root, op_value, op_other]

  expected_changes = [
      {
          "stage": "create_root",
          "asset_group": asset_group_path,
          "type": "SUBDIVISION",
          "listing_source": listing_source.upper(),
      },
      {
          "stage": "create_value_unit",
          "asset_group": asset_group_path,
          "type": "UNIT_INCLUDED",
          "dimension": dimension,
          "listing_source": listing_source.upper(),
      },
      {
          "stage": "create_other_unit",
          "asset_group": asset_group_path,
          "type": "UNIT_INCLUDED",
          "dimension": {key: None},
          "listing_source": listing_source.upper(),
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
        "root_resource_name": None,
        "value_unit_resource_name": None,
        "other_unit_resource_name": None,
    }

  resource_names: list[str] = []
  for r in response.mutate_operation_responses:
    which = r._pb.WhichOneof("response")
    if which == "asset_group_listing_group_filter_result":
      resource_names.append(
          r.asset_group_listing_group_filter_result.resource_name
      )

  return {
      "dry_run": False,
      "expected_changes": expected_changes,
      "resource_names": resource_names,
      "root_resource_name": resource_names[0] if len(resource_names) > 0 else None,
      "value_unit_resource_name": (
          resource_names[1] if len(resource_names) > 1 else None
      ),
      "other_unit_resource_name": (
          resource_names[2] if len(resource_names) > 2 else None
      ),
  }
