"""Skalar ad-group-ad mutations: status update + RSA asset update.

Two distinct surfaces because Google Ads splits the AdGroupAd into two
resources:
  * ``AdGroupAdService`` — owns the *link* between an ad and its ad group
    (status, labels, policy). This is what you pause/enable.
  * ``AdService`` — owns the *content* of the Ad (headlines, descriptions,
    URLs, paths). RSA texts are mutable here despite often being called
    "immutable" in older docs.

Using the wrong service for the wrong concern is the most common reason
people conclude "RSAs can't be updated".
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
from google.api_core import protobuf_helpers
from google.protobuf import field_mask_pb2


# RSA limits — same as in search_campaign.py; copied to keep this module
# importable without circular deps.
_RSA_HEADLINE_MAX, _RSA_HEADLINE_LEN = 15, 30
_RSA_DESCRIPTION_MAX, _RSA_DESCRIPTION_LEN = 4, 90
_RSA_PATH_LEN = 15

# Valid pinned-field values for RSA headlines. See ServedAssetFieldType.
_VALID_PINNED_HEADLINE = {"HEADLINE_1", "HEADLINE_2", "HEADLINE_3"}
# Valid pinned-field values for RSA descriptions.
_VALID_PINNED_DESCRIPTION = {"DESCRIPTION_1", "DESCRIPTION_2"}


@mcp.tool()
@audit()
@require_allowed_account
def update_ad_group_ad_status(
    customer_id: str,
    ad_group_id: str,
    ad_id: str,
    status: str,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Pauses, enables, or removes a single ad (AdGroupAd).

  Use this to swap RSAs cleanly: after creating a new RSA with
  ``create_responsive_search_ad``, pause the old one here so only one is
  serving.

  The Google Ads ``AdGroupAd`` resource is keyed by *both* ad-group and
  ad IDs (resource name ``customers/X/adGroupAds/AG~AD``); pass them
  separately. You can find both via GAQL on the ``ad_group_ad`` view
  (``ad_group_ad.ad_group``, ``ad_group_ad.ad.id``).

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_group_id: Ad group ID (digits only).
      ad_id: Ad ID (digits only) — *not* the AdGroupAd composite ID.
      status: ENABLED, PAUSED, or REMOVED. REMOVED is irreversible.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  ad_group_id = normalise_id(ad_group_id)
  ad_id = normalise_id(ad_id)
  client = get_client(login_customer_id)
  resource_name = client.get_service("AdGroupAdService").ad_group_ad_path(
      customer_id, ad_group_id, ad_id
  )

  resolved_status = resolve_enum(
      enum_types.AdGroupAdStatusEnum.AdGroupAdStatus, status, "status"
  )
  ad_group_ad = resource_types.AdGroupAd(
      resource_name=resource_name, status=resolved_status
  )
  operation = service_types.AdGroupAdOperation(update=ad_group_ad)
  operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))

  expected_changes = [{
      "resource_name": resource_name,
      "field": "status",
      "new_value": status.upper(),
  }]

  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AdGroupAdService",
      method_name="mutate_ad_group_ads",
      request_type_name="MutateAdGroupAdsRequest",
      customer_id=customer_id,
      operations=[operation],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )


def _build_text_assets(client, items, *, label: str, valid_pinned: set[str]):
  """Builds a list of AdTextAsset from ``[{text, pinned_field?}]`` dicts.

  The pinned_field, when set, must come from ``ServedAssetFieldType``;
  invalid names are rejected up-front rather than letting the API echo a
  cryptic error.
  """
  assets = []
  pinned_enum = enum_types.ServedAssetFieldTypeEnum.ServedAssetFieldType
  for i, item in enumerate(items):
    text = item.get("text") if isinstance(item, dict) else item
    if not text or not isinstance(text, str):
      raise ToolError(f"{label}[{i}]: 'text' is required")
    asset = client.get_type("AdTextAsset")
    asset.text = text
    if isinstance(item, dict) and item.get("pinned_field"):
      pf = str(item["pinned_field"]).upper()
      if pf not in valid_pinned:
        raise ToolError(
            f"{label}[{i}].pinned_field {pf!r} invalid for {label}; "
            f"valid: {sorted(valid_pinned)}"
        )
      asset.pinned_field = resolve_enum(
          pinned_enum, pf, f"{label}[{i}].pinned_field"
      )
    assets.append(asset)
  return assets


@mcp.tool()
@audit()
@require_allowed_account
def update_responsive_search_ad(
    customer_id: str,
    ad_id: str,
    headlines: list[dict | str] | None = None,
    descriptions: list[dict | str] | None = None,
    final_urls: list[str] | None = None,
    path1: str | None = None,
    path2: str | None = None,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Updates the texts/URLs/paths of an existing Responsive Search Ad.

  Goes through ``AdService.mutate_ads`` (not ``AdGroupAdService``), which
  is what the Google sample
  https://developers.google.com/google-ads/api/samples/update-responsive-search-ad
  does. Only the fields you pass end up in the update_mask — unpassed
  fields are left untouched.

  Wholesale replacement applies: if you pass ``headlines``, the *entire*
  current headline list is replaced. Same for descriptions and final_urls.
  Pass items as ``{"text": "...", "pinned_field": "HEADLINE_1"}`` to keep
  a slot pinned, or just ``"..."`` for unpinned. Descriptions accept the
  same shape with ``DESCRIPTION_1``/``DESCRIPTION_2``.

  Find ``ad_id`` via GAQL on the ``ad_group_ad`` view (``ad_group_ad.ad.id``).

  Args:
      customer_id: Google Ads customer ID (digits only).
      ad_id: Ad ID (digits only). Note: this is the ``Ad.id``, *not* the
          AdGroupAd composite key.
      headlines: 3..15 entries. Each either ``"text"`` or
          ``{"text": "...", "pinned_field": "HEADLINE_1|2|3"}``. Limit 30
          chars per text. Pass None to leave headlines unchanged.
      descriptions: 2..4 entries, same shape, pinned_field one of
          DESCRIPTION_1/DESCRIPTION_2. Limit 90 chars. None = unchanged.
      final_urls: List of landing-page URLs. None = unchanged. Empty list
          is rejected — Google requires at least one when the field is set.
      path1: Display-URL path segment 1 (<= 15 chars). None = unchanged.
      path2: Display-URL path segment 2 (<= 15 chars). None = unchanged.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  if (
      headlines is None
      and descriptions is None
      and final_urls is None
      and path1 is None
      and path2 is None
  ):
    raise ToolError(
        "At least one of headlines, descriptions, final_urls, path1, "
        "path2 must be provided"
    )

  customer_id = normalise_id(customer_id)
  ad_id = normalise_id(ad_id)
  client = get_client(login_customer_id)
  resource_name = client.get_service("AdService").ad_path(customer_id, ad_id)

  ad = resource_types.Ad()
  ad.resource_name = resource_name
  changes: dict = {"resource_name": resource_name}

  if headlines is not None:
    if not (3 <= len(headlines) <= _RSA_HEADLINE_MAX):
      raise ToolError(
          f"headlines must have 3..{_RSA_HEADLINE_MAX} entries when set, "
          f"got {len(headlines)}"
      )
    for i, h in enumerate(headlines):
      text = h.get("text") if isinstance(h, dict) else h
      if not isinstance(text, str) or len(text) > _RSA_HEADLINE_LEN:
        raise ToolError(
            f"headlines[{i}] text too long or invalid "
            f"(max {_RSA_HEADLINE_LEN} chars)"
        )
    assets = _build_text_assets(
        client, headlines, label="headlines",
        valid_pinned=_VALID_PINNED_HEADLINE,
    )
    for a in assets:
      ad.responsive_search_ad.headlines.append(a)
    changes["headlines_count"] = len(headlines)

  if descriptions is not None:
    if not (2 <= len(descriptions) <= _RSA_DESCRIPTION_MAX):
      raise ToolError(
          f"descriptions must have 2..{_RSA_DESCRIPTION_MAX} entries when "
          f"set, got {len(descriptions)}"
      )
    for i, d in enumerate(descriptions):
      text = d.get("text") if isinstance(d, dict) else d
      if not isinstance(text, str) or len(text) > _RSA_DESCRIPTION_LEN:
        raise ToolError(
            f"descriptions[{i}] text too long or invalid "
            f"(max {_RSA_DESCRIPTION_LEN} chars)"
        )
    assets = _build_text_assets(
        client, descriptions, label="descriptions",
        valid_pinned=_VALID_PINNED_DESCRIPTION,
    )
    for a in assets:
      ad.responsive_search_ad.descriptions.append(a)
    changes["descriptions_count"] = len(descriptions)

  if final_urls is not None:
    if not final_urls:
      raise ToolError(
          "final_urls is set but empty; pass None to leave unchanged or "
          "include at least one URL"
      )
    for url in final_urls:
      ad.final_urls.append(url)
    changes["final_urls"] = list(final_urls)

  if path1 is not None:
    if len(path1) > _RSA_PATH_LEN:
      raise ToolError(
          f"path1 too long ({len(path1)} > {_RSA_PATH_LEN} chars)"
      )
    ad.responsive_search_ad.path1 = path1
    changes["path1"] = path1

  if path2 is not None:
    if len(path2) > _RSA_PATH_LEN:
      raise ToolError(
          f"path2 too long ({len(path2)} > {_RSA_PATH_LEN} chars)"
      )
    ad.responsive_search_ad.path2 = path2
    changes["path2"] = path2

  operation = service_types.AdOperation(update=ad)
  # The mask is derived from whatever fields we actually set above; this
  # matches the official update-RSA sample and avoids the need to spell
  # the field paths out by hand.
  operation.update_mask.CopyFrom(
      protobuf_helpers.field_mask(None, ad._pb)
  )

  expected_changes = [changes]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AdService",
      method_name="mutate_ads",
      request_type_name="MutateAdsRequest",
      customer_id=customer_id,
      operations=[operation],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
