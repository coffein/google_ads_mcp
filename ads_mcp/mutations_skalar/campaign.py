"""Skalar campaign mutations: status + budget amount updates."""

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
def update_campaign_status(
    customer_id: str,
    campaign_id: str,
    status: str,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Pauses, enables, or removes a campaign.

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_id: Campaign ID (digits only).
      status: ENABLED, PAUSED, or REMOVED.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  campaign_id = normalise_id(campaign_id)
  client = get_client(login_customer_id)
  resource_name = client.get_service("CampaignService").campaign_path(
      customer_id, campaign_id
  )

  resolved_status = resolve_enum(
      enum_types.CampaignStatusEnum.CampaignStatus, status, "status"
  )
  campaign = resource_types.Campaign(
      resource_name=resource_name, status=resolved_status
  )
  operation = service_types.CampaignOperation(update=campaign)
  operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))

  expected_changes = [{
      "resource_name": resource_name,
      "field": "status",
      "new_value": status.upper(),
  }]

  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="CampaignService",
      method_name="mutate_campaigns",
      request_type_name="MutateCampaignsRequest",
      customer_id=customer_id,
      operations=[operation],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )


@mcp.tool()
@audit()
@require_allowed_account
def update_campaign_budget(
    customer_id: str,
    campaign_id: str,
    amount_micros: int,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Updates the daily budget (amount_micros) of the budget attached to a campaign.

  In Google Ads a campaign references a CampaignBudget resource; this tool
  resolves that reference and mutates the budget's amount_micros, not the
  campaign itself. 1 EUR = 1_000_000 micros.

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_id: Campaign ID (digits only).
      amount_micros: New daily budget in micros (e.g. 50_000_000 = 50 EUR).
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  if amount_micros <= 0:
    from fastmcp.exceptions import ToolError

    raise ToolError(
        f"amount_micros must be > 0, got {amount_micros}. "
        "Use 1_000_000 per currency unit (e.g. 50_000_000 = 50 EUR)."
    )

  customer_id = normalise_id(customer_id)
  campaign_id = normalise_id(campaign_id)
  client = get_client(login_customer_id)

  # Resolve the budget resource_name from the campaign.
  ga_service = client.get_service("GoogleAdsService")
  query = (
      "SELECT campaign.campaign_budget, campaign_budget.amount_micros "
      "FROM campaign "
      f"WHERE campaign.id = {campaign_id}"
  )
  budget_resource_name: str | None = None
  current_amount: int | None = None
  for batch in ga_service.search_stream(customer_id=customer_id, query=query):
    for row in batch.results:
      budget_resource_name = row.campaign.campaign_budget
      current_amount = row.campaign_budget.amount_micros
      break
    if budget_resource_name:
      break
  if not budget_resource_name:
    from fastmcp.exceptions import ToolError

    raise ToolError(
        f"Campaign {campaign_id} not found in customer {customer_id}, "
        "or has no attached budget."
    )

  budget = resource_types.CampaignBudget(
      resource_name=budget_resource_name, amount_micros=amount_micros
  )
  operation = service_types.CampaignBudgetOperation(update=budget)
  operation.update_mask.CopyFrom(
      field_mask_pb2.FieldMask(paths=["amount_micros"])
  )

  expected_changes = [{
      "resource_name": budget_resource_name,
      "field": "amount_micros",
      "current_value": current_amount,
      "new_value": amount_micros,
  }]

  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="CampaignBudgetService",
      method_name="mutate_campaign_budgets",
      request_type_name="MutateCampaignBudgetsRequest",
      customer_id=customer_id,
      operations=[operation],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
