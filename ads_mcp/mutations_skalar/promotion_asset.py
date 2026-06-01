"""Skalar Promotion-Asset ("Aktionen") mutations.

Promotion Assets are ad extensions that display a percent/money off,
optionally a promo code and an occasion tag, for a bounded date range.
They live in the account-level Asset library and are attached to serving
via *Asset link resources at customer, campaign, or ad-group scope.

Resource shape:
  Asset                  account-library item; promotion_asset payload sits
                         alongside final_urls on the wrapper Asset
  CustomerAsset          link: (asset, field_type=PROMOTION, status)
                         resource_name: customers/{c}/customerAssets/{a}~{ft_name}
  CampaignAsset          link: (campaign, asset, field_type=PROMOTION, status)
                         resource_name: customers/{c}/campaignAssets/{cmp}~{a}~{ft_name}
  AdGroupAsset           link: (ad_group, asset, field_type=PROMOTION, status)
                         resource_name: customers/{c}/adGroupAssets/{ag}~{a}~{ft_name}

API rules to know:
  * percent_off is INT64 of MICROS where 1_000_000 == 100% (so 1% = 10_000
    and 20.0% = 200_000). The tool accepts the friendly float (e.g. 20.0)
    and applies the conversion.
  * Exactly one of percent_off / money_amount_off may be set per asset.
  * promotion_code and orders_over_amount are independent optional fields
    (you can have either, both, or neither).
  * end_date >= start_date; the redemption window must overlap the display window.
  * Promotion Assets are MUTABLE — update_promotion_asset edits in place.
  * The composite link-resource path uses the field_type enum NAME
    ("PROMOTION"), NOT the integer value 8 — same bug class as the
    earlier AssetGroupAsset HEADLINE composite-name bug.
"""

from __future__ import annotations

from datetime import date
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

_FIELD_TYPE_PROMOTION = "PROMOTION"

_PROMOTION_TARGET_MAX_LEN = 20
_PROMOTION_CODE_MAX_LEN = 15
_TERMS_MAX_LEN = 200
_PERCENT_TO_MICROS = 10_000  # Google Ads scales percent_off so 1_000_000 = 100%
_PERCENT_MIN = 1.0
_PERCENT_MAX = 100.0

# Temporary negative ID for the not-yet-created Asset in create_and_link_promotion
# so the *AssetOperation link creates in the same batch can reference it.
# https://developers.google.com/google-ads/api/docs/mutating/best-practices#temporary_resource_names
_TEMP_ASSET_ID = "-1"


def _parse_date(value: str, name: str) -> date:
  try:
    return date.fromisoformat(value)
  except (TypeError, ValueError) as exc:
    raise ToolError(
        f"{name} must be YYYY-MM-DD, got {value!r}"
    ) from exc


def _validate_dates(
    start_date: str,
    end_date: str,
    redemption_start_date: str | None,
    redemption_end_date: str | None,
) -> tuple[str, str]:
  """Returns the resolved (redemption_start, redemption_end) after validation."""
  start = _parse_date(start_date, "start_date")
  end = _parse_date(end_date, "end_date")
  if end < start:
    raise ToolError(
        f"end_date ({end_date}) must be >= start_date ({start_date})."
    )
  redeem_start = redemption_start_date or start_date
  redeem_end = redemption_end_date or end_date
  rs = _parse_date(redeem_start, "redemption_start_date")
  re_ = _parse_date(redeem_end, "redemption_end_date")
  if re_ < rs:
    raise ToolError(
        f"redemption_end_date ({redeem_end}) must be >= "
        f"redemption_start_date ({redeem_start})."
    )
  # Overlap: [start, end] ∩ [rs, re_] must be non-empty.
  if re_ < start or rs > end:
    raise ToolError(
        f"redemption window [{redeem_start}, {redeem_end}] must overlap "
        f"the display window [{start_date}, {end_date}]."
    )
  return redeem_start, redeem_end


def _validate_discount(
    percent_off: float | None,
    money_amount_off_micros: int | None,
    money_amount_off_currency_code: str | None,
) -> None:
  has_percent = percent_off is not None
  has_money = money_amount_off_micros is not None
  if has_percent == has_money:
    raise ToolError(
        "Exactly one of percent_off / money_amount_off_micros must be provided."
    )
  if has_percent and not (_PERCENT_MIN <= percent_off <= _PERCENT_MAX):
    raise ToolError(
        f"percent_off must be in [{_PERCENT_MIN}, {_PERCENT_MAX}], got {percent_off}."
    )
  if has_money:
    if money_amount_off_micros <= 0:
      raise ToolError(
          f"money_amount_off_micros must be > 0, got {money_amount_off_micros}."
      )
    if not money_amount_off_currency_code:
      raise ToolError(
          "money_amount_off_currency_code is required when "
          "money_amount_off_micros is set."
      )


def _validate_text_lengths(
    promotion_target: str | None,
    promotion_code: str | None,
    terms_and_conditions_text: str | None,
) -> None:
  if promotion_target is not None:
    if not promotion_target:
      raise ToolError("promotion_target must be a non-empty string.")
    if len(promotion_target) > _PROMOTION_TARGET_MAX_LEN:
      raise ToolError(
          f"promotion_target exceeds {_PROMOTION_TARGET_MAX_LEN}-char limit: "
          f"{len(promotion_target)} chars in {promotion_target!r}."
      )
  if promotion_code is not None and len(promotion_code) > _PROMOTION_CODE_MAX_LEN:
    raise ToolError(
        f"promotion_code exceeds {_PROMOTION_CODE_MAX_LEN}-char limit: "
        f"{len(promotion_code)} chars."
    )
  if (
      terms_and_conditions_text is not None
      and len(terms_and_conditions_text) > _TERMS_MAX_LEN
  ):
    raise ToolError(
        f"terms_and_conditions_text exceeds {_TERMS_MAX_LEN}-char limit: "
        f"{len(terms_and_conditions_text)} chars."
    )


def _populate_promotion_asset(
    *,
    asset,
    promotion_target: str,
    language_code: str,
    start_date: str,
    end_date: str,
    redemption_start_date: str,
    redemption_end_date: str,
    percent_off: float | None,
    money_amount_off_micros: int | None,
    money_amount_off_currency_code: str | None,
    promotion_code: str | None,
    occasion: str | None,
    orders_over_amount_micros: int | None,
    orders_over_currency_code: str | None,
    discount_modifier: str | None,
    terms_and_conditions_text: str | None,
    final_urls: list[str] | None,
) -> None:
  """Fills ``asset.promotion_asset`` (+ asset.final_urls) for a CREATE op."""
  pa = asset.promotion_asset
  pa.promotion_target = promotion_target
  pa.language_code = language_code
  pa.start_date = start_date
  pa.end_date = end_date
  pa.redemption_start_date = redemption_start_date
  pa.redemption_end_date = redemption_end_date

  if percent_off is not None:
    pa.percent_off = int(round(percent_off * _PERCENT_TO_MICROS))
  else:
    pa.money_amount_off.amount_micros = int(money_amount_off_micros)
    pa.money_amount_off.currency_code = money_amount_off_currency_code

  if promotion_code is not None:
    pa.promotion_code = promotion_code

  if orders_over_amount_micros is not None:
    if not orders_over_currency_code:
      raise ToolError(
          "orders_over_currency_code is required when "
          "orders_over_amount_micros is set."
      )
    pa.orders_over_amount.amount_micros = int(orders_over_amount_micros)
    pa.orders_over_amount.currency_code = orders_over_currency_code

  if occasion is not None:
    pa.occasion = resolve_enum(
        enum_types.PromotionExtensionOccasionEnum.PromotionExtensionOccasion,
        occasion,
        "occasion",
    )

  if discount_modifier is not None:
    pa.discount_modifier = resolve_enum(
        enum_types.PromotionExtensionDiscountModifierEnum.PromotionExtensionDiscountModifier,
        discount_modifier,
        "discount_modifier",
    )

  if terms_and_conditions_text is not None:
    pa.terms_and_conditions_text = terms_and_conditions_text

  if final_urls:
    for url in final_urls:
      asset.final_urls.append(url)


def _summarise(
    *,
    promotion_target: str,
    percent_off: float | None,
    money_amount_off_micros: int | None,
    money_amount_off_currency_code: str | None,
    start_date: str,
    end_date: str,
    promotion_code: str | None,
    occasion: str | None,
) -> str:
  if percent_off is not None:
    disc = f"{percent_off:g}% off"
  else:
    amt = (money_amount_off_micros or 0) / 1_000_000
    disc = f"{amt:.2f} {money_amount_off_currency_code} off"
  code = f", code {promotion_code}" if promotion_code else ""
  occ = f" [{occasion.upper()}]" if occasion else ""
  return f"{disc} on {promotion_target}{code} ({start_date} → {end_date}){occ}"


def _link_service_config(
    scope: str, *, campaign_id: str | None, ad_group_id: str | None
) -> dict:
  scope_upper = scope.upper()
  if scope_upper == "CUSTOMER":
    return {
        "service_name": "CustomerAssetService",
        "method_name": "mutate_customer_assets",
        "request_type_name": "MutateCustomerAssetsRequest",
    }
  if scope_upper == "CAMPAIGN":
    if not campaign_id:
      raise ToolError("scope=CAMPAIGN requires campaign_id.")
    return {
        "service_name": "CampaignAssetService",
        "method_name": "mutate_campaign_assets",
        "request_type_name": "MutateCampaignAssetsRequest",
    }
  if scope_upper == "AD_GROUP":
    if not ad_group_id:
      raise ToolError("scope=AD_GROUP requires ad_group_id.")
    return {
        "service_name": "AdGroupAssetService",
        "method_name": "mutate_ad_group_assets",
        "request_type_name": "MutateAdGroupAssetsRequest",
    }
  raise ToolError(
      f"scope must be CUSTOMER | CAMPAIGN | AD_GROUP, got {scope!r}."
  )


@mcp.tool()
@audit()
@require_allowed_account
def create_promotion_asset(
    customer_id: str,
    promotion_target: str,
    language_code: str,
    start_date: str,
    end_date: str,
    percent_off: float | None = None,
    money_amount_off_micros: int | None = None,
    money_amount_off_currency_code: str | None = None,
    promotion_code: str | None = None,
    occasion: str | None = None,
    redemption_start_date: str | None = None,
    redemption_end_date: str | None = None,
    orders_over_amount_micros: int | None = None,
    orders_over_currency_code: str | None = None,
    discount_modifier: str | None = None,
    terms_and_conditions_text: str | None = None,
    final_urls: list[str] | None = None,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Creates a PromotionAsset ("Aktion") in the account-level asset library.

  The asset is unlinked: it does not appear in any account / campaign /
  ad-group until linked via ``link_promotion_asset`` or via the bundled
  ``create_and_link_promotion``.

  Args:
      customer_id: Google Ads customer ID (digits only).
      promotion_target: What's discounted (<= 20 chars, e.g. "alle Böden").
      language_code: BCP-47 language code, e.g. "de".
      start_date: Display-window start (YYYY-MM-DD).
      end_date: Display-window end (YYYY-MM-DD); >= start_date.
      percent_off: Discount percent in [1.0, 100.0]. Stored on the API
          as percent × 10_000 (Google's scale: 1_000_000 = 100%).
          Mutually exclusive with money_amount_off_micros.
      money_amount_off_micros: Fixed discount in micros of the given
          currency. Mutually exclusive with percent_off.
      money_amount_off_currency_code: ISO 4217 currency for the money
          discount (required when money_amount_off_micros is set).
      promotion_code: Optional promo code (<= 15 chars, e.g. "WM20").
      occasion: Optional PromotionExtensionOccasion enum
          (NEW_YEARS | EASTER | BLACK_FRIDAY | CHRISTMAS | WINTER_SALE |
          SUMMER_SALE | FALL_SALE | SPRING_SALE | CYBER_MONDAY | ...).
          Invalid values produce a clear error listing the valid set.
      redemption_start_date: Optional; defaults to start_date. Must overlap
          the [start_date, end_date] window.
      redemption_end_date: Optional; defaults to end_date.
      orders_over_amount_micros: Optional minimum-order threshold in micros.
      orders_over_currency_code: ISO 4217 currency (required if
          orders_over_amount_micros is set).
      discount_modifier: Optional "UP_TO" to display "Bis zu X% sparen".
      terms_and_conditions_text: Optional small-print text (<= 200 chars).
      final_urls: Optional landing-page URLs.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, resource_name,
      asset_id, summary, audit_id}``. ``resource_name`` and ``asset_id``
      are None in dry_run mode.
  """
  _validate_text_lengths(promotion_target, promotion_code, terms_and_conditions_text)
  _validate_discount(percent_off, money_amount_off_micros, money_amount_off_currency_code)
  redemption_start_date, redemption_end_date = _validate_dates(
      start_date, end_date, redemption_start_date, redemption_end_date
  )

  customer_id = normalise_id(customer_id)
  client = get_client(login_customer_id)

  asset = resource_types.Asset()
  _populate_promotion_asset(
      asset=asset,
      promotion_target=promotion_target,
      language_code=language_code,
      start_date=start_date,
      end_date=end_date,
      redemption_start_date=redemption_start_date,
      redemption_end_date=redemption_end_date,
      percent_off=percent_off,
      money_amount_off_micros=money_amount_off_micros,
      money_amount_off_currency_code=money_amount_off_currency_code,
      promotion_code=promotion_code,
      occasion=occasion,
      orders_over_amount_micros=orders_over_amount_micros,
      orders_over_currency_code=orders_over_currency_code,
      discount_modifier=discount_modifier,
      terms_and_conditions_text=terms_and_conditions_text,
      final_urls=final_urls,
  )
  op = service_types.AssetOperation(create=asset)

  summary = _summarise(
      promotion_target=promotion_target,
      percent_off=percent_off,
      money_amount_off_micros=money_amount_off_micros,
      money_amount_off_currency_code=money_amount_off_currency_code,
      start_date=start_date,
      end_date=end_date,
      promotion_code=promotion_code,
      occasion=occasion,
  )
  expected_changes = [{
      "promotion_target": promotion_target,
      "language_code": language_code,
      "start_date": start_date,
      "end_date": end_date,
      "redemption_start_date": redemption_start_date,
      "redemption_end_date": redemption_end_date,
      "percent_off": percent_off,
      "money_amount_off_micros": money_amount_off_micros,
      "money_amount_off_currency_code": money_amount_off_currency_code,
      "promotion_code": promotion_code,
      "occasion": occasion.upper() if occasion else None,
      "discount_modifier": discount_modifier.upper() if discount_modifier else None,
      "summary": summary,
  }]

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
      base["resource_names"][0] if base.get("resource_names") else None
  )
  base["resource_name"] = resource_name
  base["asset_id"] = resource_name.rsplit("/", 1)[-1] if resource_name else None
  base["summary"] = summary
  return base


@mcp.tool()
@audit()
@require_allowed_account
def update_promotion_asset(
    customer_id: str,
    asset_id: str,
    promotion_target: str | None = None,
    language_code: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    percent_off: float | None = None,
    money_amount_off_micros: int | None = None,
    money_amount_off_currency_code: str | None = None,
    promotion_code: str | None = None,
    occasion: str | None = None,
    redemption_start_date: str | None = None,
    redemption_end_date: str | None = None,
    orders_over_amount_micros: int | None = None,
    orders_over_currency_code: str | None = None,
    discount_modifier: str | None = None,
    terms_and_conditions_text: str | None = None,
    final_urls: list[str] | None = None,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Updates one or more fields of an existing PromotionAsset.

  Only the fields you pass are updated (None = leave alone). Promotion
  assets are MUTABLE, unlike text assets which must be re-created.

  When both start_date and end_date are provided, the date validation
  re-runs; if only one is provided, the API will reject incoherent
  combinations against the existing state.

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_id: PromotionAsset ID to update.
      promotion_target: Optional new target text (<= 20 chars).
      language_code: Optional new BCP-47 language code.
      start_date / end_date: Optional new display-window dates (YYYY-MM-DD).
      percent_off: Optional new percent in [1.0, 100.0]. Cannot be combined
          with money_amount_off_micros in the same call.
      money_amount_off_micros / money_amount_off_currency_code: Optional
          new fixed-discount amount + currency.
      promotion_code: Optional new promo code (<= 15 chars).
      occasion: Optional new occasion enum.
      redemption_start_date / redemption_end_date: Optional new redemption window.
      orders_over_amount_micros / orders_over_currency_code: Optional new
          minimum-order threshold.
      discount_modifier: Optional new modifier (UP_TO).
      terms_and_conditions_text: Optional new T&Cs.
      final_urls: Optional new landing URLs — REPLACES the existing list.
          Pass [] to clear.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  _validate_text_lengths(promotion_target, promotion_code, terms_and_conditions_text)
  if percent_off is not None and money_amount_off_micros is not None:
    raise ToolError(
        "percent_off and money_amount_off_micros are mutually exclusive."
    )
  if percent_off is not None and not (_PERCENT_MIN <= percent_off <= _PERCENT_MAX):
    raise ToolError(
        f"percent_off must be in [{_PERCENT_MIN}, {_PERCENT_MAX}], got {percent_off}."
    )
  if money_amount_off_micros is not None:
    if money_amount_off_micros <= 0:
      raise ToolError(
          f"money_amount_off_micros must be > 0, got {money_amount_off_micros}."
      )
    if not money_amount_off_currency_code:
      raise ToolError(
          "money_amount_off_currency_code is required when "
          "money_amount_off_micros is set."
      )
  if orders_over_amount_micros is not None and not orders_over_currency_code:
    raise ToolError(
        "orders_over_currency_code is required when "
        "orders_over_amount_micros is set."
    )
  if start_date is not None and end_date is not None:
    _validate_dates(start_date, end_date, redemption_start_date, redemption_end_date)
  else:
    for d in (start_date, end_date, redemption_start_date, redemption_end_date):
      if d is not None:
        _parse_date(d, "date")

  customer_id = normalise_id(customer_id)
  asset_id = normalise_id(asset_id)
  client = get_client(login_customer_id)
  resource_name = client.get_service("AssetService").asset_path(
      customer_id, asset_id
  )

  asset = resource_types.Asset(resource_name=resource_name)
  pa = asset.promotion_asset
  paths: list[str] = []

  if promotion_target is not None:
    pa.promotion_target = promotion_target
    paths.append("promotion_asset.promotion_target")
  if language_code is not None:
    pa.language_code = language_code
    paths.append("promotion_asset.language_code")
  if start_date is not None:
    pa.start_date = start_date
    paths.append("promotion_asset.start_date")
  if end_date is not None:
    pa.end_date = end_date
    paths.append("promotion_asset.end_date")
  if redemption_start_date is not None:
    pa.redemption_start_date = redemption_start_date
    paths.append("promotion_asset.redemption_start_date")
  if redemption_end_date is not None:
    pa.redemption_end_date = redemption_end_date
    paths.append("promotion_asset.redemption_end_date")
  if percent_off is not None:
    pa.percent_off = int(round(percent_off * _PERCENT_TO_MICROS))
    paths.append("promotion_asset.percent_off")
  if money_amount_off_micros is not None:
    pa.money_amount_off.amount_micros = int(money_amount_off_micros)
    pa.money_amount_off.currency_code = money_amount_off_currency_code
    paths.append("promotion_asset.money_amount_off.amount_micros")
    paths.append("promotion_asset.money_amount_off.currency_code")
  if promotion_code is not None:
    pa.promotion_code = promotion_code
    paths.append("promotion_asset.promotion_code")
  if orders_over_amount_micros is not None:
    pa.orders_over_amount.amount_micros = int(orders_over_amount_micros)
    pa.orders_over_amount.currency_code = orders_over_currency_code
    paths.append("promotion_asset.orders_over_amount.amount_micros")
    paths.append("promotion_asset.orders_over_amount.currency_code")
  if occasion is not None:
    pa.occasion = resolve_enum(
        enum_types.PromotionExtensionOccasionEnum.PromotionExtensionOccasion,
        occasion,
        "occasion",
    )
    paths.append("promotion_asset.occasion")
  if discount_modifier is not None:
    pa.discount_modifier = resolve_enum(
        enum_types.PromotionExtensionDiscountModifierEnum.PromotionExtensionDiscountModifier,
        discount_modifier,
        "discount_modifier",
    )
    paths.append("promotion_asset.discount_modifier")
  if terms_and_conditions_text is not None:
    pa.terms_and_conditions_text = terms_and_conditions_text
    paths.append("promotion_asset.terms_and_conditions_text")
  if final_urls is not None:
    for url in final_urls:
      asset.final_urls.append(url)
    paths.append("final_urls")

  if not paths:
    raise ToolError("update_promotion_asset called with no fields to update.")

  op = service_types.AssetOperation(update=asset)
  op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))

  expected_changes = [{
      "resource_name": resource_name,
      "fields": paths,
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name="AssetService",
      method_name="mutate_assets",
      request_type_name="MutateAssetsRequest",
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
def link_promotion_asset(
    customer_id: str,
    asset_id: str,
    scope: str,
    campaign_id: str | None = None,
    ad_group_id: str | None = None,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Links a PromotionAsset at customer, campaign, or ad-group scope.

  field_type is always PROMOTION. A promotion asset already linked to one
  scope can be additionally linked to others (e.g. attach an account-wide
  promo also to a specific campaign).

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_id: Asset ID of the PromotionAsset to link (digits only).
      scope: CUSTOMER | CAMPAIGN | AD_GROUP.
      campaign_id: Required if scope=CAMPAIGN.
      ad_group_id: Required if scope=AD_GROUP.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, resource_name, audit_id}``.
  """
  cfg = _link_service_config(scope, campaign_id=campaign_id, ad_group_id=ad_group_id)
  customer_id = normalise_id(customer_id)
  asset_id = normalise_id(asset_id)
  client = get_client(login_customer_id)
  asset_resource_name = client.get_service("AssetService").asset_path(
      customer_id, asset_id
  )
  field_type_enum = resolve_enum(
      enum_types.AssetFieldTypeEnum.AssetFieldType,
      _FIELD_TYPE_PROMOTION,
      "field_type",
  )

  scope_upper = scope.upper()
  if scope_upper == "CUSTOMER":
    link = resource_types.CustomerAsset(
        asset=asset_resource_name, field_type=field_type_enum
    )
    op = service_types.CustomerAssetOperation(create=link)
    meta: dict[str, Any] = {"scope": "CUSTOMER", "asset": asset_resource_name}
  elif scope_upper == "CAMPAIGN":
    campaign_path = client.get_service("CampaignService").campaign_path(
        customer_id, normalise_id(campaign_id)
    )
    link = resource_types.CampaignAsset(
        campaign=campaign_path,
        asset=asset_resource_name,
        field_type=field_type_enum,
    )
    op = service_types.CampaignAssetOperation(create=link)
    meta = {
        "scope": "CAMPAIGN",
        "campaign": campaign_path,
        "asset": asset_resource_name,
    }
  else:  # AD_GROUP
    ad_group_path = client.get_service("AdGroupService").ad_group_path(
        customer_id, normalise_id(ad_group_id)
    )
    link = resource_types.AdGroupAsset(
        ad_group=ad_group_path,
        asset=asset_resource_name,
        field_type=field_type_enum,
    )
    op = service_types.AdGroupAssetOperation(create=link)
    meta = {
        "scope": "AD_GROUP",
        "ad_group": ad_group_path,
        "asset": asset_resource_name,
    }

  expected_changes = [{**meta, "field_type": _FIELD_TYPE_PROMOTION}]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name=cfg["service_name"],
      method_name=cfg["method_name"],
      request_type_name=cfg["request_type_name"],
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
def create_and_link_promotion(
    customer_id: str,
    promotion_target: str,
    language_code: str,
    start_date: str,
    end_date: str,
    link_targets: list[dict],
    percent_off: float | None = None,
    money_amount_off_micros: int | None = None,
    money_amount_off_currency_code: str | None = None,
    promotion_code: str | None = None,
    occasion: str | None = None,
    redemption_start_date: str | None = None,
    redemption_end_date: str | None = None,
    orders_over_amount_micros: int | None = None,
    orders_over_currency_code: str | None = None,
    discount_modifier: str | None = None,
    terms_and_conditions_text: str | None = None,
    final_urls: list[str] | None = None,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Creates a PromotionAsset AND links it to N targets in one atomic batch.

  Bundles AssetOperation create + N {Customer,Campaign,AdGroup}AssetOperation
  creates into a single GoogleAdsService.mutate call — either the asset
  and all links land together, or nothing does.

  Args:
      ... same as create_promotion_asset, plus:
      link_targets: list of dicts each of the form
          ``{"scope": "CUSTOMER" | "CAMPAIGN" | "AD_GROUP",
              "campaign_id"?: str, "ad_group_id"?: str}``.
          Must contain >= 1 entry.

  Returns:
      ``{dry_run, expected_changes, resource_names, asset_id,
      asset_resource_name, links, summary, audit_id}``.
  """
  if not link_targets:
    raise ToolError("link_targets must contain at least one entry.")
  _validate_text_lengths(promotion_target, promotion_code, terms_and_conditions_text)
  _validate_discount(percent_off, money_amount_off_micros, money_amount_off_currency_code)
  redemption_start_date, redemption_end_date = _validate_dates(
      start_date, end_date, redemption_start_date, redemption_end_date
  )
  for i, t in enumerate(link_targets):
    if not isinstance(t, dict) or "scope" not in t:
      raise ToolError(
          f"link_targets[{i}] must be a dict with a 'scope' key, got {t!r}."
      )
    _link_service_config(
        t["scope"],
        campaign_id=t.get("campaign_id"),
        ad_group_id=t.get("ad_group_id"),
    )

  customer_id = normalise_id(customer_id)
  client = get_client(login_customer_id)

  asset_service = client.get_service("AssetService")
  googleads_service = client.get_service("GoogleAdsService")
  temp_asset_path = asset_service.asset_path(customer_id, _TEMP_ASSET_ID)
  field_type_enum = resolve_enum(
      enum_types.AssetFieldTypeEnum.AssetFieldType,
      _FIELD_TYPE_PROMOTION,
      "field_type",
  )

  operations: list = []

  op_asset = client.get_type("MutateOperation")
  asset_msg = op_asset.asset_operation.create
  asset_msg.resource_name = temp_asset_path
  _populate_promotion_asset(
      asset=asset_msg,
      promotion_target=promotion_target,
      language_code=language_code,
      start_date=start_date,
      end_date=end_date,
      redemption_start_date=redemption_start_date,
      redemption_end_date=redemption_end_date,
      percent_off=percent_off,
      money_amount_off_micros=money_amount_off_micros,
      money_amount_off_currency_code=money_amount_off_currency_code,
      promotion_code=promotion_code,
      occasion=occasion,
      orders_over_amount_micros=orders_over_amount_micros,
      orders_over_currency_code=orders_over_currency_code,
      discount_modifier=discount_modifier,
      terms_and_conditions_text=terms_and_conditions_text,
      final_urls=final_urls,
  )
  operations.append(op_asset)

  link_metas: list[dict] = []
  for t in link_targets:
    scope_upper = t["scope"].upper()
    op = client.get_type("MutateOperation")
    if scope_upper == "CUSTOMER":
      link = op.customer_asset_operation.create
      link.asset = temp_asset_path
      link.field_type = field_type_enum
      link_metas.append({"scope": "CUSTOMER"})
    elif scope_upper == "CAMPAIGN":
      campaign_path = client.get_service("CampaignService").campaign_path(
          customer_id, normalise_id(t["campaign_id"])
      )
      link = op.campaign_asset_operation.create
      link.campaign = campaign_path
      link.asset = temp_asset_path
      link.field_type = field_type_enum
      link_metas.append({"scope": "CAMPAIGN", "campaign": campaign_path})
    else:  # AD_GROUP
      ad_group_path = client.get_service("AdGroupService").ad_group_path(
          customer_id, normalise_id(t["ad_group_id"])
      )
      link = op.ad_group_asset_operation.create
      link.ad_group = ad_group_path
      link.asset = temp_asset_path
      link.field_type = field_type_enum
      link_metas.append({"scope": "AD_GROUP", "ad_group": ad_group_path})
    operations.append(op)

  summary = _summarise(
      promotion_target=promotion_target,
      percent_off=percent_off,
      money_amount_off_micros=money_amount_off_micros,
      money_amount_off_currency_code=money_amount_off_currency_code,
      start_date=start_date,
      end_date=end_date,
      promotion_code=promotion_code,
      occasion=occasion,
  )
  expected_changes = [
      {"stage": "create_asset", "summary": summary},
      {"stage": "link", "targets": link_metas},
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
        "asset_id": None,
        "asset_resource_name": None,
        "links": [],
        "summary": summary,
    }

  resource_names: list[str] = []
  asset_rn: str | None = None
  link_rns: list[str] = []
  for r in response.mutate_operation_responses:
    which = r._pb.WhichOneof("response")
    if which == "asset_result":
      asset_rn = r.asset_result.resource_name
      resource_names.append(asset_rn)
    elif which == "customer_asset_result":
      link_rns.append(r.customer_asset_result.resource_name)
      resource_names.append(r.customer_asset_result.resource_name)
    elif which == "campaign_asset_result":
      link_rns.append(r.campaign_asset_result.resource_name)
      resource_names.append(r.campaign_asset_result.resource_name)
    elif which == "ad_group_asset_result":
      link_rns.append(r.ad_group_asset_result.resource_name)
      resource_names.append(r.ad_group_asset_result.resource_name)

  return {
      "dry_run": False,
      "expected_changes": expected_changes,
      "resource_names": resource_names,
      "asset_id": asset_rn.rsplit("/", 1)[-1] if asset_rn else None,
      "asset_resource_name": asset_rn,
      "links": link_rns,
      "summary": summary,
  }


def _link_resource_name(
    client,
    *,
    scope_upper: str,
    customer_id: str,
    asset_id: str,
    campaign_id: str | None,
    ad_group_id: str | None,
) -> str:
  """Builds the composite link resource_name using the field_type NAME."""
  if scope_upper == "CUSTOMER":
    return client.get_service("CustomerAssetService").customer_asset_path(
        customer_id, asset_id, _FIELD_TYPE_PROMOTION
    )
  if scope_upper == "CAMPAIGN":
    return client.get_service("CampaignAssetService").campaign_asset_path(
        customer_id, normalise_id(campaign_id), asset_id, _FIELD_TYPE_PROMOTION
    )
  return client.get_service("AdGroupAssetService").ad_group_asset_path(
      customer_id, normalise_id(ad_group_id), asset_id, _FIELD_TYPE_PROMOTION
  )


@mcp.tool()
@audit()
@require_allowed_account
def unlink_promotion_asset(
    customer_id: str,
    asset_id: str,
    scope: str,
    campaign_id: str | None = None,
    ad_group_id: str | None = None,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Removes a PROMOTION asset link at the given scope.

  Does NOT delete the underlying Asset — it stays in the library and any
  other scope-level links remain intact. ``field_type`` is hard-coded to
  PROMOTION; the composite resource path uses the enum NAME ("PROMOTION"),
  NOT the integer 8.

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_id: PromotionAsset ID (digits only).
      scope: CUSTOMER | CAMPAIGN | AD_GROUP.
      campaign_id: Required if scope=CAMPAIGN.
      ad_group_id: Required if scope=AD_GROUP.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names,
      removed_resource_name, audit_id}``.
  """
  cfg = _link_service_config(scope, campaign_id=campaign_id, ad_group_id=ad_group_id)
  customer_id = normalise_id(customer_id)
  asset_id = normalise_id(asset_id)
  client = get_client(login_customer_id)
  scope_upper = scope.upper()
  resource_name = _link_resource_name(
      client,
      scope_upper=scope_upper,
      customer_id=customer_id,
      asset_id=asset_id,
      campaign_id=campaign_id,
      ad_group_id=ad_group_id,
  )
  if scope_upper == "CUSTOMER":
    op = service_types.CustomerAssetOperation(remove=resource_name)
  elif scope_upper == "CAMPAIGN":
    op = service_types.CampaignAssetOperation(remove=resource_name)
  else:
    op = service_types.AdGroupAssetOperation(remove=resource_name)

  expected_changes = [{
      "resource_name": resource_name,
      "operation": "REMOVE",
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name=cfg["service_name"],
      method_name=cfg["method_name"],
      request_type_name=cfg["request_type_name"],
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
def update_promotion_asset_link_status(
    customer_id: str,
    asset_id: str,
    scope: str,
    status: str,
    campaign_id: str | None = None,
    ad_group_id: str | None = None,
    dry_run: bool = True,
    login_customer_id: str | None = None,
) -> dict:
  """Pauses, enables, or removes a PromotionAsset link at any scope.

  REMOVED is equivalent to ``unlink_promotion_asset``.

  Args:
      customer_id: Google Ads customer ID (digits only).
      asset_id: PromotionAsset ID (digits only).
      scope: CUSTOMER | CAMPAIGN | AD_GROUP.
      status: ENABLED | PAUSED | REMOVED.
      campaign_id: Required if scope=CAMPAIGN.
      ad_group_id: Required if scope=AD_GROUP.
      dry_run: When True (default) the change is validated but NOT applied.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{dry_run, expected_changes, resource_names, audit_id}``.
  """
  cfg = _link_service_config(scope, campaign_id=campaign_id, ad_group_id=ad_group_id)
  customer_id = normalise_id(customer_id)
  asset_id = normalise_id(asset_id)
  resolved_status = resolve_enum(
      enum_types.AssetLinkStatusEnum.AssetLinkStatus, status, "status"
  )
  client = get_client(login_customer_id)
  scope_upper = scope.upper()
  resource_name = _link_resource_name(
      client,
      scope_upper=scope_upper,
      customer_id=customer_id,
      asset_id=asset_id,
      campaign_id=campaign_id,
      ad_group_id=ad_group_id,
  )

  if scope_upper == "CUSTOMER":
    link = resource_types.CustomerAsset(
        resource_name=resource_name, status=resolved_status
    )
    op = service_types.CustomerAssetOperation(update=link)
  elif scope_upper == "CAMPAIGN":
    link = resource_types.CampaignAsset(
        resource_name=resource_name, status=resolved_status
    )
    op = service_types.CampaignAssetOperation(update=link)
  else:  # AD_GROUP
    link = resource_types.AdGroupAsset(
        resource_name=resource_name, status=resolved_status
    )
    op = service_types.AdGroupAssetOperation(update=link)
  op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))

  expected_changes = [{
      "resource_name": resource_name,
      "field": "status",
      "new_value": status.upper(),
  }]
  response = wrap_google_ads_error(lambda: execute_mutation(
      client=client,
      service_name=cfg["service_name"],
      method_name=cfg["method_name"],
      request_type_name=cfg["request_type_name"],
      customer_id=customer_id,
      operations=[op],
      dry_run=dry_run,
  ))
  return build_result(
      dry_run=dry_run, expected_changes=expected_changes, response=response
  )


@mcp.tool()
@require_allowed_account
def list_promotion_assets(
    customer_id: str,
    scope: str | None = None,
    campaign_id: str | None = None,
    ad_group_id: str | None = None,
    login_customer_id: str | None = None,
) -> dict:
  """Lists PromotionAssets, optionally filtered by link scope.

  Read-only — no @audit / dry_run.

  Args:
      customer_id: Google Ads customer ID (digits only).
      scope: Optional. None = list the entire account library of promotion
          assets (linked or not). CUSTOMER | CAMPAIGN | AD_GROUP = list
          link rows at that scope. campaign_id / ad_group_id further filter.
      campaign_id: Optional. Used only when scope=CAMPAIGN.
      ad_group_id: Optional. Used only when scope=AD_GROUP.
      login_customer_id: MCC ID if customer is managed.

  Returns:
      ``{customer_id, scope, count, promotions: [{asset_id, resource_name,
      promotion_target, percent_off, money_amount_off, currency_code,
      start_date, end_date, promotion_code, occasion, language_code,
      discount_modifier, link_status?, link_resource_name?}, ...]}``.
      Sorted by start_date ascending.
  """
  customer_id = normalise_id(customer_id)
  scope_upper = scope.upper() if scope else None
  client = get_client(login_customer_id)
  ga_service = client.get_service("GoogleAdsService")

  asset_fields = (
      "asset.id,"
      " asset.resource_name,"
      " asset.promotion_asset.promotion_target,"
      " asset.promotion_asset.percent_off,"
      " asset.promotion_asset.money_amount_off.amount_micros,"
      " asset.promotion_asset.money_amount_off.currency_code,"
      " asset.promotion_asset.start_date,"
      " asset.promotion_asset.end_date,"
      " asset.promotion_asset.redemption_start_date,"
      " asset.promotion_asset.redemption_end_date,"
      " asset.promotion_asset.promotion_code,"
      " asset.promotion_asset.occasion,"
      " asset.promotion_asset.language_code,"
      " asset.promotion_asset.discount_modifier,"
      " asset.promotion_asset.terms_and_conditions_text"
  )

  if scope_upper is None:
    query = (
        f"SELECT {asset_fields} FROM asset WHERE asset.type = 'PROMOTION'"
    )
    from_link = None
  elif scope_upper == "CUSTOMER":
    query_parts = [
        f"SELECT {asset_fields},"
        " customer_asset.resource_name, customer_asset.status",
        "FROM customer_asset",
        "WHERE customer_asset.field_type = 'PROMOTION'",
    ]
    query = " ".join(query_parts)
    from_link = "customer_asset"
  elif scope_upper == "CAMPAIGN":
    query_parts = [
        f"SELECT {asset_fields},"
        " campaign_asset.resource_name, campaign_asset.status,"
        " campaign_asset.campaign",
        "FROM campaign_asset",
        "WHERE campaign_asset.field_type = 'PROMOTION'",
    ]
    if campaign_id:
      query_parts.append(f"AND campaign.id = {normalise_id(campaign_id)}")
    query = " ".join(query_parts)
    from_link = "campaign_asset"
  elif scope_upper == "AD_GROUP":
    query_parts = [
        f"SELECT {asset_fields},"
        " ad_group_asset.resource_name, ad_group_asset.status,"
        " ad_group_asset.ad_group",
        "FROM ad_group_asset",
        "WHERE ad_group_asset.field_type = 'PROMOTION'",
    ]
    if ad_group_id:
      query_parts.append(f"AND ad_group.id = {normalise_id(ad_group_id)}")
    query = " ".join(query_parts)
    from_link = "ad_group_asset"
  else:
    raise ToolError(
        f"scope must be None | CUSTOMER | CAMPAIGN | AD_GROUP, got {scope!r}."
    )

  rows: list[dict[str, Any]] = []
  for batch in ga_service.search_stream(customer_id=customer_id, query=query):
    for row in batch.results:
      pa = row.asset.promotion_asset
      entry: dict[str, Any] = {
          "asset_id": str(row.asset.id),
          "resource_name": row.asset.resource_name,
          "promotion_target": pa.promotion_target or None,
          "percent_off": (pa.percent_off / _PERCENT_TO_MICROS) if pa.percent_off else None,
          "money_amount_off_micros": (
              pa.money_amount_off.amount_micros
              if pa.money_amount_off.amount_micros
              else None
          ),
          "money_amount_off_currency_code": (
              pa.money_amount_off.currency_code or None
          ),
          "start_date": pa.start_date or None,
          "end_date": pa.end_date or None,
          "redemption_start_date": pa.redemption_start_date or None,
          "redemption_end_date": pa.redemption_end_date or None,
          "promotion_code": pa.promotion_code or None,
          "occasion": (
              enum_types.PromotionExtensionOccasionEnum.PromotionExtensionOccasion(
                  pa.occasion
              ).name
              if pa.occasion
              else None
          ),
          "language_code": pa.language_code or None,
          "discount_modifier": (
              enum_types.PromotionExtensionDiscountModifierEnum.PromotionExtensionDiscountModifier(
                  pa.discount_modifier
              ).name
              if pa.discount_modifier
              else None
          ),
          "terms_and_conditions_text": pa.terms_and_conditions_text or None,
      }
      if from_link == "customer_asset":
        entry["link_resource_name"] = row.customer_asset.resource_name
        entry["link_status"] = enum_types.AssetLinkStatusEnum.AssetLinkStatus(
            row.customer_asset.status
        ).name
      elif from_link == "campaign_asset":
        entry["link_resource_name"] = row.campaign_asset.resource_name
        entry["link_status"] = enum_types.AssetLinkStatusEnum.AssetLinkStatus(
            row.campaign_asset.status
        ).name
        entry["campaign"] = row.campaign_asset.campaign
      elif from_link == "ad_group_asset":
        entry["link_resource_name"] = row.ad_group_asset.resource_name
        entry["link_status"] = enum_types.AssetLinkStatusEnum.AssetLinkStatus(
            row.ad_group_asset.status
        ).name
        entry["ad_group"] = row.ad_group_asset.ad_group
      rows.append(entry)

  rows.sort(key=lambda r: (r.get("start_date") or "9999-99-99", r["asset_id"]))
  return {
      "customer_id": customer_id,
      "scope": scope_upper,
      "count": len(rows),
      "promotions": rows,
  }
