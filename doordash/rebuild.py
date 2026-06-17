"""Rebuild a source cart on the build account."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any

from curl_cffi.requests.exceptions import RequestException

from core.logger import log, vlog
from doordash.cart_extract import CartItemSpec
from doordash.item_options import build_nested_from_item_page, fetch_item_page
from doordash.web_client import (
    DoorDashWebSession,
    add_cart_item,
    list_detailed_carts,
    remove_cart_item,
    resolve_csrf,
    store_referer,
)


def _clear_existing_carts(
    client: DoorDashWebSession,
    store_id: str,
    referer: str,
) -> None:
    """Find and clear any active build-account carts for this store."""
    vlog("clear_cart", f"fetching carts for store {store_id}")
    try:
        detailed = list_detailed_carts(client, referer=referer)
    except Exception as exc:
        vlog("clear_cart", f"listDetailedCarts failed: {exc}")
        return

    vlog("clear_cart", f"{len(detailed)} active cart(s) found")
    for entry in detailed:
        cart = entry.get("cart") or {}
        cart_id = str(cart.get("id") or "")
        restaurant = cart.get("restaurant") or {}
        cart_store_id = str(restaurant.get("id") or "")
        vlog("clear_cart", f"cart {cart_id[:8]}… → store {cart_store_id}")

        if cart_store_id != store_id:
            vlog("clear_cart", "skipping (different store)")
            continue

        item_ids = [
            str(order_item["id"])
            for order in (cart.get("orders") or [])
            for order_item in (order.get("orderItems") or [])
            if order_item.get("id")
        ]

        if not item_ids:
            vlog("clear_cart", f"cart {cart_id[:8]}… already empty")
            continue

        vlog("clear_cart", f"removing {len(item_ids)} item(s) from cart {cart_id[:8]}…")
        for item_id in item_ids:
            try:
                resp = remove_cart_item(client, cart_id=cart_id, item_id=item_id, referer=referer)
                if resp.get("errors"):
                    vlog("clear_cart", f"error removing {item_id}: {resp['errors']}")
                else:
                    vlog("clear_cart", f"removed {item_id} ✓")
            except Exception as exc:
                vlog("clear_cart", f"exception removing {item_id}: {exc}")


def rebuild_cart(
    specs: list[CartItemSpec],
    restaurant: dict[str, Any],
    menu_id: str,
    build_cookies: dict[str, str],
    *,
    on_item_added: Callable[[list[str]], None] | None = None,
    on_status: Callable[[str], None] | None = None,
) -> tuple[dict[str, Any], list[str], DoorDashWebSession, str]:
    def _status(msg: str) -> None:
        if on_status:
            on_status(msg)

    referer = store_referer(restaurant, menu_id)
    client = DoorDashWebSession(build_cookies)
    # build_cookies come from the already-warmed pre_client (Phase 1), so they
    # carry a valid CSRF token. Skip the store-page warm to avoid a redundant
    # HTTP round-trip through the proxy that can hit the 20s timeout.
    client.csrf = resolve_csrf(client.cookies)
    client.cookies["csrf_token"] = client.csrf

    _status("Adding items...")

    cart_id = ""
    last_response: dict[str, Any] = {}
    last_successful_cart: dict[str, Any] = {}
    failed: list[str] = []
    added_lines: list[str] = []

    for spec in specs:
        nested_attempts = [spec.nested_options]
        if spec.fallback_nested_options and spec.fallback_nested_options != spec.nested_options:
            nested_attempts.append(spec.fallback_nested_options)
        if "[]" not in nested_attempts:
            nested_attempts.append("[]")

        added = False
        last_err = ""
        item_page_fetched = False
        log("rebuild", f"+ {spec.quantity}x {spec.item_name}")
        vlog("rebuild", f"  item_id={spec.item_id}")
        attempt_idx = 0
        while attempt_idx < len(nested_attempts):
            nested = nested_attempts[attempt_idx]
            vlog("rebuild", f"  attempt {attempt_idx}: nestedOptions={nested[:200]}")
            item_input = spec.to_add_cart_input()
            item_input["nestedOptions"] = nested
            try:
                last_response = add_cart_item(
                    client,
                    add_cart_item_input=item_input,
                    referer=referer,
                    cart_id=cart_id,
                )
            except RequestException as exc:
                last_err = str(exc)
                vlog("rebuild", f"  attempt {attempt_idx} RequestException: {last_err}")
                attempt_idx += 1
                continue

            if last_response.get("errors"):
                last_err = json.dumps(last_response["errors"])[:300]
                vlog("rebuild", f"  attempt {attempt_idx} GraphQL errors: {last_err}")

                # On "wrong level" error, fetch the item page and inject a
                # hierarchy-corrected attempt immediately after this one.
                if "wrong level" in last_err and not item_page_fetched:
                    item_page_fetched = True
                    vlog("rebuild", f"  fetching option hierarchy for item {spec.item_id}")
                    item_page = fetch_item_page(client, spec.store_id, spec.item_id, referer)
                    if item_page:
                        corrected = build_nested_from_item_page(nested, item_page)
                        if corrected and corrected not in nested_attempts:
                            nested_attempts.insert(attempt_idx + 1, corrected)
                attempt_idx += 1
                continue

            cart = (last_response.get("data") or {}).get("addCartItemV2")
            if not cart:
                last_err = "no addCartItemV2 in response"
                vlog("rebuild", f"  attempt {attempt_idx}: {last_err}")
                attempt_idx += 1
                continue

            cart_id = str(cart.get("id") or cart_id)
            last_successful_cart = cart
            added = True
            log("rebuild", f"  ✓ added (attempt {attempt_idx})")
            vlog("rebuild", f"  cart_id={cart_id}")
            break

        if not added:
            if "Item is not available" in last_err:
                display_err = "Out of Stock"
            else:
                display_err = last_err
            log("rebuild", f"  ✗ FAILED — {display_err}")
            failed.append(f"{spec.quantity}x {spec.item_name} — {display_err}")
            continue

        for _ in range(spec.quantity):
            added_lines.append(spec.item_name)
        if on_item_added:
            on_item_added(list(added_lines))

    return last_successful_cart, failed, client, cart_id


def schedule_cart_cleanup(
    client: DoorDashWebSession,
    cart_id: str,
    cart_data: dict[str, Any],
    referer: str,
) -> None:
    """Fire a daemon thread to remove ALL current items from the cart after a price check."""
    def _get_item_ids() -> list[str]:
        try:
            all_carts = list_detailed_carts(client, referer=referer)
            target = next(
                (e["cart"] for e in all_carts
                 if str((e.get("cart") or {}).get("id") or "") == cart_id),
                None,
            )
            if target is not None:
                return [
                    str(oi["id"])
                    for order in (target.get("orders") or [])
                    for oi in (order.get("orderItems") or [])
                    if oi.get("id")
                ]
        except Exception as exc:
            vlog("cleanup", f"could not fetch live cart, using rebuild data: {exc}")
        return [
            str(oi["id"])
            for order in (cart_data.get("orders") or [])
            for oi in (order.get("orderItems") or [])
            if oi.get("id")
        ]

    def _run() -> None:
        item_ids = _get_item_ids()
        if not item_ids:
            vlog("cleanup", f"cart {cart_id[:8]}… already empty")
            return
        log("cleanup", f"clearing {len(item_ids)} item(s) from cart {cart_id[:8]}…")
        for item_id in item_ids:
            try:
                remove_cart_item(client, cart_id=cart_id, item_id=item_id, referer=referer)
            except Exception as exc:
                log("cleanup", f"✗ failed to remove {item_id}: {exc}")
        log("cleanup", "✓ done")

    threading.Thread(target=_run, daemon=True).start()
