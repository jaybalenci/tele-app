"""Fetch checkout pricing for a rebuilt cart."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.logger import log, vlog
from doordash.web_client import BROWSER_UA, DoorDashWebSession, load_query, request_kwargs

CHECKOUT_URL = "https://www.doordash.com/graphql/checkout?operation=checkout"
CHECKOUT_QUERY = Path(__file__).resolve().parent / "graphql" / "checkout.graphql"

APPLY_PROMO_URL = "https://www.doordash.com/unified-gateway/cx/ads/v1/apply_promotion"
UPDATE_GROUP_CART_URL = "https://www.doordash.com/graphql/updateGroupCart?operation=updateGroupCart"
UPDATE_GROUP_CART_QUERY = Path(__file__).resolve().parent / "graphql" / "update_order_cart.graphql"


def checkout_referer(cart_id: str, lat: float, lng: float) -> str:
    return (
        f"https://www.doordash.com/consumer/checkout/"
        f"?lat={lat}&lng={lng}&order_cart_id={cart_id}"
    )


def fetch_checkout(
    client: DoorDashWebSession,
    cart_id: str,
    *,
    lat: float,
    lng: float,
    should_apply_credits: bool = True,
) -> dict[str, Any]:
    log("checkout", "fetching final price...")
    referer = checkout_referer(cart_id, lat, lng)
    data = client.graphql(
        CHECKOUT_URL,
        "checkout",
        {
            "orderCartId": cart_id,
            "shouldApplyCredits": should_apply_credits,
        },
        load_query(CHECKOUT_QUERY),
        referer,
    )
    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))
    cart = (data.get("data") or {}).get("orderCart")
    if not cart:
        raise RuntimeError("Checkout response missing orderCart.")
    log("checkout", "✓ done")
    return cart


def apply_promo_code(
    client: DoorDashWebSession,
    cart_id: str,
    promo_code: str,
    *,
    lat: float,
    lng: float,
) -> bool:
    """Apply a promo code via the unified-gateway REST endpoint."""
    referer = checkout_referer(cart_id, lat, lng)
    device_id = client.cookies.get("dd_device_id", "")
    session_id = client.cookies.get("dd_session_id", "")
    headers = {
        "accept": "*/*",
        "accept-language": "en-US",
        "content-type": "application/json",
        "origin": "https://www.doordash.com",
        "referer": referer,
        "user-agent": BROWSER_UA,
        "x-experience-id": "doordash",
        "x-unified-gateway-generated-source": "v1",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if device_id and session_id:
        import json as _json
        headers["dd-ids"] = _json.dumps({"dd_device_id": device_id, "dd_session_id": session_id})
    payload = {
        "cart_id": cart_id,
        "promotion_code": promo_code,
        "delivery_option_type": "NOT_SET",
    }
    try:
        resp = client.session.post(
            APPLY_PROMO_URL,
            headers=headers,
            cookies=client.cookies,
            json=payload,
            **request_kwargs(),
        )
        vlog("promo", f"{resp.status_code}: {resp.text[:300]}")
        if resp.status_code < 400:
            try:
                data = resp.json()
                if data.get("is_redeemable"):
                    log("promo", f"✓ {promo_code} applied")
                else:
                    log("promo", f"not redeemable ({promo_code})")
            except Exception:
                log("promo", f"status {resp.status_code}")
        else:
            log("promo", f"✗ failed ({resp.status_code})")
        return resp.status_code < 400
    except Exception as exc:
        log("promo", f"✗ exception: {exc}")
        return False


def _line_item(cart: dict[str, Any], charge_id: str) -> dict[str, Any] | None:
    for entry in cart.get("lineItemsList") or []:
        if entry.get("chargeId") == charge_id:
            return entry
    return None


def _money_cents(line_item: dict[str, Any] | None) -> int:
    if not line_item:
        return 0
    money = line_item.get("finalMoney") or {}
    amount = money.get("unitAmount")
    return int(amount) if isinstance(amount, int) else 0


def _money_display(line_item: dict[str, Any] | None, fallback: str = "$0.00") -> str:
    if not line_item:
        return fallback
    money = line_item.get("finalMoney") or {}
    display = money.get("displayString")
    if isinstance(display, str) and display:
        import re as _re
        return _re.sub(r"US\$[\s\xa0]*", "$", display)
    return fallback


@dataclass
class PriceBreakdown:
    subtotal_cents: int
    fees_tax_cents: int
    delivery_cents: int
    delivery_fee_display: str
    discounts_cents: int
    total_cents: int
    subtotal_display: str
    fees_tax_display: str
    discounts_display: str
    total_display: str

    @classmethod
    def from_cart(cls, cart: dict[str, Any]) -> PriceBreakdown:
        subtotal = int(cart.get("subtotal") or 0)
        total = int(cart.get("total") or 0)
        delivery_item = _line_item(cart, "DELIVERY_FEE")
        delivery_cents = _money_cents(delivery_item)

        _ALWAYS_SKIP = frozenset({"DELIVERY_FEE", "DASHER_TIP", "DRIVER_TIP", "SUBTOTAL"})
        _SKIP_KEYWORDS = ("DISCOUNT", "PROMOTION", "CREDIT", "TIP")
        fees_tax = 0
        for entry in cart.get("lineItemsList") or []:
            cid = (entry.get("chargeId") or "").upper()
            if cid in _ALWAYS_SKIP:
                continue
            if any(k in cid for k in _SKIP_KEYWORDS):
                continue
            amt = (entry.get("finalMoney") or {}).get("unitAmount")
            try:
                amt_int = int(amt) if amt is not None else 0
            except (ValueError, TypeError):
                amt_int = 0
            if amt_int > 0:
                fees_tax += amt_int

        log("checkout", f"line items: {[(e.get('chargeId'), (e.get('finalMoney') or {}).get('displayString')) for e in cart.get('lineItemsList') or []]}")

        # Discount = arithmetic difference so line items always add up to the total.
        # DoorDash's totalBeforeDiscountsAndCredits includes a default dasher tip
        # which inflates the savings number and breaks the math.
        discounts = max(subtotal + fees_tax + delivery_cents - total, 0)

        def fmt(cents: int) -> str:
            return f"${cents / 100:.2f}"

        return cls(
            subtotal_cents=subtotal,
            fees_tax_cents=fees_tax,
            delivery_cents=delivery_cents,
            delivery_fee_display=_money_display(delivery_item),
            discounts_cents=discounts,
            total_cents=total,
            subtotal_display=fmt(subtotal),
            fees_tax_display=fmt(fees_tax),
            discounts_display=f"-{fmt(discounts)}" if discounts else "",
            total_display=fmt(total),
        )


def set_dasher_tip(
    client: DoorDashWebSession,
    cart_id: str,
    tip_pct: int,
    *,
    lat: float,
    lng: float,
) -> str | None:
    """Set the dasher tip as an integer percentage (e.g. 20 = 20%).
    Returns None on success, error string on failure."""
    if tip_pct <= 0:
        return None
    referer = checkout_referer(cart_id, lat, lng)
    errors: list[str] = []

    query = load_query(UPDATE_GROUP_CART_QUERY)
    for variables in [
        {"input": {"id": cart_id, "groupCartPreCheckoutDetails": {"dasherTipPercentage": tip_pct}}},
        {"input": {"id": cart_id, "dasherTipPercentage": tip_pct}},
    ]:
        try:
            data = client.graphql(UPDATE_GROUP_CART_URL, "updateGroupCart", variables, query, referer)
            log("tip", f"updateGroupCart vars={list((variables.get('input') or {}).keys())}: {str(data)[:400]}")
            if data.get("errors"):
                errors.append(f"updateGroupCart: {data['errors']}")
                continue
            result = (data.get("data") or {}).get("updateGroupCart")
            if result is not None:
                pct = (result.get("groupCartPreCheckoutDetails") or {}).get("dasherTipPercentage")
                log("tip", f"✓ tip set to {pct}%")
                return None
            errors.append("updateGroupCart: empty data")
        except Exception as exc:
            errors.append(f"updateGroupCart: {exc}")
            log("tip", f"✗ updateGroupCart failed: {exc}")

    err = " | ".join(errors)
    log("tip", f"✗ all attempts failed: {err}")
    return err


def calc_tip_pct(tip_str: str, subtotal_cents: int) -> int:
    """Convert tip string ('15%', '$2.50', 'none') to an integer percentage for DoorDash."""
    s = (tip_str or "").strip().lower()
    if not s or s == "none" or s == "0" or s == "0%":
        return 0
    if s.endswith("%"):
        try:
            return round(float(s[:-1]))
        except ValueError:
            return 0
    # dollar amount — convert to percentage of subtotal
    clean = s.replace("$", "").replace(",", "")
    try:
        dollars = float(clean)
        if subtotal_cents <= 0:
            return 0
        return round(dollars * 100 / subtotal_cents * 100)
    except ValueError:
        return 0


def calc_tip_cents(tip_str: str, subtotal_cents: int) -> int:
    """Convert tip string ('15%', '$2.50', 'none') to cents."""
    s = (tip_str or "").strip().lower()
    if not s or s == "none" or s == "0" or s == "0%":
        return 0
    if s.endswith("%"):
        try:
            return round(subtotal_cents * float(s[:-1]) / 100)
        except ValueError:
            return 0
    clean = s.replace("$", "").replace(",", "")
    try:
        return round(float(clean) * 100)
    except ValueError:
        return 0


def summarize_order_items_detail(cart: dict[str, Any]) -> list[dict]:
    """Like summarize_order_items but includes imageUrl."""
    seen: dict[str, dict] = {}
    for order in cart.get("orders") or []:
        for oi in order.get("orderItems") or []:
            item = oi.get("item") or {}
            name = item.get("name") or "Item"
            qty  = int(oi.get("quantity") or 1)
            img  = item.get("imageUrl") or ""
            if name in seen:
                seen[name]["qty"] += qty
            else:
                seen[name] = {"name": name, "qty": qty, "img": img}
    return list(seen.values())


def summarize_order_items(cart: dict[str, Any]) -> list[tuple[str, int]]:
    tallies: dict[str, int] = {}
    for order in cart.get("orders") or []:
        for order_item in order.get("orderItems") or []:
            name = (order_item.get("item") or {}).get("name") or "Item"
            qty = int(order_item.get("quantity") or 1)
            tallies[name] = tallies.get(name, 0) + qty
    return [(name, qty) for name, qty in tallies.items()]
