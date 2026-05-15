"""Skalar Shopping listing-group mutations.

Listing groups partition a Shopping ad group's product inventory into a
tree of (SUBDIVISION) inner nodes and (UNIT) leaves. UNITs carry a CPC
bid (or are excluded). SUBDIVISIONs are pure structure.

Resource shape (every node is an AdGroupCriterion):
  type_              = LISTING_GROUP
  listing_group.type = SUBDIVISION | UNIT
  listing_group.case_value             ← the dimension that *this* node represents
  listing_group.parent_ad_group_criterion ← parent (None for root)
  cpc_bid_micros      ← only on UNIT nodes
  status              = ENABLED | PAUSED | REMOVED

API rules to know:
  * The root is a SUBDIVISION with NO parent and NO case_value.
  * To turn a UNIT into a SUBDIVISION (or vice-versa), you DELETE and
    re-CREATE — there is no in-place type change.
  * Deleting a SUBDIVISION removes all of its descendants in the same
    mutate; we only support "leaf" deletes here.
"""

from __future__ import annotations

from typing import Any

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


def _ad_group_criterion_path(client, customer_id: str, composite_id: str) -> str:
  """Composite_id is "AGID~CRITERION_ID" as Google uses it for AdGroupCriterion."""
  ag_id, crit_id = composite_id.split("~", 1)
  return client.get_service("AdGroupCriterionService").ad_group_criterion_path(
      customer_id, ag_id, crit_id
  )


def _set_dimension(listing_group, dimension: dict | None) -> None:
  """Populates listing_group.case_value from a dict like {"product_brand": "Nike"}.

  Supported dimension keys (one per node):
    product_brand        : str
    product_condition    : NEW | USED | REFURBISHED
    product_channel      : ONLINE | LOCAL
    product_item_id      : str (offer id)
    product_type_l1..l5  : str
    product_category_l1..l5 : int (Google product category id)
    product_custom_attr0..4 : str
    unknown              : True (catch-all sibling)

  Pass dimension=None ONLY when creating the ROOT subdivision.
  """
  if dimension is None:
    return
  if not isinstance(dimension, dict) or len(dimension) != 1:
    raise ToolError(
        f"dimension must be a dict with exactly one key, got {dimension!r}"
    )
  key, value = next(iter(dimension.items()))
  cv = listing_group.case_value
  if key == "product_brand":
    cv.product_brand.value = str(value)
  elif key == "product_condition":
    cv.product_condition.condition = resolve_enum(
        enum_types.ProductConditionEnum.ProductCondition,
        str(value),
        "product_condition",
    )
  elif key == "product_channel":
    cv.product_channel.channel = resolve_enum(
        enum_types.ProductChannelEnum.ProductChannel,
        str(value),
        "product_channel",
    )
  elif key == "product_item_id":
    cv.product_item_id.value = str(value)
  elif key.startswith("product_type_l"):
    level_n = int(key[-1])
    cv.product_type.value = str(value)
    cv.product_type.level = resolve_enum(
        enum_types.ProductTypeLevelEnum.ProductTypeLevel,
        f"LEVEL{level_n}",
        "product_type level",
    )
  elif key.startswith("product_category_l"):
    level_n = int(key[-1])
    cv.product_category.category_id = int(value)
    cv.product_category.level = resolve_enum(
        enum_types.ProductCategoryLevelEnum.ProductCategoryLevel,
        f"LEVEL{level_n}",
        "product_category level",
    )
  elif key.startswith("product_custom_attr"):
    idx = int(key[-1])
    cv.product_custom_attribute.value = str(value)
    cv.product_custom_attribute.index = resolve_enum(
        enum_types.ProductCustomAttributeIndexEnum.ProductCustomAttributeIndex,
        f"INDEX{idx}",
        "custom attribute index",
    )
  elif key == "unknown" and value:
    # The proto has no payload for the "everything-else" sibling — leaving
    # case_value empty alongside type=UNIT is how Google models it.
    pass
  else:
    raise ToolError(f"Unsupported dimension key: {key!r}")


@mcp.tool()
@audit()
@require_allowed_account
def update_listing_group_unit_bid(
    customer_id: str,
    ad_group_id: str,
    criterion_id: str,
    cpc_bid_micros: int,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Updates the CPC bid on an existing Shopping listing-group UNIT node.

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_group_id: Shopping ad group ID containing the UNIT.
      criterion_id: AdGroupCriterion ID of the UNIT node.
      cpc_bid_micros: New CPC bid in micros (1 EUR = 1_000_000).
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  if cpc_bid_micros < 0:
    raise ToolError(f"cpc_bid_micros must be >= 0, got {cpc_bid_micros}")
  customer_id = normalise_id(customer_id)
  ad_group_id = normalise_id(ad_group_id)
  criterion_id = normalise_id(criterion_id)
  client = get_client(login_customer_id)
  resource_name = client.get_service(
      "AdGroupCriterionService"
  ).ad_group_criterion_path(customer_id, ad_group_id, criterion_id)

  agc = resource_types.AdGroupCriterion(
      resource_name=resource_name, cpc_bid_micros=cpc_bid_micros
  )
  op = service_types.AdGroupCriterionOperation(update=agc)
  op.update_mask.CopyFrom(
      field_mask_pb2.FieldMask(paths=["cpc_bid_micros"])
  )
  expected_changes = [{
      "resource_name": resource_name,
      "field": "cpc_bid_micros",
      "new_value": cpc_bid_micros,
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AdGroupCriterionService",
      method_name="mutate_ad_group_criteria",
      request_type_name="MutateAdGroupCriteriaRequest",
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
def update_listing_group_unit_status(
    customer_id: str,
    ad_group_id: str,
    criterion_id: str,
    status: str,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Pauses, enables, or removes a Shopping listing-group UNIT node.

  REMOVED is the same as deleting via delete_listing_group_node.

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_group_id: Shopping ad group ID.
      criterion_id: AdGroupCriterion ID of the UNIT.
      status: ENABLED | PAUSED | REMOVED.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  ad_group_id = normalise_id(ad_group_id)
  criterion_id = normalise_id(criterion_id)
  client = get_client(login_customer_id)
  resource_name = client.get_service(
      "AdGroupCriterionService"
  ).ad_group_criterion_path(customer_id, ad_group_id, criterion_id)

  agc = resource_types.AdGroupCriterion(
      resource_name=resource_name,
      status=resolve_enum(
          enum_types.AdGroupCriterionStatusEnum.AdGroupCriterionStatus,
          status,
          "status",
      ),
  )
  op = service_types.AdGroupCriterionOperation(update=agc)
  op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
  expected_changes = [{
      "resource_name": resource_name,
      "field": "status",
      "new_value": status.upper(),
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AdGroupCriterionService",
      method_name="mutate_ad_group_criteria",
      request_type_name="MutateAdGroupCriteriaRequest",
      customer_id=customer_id,
      operations=[op],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )


def _create_listing_group(
    *,
    listing_group_type: str,
    customer_id: str,
    ad_group_id: str,
    parent_criterion_id: str | None,
    dimension: dict | None,
    cpc_bid_micros: int | None,
    dry_run: bool,
    login_customer_id: str | None,
) -> dict:
  customer_id = normalise_id(customer_id)
  ad_group_id = normalise_id(ad_group_id)
  client = get_client(login_customer_id)
  ad_group_path = client.get_service("AdGroupService").ad_group_path(
      customer_id, ad_group_id
  )

  agc = resource_types.AdGroupCriterion(ad_group=ad_group_path)
  agc.status = enum_types.AdGroupCriterionStatusEnum.AdGroupCriterionStatus.ENABLED
  agc.listing_group.type_ = resolve_enum(
      enum_types.ListingGroupTypeEnum.ListingGroupType,
      listing_group_type,
      "listing_group_type",
  )
  if parent_criterion_id is not None:
    agc.listing_group.parent_ad_group_criterion = (
        client.get_service(
            "AdGroupCriterionService"
        ).ad_group_criterion_path(
            customer_id, ad_group_id, normalise_id(parent_criterion_id)
        )
    )
  _set_dimension(agc.listing_group, dimension)
  if listing_group_type.upper() == "UNIT":
    if cpc_bid_micros is None:
      raise ToolError("UNIT nodes require cpc_bid_micros (use 0 to exclude).")
    agc.cpc_bid_micros = int(cpc_bid_micros)

  op = service_types.AdGroupCriterionOperation(create=agc)
  expected_changes = [{
      "ad_group": ad_group_path,
      "type": listing_group_type.upper(),
      "parent_criterion_id": parent_criterion_id,
      "dimension": dimension,
      "cpc_bid_micros": cpc_bid_micros,
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AdGroupCriterionService",
      method_name="mutate_ad_group_criteria",
      request_type_name="MutateAdGroupCriteriaRequest",
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
def create_listing_group_subdivision(
    customer_id: str,
    ad_group_id: str,
    parent_criterion_id: str | None = None,
    dimension: dict | None = None,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Creates a SUBDIVISION (inner-tree node) in a Shopping listing-group tree.

  Pass parent_criterion_id=None AND dimension=None to create the ROOT
  subdivision of an empty ad group.

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_group_id: Shopping ad group ID.
      parent_criterion_id: Parent SUBDIVISION's criterion_id, or None for root.
      dimension: One-key dict like {"product_brand": "Nike"}; None for root.
          See _set_dimension for supported keys.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  return _create_listing_group(
      listing_group_type="SUBDIVISION",
      customer_id=customer_id,
      ad_group_id=ad_group_id,
      parent_criterion_id=parent_criterion_id,
      dimension=dimension,
      cpc_bid_micros=None,
      dry_run=dry_run,
      login_customer_id=login_customer_id,
  )


@mcp.tool()
@audit()
@require_allowed_account
def create_listing_group_unit(
    customer_id: str,
    ad_group_id: str,
    parent_criterion_id: str,
    dimension: dict,
    cpc_bid_micros: int,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Creates a UNIT (leaf node) under a SUBDIVISION.

  Pass cpc_bid_micros=0 to exclude this product partition from showing.

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_group_id: Shopping ad group ID.
      parent_criterion_id: Parent SUBDIVISION's criterion_id.
      dimension: One-key dict like {"product_brand": "Nike"}.
      cpc_bid_micros: CPC bid in micros (0 = exclude).
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  return _create_listing_group(
      listing_group_type="UNIT",
      customer_id=customer_id,
      ad_group_id=ad_group_id,
      parent_criterion_id=parent_criterion_id,
      dimension=dimension,
      cpc_bid_micros=cpc_bid_micros,
      dry_run=dry_run,
      login_customer_id=login_customer_id,
  )


@mcp.tool()
@audit()
@require_allowed_account
def delete_listing_group_node(
    customer_id: str,
    ad_group_id: str,
    criterion_id: str,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Deletes a listing-group node (UNIT or leaf SUBDIVISION).

  Deleting a non-leaf SUBDIVISION will fail with PARENT_HAS_CHILDREN —
  remove its children first.

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_group_id: Shopping ad group ID.
      criterion_id: Criterion ID of the node to delete.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  ad_group_id = normalise_id(ad_group_id)
  criterion_id = normalise_id(criterion_id)
  client = get_client(login_customer_id)
  resource_name = client.get_service(
      "AdGroupCriterionService"
  ).ad_group_criterion_path(customer_id, ad_group_id, criterion_id)

  op = service_types.AdGroupCriterionOperation(remove=resource_name)
  expected_changes = [{
      "resource_name": resource_name,
      "operation": "REMOVE",
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AdGroupCriterionService",
      method_name="mutate_ad_group_criteria",
      request_type_name="MutateAdGroupCriteriaRequest",
      customer_id=customer_id,
      operations=[op],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
