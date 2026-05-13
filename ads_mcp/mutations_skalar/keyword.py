"""Skalar keyword mutations: status, bulk-add, negatives in shared sets."""

from __future__ import annotations

from typing import TypedDict

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


class Keyword(TypedDict, total=False):
  """One keyword to add to an ad group."""

  text: str  # required: the keyword text
  match_type: str  # required: EXACT | PHRASE | BROAD
  cpc_bid_micros: int  # optional: per-keyword CPC bid in micros
  status: str  # optional: ENABLED (default) | PAUSED


@mcp.tool()
@audit()
@require_allowed_account
def update_keyword_status(
    customer_id: str,
    ad_group_id: str,
    criterion_id: str,
    status: str,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Pauses, enables, or removes a single keyword (ad-group criterion).

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_group_id: Ad group ID containing the keyword.
      criterion_id: Criterion ID of the keyword.
      status: ENABLED, PAUSED, or REMOVED.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  ad_group_id = normalise_id(ad_group_id)
  criterion_id = normalise_id(criterion_id)
  client = get_client(login_customer_id)
  service = client.get_service("AdGroupCriterionService")
  resource_name = service.ad_group_criterion_path(
      customer_id, ad_group_id, criterion_id
  )

  resolved_status = resolve_enum(
      enum_types.AdGroupCriterionStatusEnum.AdGroupCriterionStatus,
      status,
      "status",
  )
  criterion = resource_types.AdGroupCriterion(
      resource_name=resource_name, status=resolved_status
  )
  operation = service_types.AdGroupCriterionOperation(update=criterion)
  operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))

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
      operations=[operation],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )


@mcp.tool()
@audit()
@require_allowed_account
def add_keywords_to_ad_group(
    customer_id: str,
    ad_group_id: str,
    keywords: list[Keyword],
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Bulk-adds keywords to an ad group.

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_group_id: Target ad group ID.
      keywords: List of dicts with ``text`` (str), ``match_type``
          (EXACT|PHRASE|BROAD), optional ``cpc_bid_micros`` (int) and
          optional ``status`` (default ENABLED).
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  if not keywords:
    raise ToolError("keywords must not be empty")

  customer_id = normalise_id(customer_id)
  ad_group_id = normalise_id(ad_group_id)
  client = get_client(login_customer_id)
  ad_group_path = client.get_service("AdGroupService").ad_group_path(
      customer_id, ad_group_id
  )

  match_type_enum = enum_types.KeywordMatchTypeEnum.KeywordMatchType
  status_enum = enum_types.AdGroupCriterionStatusEnum.AdGroupCriterionStatus

  operations = []
  expected_changes: list[dict] = []
  for idx, kw in enumerate(keywords):
    text = kw.get("text")
    match_type = kw.get("match_type")
    if not text or not match_type:
      raise ToolError(
          f"keywords[{idx}]: 'text' and 'match_type' are required"
      )
    criterion = resource_types.AdGroupCriterion(
        ad_group=ad_group_path,
        status=resolve_enum(
            status_enum, kw.get("status", "ENABLED"), f"keywords[{idx}].status"
        ),
    )
    criterion.keyword.text = text
    criterion.keyword.match_type = resolve_enum(
        match_type_enum, match_type, f"keywords[{idx}].match_type"
    )
    if "cpc_bid_micros" in kw:
      criterion.cpc_bid_micros = int(kw["cpc_bid_micros"])
    operations.append(service_types.AdGroupCriterionOperation(create=criterion))
    expected_changes.append({
        "ad_group": ad_group_path,
        "text": text,
        "match_type": match_type.upper(),
        "status": kw.get("status", "ENABLED").upper(),
        "cpc_bid_micros": kw.get("cpc_bid_micros"),
    })

  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AdGroupCriterionService",
      method_name="mutate_ad_group_criteria",
      request_type_name="MutateAdGroupCriteriaRequest",
      customer_id=customer_id,
      operations=operations,
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )


@mcp.tool()
@audit()
@require_allowed_account
def add_negative_keywords_to_shared_set(
    customer_id: str,
    shared_set_id: str,
    keywords: list[str],
    match_type: str = "EXACT",
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Adds negative keywords to a shared negative-keyword list.

  Args:
      customer_id: Google Ads customer ID (digits only).
      shared_set_id: Shared set ID (must be a NEGATIVE_KEYWORDS shared set).
      keywords: List of keyword strings (the same match_type applies to all).
      match_type: EXACT | PHRASE | BROAD. Default EXACT.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  if not keywords:
    raise ToolError("keywords must not be empty")

  customer_id = normalise_id(customer_id)
  shared_set_id = normalise_id(shared_set_id)
  client = get_client(login_customer_id)
  shared_set_path = client.get_service("SharedSetService").shared_set_path(
      customer_id, shared_set_id
  )

  match_type_enum = enum_types.KeywordMatchTypeEnum.KeywordMatchType
  resolved_mt = resolve_enum(match_type_enum, match_type, "match_type")

  operations = []
  expected_changes: list[dict] = []
  for text in keywords:
    if not text or not isinstance(text, str):
      raise ToolError(f"keyword must be a non-empty string, got {text!r}")
    sc = resource_types.SharedCriterion(shared_set=shared_set_path)
    sc.keyword.text = text
    sc.keyword.match_type = resolved_mt
    operations.append(service_types.SharedCriterionOperation(create=sc))
    expected_changes.append({
        "shared_set": shared_set_path,
        "text": text,
        "match_type": match_type.upper(),
    })

  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="SharedCriterionService",
      method_name="mutate_shared_criteria",
      request_type_name="MutateSharedCriteriaRequest",
      customer_id=customer_id,
      operations=operations,
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
