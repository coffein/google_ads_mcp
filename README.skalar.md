# Skalar Marketing fork — write-enabled with safety layer

This branch (`skalar/main`) extends [google-marketing-solutions/google_ads_mcp](https://github.com/google-marketing-solutions/google_ads_mcp) with a small, write-capable surface intended for an agency workflow over the Skalar Marketing MCC. Read tools come straight from upstream — see the [original README](README.md) for those. Everything documented here is Skalar-specific.

## What this fork adds

| | |
|---|---|
| **36 write tools** (see catalogue below) | grouped by domain in `mutations_skalar/` |
| Safety layer | Account whitelist, dry-run-by-default, SQLite audit log, structured Google Ads errors |
| Transport flag | `--transport stdio\|http`, `--host`, `--port` (defaults to `http` on `127.0.0.1:3011`) |

### Tool catalogue

| Domain | Tools |
|---|---|
| Campaigns | `update_campaign_status`, `update_campaign_budget`, `update_campaign_bidding_strategy` |
| Search campaign creation | `create_search_campaign_bundle` (atomic: budget + campaign + ad group + RSA + keywords + geo/language in one mutate) |
| Ad groups | `update_ad_group_status`, `create_ad_group` |
| Ads | `create_responsive_search_ad` |
| Keywords (positive) | `add_keywords_to_ad_group`, `update_keyword_status` |
| Keywords (negative) | `add_negative_keywords_to_campaign`, `add_negative_keywords_to_ad_group`, `add_negative_keywords_to_shared_set` |
| Bid modifiers | `set_campaign_device_bid_modifier`, `set_campaign_location_bid_modifier`, `set_campaign_ad_schedule_bid_modifier` |
| Shopping listing groups | `update_listing_group_unit_bid`, `update_listing_group_unit_status`, `create_listing_group_subdivision`, `create_listing_group_unit`, `delete_listing_group_node` |
| PMax asset groups | `create_asset_group`, `update_asset_group_status` |
| PMax listing-group filters | `create_asset_group_listing_group_filter_subdivision`, `create_asset_group_listing_group_filter_unit`, `delete_asset_group_listing_group_filter`, `create_asset_group_listing_group_two_way_split` (atomic Root + 2-leaf split) |
| Promotion Assets (Aktionen) | `create_promotion_asset`, `update_promotion_asset`, `link_promotion_asset`, `unlink_promotion_asset`, `update_promotion_asset_link_status`, `create_and_link_promotion` (atomic asset + N-link batch), `list_promotion_assets` |
| Conversion value rules | `create_conversion_value_rule`, `update_conversion_value_rule`, `remove_conversion_value_rule` |

The upstream `ADS_MCP_ENABLE_MUTATIONS=true` flag is **left off**. Upstream's mutation tools have no whitelist, no dry-run, no audit, and would otherwise be exposed unguarded next to ours.

## How to enable

Two env vars activate the Skalar surface; a third pins the audit DB:

```bash
SKALAR_MCP_ENABLE_MUTATIONS=true            # registers the write-tool surface
GOOGLE_ADS_ALLOWED_ACCOUNTS=3332619762      # comma-separated customer IDs
GOOGLE_ADS_AUDIT_DB=/var/lib/mcp-google-ads/audit.db   # default if unset
```

`GOOGLE_ADS_ALLOWED_ACCOUNTS` is **fail-closed**: empty/missing rejects every mutation. Add real customer IDs only after dry-running.

## The contract every write tool keeps

```
@mcp.tool()
@audit()
@require_allowed_account
def update_campaign_status(customer_id, campaign_id, status, dry_run=True): ...
```

| Concern | Implementation |
|---|---|
| `dry_run=True` (default) | Translates to `validate_only=True` on the API request. Google validates and returns what would change. |
| Whitelist | `@require_allowed_account` reads `GOOGLE_ADS_ALLOWED_ACCOUNTS` on every call (no process-restart needed). |
| Audit | `@audit()` writes one row per call (success **or** failure) to `mutations` table. The audit row id rides back in the response as `audit_id`. |
| Errors | `wrap_google_ads_error` converts `GoogleAdsException` to `{error_code, message, request_id}`. |

Every tool returns:

```json
{
  "dry_run": false,
  "expected_changes": [...],
  "resource_names": ["customers/.../..."] ,
  "audit_id": 42
}
```

## Where the code lives

```
ads_mcp/
  safety/                  ← whitelist, audit, errors (independent, unit-tested)
    whitelist.py
    audit.py
    errors.py
  mutations_skalar/        ← the Skalar write tools, one file per domain
    _common.py             ← validate_only helper, enum resolver, result shape
    campaign.py            ← campaign status / budget / bidding-strategy
    ad_group.py            ← ad-group status
    keyword.py             ← keyword + negative-keyword tools
    bid_modifier.py        ← device / location / ad-schedule modifiers
    bidding.py             ← campaign bidding-strategy switch
    listing_group.py       ← Shopping listing-group tree mutations
    asset.py               ← PMax text-asset library + link rotation
    asset_group.py         ← PMax AssetGroup create + status
    asset_group_listing_group_filter.py   ← PMax product-partition tree
    promotion_asset.py     ← Promotion Assets ("Aktionen") create/link/list
    conversion_value_rule.py
tests/safety/              ← 11 unit tests (whitelist, audit decorators)
```

Files under `mutations_skalar/` and `safety/` are isolated from upstream so rebasing onto upstream `main` stays mechanical.

## Production deployment (tools.skalar.marketing)

Same shared-`mcp`-user pattern as the other 10 MCPs on the box. Port `3011` is the next free upstream in `/etc/nginx/sites-enabled/mcp.skalar.marketing`.

```ini
# /etc/systemd/system/mcp-google-ads.service
[Service]
User=mcp
Group=mcp
WorkingDirectory=/opt/mcp-servers/google-ads-mcp-server
ExecStart=/usr/local/bin/uv run --no-sync run-mcp-server --transport http --host 127.0.0.1 --port 3011
Environment=SKALAR_MCP_ENABLE_MUTATIONS=true
Environment=GOOGLE_ADS_ALLOWED_ACCOUNTS=3332619762
Environment=GOOGLE_ADS_AUDIT_DB=/var/lib/mcp-google-ads/audit.db
Environment=GOOGLE_ADS_CREDENTIALS=/opt/mcp-servers/google-ads-mcp-server/google-ads.yaml
EnvironmentFile=/opt/mcp-servers/.env
Restart=always
```

```nginx
# In /etc/nginx/sites-enabled/mcp.skalar.marketing
upstream mcp_google_ads { server 127.0.0.1:3011; }

location /<token>/google-ads/mcp { proxy_pass http://mcp_google_ads/mcp; }
```

`google-ads.yaml` lives next to the code (`chmod 600 mcp:mcp`); the audit DB is at `/var/lib/mcp-google-ads/audit.db` (`chown mcp:mcp`, `chmod 750`).

## Adding it to a Claude client

```bash
claude mcp add --transport http --scope user skalar-google-ads \
  https://mcp.skalar.marketing/<token>/google-ads/mcp
```

## Adding more accounts to the whitelist

Update the env var and reload the service:

```bash
ssh root@tools.skalar.marketing 'sed -i "s|GOOGLE_ADS_ALLOWED_ACCOUNTS=.*|GOOGLE_ADS_ALLOWED_ACCOUNTS=3332619762,2044671737|" /etc/systemd/system/mcp-google-ads.service && systemctl daemon-reload && systemctl restart mcp-google-ads'
```

(Or move `GOOGLE_ADS_ALLOWED_ACCOUNTS` to the shared `/opt/mcp-servers/.env` and just `systemctl restart mcp-google-ads`.)

## Inspecting the audit log

```bash
ssh root@tools.skalar.marketing \
  '/usr/local/bin/uv run --no-sync --directory /opt/mcp-servers/google-ads-mcp-server \
   python -c "import sqlite3; c = sqlite3.connect(\"/var/lib/mcp-google-ads/audit.db\"); c.row_factory = sqlite3.Row; [print(dict(r)) for r in c.execute(\"SELECT * FROM mutations ORDER BY id DESC LIMIT 20\")]"'
```
