"""Local web dashboard for the agent (`memecoin-agent ui`)."""

from __future__ import annotations

import dataclasses
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..config import Config


def redacted_config(cfg: Config) -> dict[str, Any]:
    """Config for display, with anything that may hold a credential masked."""
    d = dataclasses.asdict(cfg)
    ex = d["execution"]
    if ex.get("jupiter_api_key"):
        ex["jupiter_api_key"] = "••••••"
    # Private RPC URLs often embed the API key in the query string or path.
    parts = urlsplit(ex.get("rpc_url") or "")
    if parts.query or (parts.path and parts.path != "/"):
        ex["rpc_url"] = urlunsplit((parts.scheme, parts.netloc, "/••••••" if parts.path not in ("", "/") else parts.path,
                                    "••••••" if parts.query else "", ""))
    return d
