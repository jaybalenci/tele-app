"""DoorDash order tracking — gift orders and Drive orders."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo


def _build_proxy_url(raw: str) -> str:
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    return f"http://{raw}"


def _make_opener():
    raw = os.getenv("DOORDASH_PROXY", "").strip()
    if not raw:
        return build_opener()
    url = _build_proxy_url(raw)
    return build_opener(ProxyHandler({"http": url, "https": url}))


def _open(req: Request, *, timeout: int = 60, retries: int = 3):
    last_err: Exception = RuntimeError("no attempts")
    for attempt in range(retries):
        try:
            return _make_opener().open(req, timeout=timeout)
        except URLError as e:
            last_err = e
            # Don't retry hard HTTP errors (4xx) — only transient failures
            reason = str(e.reason) if hasattr(e, "reason") else str(e)
            if "503" in reason or "502" in reason or "Tunnel" in reason or "timed out" in reason or "handshake" in reason:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)  # 1s, 2s backoff
                continue
            raise
    raise last_err

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

# ── Gift order ────────────────────────────────────────────────────────────────

_GIFT_UUID_RE = re.compile(
    r"doordash\.com/gifts/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_BARE_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_GIFT_GQL_URL = "https://www.doordash.com/graphql/getGiftTrackingData?operation=getGiftTrackingData"

_GIFT_QUERY = """query getGiftTrackingData($orderUuid: ID!, $ddtm: String, $recipientAuthenticationParams: RecipientAuthenticationParams) {
  getGiftTrackingData(
    orderUuid: $orderUuid
    ddtm: $ddtm
    recipientAuthenticationParams: $recipientAuthenticationParams
  ) {
    orders {
      orderUuid
      cancelledAt
      orderItems {
        quantity
        item { name __typename }
        __typename
      }
      giftInfo { recipientName senderName recipientDeliveryStatus __typename }
      __typename
    }
    delivery {
      dasherAtStoreTime
      dasherConfirmedTime
      quotedDeliveryTime
      actualPickupTime
      actualDeliveryTime
      dynamicEta { estimate __typename }
      deliveryAddress { printableAddress lat lng __typename }
      pickupAddress { printableAddress lat lng __typename }
      __typename
    }
    shippingOrderStatus {
      translatedStrings { title subtitle substatus __typename }
      etaDetails {
        estimatedDeliveryTime
        minEstimatedDeliveryTime
        maxEstimatedDeliveryTime
        __typename
      }
      __typename
    }
    dasher { firstName lastName __typename }
    store { name __typename }
    error { code errorCode message __typename }
    __typename
  }
}"""


def _gift_url(uuid: str) -> str:
    return f"https://www.doordash.com/gifts/{uuid.lower()}"


def _extract_gift_uuid(raw: str) -> str:
    stripped = raw.strip()
    m = _GIFT_UUID_RE.search(stripped)
    if m:
        return m.group(1).lower()
    if _BARE_UUID_RE.fullmatch(stripped):
        return stripped.lower()
    raise ValueError("Not a valid DoorDash gift tracking link or order UUID.")


def _fmt_unix(ts: int | float | None, tz_name: str = "America/New_York") -> str | None:
    if not ts:
        return None
    try:
        dt = datetime.fromtimestamp(int(ts), tz=ZoneInfo(tz_name))
        return dt.strftime("%I:%M %p").lstrip("0")
    except (ValueError, OSError, OverflowError):
        return None


def _fmt_eta_window(min_ts: int | None, max_ts: int | None, fallback: int | None) -> str:
    min_l = _fmt_unix(min_ts)
    max_l = _fmt_unix(max_ts)
    if min_l and max_l and min_l != max_l:
        return f"{min_l} – {max_l}"
    single = _fmt_unix(fallback) or min_l or max_l
    return single or "—"


def _gift_eta(delivery: dict, eta_details: dict) -> str:
    quoted = delivery.get("quotedDeliveryTime")
    dynamic = (delivery.get("dynamicEta") or {}).get("estimate")
    min_ts = eta_details.get("minEstimatedDeliveryTime")
    max_ts = eta_details.get("maxEstimatedDeliveryTime")
    if quoted and dynamic:
        min_ts = min(int(quoted), int(dynamic))
        max_ts = max(int(quoted), int(dynamic))
    elif quoted and not min_ts:
        min_ts = quoted
    if dynamic and not max_ts:
        max_ts = dynamic
    fallback = dynamic or quoted or eta_details.get("estimatedDeliveryTime")
    return _fmt_eta_window(min_ts, max_ts, fallback)


def _gift_status(root: dict) -> tuple[str, str, str]:
    orders = root.get("orders") or []
    order = orders[0] if orders else {}
    delivery = root.get("delivery") or {}
    strings = (root.get("shippingOrderStatus") or {}).get("translatedStrings") or {}
    dasher = root.get("dasher") or {}
    store_name = (root.get("store") or {}).get("name") or "the store"

    dd_title = strings.get("title")
    dd_sub = strings.get("substatus") or strings.get("subtitle")
    dasher_name = " ".join(p for p in [dasher.get("firstName"), dasher.get("lastName")] if p)

    # Determine GIF status code from timestamps
    if order.get("cancelledAt"):
        code = "cancelled"
    elif delivery.get("actualDeliveryTime"):
        code = "delivered"
    elif delivery.get("actualPickupTime"):
        code = "en_route"
    elif delivery.get("dasherAtStoreTime"):
        code = "preparing"
    elif dasher_name and delivery.get("dasherConfirmedTime"):
        code = "preparing"
    else:
        code = "confirmed"

    # Always prefer DoorDash's own title/substatus strings for display
    if order.get("cancelledAt"):
        return dd_title or "Cancelled", dd_sub or "This order was cancelled.", code
    if delivery.get("actualDeliveryTime"):
        return dd_title or "Delivered", dd_sub or "Your order has been delivered.", code
    if dd_title:
        return dd_title, dd_sub or "", code

    # Pure fallbacks — match DoorDash site text exactly, using real store name
    if delivery.get("actualPickupTime"):
        return "Heading to you", "Your Dasher is heading to you.", code
    if delivery.get("dasherAtStoreTime"):
        return "Dasher waiting for order", f"Your Dasher is at {store_name} waiting to pick up your order.", code
    if dasher_name and delivery.get("dasherConfirmedTime"):
        return "Heading to store", "Your Dasher is heading to the store.", code
    if dasher_name:
        return "Dasher assigned", "Your Dasher has been assigned.", code
    if delivery.get("dasherConfirmedTime"):
        return "Order confirmed", "Your order is being prepared.", code
    return "Order placed", "Your order has been placed and is being confirmed.", code


def fetch_gift_tracking(uuid: str) -> dict[str, Any]:
    payload = {
        "operationName": "getGiftTrackingData",
        "variables": {
            "orderUuid": uuid,
            "recipientAuthenticationParams": {
                "authenticationType": "RECIPIENT_PHONE_NUMBER",
                "authenticationInput": "",
            },
        },
        "query": _GIFT_QUERY,
    }
    req = Request(
        _GIFT_GQL_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "accept": "*/*",
            "accept-language": "en-US",
            "apollographql-client-name": "@doordash/app-consumer-production-ssr-client",
            "apollographql-client-version": "3.0",
            "content-type": "application/json",
            "origin": "https://www.doordash.com",
            "referer": _gift_url(uuid),
            "user-agent": USER_AGENT,
            "x-channel-id": "marketplace",
            "x-experience-id": "doordash",
        },
        method="POST",
    )
    with _open(req) as resp:
        body = json.loads(resp.read().decode("utf-8", errors="replace"))

    if body.get("errors"):
        raise ValueError("; ".join(e.get("message", "Unknown") for e in body["errors"]))

    root = (body.get("data") or {}).get("getGiftTrackingData") or {}
    err = root.get("error")
    if err and err.get("message"):
        raise ValueError(err["message"])

    orders = root.get("orders") or []
    order = orders[0] if orders else {}
    delivery = root.get("delivery") or {}
    shipping = root.get("shippingOrderStatus") or {}
    eta_details = shipping.get("etaDetails") or {}
    store = root.get("store") or {}

    items: list[str] = []
    for entry in order.get("orderItems") or []:
        qty = int(entry.get("quantity") or 1)
        name = (entry.get("item") or {}).get("name") or "Item"
        items.append(f"{name} ×{qty}" if qty > 1 else name)

    status, status_msg, status_code = _gift_status(root)

    dasher = root.get("dasher") or {}
    dasher_name = " ".join(p for p in [dasher.get("firstName"), dasher.get("lastName")] if p) or None
    dasher_phone = dasher.get("phone") or None

    gift_info = order.get("giftInfo") or {}
    recipient_name = gift_info.get("recipientName") or None

    pickup_addr = delivery.get("pickupAddress") or {}
    delivery_addr = delivery.get("deliveryAddress") or {}

    return {
        "link_type": "gift",
        "store": store.get("name") or "DoorDash Order",
        "recipient_name": recipient_name,
        "address": delivery_addr.get("printableAddress") or "—",
        "items": items,
        "status": status,
        "status_message": status_msg,
        "status_code": status_code,
        "eta_window": _gift_eta(delivery, eta_details),
        "dasher_name": dasher_name,
        "dasher_phone": dasher_phone,
        "store_lat": pickup_addr.get("lat"),
        "store_lng": pickup_addr.get("lng"),
        "delivery_lat": delivery_addr.get("lat"),
        "delivery_lng": delivery_addr.get("lng"),
    }


# ── Dasher location ──────────────────────────────────────────────────────────

_DASHER_LOC_URL = "https://www.doordash.com/graphql/getGiftTrackingDasherLocation?operation=getGiftTrackingDasherLocation"

_DASHER_LOC_QUERY = """query getGiftTrackingDasherLocation($orderUuid: ID!) {
  getGiftTrackingDasherLocation(orderUuid: $orderUuid) {
    location { lat lng __typename }
    errorCode
    __typename
  }
}"""


def fetch_dasher_location(uuid: str) -> tuple[float, float] | None:
    """Returns (lat, lng) of the dasher, or None if unavailable/not yet assigned."""
    payload = {
        "operationName": "getGiftTrackingDasherLocation",
        "variables": {"orderUuid": uuid},
        "query": _DASHER_LOC_QUERY,
    }
    req = Request(
        _DASHER_LOC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "accept": "*/*",
            "accept-language": "en-US",
            "apollographql-client-name": "@doordash/app-consumer-production-ssr-client",
            "apollographql-client-version": "3.0",
            "content-type": "application/json",
            "origin": "https://www.doordash.com",
            "referer": _gift_url(uuid),
            "user-agent": USER_AGENT,
            "x-channel-id": "marketplace",
            "x-experience-id": "doordash",
        },
        method="POST",
    )
    try:
        with _open(req) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
        data = (body.get("data") or {}).get("getGiftTrackingDasherLocation") or {}
        if data.get("errorCode"):
            return None
        loc = data.get("location") or {}
        lat, lng = loc.get("lat"), loc.get("lng")
        if lat is not None and lng is not None:
            return float(lat), float(lng)
    except Exception:
        pass
    return None


# ── Drive order ───────────────────────────────────────────────────────────────

_DRIVE_BASE = "https://www.doordash.com/orders/drive"
_URLCODE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_DRIVE_STATUS_LABELS: dict[str, str] = {
    "order_completed": "Order complete",
    "order_delivered": "Delivered",
    "order_picked_up": "Picked up",
    "order_confirmed": "Order confirmed",
    "order_placed": "Order placed",
    "dasher_confirmed": "Dasher assigned",
    "store_confirmed": "Store confirmed",
    "preparing_order": "Preparing",
    "ready_for_pickup": "Ready for pickup",
    "picked_up": "Picked up",
    "en_route": "On the way",
}
_DRIVE_STATUS_CODE_MAP: dict[str, str] = {
    "order_confirmed": "confirmed",
    "order_placed": "confirmed",
    "dasher_confirmed": "confirmed",
    "store_confirmed": "confirmed",
    "preparing_order": "preparing",
    "ready_for_pickup": "ready_for_pickup",
    "picked_up": "en_route",
    "order_picked_up": "en_route",
    "en_route": "en_route",
    "order_delivered": "delivered",
    "order_completed": "delivered",
}


def _url_code_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    code = parse_qs(parsed.query).get("urlCode", [None])[0]
    if code:
        return code
    m = re.search(r"urlCode=([0-9a-f-]{36})", url, re.IGNORECASE)
    return m.group(1) if m else None


def _resolve_drive_url_code(raw: str) -> str:
    stripped = raw.strip()
    if _URLCODE_RE.fullmatch(stripped):
        return stripped.lower()
    if not stripped.startswith(("http://", "https://")):
        stripped = f"https://{stripped}"
    parsed = urlparse(stripped)
    if (parsed.netloc or "").lower().endswith("doordash.com"):
        code = _url_code_from_url(stripped)
        if not code:
            raise ValueError("DoorDash drive link is missing urlCode.")
        return code
    req = Request(stripped, headers={"user-agent": USER_AGENT, "accept": "text/html"})
    with _open(req) as resp:
        final_url = resp.url
    code = _url_code_from_url(final_url)
    if not code:
        raise ValueError(f"Redirect did not yield a urlCode (got {final_url})")
    return code


def _re1(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


def _unescape(value: str | None) -> str | None:
    if not value or value in ("$undefined", "undefined") or value.startswith("$"):
        return None
    return value.replace("\\u0026", "&").replace("&amp;", "&")


def _fmt_iso(ts: str | None, tz_name: str | None) -> str | None:
    if not ts:
        return None
    try:
        raw = ts[2:] if ts.startswith("$D") else ts
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if tz_name:
            dt = dt.astimezone(ZoneInfo(tz_name))
        return dt.strftime("%I:%M %p").lstrip("0")
    except (ValueError, OSError):
        return None


def fetch_drive_tracking(url_code: str) -> dict[str, Any]:
    tracking_url = f"{_DRIVE_BASE}?urlCode={url_code}"
    req = Request(
        tracking_url,
        headers={
            "accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "user-agent": USER_AGENT,
        },
    )
    with _open(req) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    store = _re1(r'\\"businessName\\":\\"([^"\\]+)\\"', html)
    api_status = _unescape(_re1(r'\\"orderStatus\\":\\"([^"\\]+)\\"', html))
    tz = _re1(r'\\"timezone\\":\\"([^"\\]+)\\"', html) or "UTC"

    actual = _unescape(_re1(r'\\"actualDeliveryTime\\":\\"([^"\\]+)\\"', html))
    terminal = _unescape(_re1(r'\\"terminalStateTimestamp\\":\\"\$D([^"\\]+)\\"', html))
    estimated = _unescape(_re1(r'\\"estimatedDeliveryTime\\":\\"([^"\\]+)\\"', html))
    quoted = _unescape(_re1(r'\\"quotedDeliveryTime\\":\\"([^"\\]+)\\"', html))

    eta_raw = actual or terminal or estimated or quoted
    eta_display = _fmt_iso(eta_raw, tz) or "—"
    eta_window_raw = _re1(r'\\"etaText\\":\\"([^"\\]+)\\"', html)

    tracker_title = None
    m = re.search(
        r'\\"title\\":\\"([^"\\]+)\\",\\"subtitle\\":\\"(?:\\$undefined|[^"\\]*)\\"'
        r',\\"substatus\\":\\"([^"\\]+)\\",\\"bundleTranslatedStrings\\"',
        html,
    )
    if m:
        tracker_title = m.group(1)

    status_label = tracker_title or _DRIVE_STATUS_LABELS.get(
        api_status or "", (api_status or "Tracking").replace("_", " ").title()
    )
    status_code = _DRIVE_STATUS_CODE_MAP.get(api_status or "", "confirmed")

    street = _re1(r'\\"street\\":\\"([^"\\]+)\\"', html)
    city = _re1(r'\\"city\\":\\"([^"\\]+)\\"', html)
    state = _re1(r'\\"state\\":\\"([^"\\]+)\\"', html)
    zipcode = _re1(r'\\"zipcode\\":\\"([^"\\]+)\\"', html)
    address = ", ".join(p for p in [street, city, state, zipcode] if p) or "—"

    return {
        "link_type": "drive",
        "store": store or "DoorDash Order",
        "address": address,
        "items": [],
        "status": status_label,
        "status_message": None,
        "status_code": status_code,
        "eta_window": eta_window_raw or eta_display,
        "dasher_name": None,
        "dasher_phone": None,
        "recipient_name": None,
    }


# ── Unified entry point ───────────────────────────────────────────────────────

def detect_link_type(raw: str) -> str:
    s = raw.strip()
    if "/gifts/" in s or _GIFT_UUID_RE.search(s):
        return "gift"
    return "drive"


def fetch_tracking(raw_link: str) -> tuple[str, str, dict[str, Any]]:
    """Returns (link_type, key, details). key is UUID for gift, url_code for drive."""
    link_type = detect_link_type(raw_link)
    if link_type == "gift":
        uuid = _extract_gift_uuid(raw_link)
        return "gift", uuid, fetch_gift_tracking(uuid)
    else:
        url_code = _resolve_drive_url_code(raw_link)
        return "drive", url_code, fetch_drive_tracking(url_code)
