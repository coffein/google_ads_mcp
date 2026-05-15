"""Skalar Conversion-Value-Rule CRUD.

Conversion-value rules let Smart Bidding adjust the reported conversion
value based on context (geo, device, audience). For example, "multiply
conversion value by 1.20 when the user is in DE on MOBILE".

A rule on its own is inert: it must belong to a ConversionValueRuleSet
that's attached to a campaign or the customer. Set-management is left
to the Google Ads UI for now (heavier CRUD with attachment semantics).

Resource shape:
  name              : human-readable
  action.operation  : ADD | MULTIPLY  (MULTIPLY uses value as factor;
                                       ADD uses value as currency offset)
  action.value      : float
  geo_location_condition.geo_match_type : LOCATION_OF_PRESENCE | AREA_OF_INTEREST
  geo_location_condition.geo_target_constants : ["geoTargetConstants/2276", …]
  device_condition.devices : [MOBILE, …]
  audience_condition.user_lists : ["customers/X/userLists/Y", …]
  status            : ENABLED | PAUSED | REMOVED
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

_OPERATION_ENUM = (
    enum_types.ValueRuleOperationEnum.ValueRuleOperation
    if hasattr(enum_types, "ValueRuleOperationEnum")
    else enum_types.ConversionValueRuleActionOperationEnum.ConversionValueRuleActionOperation
    if hasattr(enum_types, "ConversionValueRuleActionOperationEnum")
    else None
)


def _conditions_to_rule(rule, *, geo_target_constant_ids, geo_match_type, devices, user_list_resource_names) -> list[str]:
  """Populates the optional condition fields and returns update-mask paths."""
  paths: list[str] = []
  if geo_target_constant_ids:
    geo_match = resolve_enum(
        enum_types.ValueRuleGeoLocationMatchTypeEnum.ValueRuleGeoLocationMatchType,
        geo_match_type or "LOCATION_OF_PRESENCE",
        "geo_match_type",
    )
    rule.geo_location_condition.geo_match_type = geo_match
    for gid in geo_target_constant_ids:
      rule.geo_location_condition.geo_target_constants.append(
          f"geoTargetConstants/{normalise_id(gid)}"
      )
    paths.extend([
        "geo_location_condition.geo_match_type",
        "geo_location_condition.geo_target_constants",
    ])
  if devices:
    for d in devices:
      rule.device_condition.devices.append(
          resolve_enum(
              enum_types.ValueRuleDeviceTypeEnum.ValueRuleDeviceType,
              d,
              "device",
          )
      )
    paths.append("device_condition.devices")
  if user_list_resource_names:
    for url in user_list_resource_names:
      rule.audience_condition.user_lists.append(url)
    paths.append("audience_condition.user_lists")
  return paths


@mcp.tool()
@audit()
@require_allowed_account
def create_conversion_value_rule(
    customer_id: str,
    operation: str,
    value: float,
    geo_target_constant_ids: list[str] | None = None,
    geo_match_type: str = "LOCATION_OF_PRESENCE",
    devices: list[str] | None = None,
    user_list_resource_names: list[str] | None = None,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Creates a ConversionValueRule with the given action + conditions.

  At least one condition (geo / device / audience) should be set;
  otherwise the rule fires for every conversion.

  Args:
      customer_id: Google Ads customer ID (digits only).
      operation: ADD | MULTIPLY.
      value: For MULTIPLY a factor (1.20 = +20%); for ADD a currency
          offset in the account's currency.
      geo_target_constant_ids: Optional list of geo IDs (e.g. ["2276"]).
      geo_match_type: LOCATION_OF_PRESENCE | AREA_OF_INTEREST.
      devices: Optional subset of [MOBILE, DESKTOP, TABLET].
      user_list_resource_names: Optional audience user-list resource names
          ("customers/X/userLists/Y").
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
      Note: the rule is inert until added to a ConversionValueRuleSet that
      is attached to the customer or a campaign.
  """
  if _OPERATION_ENUM is None:
    raise ToolError(
        "ConversionValueRule operation enum not found in this google-ads "
        "library version. Upgrade google-ads."
    )
  customer_id = normalise_id(customer_id)
  client = get_client(login_customer_id)

  rule = resource_types.ConversionValueRule()
  rule.action.operation = resolve_enum(_OPERATION_ENUM, operation, "operation")
  rule.action.value = float(value)
  _conditions_to_rule(
      rule,
      geo_target_constant_ids=geo_target_constant_ids,
      geo_match_type=geo_match_type,
      devices=devices,
      user_list_resource_names=user_list_resource_names,
  )

  op = service_types.ConversionValueRuleOperation(create=rule)
  expected_changes = [{
      "operation": operation.upper(),
      "value": value,
      "geo_target_constant_ids": geo_target_constant_ids,
      "geo_match_type": geo_match_type,
      "devices": [d.upper() for d in (devices or [])] or None,
      "user_list_resource_names": user_list_resource_names,
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="ConversionValueRuleService",
      method_name="mutate_conversion_value_rules",
      request_type_name="MutateConversionValueRulesRequest",
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
def update_conversion_value_rule(
    customer_id: str,
    rule_id: str,
    operation: str | None = None,
    value: float | None = None,
    status: str | None = None,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Updates an existing ConversionValueRule's action or status.

  Pass only the fields you want to change. Conditions (geo/device/audience)
  cannot be edited in place — re-create the rule for that.

  Args:
      customer_id: Google Ads customer ID (digits only).
      rule_id: ConversionValueRule ID.
      operation: New action operation (ADD | MULTIPLY).
      value: New action value.
      status: ENABLED | PAUSED | REMOVED.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  if operation is None and value is None and status is None:
    raise ToolError(
        "At least one of operation, value, status must be provided."
    )
  customer_id = normalise_id(customer_id)
  rule_id = normalise_id(rule_id)
  client = get_client(login_customer_id)
  resource_name = client.get_service(
      "ConversionValueRuleService"
  ).conversion_value_rule_path(customer_id, rule_id)

  rule = resource_types.ConversionValueRule(resource_name=resource_name)
  paths: list[str] = []
  if operation is not None:
    rule.action.operation = resolve_enum(
        _OPERATION_ENUM, operation, "operation"
    )
    paths.append("action.operation")
  if value is not None:
    rule.action.value = float(value)
    paths.append("action.value")
  if status is not None:
    rule.status = resolve_enum(
        enum_types.ConversionValueRuleStatusEnum.ConversionValueRuleStatus,
        status,
        "status",
    )
    paths.append("status")

  op = service_types.ConversionValueRuleOperation(update=rule)
  op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
  expected_changes = [{
      "resource_name": resource_name,
      "fields": paths,
      "new_values": {
          k: v
          for k, v in (
              ("operation", operation),
              ("value", value),
              ("status", status),
          )
          if v is not None
      },
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="ConversionValueRuleService",
      method_name="mutate_conversion_value_rules",
      request_type_name="MutateConversionValueRulesRequest",
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
def remove_conversion_value_rule(
    customer_id: str,
    rule_id: str,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Removes (deletes) a ConversionValueRule.

  Args:
      customer_id: Google Ads customer ID (digits only).
      rule_id: ConversionValueRule ID.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  rule_id = normalise_id(rule_id)
  client = get_client(login_customer_id)
  resource_name = client.get_service(
      "ConversionValueRuleService"
  ).conversion_value_rule_path(customer_id, rule_id)

  op = service_types.ConversionValueRuleOperation(remove=resource_name)
  expected_changes = [{
      "resource_name": resource_name,
      "operation": "REMOVE",
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="ConversionValueRuleService",
      method_name="mutate_conversion_value_rules",
      request_type_name="MutateConversionValueRulesRequest",
      customer_id=customer_id,
      operations=[op],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
