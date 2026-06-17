"""Parse DoorDash order / group-cart links."""

from __future__ import annotations

import re

_CART_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_CART_URL_CODE_RE = re.compile(
    r"(?:drd\.sh/cart/|doordash\.com/cart/)([A-Za-z0-9]+)",
    re.IGNORECASE,
)


def parse_cart_reference(order_link: str) -> str:
    """Return a cart UUID or group-order urlCode from a share link."""
    link = order_link.strip()
    if not link:
        raise ValueError("Order link is empty.")

    uuid_match = _CART_UUID_RE.search(link)
    if uuid_match:
        return uuid_match.group(0)

    code_match = _CART_URL_CODE_RE.search(link)
    if code_match:
        return code_match.group(1)

    raise ValueError(
        "Could not read that order link. Paste a DoorDash group cart link "
        "(for example https://drd.sh/cart/... or a cart URL with a cart ID)."
    )


def parse_cart_id(order_link: str) -> str:
    """Backward-compatible alias for parse_cart_reference."""
    return parse_cart_reference(order_link)


def is_url_code(cart_ref: str) -> bool:
    return bool(cart_ref) and _CART_UUID_RE.fullmatch(cart_ref) is None


def group_cart_referer(cart_ref: str) -> str:
    return f"https://www.doordash.com/cart/{cart_ref}/"
