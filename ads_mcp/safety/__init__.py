"""Safety layer for Skalar Marketing fork: whitelist + audit + dry-run helpers.

Wrap every mutating tool with::

    @mcp.tool()
    @audit()
    @require_allowed_account
    def my_mutation(customer_id: str, ..., dry_run: bool = True): ...

Keep this module independent from FastMCP and Google Ads imports so that it
stays unit-testable in isolation.
"""

from ads_mcp.safety.audit import AuditError
from ads_mcp.safety.audit import audit
from ads_mcp.safety.audit import init_audit_db
from ads_mcp.safety.errors import GoogleAdsToolError
from ads_mcp.safety.errors import wrap_google_ads_error
from ads_mcp.safety.whitelist import AccountNotAllowedError
from ads_mcp.safety.whitelist import allowed_accounts
from ads_mcp.safety.whitelist import require_allowed_account

__all__ = [
    "AccountNotAllowedError",
    "AuditError",
    "GoogleAdsToolError",
    "allowed_accounts",
    "audit",
    "init_audit_db",
    "require_allowed_account",
    "wrap_google_ads_error",
]
