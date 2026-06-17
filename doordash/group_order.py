"""Join a DoorDash group order from a share link."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from curl_cffi.requests.exceptions import RequestException

from core.logger import log, vlog
from doordash.order_link import group_cart_referer, is_url_code, parse_cart_reference
from doordash.web_client import (
    DETAILED_CART_QUERY,
    DETAILED_CART_URL,
    DoorDashWebSession,
    build_browser_headers,
    load_query,
    merge_session_cookies,
    request_kwargs,
    resolve_csrf,
)


def _cart_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("orderCartId", "order_cart_id"):
        values = query.get(key) or []
        if values and values[0].strip():
            return values[0].strip()

    next_path = unquote((query.get("next") or [""])[0])
    for segment in (next_path, parsed.path):
        for part in segment.split("/"):
            if len(part) == 36 and part.count("-") == 4:
                return part
    return None


def _invite_urls(order_link: str, cart_ref: str) -> list[str]:
    urls: list[str] = []
    link = order_link.strip()
    if link.lower().startswith("http"):
        urls.append(link.rstrip("/") + ("/" if not link.endswith("/") else ""))

    if is_url_code(cart_ref):
        urls.extend(
            [
                f"https://drd.sh/cart/{cart_ref}/",
                group_cart_referer(cart_ref),
            ]
        )
    else:
        urls.append(group_cart_referer(cart_ref))

    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = url.rstrip("/")
        if normalized not in seen:
            seen.add(normalized)
            deduped.append(url if url.endswith("/") else f"{url}/")
    return deduped


def join_group_order(
    cookies: dict[str, str],
    order_link: str,
) -> tuple[str, DoorDashWebSession, dict[str, Any]]:
    """Open the group-order invite so the session can read the shared cart."""
    log("group", "opening order link...")
    cart_ref = parse_cart_reference(order_link)
    client = DoorDashWebSession(dict(cookies))
    resolved_uuid: str | None = None
    last_url = ""

    for invite_url in _invite_urls(order_link, cart_ref):
        try:
            response = client.session.get(
                invite_url,
                headers=build_browser_headers(invite_url),
                cookies=client.cookies,
                allow_redirects=True,
                **request_kwargs(),
            )
        except RequestException as exc:
            raise RuntimeError(f"Could not open group order link: {exc}") from exc

        client.cookies = merge_session_cookies(client.cookies, client.session)
        last_url = str(response.url or "")
        found = _cart_id_from_url(last_url)
        if found:
            resolved_uuid = found
            break  # one successful redirect is enough; skip remaining invite URLs

    # If we followed all invite URLs and landed somewhere that has no cart UUID,
    # DoorDash redirected us away — the link is inactive or expired.
    if resolved_uuid is None:
        dead_indicators = ("/home", "/404", "/error", "login", "signin")
        if any(d in last_url for d in dead_indicators) or not last_url:
            raise RuntimeError(
                "That group order link is no longer active. "
                "It may have already been placed or has expired."
            )

    query_cart_id = resolved_uuid or cart_ref
    vlog("group", f"cart UUID: {query_cart_id}")
    log("group", "fetching cart items...")
    referer = group_cart_referer(query_cart_id)
    # The redirect above already visited doordash.com/cart/{uuid}/ and set session
    # cookies (including csrf_token) via merge_session_cookies — no separate warm needed.
    client.csrf = resolve_csrf(client.cookies)
    client.cookies["csrf_token"] = client.csrf

    data = client.graphql(
        DETAILED_CART_URL,
        "detailedCartItems",
        {"orderCartId": query_cart_id},
        load_query(DETAILED_CART_QUERY),
        referer,
    )
    if data.get("errors"):
        err_msgs = " ".join(
            str(e.get("message", "")) for e in (data["errors"] or [])
        ).lower()
        if any(w in err_msgs for w in ("not found", "invalid", "expired", "does not exist")):
            raise RuntimeError(
                "That group order link is inactive or has expired."
            )
        raise RuntimeError(f"Could not load group order cart: {data['errors']}")

    source_cart = (data.get("data") or {}).get("orderCart")
    if not source_cart:
        raise RuntimeError(
            "That group order link is no longer active — it may have already been placed or expired."
        )

    orders = source_cart.get("orders") or []
    if not orders or not any(o.get("orderItems") for o in orders):
        raise RuntimeError(
            "The group order has no items yet. Add items to the cart before price checking."
        )

    cart_uuid = str(source_cart.get("id") or resolved_uuid or cart_ref)
    return cart_uuid, client, source_cart
