"""Skalar ad-group mutations: status update."""

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
from google.protobuf import field_mask_pb2


@mcp.tool()
@audit()
@require_allowed_account
def update_ad_group_status(
    customer_id: str,
    ad_group_id: str,
    status: str,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Pauses, enables, or removes an ad group.

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_group_id: Ad group ID (digits only).
      status: ENABLED, PAUSED, or REMOVED.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  ad_group_id = normalise_id(ad_group_id)
  client = get_client(login_customer_id)
  resource_name = client.get_service("AdGroupService").ad_group_path(
      customer_id, ad_group_id
  )

  resolved_status = resolve_enum(
      enum_types.AdGroupStatusEnum.AdGroupStatus, status, "status"
  )
  ad_group = resource_types.AdGroup(
      resource_name=resource_name, status=resolved_status
  )
  operation = service_types.AdGroupOperation(update=ad_group)
  operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))

  expected_changes = [{
      "resource_name": resource_name,
      "field": "status",
      "new_value": status.upper(),
  }]

  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AdGroupService",
      method_name="mutate_ad_groups",
      request_type_name="MutateAdGroupsRequest",
      customer_id=customer_id,
      operations=[operation],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
