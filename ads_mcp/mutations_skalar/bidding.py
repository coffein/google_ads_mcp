"""Skalar bidding-strategy mutations.

Switching a campaign from one bidding strategy to another is a structurally
different operation from updating a single field — the campaign carries a
proto oneof for the strategy, so we set exactly one strategy field and put
its name into the update mask. The other strategies' fields are left alone
(the API clears the previous oneof automatically when the new one is set).
"""

from __future__ import annotations

from ads_mcp.coordinator import mcp_server as mcp
from ads_mcp.mutations_skalar._common import (
    build_result,
    execute_mutation,
    get_client,
    normalise_id,
)
from ads_mcp.safety import audit, require_allowed_account, wrap_google_ads_error
from ads_mcp.tools._ads_api import resource_types
from ads_mcp.tools._ads_api import service_types
from fastmcp.exceptions import ToolError
from google.protobuf import field_mask_pb2

_VALID_STRATEGIES = {
    "TARGET_ROAS",
    "TARGET_CPA",
    "MAXIMIZE_CONVERSIONS",
    "MAXIMIZE_CONVERSION_VALUE",
    "MANUAL_CPC",
}


@mcp.tool()
@audit()
@require_allowed_account
def update_campaign_bidding_strategy(
    customer_id: str,
    campaign_id: str,
    strategy_type: str,
    target_roas: float | None = None,
    target_cpa_micros: int | None = None,
    enhanced_cpc: bool = False,
    target_roas_for_max_conv_value: float | None = None,
    target_cpa_micros_for_max_conv: int | None = None,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Switches a campaign's bidding strategy.

  Supported strategy_type values:
    TARGET_ROAS                — requires target_roas (e.g. 2.5 = 250%).
    TARGET_CPA                 — requires target_cpa_micros (e.g. 25_000_000 = 25 EUR).
    MAXIMIZE_CONVERSIONS       — optional target_cpa_micros_for_max_conv (a soft cap).
    MAXIMIZE_CONVERSION_VALUE  — optional target_roas_for_max_conv_value.
    MANUAL_CPC                 — optional enhanced_cpc (default False).

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_id: Campaign ID (digits only).
      strategy_type: One of the names above.
      target_roas: For TARGET_ROAS, e.g. 2.5 means 250%.
      target_cpa_micros: For TARGET_CPA. 1 EUR = 1_000_000 micros.
      enhanced_cpc: For MANUAL_CPC, enables auto-bidding tweaks on top.
      target_roas_for_max_conv_value: Soft target on MAXIMIZE_CONVERSION_VALUE.
      target_cpa_micros_for_max_conv: Soft target on MAXIMIZE_CONVERSIONS.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  st = strategy_type.upper()
  if st not in _VALID_STRATEGIES:
    raise ToolError(
        f"Invalid strategy_type {strategy_type!r}. "
        f"Valid: {sorted(_VALID_STRATEGIES)}."
    )

  customer_id = normalise_id(customer_id)
  campaign_id = normalise_id(campaign_id)
  client = get_client(login_customer_id)
  resource_name = client.get_service("CampaignService").campaign_path(
      customer_id, campaign_id
  )

  # Build campaign + mask in lockstep. Google rejects two shapes:
  #  (a) a Message-typed top-level path on the mask without listing a
  #      subfield ("field mask updated a field with subfields: 'X'") —
  #      empty messages still count as "having subfields";
  #  (b) a oneof set without a value populated ("required field was not
  #      present") if the proto-plus assignment is only a read access.
  # Pin both: assign the message via client.get_type() AND list at least
  # one concrete subfield path (the soft-target field is the natural anchor
  # for the empty-payload strategies — sending it as 0 means "no soft cap").
  campaign = resource_types.Campaign(resource_name=resource_name)
  expected_value: dict = {"strategy_type": st}
  mask_paths: list[str] = []

  if st == "TARGET_ROAS":
    if target_roas is None:
      raise ToolError("target_roas is required for TARGET_ROAS strategy.")
    campaign.target_roas = client.get_type("TargetRoas")
    campaign.target_roas.target_roas = float(target_roas)
    mask_paths.append("target_roas.target_roas")
    expected_value["target_roas"] = target_roas
  elif st == "TARGET_CPA":
    if target_cpa_micros is None:
      raise ToolError("target_cpa_micros is required for TARGET_CPA strategy.")
    campaign.target_cpa = client.get_type("TargetCpa")
    campaign.target_cpa.target_cpa_micros = int(target_cpa_micros)
    mask_paths.append("target_cpa.target_cpa_micros")
    expected_value["target_cpa_micros"] = target_cpa_micros
  elif st == "MAXIMIZE_CONVERSIONS":
    campaign.maximize_conversions = client.get_type("MaximizeConversions")
    if target_cpa_micros_for_max_conv is not None:
      campaign.maximize_conversions.target_cpa_micros = int(
          target_cpa_micros_for_max_conv
      )
      expected_value["target_cpa_micros_for_max_conv"] = (
          target_cpa_micros_for_max_conv
      )
    mask_paths.append("maximize_conversions.target_cpa_micros")
  elif st == "MAXIMIZE_CONVERSION_VALUE":
    campaign.maximize_conversion_value = client.get_type(
        "MaximizeConversionValue"
    )
    if target_roas_for_max_conv_value is not None:
      campaign.maximize_conversion_value.target_roas = float(
          target_roas_for_max_conv_value
      )
      expected_value["target_roas_for_max_conv_value"] = (
          target_roas_for_max_conv_value
      )
    mask_paths.append("maximize_conversion_value.target_roas")
  elif st == "MANUAL_CPC":
    campaign.manual_cpc = client.get_type("ManualCpc")
    campaign.manual_cpc.enhanced_cpc_enabled = bool(enhanced_cpc)
    mask_paths.append("manual_cpc.enhanced_cpc_enabled")
    expected_value["enhanced_cpc"] = bool(enhanced_cpc)

  operation = service_types.CampaignOperation(update=campaign)
  operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=mask_paths))

  expected_changes = [{
      "resource_name": resource_name,
      "field": st,
      "update_mask_paths": mask_paths,
      "new_value": expected_value,
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
