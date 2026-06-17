"""DoorDash address autocomplete via the geo-intelligence v2 endpoint."""

from __future__ import annotations

import os
import time
from typing import Any

from curl_cffi import requests as cffi_requests

from doordash.web_client import BROWSER_UA, _build_proxy_url
from doordash.constants import IMPERSONATE

_AUTOCOMPLETE_URL = (
    "https://www.doordash.com/unified-gateway/geo-intelligence/v2/address/autocomplete"
)
_WARM_URL = "https://www.doordash.com/"

_SESSION_TTL = 180  # seconds before re-warming

_session: cffi_requests.Session | None = None
_session_born: float = 0.0


def _request_kw() -> dict[str, Any]:
    kw: dict[str, Any] = {"impersonate": IMPERSONATE, "timeout": 10, "verify": False}
    raw = os.getenv("DOORDASH_PROXY", "").strip()
    if raw:
        kw["proxy"] = _build_proxy_url(raw)
    return kw


def _get_session() -> cffi_requests.Session:
    global _session, _session_born
    now = time.monotonic()
    if _session is None or (now - _session_born) > _SESSION_TTL:
        s = cffi_requests.Session()
        s.get(
            _WARM_URL,
            headers={
                "accept": "text/html,application/xhtml+xml,*/*",
                "user-agent": BROWSER_UA,
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
                "sec-fetch-site": "none",
            },
            **_request_kw(),
        )
        _session = s
        _session_born = now
    return _session


def fetch_address_suggestions(query: str, limit: int = 5) -> list[dict]:
    """Return up to *limit* autocomplete suggestions for *query*.

    Each item: {"label": str, "value": str, "description": str}
    """
    if not query or len(query) < 3:
        return []

    s = _get_session()
    try:
        resp = s.get(
            _AUTOCOMPLETE_URL,
            params={"input_address": query},
            headers={
                "accept": "application/json, text/plain, */*",
                "user-agent": BROWSER_UA,
                "x-experience-id": "doordash",
                "referer": _WARM_URL,
            },
            **_request_kw(),
        )
    except Exception:
        return []

    if resp.status_code != 200:
        return []

    try:
        data = resp.json()
    except Exception:
        return []

    results = []
    for p in (data.get("predictions") or [])[:limit]:
        segs = p.get("formatted_address_segmented") or []
        label = segs[0] if segs else p.get("formatted_address", "")
        desc = segs[1] if len(segs) > 1 else ""
        value = p.get("formatted_address", label)
        results.append({"label": label, "value": value, "description": desc})

    return results
