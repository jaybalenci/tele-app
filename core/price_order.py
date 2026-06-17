"""End-to-end /price flow: extract → rebuild → address → checkout."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from curl_cffi.requests.exceptions import RequestException

from core.account_pool import acquire
from core.logger import log
from doordash.address import set_delivery_address
from doordash.cart_extract import extract_cart_items
from doordash.checkout import PriceBreakdown, apply_promo_code, calc_tip_cents, fetch_checkout, summarize_order_items, summarize_order_items_detail
from doordash.group_order import join_group_order
from doordash.rebuild import rebuild_cart, schedule_cart_cleanup
from doordash.web_client import DoorDashWebSession, resolve_csrf, store_referer
from core.pricing import PriceBreakdownFields


def _warm_and_set_address(
    client: DoorDashWebSession,
    address: str,
    *,
    on_address: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Warm the session then fully resolve the delivery address in one chain."""
    log("session", "warming...")
    client.warm("https://www.doordash.com/")
    log("session", "resolving address...")
    result = set_delivery_address(client, address, referer="https://www.doordash.com/")
    log("session", "✓ address ready")
    if on_address:
        default = result.get("default_address") or {}
        on_address(default.get("printableAddress") or address)
    return result


@dataclass
class PriceOrderResult:
    address: str
    store: str
    items: list[tuple[str, int]]
    items_detail: list[dict]  # [{name, qty, img}]
    pricing: PriceBreakdownFields
    cart_id: str
    failures: list[str]
    tip_error: str | None = None
    cleanup_fn: Callable[[], None] | None = field(default=None, repr=False)


def run_price_order(
    order_link: str,
    address: str,
    *,
    prefetched_source_cart: dict | None = None,
    on_item_added: Callable[[list[str]], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    on_info: Callable[[str, str], None] | None = None,
    promo_code: str = "YOUGOT40",
    tip_str: str = "none",
) -> PriceOrderResult:
    def _status(msg: str) -> None:
        if on_status:
            on_status(msg)

    def _info(key: str, value: str) -> None:
        if on_info:
            on_info(key, value)

    tried: set[int] = set()
    failures = 0

    while True:
        if failures >= 3:
            raise RuntimeError("Price check failed after 3 attempts. Please try again.")
        with acquire(exclude=frozenset(tried)) as (idx, cookies):
            _status("Checking order...")
            pre_client = DoorDashWebSession(cookies)

            _switch_account = False

            if prefetched_source_cart is not None:
                # ── Fast path: cart pre-loaded, run address + rebuild in parallel ──
                source_cart = prefetched_source_cart
                specs = extract_cart_items(source_cart)
                if not specs:
                    raise RuntimeError("That cart has no items to rebuild.")
                restaurant = source_cart.get("restaurant") or {}
                menu_id = str((source_cart.get("menu") or {}).get("id") or "")
                log("price", f"phase 1: warming (pre-loaded: {restaurant.get('name', '?')}, {len(specs)} item(s))...")

                try:
                    pre_client.warm("https://www.doordash.com/")
                except RequestException as exc:
                    exc_str = str(exc)
                    if "403" in exc_str or "curl: (56)" in exc_str or "503" in exc_str:
                        log("price", f"warm blocked (account {idx + 1}), retrying...")
                        failures += 1
                        tried.add(idx)
                        continue
                    raise
                warmed_cookies = dict(pre_client.cookies)

                log("price", f"phase 2: address + rebuild in parallel ({len(specs)} item(s))...")

                def _do_address() -> dict[str, Any]:
                    c = DoorDashWebSession(dict(warmed_cookies))
                    c.csrf = resolve_csrf(c.cookies)
                    c.cookies["csrf_token"] = c.csrf
                    return set_delivery_address(c, address, referer="https://www.doordash.com/")

                with ThreadPoolExecutor(max_workers=2) as pool:
                    f_addr = pool.submit(_do_address)
                    f_rebuild = pool.submit(
                        rebuild_cart,
                        specs, restaurant, menu_id, dict(warmed_cookies),
                        on_item_added=on_item_added,
                        on_status=on_status,
                    )

                try:
                    address_result = f_addr.result()
                except RequestException as exc:
                    if "403" in str(exc):
                        log("price", f"address blocked 403 (account {idx + 1}), retrying...")
                        failures += 1
                        tried.add(idx)
                        continue
                    raise

                default_address = (address_result.get("default_address") or {})
                printable_address = default_address.get("printableAddress") or address
                _info("address", printable_address)

                try:
                    rebuilt, item_failures, client, built_cart_id = f_rebuild.result()
                except RequestException as exc:
                    exc_str = str(exc)
                    if "timed out" in exc_str.lower() or "curl: (28)" in exc_str or "curl: (56)" in exc_str or "503" in exc_str or "403" in exc_str:
                        log("price", f"rebuild failed (account {idx + 1}), retrying...")
                        failures += 1
                        tried.add(idx)
                        continue
                    raise

            else:
                # ── Standard path: warm+address parallel with join, then rebuild ──
                log("price", "phase 1: group order + session warm + address...")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    f_setup = pool.submit(
                        _warm_and_set_address, pre_client, address,
                        on_address=lambda addr: _info("address", addr),
                    )

                    def _on_join_done(fut: Any) -> None:
                        try:
                            _, _, sc = fut.result()
                            name = (sc.get("restaurant") or {}).get("name") or ""
                            if name:
                                _info("store", name)
                        except Exception:
                            pass

                    f_join = pool.submit(join_group_order, cookies, order_link)
                    f_join.add_done_callback(_on_join_done)

                try:
                    address_result = f_setup.result()
                except RequestException as exc:
                    if "403" in str(exc):
                        log("price", f"address blocked 403 (account {idx + 1}), retrying...")
                        failures += 1
                        tried.add(idx)
                        continue
                    raise

                default_address = (address_result.get("default_address") or {})
                printable_address = default_address.get("printableAddress") or address

                _join_try = 0
                while True:
                    try:
                        if _join_try == 0:
                            cart_id, _, source_cart = f_join.result()
                        else:
                            log("price", f"retrying join (attempt {_join_try + 1})...")
                            cart_id, _, source_cart = join_group_order(cookies, order_link)
                        break
                    except (RuntimeError, RequestException) as exc:
                        exc_str = str(exc)
                        if "timed out" in exc_str.lower() or "curl: (28)" in exc_str or "curl: (56)" in exc_str or "503" in exc_str:
                            failures += 1
                            _join_try += 1
                            log("price", f"join timed out (account {idx + 1}), retrying join only...")
                            if failures >= 3:
                                _switch_account = True
                                tried.add(idx)
                                break
                            continue
                        if "403" in exc_str:
                            log("price", f"join blocked 403 (account {idx + 1}), switching account...")
                            failures += 1
                            tried.add(idx)
                            _switch_account = True
                            break
                        raise

                if _switch_account:
                    continue

                specs = extract_cart_items(source_cart)
                if not specs:
                    raise RuntimeError("That cart has no items to rebuild.")

                restaurant = source_cart.get("restaurant") or {}
                menu_id = str((source_cart.get("menu") or {}).get("id") or "")
                log("price", f"✓ group order → {restaurant.get('name', '?')} ({len(specs)} item(s))")

                log("price", f"phase 2: building cart ({len(specs)} items)...")
                warmed_cookies = dict(pre_client.cookies)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    f_rebuild = pool.submit(
                        rebuild_cart,
                        specs, restaurant, menu_id, warmed_cookies,
                        on_item_added=on_item_added,
                        on_status=on_status,
                    )

                try:
                    rebuilt, item_failures, client, built_cart_id = f_rebuild.result()
                except RequestException as exc:
                    exc_str = str(exc)
                    if "timed out" in exc_str.lower() or "curl: (28)" in exc_str or "curl: (56)" in exc_str or "503" in exc_str:
                        log("price", f"rebuild timed out (account {idx + 1}), retrying...")
                        failures += 1
                        tried.add(idx)
                        continue
                    if "403" in exc_str:
                        log("price", f"rebuild blocked 403 (account {idx + 1}), retrying...")
                        failures += 1
                        tried.add(idx)
                        continue
                    raise

            if not built_cart_id:
                if item_failures:
                    lines = "\n".join(f"• {f}" for f in item_failures)
                    raise RuntimeError(f"Could not add any items to the cart:\n{lines}")
                raise RuntimeError(
                    "The cart appears to be empty. Make sure the group order has items before price-checking."
                )

            lat = float(default_address.get("lat") or 0)
            lng = float(default_address.get("lng") or 0)

            if promo_code and promo_code != "Not Set":
                _status("Applying promotion...")
                apply_promo_code(client, built_cart_id, promo_code, lat=lat, lng=lng)

            tip_cents = calc_tip_cents(tip_str, int(rebuilt.get("subtotal") or 0))
            tip_error: str | None = None

            _checkout_try = 0
            while True:
                try:
                    checkout_cart = fetch_checkout(client, built_cart_id, lat=lat, lng=lng)
                    break
                except RequestException as exc:
                    exc_str = str(exc)
                    if "timed out" in exc_str.lower() or "curl: (28)" in exc_str or "curl: (56)" in exc_str or "503" in exc_str:
                        failures += 1
                        _checkout_try += 1
                        log("price", f"checkout timed out (account {idx + 1}), retrying...")
                        if failures >= 3:
                            tried.add(idx)
                            _switch_account = True
                            break
                        continue
                    if "403" in exc_str:
                        log("price", f"checkout blocked 403 (account {idx + 1}), retrying...")
                        failures += 1
                        tried.add(idx)
                        _switch_account = True
                        break
                    raise

            if _switch_account:
                continue

        breakdown = PriceBreakdown.from_cart(checkout_cart)
        items = summarize_order_items(checkout_cart)
        if not items and rebuilt:
            items = summarize_order_items(rebuilt)
        items_detail = summarize_order_items_detail(checkout_cart)
        if not items_detail and rebuilt:
            items_detail = summarize_order_items_detail(rebuilt)

        # Total = arithmetic sum of displayed components — always consistent.
        # Original (strikethrough) = same without the promo discount.
        total_with_tip = breakdown.subtotal_cents + breakdown.fees_tax_cents + breakdown.delivery_cents - breakdown.discounts_cents + tip_cents
        original_with_tip = (breakdown.subtotal_cents + breakdown.fees_tax_cents + breakdown.delivery_cents + tip_cents) if breakdown.discounts_cents else 0
        pricing = PriceBreakdownFields(
            subtotal_display=breakdown.subtotal_display,
            fees_tax_display=breakdown.fees_tax_display,
            delivery_fee_display=breakdown.delivery_fee_display,
            discounts_display=breakdown.discounts_display,
            tip_display=f"${tip_cents / 100:.2f}" if tip_cents else "",
            total_display=f"${total_with_tip / 100:.2f}",
            original_total_display=f"${original_with_tip / 100:.2f}" if original_with_tip else "",
        )

        # Capture cleanup args in a closure — called by main.py AFTER the price
        # is shown to the user, not during the price check itself.
        _client, _cart_id, _rebuilt, _referer = client, built_cart_id, rebuilt, store_referer(restaurant, menu_id)

        return PriceOrderResult(
            address=printable_address,
            store=restaurant.get("name") or "Unknown Restaurant",
            items=items,
            items_detail=items_detail,
            pricing=pricing,
            cart_id=built_cart_id,
            failures=item_failures,
            tip_error=tip_error,
            cleanup_fn=lambda: schedule_cart_cleanup(_client, _cart_id, _rebuilt, _referer),
        )
