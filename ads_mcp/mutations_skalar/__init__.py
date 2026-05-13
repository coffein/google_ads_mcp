"""Skalar Marketing fork: 6 priority-1 write tools wrapped with safety layer.

Importing the submodules registers their @mcp.tool() decorators against the
shared ``mcp_server`` instance. Each tool enforces:

  * GOOGLE_ADS_ALLOWED_ACCOUNTS whitelist (fail-closed),
  * dry_run=True default → validate_only=True on the API request,
  * SQLite audit row per call (success or failure),
  * structured error wrapping for GoogleAdsException.

Toggle via env: SKALAR_MCP_ENABLE_MUTATIONS=true (default false).
"""

from ads_mcp.mutations_skalar import ad_group  # noqa: F401
from ads_mcp.mutations_skalar import campaign  # noqa: F401
from ads_mcp.mutations_skalar import keyword  # noqa: F401
