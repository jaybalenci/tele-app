"""DoorDash consumer web GraphQL client (www.doordash.com)."""

from __future__ import annotations

import json
import os
import secrets
import string
from pathlib import Path
from typing import Any

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from doordash.constants import IMPERSONATE

_GQL = Path(__file__).resolve().parent / "graphql"
GROUP_CART_QUERY = _GQL / "group_cart.graphql"
DETAILED_CART_QUERY = _GQL / "detailed_cart_items.graphql"
ADD_CART_ITEM_QUERY = _GQL / "add_cart_item.graphql"
REMOVE_CART_ITEM_QUERY = _GQL / "remove_cart_item.graphql"
LIST_DETAILED_CARTS_QUERY = _GQL / "list_detailed_carts.graphql"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

GROUP_CART_URL = "https://www.doordash.com/graphql/groupCart?operation=groupCart"
DETAILED_CART_URL = "https://www.doordash.com/graphql/detailedCartItems?operation=detailedCartItems"
ADD_CART_ITEM_URL = "https://www.doordash.com/graphql/addCartItem?operation=addCartItem"
REMOVE_CART_ITEM_URL = "https://www.doordash.com/graphql/removeCartItem?operation=removeCartItem"
LIST_DETAILED_CARTS_URL = "https://www.doordash.com/graphql/listDetailedCarts?operation=listDetailedCarts"


def generate_csrf_token(length: int = 43) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def parse_cookie_string(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or part.startswith("#") or "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def load_cookies(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"Cookie file not found: {path}")
    cookies = parse_cookie_string(path.read_text(encoding="utf-8"))
    if not cookies:
        raise ValueError(f"No cookies parsed from {path}")
    return cookies


def load_account_pool(
    accounts_dir: Path,
    cf_clearance_path: Path | None = None,
) -> list[dict[str, str]]:
    """Load all account_*.txt files from a directory, injecting shared cf_clearance."""
    cf_clearance = ""
    if cf_clearance_path and cf_clearance_path.is_file():
        val = cf_clearance_path.read_text(encoding="utf-8").strip()
        if val and not val.startswith("PASTE_"):
            cf_clearance = val

    accounts: list[dict[str, str]] = []
    for f in sorted(accounts_dir.glob("account_*.txt")):
        cookies = parse_cookie_string(f.read_text(encoding="utf-8"))
        if cookies:
            if cf_clearance:
                cookies["cf_clearance"] = cf_clearance
            accounts.append(cookies)

    if not accounts:
        raise FileNotFoundError(f"No account files found in {accounts_dir}")
    return accounts




def _sec_fetch_navigate() -> dict[str, str]:
    return {
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
    }


def _sec_fetch_cors() -> dict[str, str]:
    return {
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }


def build_browser_headers(referer: str, csrf: str | None = None) -> dict[str, str]:
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US",
        "origin": "https://www.doordash.com",
        "referer": referer,
        "user-agent": BROWSER_UA,
        "x-experience-id": "doordash",
        **_sec_fetch_navigate(),
    }
    if csrf:
        headers["x-csrftoken"] = csrf
    return headers


def build_graphql_headers(csrf: str, referer: str) -> dict[str, str]:
    return {
        "accept": "*/*",
        "accept-language": "en-US",
        "apollographql-client-name": "@doordash/app-consumer-production-ssr-client",
        "apollographql-client-version": "3.0",
        "content-type": "application/json",
        "origin": "https://www.doordash.com",
        "referer": referer,
        "user-agent": BROWSER_UA,
        "x-channel-id": "marketplace",
        "x-csrftoken": csrf,
        "x-experience-id": "doordash",
        **_sec_fetch_cors(),
    }


def _build_proxy_url(raw: str) -> str:
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    return f"http://{raw}"


def request_kwargs() -> dict[str, Any]:
    kw: dict[str, Any] = {"impersonate": IMPERSONATE, "timeout": 20}
    raw = os.getenv("DOORDASH_PROXY", "").strip()
    if raw:
        kw["proxy"] = _build_proxy_url(raw)
    return kw


def merge_session_cookies(
    base: dict[str, str],
    session: requests.Session,
) -> dict[str, str]:
    merged = dict(base)
    for cookie in session.cookies.jar:
        domain = cookie.domain or ""
        if domain and "doordash.com" not in domain:
            continue
        merged[cookie.name] = cookie.value
    return merged


def resolve_csrf(cookies: dict[str, str]) -> str:
    existing = (cookies.get("csrf_token") or "").strip()
    if existing:
        return existing
    return generate_csrf_token()


class DoorDashWebSession:
    """One curl_cffi session for warm GET + GraphQL POSTs."""

    def __init__(self, cookies: dict[str, str]) -> None:
        self.session = requests.Session()
        self.cookies = dict(cookies)
        self.csrf = ""

    def warm(self, url: str) -> str:
        response = self.session.get(
            url,
            headers=build_browser_headers(url),
            cookies=self.cookies,
            **request_kwargs(),
        )
        self.cookies = merge_session_cookies(self.cookies, self.session)
        if response.status_code >= 400:
            raise RequestException(
                f"Warm-up GET failed HTTP {response.status_code}: {response.text[:300]}"
            )
        self.csrf = resolve_csrf(self.cookies)
        self.cookies["csrf_token"] = self.csrf
        return response.text

    def graphql(
        self,
        url: str,
        operation_name: str,
        variables: dict[str, Any],
        query: str,
        referer: str,
    ) -> dict[str, Any]:
        if not self.csrf:
            self.csrf = resolve_csrf(self.cookies)
            self.cookies["csrf_token"] = self.csrf

        payload = {
            "operationName": operation_name,
            "variables": variables,
            "query": query,
        }
        response = self.session.post(
            url,
            headers=build_graphql_headers(self.csrf, referer),
            cookies=self.cookies,
            json=payload,
            **request_kwargs(),
        )
        if response.status_code >= 400:
            raise RequestException(
                f"HTTP {response.status_code}: {response.text[:500]}"
            )
        return response.json()


def load_query(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def store_referer(restaurant: dict[str, Any], menu_id: str) -> str:
    slug = restaurant.get("slug") or "store"
    store_id = restaurant.get("id") or ""
    return f"https://www.doordash.com/store/{slug}-{store_id}/{menu_id}/?pickup=false"


def cart_referer(cart_id: str) -> str:
    return f"https://www.doordash.com/cart/{cart_id}/"


def fetch_group_cart(
    cart_id: str,
    cookies: dict[str, str],
    *,
    warm: bool = True,
) -> tuple[dict[str, Any], DoorDashWebSession]:
    client = DoorDashWebSession(cookies)
    if warm:
        client.warm(cart_referer(cart_id))

    data = client.graphql(
        GROUP_CART_URL,
        "groupCart",
        {
            "id": cart_id,
            "shouldApplyAutocheckoutConfig": True,
        },
        load_query(GROUP_CART_QUERY),
        cart_referer(cart_id),
    )
    return data, client


def fetch_detailed_cart(
    cart_id: str,
    cookies: dict[str, str],
    *,
    warm: bool = True,
) -> dict[str, Any]:
    client = DoorDashWebSession(cookies)
    if warm:
        client.warm(cart_referer(cart_id))

    return client.graphql(
        DETAILED_CART_URL,
        "detailedCartItems",
        {"orderCartId": cart_id},
        load_query(DETAILED_CART_QUERY),
        cart_referer(cart_id),
    )


def list_detailed_carts(
    client: DoorDashWebSession,
    *,
    referer: str,
) -> list[dict[str, Any]]:
    data = client.graphql(
        LIST_DETAILED_CARTS_URL,
        "listDetailedCarts",
        {"input": {}},
        load_query(LIST_DETAILED_CARTS_QUERY),
        referer,
    )
    result = (data.get("data") or {}).get("listDetailedCarts") or {}
    return result.get("detailedCarts") or []


def remove_cart_item(
    client: DoorDashWebSession,
    *,
    cart_id: str,
    item_id: str,
    referer: str,
) -> dict[str, Any]:
    return client.graphql(
        REMOVE_CART_ITEM_URL,
        "removeCartItem",
        {
            "cartId": cart_id,
            "itemId": item_id,
            "returnCartFromOrderService": False,
            "monitoringContext": {"isGroup": False},
            "cartContext": {"deleteBundleCarts": False},
            "cartFilter": None,
        },
        load_query(REMOVE_CART_ITEM_QUERY),
        referer,
    )


def add_cart_item(
    client: DoorDashWebSession,
    *,
    add_cart_item_input: dict[str, Any],
    referer: str,
    cart_id: str = "",
) -> dict[str, Any]:
    add_cart_item_input = dict(add_cart_item_input)
    add_cart_item_input["cartId"] = cart_id

    return client.graphql(
        ADD_CART_ITEM_URL,
        "addCartItem",
        {
            "addCartItemInput": add_cart_item_input,
            "fulfillmentContext": {
                "shouldUpdateFulfillment": False,
                "fulfillmentType": None,
            },
            "monitoringContext": {
                "isGroup": False,
                "containerName": "RecommendedItemsForYou",
            },
            "cartContext": {},
            "returnCartFromOrderService": False,
            "shouldKeepOnlyOneActiveCart": False,
        },
        load_query(ADD_CART_ITEM_QUERY),
        referer,
    )
