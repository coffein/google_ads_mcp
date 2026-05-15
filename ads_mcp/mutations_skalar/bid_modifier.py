"""Skalar bid-modifier mutations on campaign-level criteria.

A bid_modifier of 1.20 means "bid 20% more" for matches against this
criterion; 0.80 means "bid 20% less"; 0.0 specifically excludes (e.g.
device=MOBILE with modifier=0 excludes mobile entirely).

We split DEVICE / LOCATION / AD_SCHEDULE into dedicated tools rather than
one polymorphic ``criterion`` parameter — three short signatures the LLM
can reason about cleanly, instead of one tool whose required params shift
based on a discriminator string.

Each tool is a setter (upsert): it queries whether the criterion exists
on the campaign and either UPDATEs the bid_modifier or CREATEs a new
criterion. Without this, the LLM would have to know the criterion's
state before calling.
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


def _existing_campaign_criterion(
    client, customer_id: str, where: str
) -> str | None:
  """Returns resource_name of a matching campaign_criterion, or None."""
  ga = client.get_service("GoogleAdsService")
  query = f"SELECT campaign_criterion.resource_name FROM campaign_criterion WHERE {where}"
  for batch in ga.search_stream(customer_id=customer_id, query=query):
    for row in batch.results:
      return row.campaign_criterion.resource_name
  return None


def _upsert_campaign_criterion(
    *,
    client,
    customer_id: str,
    existing_resource_name: str | None,
    new_criterion: Any,  # CampaignCriterion already populated
    bid_modifier: float,
    dry_run: bool,
) -> Any:
  if existing_resource_name:
    update = resource_types.CampaignCriterion(
        resource_name=existing_resource_name, bid_modifier=bid_modifier
    )
    op = service_types.CampaignCriterionOperation(update=update)
    op.update_mask.CopyFrom(
        field_mask_pb2.FieldMask(paths=["bid_modifier"])
    )
  else:
    new_criterion.bid_modifier = bid_modifier
    op = service_types.CampaignCriterionOperation(create=new_criterion)
  return wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="CampaignCriterionService",
      method_name="mutate_campaign_criteria",
      request_type_name="MutateCampaignCriteriaRequest",
      customer_id=customer_id,
      operations=[op],
      dry_run=dry_run,
  ))


@mcp.tool()
@audit()
@require_allowed_account
def set_campaign_device_bid_modifier(
    customer_id: str,
    campaign_id: str,
    device: str,
    bid_modifier: float,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Sets (creates or updates) the bid modifier for a device on a campaign.

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_id: Campaign ID (digits only).
      device: MOBILE | TABLET | DESKTOP | CONNECTED_TV | OTHER.
      bid_modifier: 1.20 = +20%, 0.80 = -20%, 0.0 = exclude device.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  campaign_id = normalise_id(campaign_id)
  client = get_client(login_customer_id)
  campaign_path = client.get_service("CampaignService").campaign_path(
      customer_id, campaign_id
  )
  device_enum = resolve_enum(
      enum_types.DeviceEnum.Device, device, "device"
  )
  existing = _existing_campaign_criterion(
      client,
      customer_id,
      f"campaign.id = {campaign_id} AND campaign_criterion.device.type = '{device.upper()}'",
  )
  cc = resource_types.CampaignCriterion(campaign=campaign_path)
  cc.device.type_ = device_enum
  expected_changes = [{
      "campaign": campaign_path,
      "criterion": {"device": device.upper()},
      "bid_modifier": bid_modifier,
      "operation": "UPDATE" if existing else "CREATE",
  }]
  response = _upsert_campaign_criterion(
      client=client,
      customer_id=customer_id,
      existing_resource_name=existing,
      new_criterion=cc,
      bid_modifier=bid_modifier,
      dry_run=dry_run,
  )
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )


@mcp.tool()
@audit()
@require_allowed_account
def set_campaign_location_bid_modifier(
    customer_id: str,
    campaign_id: str,
    geo_target_constant_id: str,
    bid_modifier: float,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Sets (creates or updates) the bid modifier for a geo target on a campaign.

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_id: Campaign ID (digits only).
      geo_target_constant_id: Numeric ID from geo_target_constants
          (e.g. 2276 = Germany, 1023191 = Berlin). Look up via
          GoogleAdsService.search query 'SELECT geo_target_constant.* FROM
          geo_target_constant WHERE …'.
      bid_modifier: 1.20 = +20%, 0.80 = -20%, 0.0 = exclude region.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  campaign_id = normalise_id(campaign_id)
  geo_id = normalise_id(geo_target_constant_id)
  client = get_client(login_customer_id)
  campaign_path = client.get_service("CampaignService").campaign_path(
      customer_id, campaign_id
  )
  geo_constant = (
      client.get_service("GeoTargetConstantService").geo_target_constant_path(
          geo_id
      )
  )
  existing = _existing_campaign_criterion(
      client,
      customer_id,
      f"campaign.id = {campaign_id} AND campaign_criterion.location.geo_target_constant = '{geo_constant}'",
  )
  cc = resource_types.CampaignCriterion(campaign=campaign_path)
  cc.location.geo_target_constant = geo_constant
  expected_changes = [{
      "campaign": campaign_path,
      "criterion": {"geo_target_constant": geo_constant},
      "bid_modifier": bid_modifier,
      "operation": "UPDATE" if existing else "CREATE",
  }]
  response = _upsert_campaign_criterion(
      client=client,
      customer_id=customer_id,
      existing_resource_name=existing,
      new_criterion=cc,
      bid_modifier=bid_modifier,
      dry_run=dry_run,
  )
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )


@mcp.tool()
@audit()
@require_allowed_account
def set_campaign_ad_schedule_bid_modifier(
    customer_id: str,
    campaign_id: str,
    day_of_week: str,
    start_hour: int,
    end_hour: int,
    bid_modifier: float,
    start_minute: str = "ZERO",
    end_minute: str = "ZERO",
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Sets (creates or updates) the bid modifier for an ad-schedule slot.

  Slots are matched on (day_of_week, start_hour, start_minute, end_hour,
  end_minute) — to update an existing slot, pass exactly the same time
  spec.

  Args:
      customer_id: Google Ads customer ID (digits only).
      campaign_id: Campaign ID (digits only).
      day_of_week: MONDAY | TUESDAY | … | SUNDAY.
      start_hour: 0..23.
      end_hour: 1..24 (24 means end-of-day).
      bid_modifier: 1.20 = +20%, 0.80 = -20%.
      start_minute: ZERO | FIFTEEN | THIRTY | FORTY_FIVE. Default ZERO.
      end_minute: same enum. Default ZERO.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  if not (0 <= start_hour <= 23):
    raise ToolError(f"start_hour must be in 0..23, got {start_hour}")
  if not (1 <= end_hour <= 24):
    raise ToolError(f"end_hour must be in 1..24, got {end_hour}")
  customer_id = normalise_id(customer_id)
  campaign_id = normalise_id(campaign_id)
  client = get_client(login_customer_id)
  campaign_path = client.get_service("CampaignService").campaign_path(
      customer_id, campaign_id
  )
  dow_enum = resolve_enum(
      enum_types.DayOfWeekEnum.DayOfWeek, day_of_week, "day_of_week"
  )
  start_min_enum = resolve_enum(
      enum_types.MinuteOfHourEnum.MinuteOfHour, start_minute, "start_minute"
  )
  end_min_enum = resolve_enum(
      enum_types.MinuteOfHourEnum.MinuteOfHour, end_minute, "end_minute"
  )
  where = (
      f"campaign.id = {campaign_id} "
      f"AND campaign_criterion.ad_schedule.day_of_week = '{day_of_week.upper()}' "
      f"AND campaign_criterion.ad_schedule.start_hour = {start_hour} "
      f"AND campaign_criterion.ad_schedule.end_hour = {end_hour}"
  )
  existing = _existing_campaign_criterion(client, customer_id, where)
  cc = resource_types.CampaignCriterion(campaign=campaign_path)
  cc.ad_schedule.day_of_week = dow_enum
  cc.ad_schedule.start_hour = start_hour
  cc.ad_schedule.end_hour = end_hour
  cc.ad_schedule.start_minute = start_min_enum
  cc.ad_schedule.end_minute = end_min_enum
  expected_changes = [{
      "campaign": campaign_path,
      "criterion": {
          "ad_schedule": {
              "day_of_week": day_of_week.upper(),
              "start_hour": start_hour,
              "start_minute": start_minute.upper(),
              "end_hour": end_hour,
              "end_minute": end_minute.upper(),
          }
      },
      "bid_modifier": bid_modifier,
      "operation": "UPDATE" if existing else "CREATE",
  }]
  response = _upsert_campaign_criterion(
      client=client,
      customer_id=customer_id,
      existing_resource_name=existing,
      new_criterion=cc,
      bid_modifier=bid_modifier,
      dry_run=dry_run,
  )
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
