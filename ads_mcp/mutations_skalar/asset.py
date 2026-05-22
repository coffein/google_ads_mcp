"""Skalar PMAX text-asset mutations.

Performance Max asset groups serve combinations of HEADLINEs, LONG_HEADLINEs,
DESCRIPTIONs (and image / video / business-name assets) that Google's ML
assembles into ads. Rotating the copy for a seasonal action (Pfingst-Rabatt,
Black Week, ...) means swapping out the texts attached to one or more asset
groups — the assets themselves live in the account-level Asset library and
are *linked* to an asset group through AssetGroupAsset resources.

Resource shape:
  Asset                       account-library item with a text_asset payload
  AssetGroupAsset             link: (asset_group, asset, field_type, status)
                              resource_name format:
                              customers/{c}/assetGroupAssets/{ag}~{a}~{ft_int}

API rules to know:
  * An asset group must keep >= 3 enabled HEADLINEs and >= 1 LONG_HEADLINE at
    all times. The API rejects mutations that would drop below this.
  * Removing an AssetGroupAsset only unlinks; the underlying Asset stays in
    the library and any other asset-group links remain intact.
  * field_type in the resource name is the INTEGER value of AssetFieldType.
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

# Hard caps Google enforces server-side; validating client-side gives a
# clearer error than the API's generic STRING_TOO_LONG.
_TEXT_MAX_LEN = {
    "HEADLINE": 30,
    "SHORT_HEADLINE": 30,
    "DESCRIPTION": 90,
    "LONG_HEADLINE": 90,
    "BUSINESS_NAME": 25,
}


def _resolve_field_type(value: str):
  return resolve_enum(
      enum_types.AssetFieldTypeEnum.AssetFieldType, value, "field_type"
  )


def _validate_text_length(text: str, field_type_upper: str) -> None:
  limit = _TEXT_MAX_LEN.get(field_type_upper)
  if limit is not None and len(text) > limit:
    raise ToolError(
        f"text exceeds {limit}-char limit for {field_type_upper}: "
        f"{len(text)} chars in {text!r}"
    )


def _asset_id_from_resource_name(resource_name: str) -> str | None:
  if not resource_name:
    return None
  return resource_name.rsplit("/", 1)[-1]


@mcp.tool()
@audit()
@require_allowed_account
def create_text_asset(
    customer_id: str,
    text: str,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Creates a new TextAsset in the account-level asset library.

  The asset is unlinked: it does not appear in any asset group until you
  call ``link_asset_to_asset_group``.

  Args:
      customer_id: Google Ads customer ID (digits only).
      text: The text content of the asset.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, resource_name, asset_id,
      text, audit_id}``. ``asset_id`` and ``resource_name`` are None in
      dry_run mode (the API does not assign an ID under validate_only).
  """
  if not text:
    raise ToolError("text must be a non-empty string.")
  customer_id = normalise_id(customer_id)
  client = get_client(login_customer_id)

  asset = resource_types.Asset()
  asset.text_asset.text = text

  op = service_types.AssetOperation(create=asset)
  expected_changes = [{"text": text}]

  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AssetService",
      method_name="mutate_assets",
      request_type_name="MutateAssetsRequest",
      customer_id=customer_id,
      operations=[op],
      dry_run=dry_run,
  ))
  base = build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
  resource_name = (
      base["resource_names"][0]
      if base.get("resource_names")
      else None
  )
  base["resource_name"] = resource_name
  base["asset_id"] = _asset_id_from_resource_name(resource_name) if resource_name else None
  base["text"] = text
  return base


@mcp.tool()
@audit()
@require_allowed_account
def link_asset_to_asset_group(
    customer_id: str,
    asset_group_id: str,
    asset_id: str,
    field_type: str,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Links an existing Asset to an AssetGroup with the given field_type.

  Same asset can be linked to many asset groups, and to the same asset
  group under different field_types (though that's unusual).

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_group_id: AssetGroup ID (digits only).
      asset_id: Asset ID to link (digits only).
      field_type: HEADLINE | DESCRIPTION | LONG_HEADLINE | BUSINESS_NAME |
          CALL_TO_ACTION_SELECTION | MARKETING_IMAGE | SQUARE_MARKETING_IMAGE |
          LOGO | LANDSCAPE_LOGO | PORTRAIT_MARKETING_IMAGE | VIDEO |
          YOUTUBE_VIDEO. Case-insensitive.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, resource_name, audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  asset_group_id = normalise_id(asset_group_id)
  asset_id = normalise_id(asset_id)
  field_type_upper = field_type.upper()
  resolved_field_type = _resolve_field_type(field_type_upper)

  client = get_client(login_customer_id)
  asset_group_path = client.get_service("AssetGroupService").asset_group_path(
      customer_id, asset_group_id
  )
  asset_path = client.get_service("AssetService").asset_path(
      customer_id, asset_id
  )

  link = resource_types.AssetGroupAsset(
      asset_group=asset_group_path,
      asset=asset_path,
      field_type=resolved_field_type,
  )
  op = service_types.AssetGroupAssetOperation(create=link)
  expected_changes = [{
      "asset_group": asset_group_path,
      "asset": asset_path,
      "field_type": field_type_upper,
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AssetGroupAssetService",
      method_name="mutate_asset_group_assets",
      request_type_name="MutateAssetGroupAssetsRequest",
      customer_id=customer_id,
      operations=[op],
      dry_run=dry_run,
  ))
  base = build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
  base["resource_name"] = (
      base["resource_names"][0] if base.get("resource_names") else None
  )
  return base


@mcp.tool()
@audit()
@require_allowed_account
def unlink_asset_from_asset_group(
    customer_id: str,
    asset_group_id: str,
    asset_id: str,
    field_type: str,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Unlinks an Asset from an AssetGroup.

  Does NOT delete the underlying Asset; it stays in the account library
  and remains linked to any other asset groups it was attached to.
  ``field_type`` is required because the AssetGroupAsset resource name
  is composite (asset_group_id~asset_id~field_type).

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_group_id: AssetGroup ID (digits only).
      asset_id: Asset ID to unlink (digits only).
      field_type: Same enum as link_asset_to_asset_group; required to
          identify the specific link to remove.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, removed_resource_name,
      audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  asset_group_id = normalise_id(asset_group_id)
  asset_id = normalise_id(asset_id)
  field_type_upper = field_type.upper()
  _resolve_field_type(field_type_upper)  # validate enum name

  client = get_client(login_customer_id)
  # Composite resource name uses the field_type enum NAME (e.g. "HEADLINE"),
  # not the integer value — the API rejects "...~2" with
  # "'2' part of the resource name is invalid".
  resource_name = client.get_service(
      "AssetGroupAssetService"
  ).asset_group_asset_path(
      customer_id, asset_group_id, asset_id, field_type_upper
  )
  op = service_types.AssetGroupAssetOperation(remove=resource_name)
  expected_changes = [{
      "resource_name": resource_name,
      "field_type": field_type_upper,
      "operation": "REMOVE",
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AssetGroupAssetService",
      method_name="mutate_asset_group_assets",
      request_type_name="MutateAssetGroupAssetsRequest",
      customer_id=customer_id,
      operations=[op],
      dry_run=dry_run,
  ))
  base = build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
  base["removed_resource_name"] = (
      base["resource_names"][0]
      if base.get("resource_names")
      else resource_name
  )
  return base


@mcp.tool()
@audit()
@require_allowed_account
def rotate_asset_group_text(
    customer_id: str,
    asset_group_id: str,
    field_type: str,
    texts_to_add: list[str],
    asset_ids_to_unlink: list[str],
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Swaps texts on a PMAX asset group in one safe sequence.

  Operations run create → link → unlink so the PMAX minimum-headline
  constraint (>= 3 enabled HEADLINEs, >= 1 LONG_HEADLINE) is never
  violated mid-batch.

  Three sequential API calls (AssetService.MutateAssets, then two
  AssetGroupAssetService.MutateAssetGroupAssets); each is atomic
  (partial_failure=false). If a later call fails after an earlier one
  succeeded, the partial state stands — typically a few extra assets in
  the library or a few extra links, both safe and reversible by hand.

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_group_id: AssetGroup ID (digits only).
      field_type: HEADLINE | DESCRIPTION | LONG_HEADLINE | etc.
          Applies to BOTH the new links and the unlinks. To rotate
          across multiple field_types, call this tool once per type.
      texts_to_add: New text-asset strings to create + link. May be empty.
      asset_ids_to_unlink: Existing Asset IDs to unlink from this asset
          group at the given field_type. May be empty.
      dry_run: When True (default) the changes are validated but NOT
          applied. In dry_run mode only the create step is validated
          (the link/unlink steps reference IDs that won't exist yet);
          the response still echoes all three planned stages.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, created, linked, unlinked, expected_changes,
      resource_names, audit_id}``. In dry_run mode ``created``,
      ``linked``, and ``unlinked`` are empty lists; ``expected_changes``
      describes what the live call would do.
  """
  customer_id = normalise_id(customer_id)
  asset_group_id = normalise_id(asset_group_id)
  field_type_upper = field_type.upper()
  resolved_field_type = _resolve_field_type(field_type_upper)
  asset_ids_to_unlink = [normalise_id(a) for a in asset_ids_to_unlink]

  for t in texts_to_add:
    if not t:
      raise ToolError("texts_to_add must not contain empty strings.")
    _validate_text_length(t, field_type_upper)

  client = get_client(login_customer_id)
  asset_group_path = client.get_service("AssetGroupService").asset_group_path(
      customer_id, asset_group_id
  )

  expected_changes = (
      [{"stage": "create", "text": t} for t in texts_to_add]
      + [
          {
              "stage": "link",
              "text": t,
              "asset_group": asset_group_path,
              "field_type": field_type_upper,
          }
          for t in texts_to_add
      ]
      + [
          {
              "stage": "unlink",
              "asset_id": aid,
              "asset_group": asset_group_path,
              "field_type": field_type_upper,
          }
          for aid in asset_ids_to_unlink
      ]
  )

  if dry_run:
    # Validate the create step against the API (catches duplicate text,
    # disallowed chars, etc.) — link/unlink can't be validated yet
    # because the new asset IDs don't exist.
    if texts_to_add:
      create_ops = []
      for t in texts_to_add:
        a = resource_types.Asset()
        a.text_asset.text = t
        create_ops.append(service_types.AssetOperation(create=a))
      wrap_google_ads_error(lambda: execute_mutation(
          client=client,
          service_name="AssetService",
          method_name="mutate_assets",
          request_type_name="MutateAssetsRequest",
          customer_id=customer_id,
          operations=create_ops,
          dry_run=True,
      ))
    if asset_ids_to_unlink:
      unlink_ops = []
      svc = client.get_service("AssetGroupAssetService")
      for aid in asset_ids_to_unlink:
        rn = svc.asset_group_asset_path(
            customer_id, asset_group_id, aid, field_type_upper
        )
        unlink_ops.append(service_types.AssetGroupAssetOperation(remove=rn))
      wrap_google_ads_error(lambda: execute_mutation(
          client=client,
          service_name="AssetGroupAssetService",
          method_name="mutate_asset_group_assets",
          request_type_name="MutateAssetGroupAssetsRequest",
          customer_id=customer_id,
          operations=unlink_ops,
          dry_run=True,
      ))
    return {
        "dry_run": True,
        "expected_changes": expected_changes,
        "resource_names": None,
        "created": [],
        "linked": [],
        "unlinked": [],
    }

  # Live execution: create → link → unlink, each atomically.
  created: list[dict[str, str]] = []
  if texts_to_add:
    create_ops = []
    for t in texts_to_add:
      a = resource_types.Asset()
      a.text_asset.text = t
      create_ops.append(service_types.AssetOperation(create=a))
    create_resp = wrap_google_ads_error(lambda: execute_mutation(
        client=client,
        service_name="AssetService",
        method_name="mutate_assets",
        request_type_name="MutateAssetsRequest",
        customer_id=customer_id,
        operations=create_ops,
        dry_run=False,
    ))
    for t, r in zip(texts_to_add, list(create_resp.results)):
      created.append({
          "asset_id": _asset_id_from_resource_name(r.resource_name),
          "resource_name": r.resource_name,
          "text": t,
      })

  asset_service = client.get_service("AssetService")
  link_ops = []
  for c in created:
    link = resource_types.AssetGroupAsset(
        asset_group=asset_group_path,
        asset=asset_service.asset_path(customer_id, c["asset_id"]),
        field_type=resolved_field_type,
    )
    link_ops.append(service_types.AssetGroupAssetOperation(create=link))
  linked: list[str] = []
  if link_ops:
    link_resp = wrap_google_ads_error(lambda: execute_mutation(
        client=client,
        service_name="AssetGroupAssetService",
        method_name="mutate_asset_group_assets",
        request_type_name="MutateAssetGroupAssetsRequest",
        customer_id=customer_id,
        operations=link_ops,
        dry_run=False,
    ))
    linked = [r.resource_name for r in link_resp.results]

  unlinked: list[str] = []
  if asset_ids_to_unlink:
    svc = client.get_service("AssetGroupAssetService")
    unlink_ops = []
    for aid in asset_ids_to_unlink:
      rn = svc.asset_group_asset_path(
          customer_id, asset_group_id, aid, field_type_upper
      )
      unlink_ops.append(service_types.AssetGroupAssetOperation(remove=rn))
    unlink_resp = wrap_google_ads_error(lambda: execute_mutation(
        client=client,
        service_name="AssetGroupAssetService",
        method_name="mutate_asset_group_assets",
        request_type_name="MutateAssetGroupAssetsRequest",
        customer_id=customer_id,
        operations=unlink_ops,
        dry_run=False,
    ))
    unlinked = [r.resource_name for r in unlink_resp.results]

  return {
      "dry_run": False,
      "expected_changes": expected_changes,
      "resource_names": [c["resource_name"] for c in created] + linked + unlinked,
      "created": [{"asset_id": c["asset_id"], "text": c["text"]} for c in created],
      "linked": linked,
      "unlinked": unlinked,
  }


@mcp.tool()
@require_allowed_account
def list_asset_group_assets(
    customer_id: str,
    asset_group_id: str,
    field_type: str | None = None,
    login_customer_id: str | None = None,
) -> dict:
  """Lists assets currently linked to an asset group.

  Read-only — does not write, so no @audit / dry_run.

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_group_id: AssetGroup ID (digits only).
      field_type: Optional filter. HEADLINE | DESCRIPTION | LONG_HEADLINE | ...
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{asset_group_id, field_type, assets: [{asset_id, resource_name,
      field_type, status, text, asset_resource_name}, ...]}``. ``text``
      is None for non-text assets.
  """
  customer_id = normalise_id(customer_id)
  asset_group_id = normalise_id(asset_group_id)
  field_type_upper = field_type.upper() if field_type else None
  if field_type_upper:
    _resolve_field_type(field_type_upper)  # validates the enum

  client = get_client(login_customer_id)
  query_parts = [
      "SELECT asset_group_asset.resource_name,"
      " asset_group_asset.asset,"
      " asset_group_asset.field_type,"
      " asset_group_asset.status,"
      " asset.id,"
      " asset.text_asset.text",
      "FROM asset_group_asset",
      f"WHERE asset_group.id = {asset_group_id}",
  ]
  if field_type_upper:
    query_parts.append(f"AND asset_group_asset.field_type = '{field_type_upper}'")
  query = " ".join(query_parts)

  ga_service = client.get_service("GoogleAdsService")
  rows = []
  for batch in ga_service.search_stream(customer_id=customer_id, query=query):
    for row in batch.results:
      rows.append({
          "asset_id": str(row.asset.id),
          "resource_name": row.asset_group_asset.resource_name,
          "asset_resource_name": row.asset_group_asset.asset,
          "field_type": enum_types.AssetFieldTypeEnum.AssetFieldType(
              row.asset_group_asset.field_type
          ).name,
          "status": enum_types.AssetLinkStatusEnum.AssetLinkStatus(
              row.asset_group_asset.status
          ).name,
          "text": row.asset.text_asset.text or None,
      })
  rows.sort(key=lambda r: int(r["asset_id"]))
  return {
      "asset_group_id": asset_group_id,
      "field_type": field_type_upper,
      "assets": rows,
  }


@mcp.tool()
@audit()
@require_allowed_account
def update_asset_group_asset_status(
    customer_id: str,
    asset_group_id: str,
    asset_id: str,
    field_type: str,
    status: str,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Pauses or re-enables an AssetGroupAsset link without unlinking.

  Useful for "park this headline for next season" — the link stays in
  the resource so the asset_id is preserved, but PMAX stops serving it.

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_group_id: AssetGroup ID (digits only).
      asset_id: Asset ID of the linked asset (digits only).
      field_type: Same enum as link_asset_to_asset_group.
      status: ENABLED | PAUSED | REMOVED. REMOVED is equivalent to
          unlink_asset_from_asset_group.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  customer_id = normalise_id(customer_id)
  asset_group_id = normalise_id(asset_group_id)
  asset_id = normalise_id(asset_id)
  field_type_upper = field_type.upper()
  _resolve_field_type(field_type_upper)  # validate enum name
  resolved_status = resolve_enum(
      enum_types.AssetLinkStatusEnum.AssetLinkStatus, status, "status"
  )

  client = get_client(login_customer_id)
  resource_name = client.get_service(
      "AssetGroupAssetService"
  ).asset_group_asset_path(
      customer_id, asset_group_id, asset_id, field_type_upper
  )
  link = resource_types.AssetGroupAsset(
      resource_name=resource_name, status=resolved_status
  )
  op = service_types.AssetGroupAssetOperation(update=link)
  op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
  expected_changes = [{
      "resource_name": resource_name,
      "field": "status",
      "new_value": status.upper(),
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AssetGroupAssetService",
      method_name="mutate_asset_group_assets",
      request_type_name="MutateAssetGroupAssetsRequest",
      customer_id=customer_id,
      operations=[op],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )
