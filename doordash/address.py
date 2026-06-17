"""Resolve and set DoorDash delivery address on the active account/cart."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode

from core.logger import vlog
from doordash.web_client import (
    DoorDashWebSession,
    load_query,
    request_kwargs,
)
from pathlib import Path

_validated_addresses: set[str] = set()

ROOT = Path(__file__).resolve().parent.parent
_GQL = Path(__file__).resolve().parent / "graphql"
GET_AVAILABLE_ADDRESSES_URL = (
    "https://www.doordash.com/graphql/getAvailableAddresses?operation=getAvailableAddresses"
)
UPDATE_DEFAULT_ADDRESS_URL = (
    "https://www.doordash.com/graphql/updateConsumerDefaultAddressV2"
    "?operation=updateConsumerDefaultAddressV2"
)
ADD_CONSUMER_ADDRESS_URL = (
    "https://www.doordash.com/graphql/addConsumerAddressV2?operation=addConsumerAddressV2"
)

AUTocomplete_URL = (
    "https://www.doordash.com/unified-gateway/geo-intelligence/v2/address/autocomplete"
)
GET_OR_CREATE_URL = (
    "https://www.doordash.com/unified-gateway/geo-intelligence/v2/address/get-or-create"
)


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_address(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _dd_ids_header(cookies: dict[str, str]) -> str | None:
    device_id = cookies.get("dd_device_id")
    session_id = cookies.get("dd_session_id")
    if not device_id and not session_id:
        return None
    payload: dict[str, str] = {}
    if device_id:
        payload["dd_device_id"] = device_id
    if session_id:
        payload["dd_session_id"] = session_id
    return json.dumps(payload, separators=(",", ":"))


def _gateway_headers(client: DoorDashWebSession, referer: str) -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "accept-language": "en-US",
        "content-type": "application/json",
        "origin": "https://www.doordash.com",
        "referer": referer,
        "user-agent": client.session.headers.get("User-Agent")
        if hasattr(client.session, "headers")
        else None,
        "x-experience-id": "doordash",
        "x-unified-gateway-generated-source": "v1",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    from doordash.web_client import BROWSER_UA

    headers["user-agent"] = BROWSER_UA
    dd_ids = _dd_ids_header(client.cookies)
    if dd_ids:
        headers["dd-ids"] = dd_ids
    return headers


_ZIP_CITY_HINTS: dict[str, str] = {
    "06704": "Waterbury CT",
    "06708": "Waterbury CT",
}

ADDRESS_APT_NOT_FOUND_MESSAGE = (
    "Sorry, we couldn't find that address. Try it without apt/suite/floor numbers."
)

_UNIT_DESIGNATOR_RE = re.compile(
    r"\b(apt|apartment|suite|ste|unit|floor|fl|bldg|building)\b|#",
    re.IGNORECASE,
)


class AddressLookupError(ValueError):
    """Raised when DoorDash cannot resolve a delivery address."""


def autocomplete_queries(input_address: str) -> list[str]:
    """Build autocomplete query variants, newest first."""
    requested = input_address.strip()
    queries = [requested]
    zip_match = re.search(r"\b(\d{5})\b", requested)
    if zip_match:
        zip_code = zip_match.group(1)
        hint = _ZIP_CITY_HINTS.get(zip_code)
        street = re.sub(r"\b\d{5}\b", "", requested).strip(" ,")
        if hint and street and street.lower() not in hint.lower():
            queries.append(f"{street} {hint}")
    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        normalized = query.lower()
        if normalized not in seen:
            seen.add(normalized)
            deduped.append(query)
    return deduped


def _has_unit_designator(text: str) -> bool:
    return bool(_UNIT_DESIGNATOR_RE.search(text))


def autocomplete_address(
    client: DoorDashWebSession,
    input_address: str,
    *,
    referer: str = "https://www.doordash.com/",
) -> list[dict[str, Any]]:
    params = urlencode(
        {
            "input_address": input_address,
            "autocomplete_type": "AUTOCOMPLETE_TYPE_V2_UNSPECIFIED",
        }
    )
    response = client.session.get(
        f"{AUTocomplete_URL}?{params}",
        headers=_gateway_headers(client, referer),
        cookies=client.cookies,
        **request_kwargs(),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"autocomplete HTTP {response.status_code}: {response.text[:400]}")
    body = response.json()
    predictions = body.get("predictions")
    return predictions if isinstance(predictions, list) else []


def get_or_create_address(
    client: DoorDashWebSession,
    source_place_id: str,
    *,
    referer: str = "https://www.doordash.com/",
) -> dict[str, Any] | None:
    response = client.session.post(
        GET_OR_CREATE_URL,
        headers=_gateway_headers(client, referer),
        cookies=client.cookies,
        json={
            "address_identifier": {
                "_type": "source_place_id_request",
                "source_place_id": source_place_id,
            }
        },
        **request_kwargs(),
    )
    if response.status_code >= 400:
        raise RuntimeError(f"get-or-create HTTP {response.status_code}: {response.text[:400]}")
    body = response.json()
    address = body.get("address")
    return address if isinstance(address, dict) else None


def get_available_addresses(client: DoorDashWebSession, *, referer: str) -> list[dict[str, Any]]:
    data = client.graphql(
        GET_AVAILABLE_ADDRESSES_URL,
        "getAvailableAddresses",
        {},
        load_query(_GQL / "get_available_addresses.graphql"),
        referer,
    )
    addresses = (data.get("data") or {}).get("getAvailableAddresses")
    return addresses if isinstance(addresses, list) else []


def _match_saved_address(
    requested: str,
    available: list[dict[str, Any]],
    *,
    prediction: dict[str, Any] | None = None,
    created: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    geo_ids = [
        _first_str(
            (prediction or {}).get("geo_address_id"),
            (created or {}).get("id"),
        )
    ]
    geo_ids = [value for value in geo_ids if value]

    for entry in available:
        default_id = _first_str(entry.get("id"))
        address_id = _first_str(entry.get("addressId"))
        if default_id and address_id and address_id in geo_ids:
            return entry

    normalized_requested = _normalize_address(requested)
    candidates = [_normalize_address(requested)]
    for source in (prediction, created):
        if not source:
            continue
        for key in ("formatted_address", "formatted_address_short"):
            value = _first_str(source.get(key))
            if value:
                candidates.append(_normalize_address(value))

    for entry in available:
        default_id = _first_str(entry.get("id"))
        if not default_id:
            continue
        for key in ("printableAddress", "shortname", "street"):
            value = _first_str(entry.get(key))
            if value and _normalize_address(value) in candidates:
                return entry
        printable = _normalize_address(_first_str(entry.get("printableAddress")) or "")
        if printable and (
            printable in normalized_requested or normalized_requested in printable
        ):
            return entry
    return None


def _combine_postal_code(prediction: dict[str, Any]) -> str | None:
    postal = _first_str(prediction.get("postal_code"))
    if postal:
        return postal
    suffix = _first_str(prediction.get("postal_code_suffix"))
    base = _first_str(prediction.get("postal_code_prefix"))
    if base and suffix:
        return f"{base}-{suffix}"
    return base


def build_add_address_payload(
    requested_address: str,
    prediction: dict[str, Any],
    created: dict[str, Any] | None,
) -> dict[str, Any]:
    lat = created.get("lat") if isinstance(created, dict) else None
    lng = created.get("lng") if isinstance(created, dict) else None
    if not isinstance(lat, (int, float)):
        lat = prediction.get("lat")
    if not isinstance(lng, (int, float)):
        lng = prediction.get("lng")

    city = _first_str(
        (created or {}).get("locality"),
        prediction.get("locality"),
    )
    state = _first_str(
        (created or {}).get("administrative_area_level1"),
        prediction.get("administrative_area_level1"),
    )
    zip_code = _first_str(
        (created or {}).get("postal_code"),
        _combine_postal_code(prediction),
    )
    printable = _first_str(
        (created or {}).get("formatted_address"),
        prediction.get("formatted_address"),
        requested_address,
    )
    shortname = _first_str(
        (created or {}).get("formatted_address_short"),
        prediction.get("formatted_address_short"),
        requested_address,
    )
    google_place_id = _first_str(prediction.get("source_place_id"))
    address_id = _first_str((created or {}).get("id"))

    if not all(
        [
            isinstance(lat, (int, float)),
            isinstance(lng, (int, float)),
            city,
            state,
            zip_code,
            printable,
            shortname,
            google_place_id,
        ]
    ):
        raise ValueError(
            "DoorDash returned incomplete address fields; cannot build addConsumerAddressV2 payload."
        )

    payload: dict[str, Any] = {
        "lat": float(lat),
        "lng": float(lng),
        "city": city,
        "state": state,
        "zipCode": zip_code,
        "printableAddress": printable,
        "shortname": shortname,
        "googlePlaceId": google_place_id,
        "subpremise": None,
        "driverInstructions": None,
        "dropoffOptionId": None,
        "manualLat": None,
        "manualLng": None,
        "addressLinkType": "ADDRESS_LINK_TYPE_UNSPECIFIED",
        "buildingName": None,
        "entryCode": None,
    }
    if address_id:
        payload["addressId"] = address_id
    return payload


def update_default_address(
    client: DoorDashWebSession,
    default_address_id: str,
    *,
    referer: str,
) -> dict[str, Any]:
    data = client.graphql(
        UPDATE_DEFAULT_ADDRESS_URL,
        "updateConsumerDefaultAddressV2",
        {"defaultAddressId": default_address_id},
        load_query(_GQL / "update_consumer_default_address.graphql"),
        referer,
    )
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return (data.get("data") or {}).get("updateConsumerDefaultAddressV2") or {}


def add_consumer_address(
    client: DoorDashWebSession,
    payload: dict[str, Any],
    *,
    referer: str,
) -> dict[str, Any]:
    data = client.graphql(
        ADD_CONSUMER_ADDRESS_URL,
        "addConsumerAddressV2",
        payload,
        load_query(_GQL / "add_consumer_address.graphql"),
        referer,
    )
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"], indent=2))
    return (data.get("data") or {}).get("addConsumerAddressV2") or {}


def validate_address(
    client: DoorDashWebSession,
    address_text: str,
    *,
    referer: str = "https://www.doordash.com/",
) -> None:
    """
    Confirm the address resolves via DoorDash autocomplete.
    Raises AddressLookupError early so the caller can fail before doing heavy work.
    """
    requested = address_text.strip()
    if not requested:
        raise ValueError("Address cannot be empty.")

    if requested.lower() in _validated_addresses:
        vlog("address", "cached — skipping autocomplete lookup")
        return

    for query in autocomplete_queries(requested):
        predictions = autocomplete_address(client, query, referer=referer)
        if predictions:
            _validated_addresses.add(requested.lower())
            return

    if _has_unit_designator(requested):
        raise AddressLookupError(ADDRESS_APT_NOT_FOUND_MESSAGE)
    raise AddressLookupError(
        f'Could not find address "{requested}". Please double-check and try again.'
    )


def set_delivery_address(
    client: DoorDashWebSession,
    address_text: str,
    *,
    referer: str = "https://www.doordash.com/",
) -> dict[str, Any]:
    """Resolve address text, set as default delivery address, return result metadata."""
    requested = address_text.strip()
    if not requested:
        raise ValueError("address_text is required")

    available = get_available_addresses(client, referer=referer)
    direct_match = _match_saved_address(requested, available)
    if direct_match:
        result = update_default_address(client, direct_match["id"], referer=referer)
        default_address = result.get("defaultAddress") or direct_match
        return {
            "mode": "saved-address",
            "requested": requested,
            "default_address": default_address,
            "order_cart_id": (result.get("orderCart") or {}).get("id"),
        }

    prediction: dict[str, Any] | None = None
    for query in autocomplete_queries(requested):
        predictions = autocomplete_address(client, query, referer=referer)
        if predictions:
            prediction = predictions[0]
            break
    if not prediction:
        if _has_unit_designator(requested):
            raise AddressLookupError(ADDRESS_APT_NOT_FOUND_MESSAGE)
        raise AddressLookupError(
            f'DoorDash returned no autocomplete results for "{requested}".'
        )

    source_place_id = _first_str(prediction.get("source_place_id"))
    if not source_place_id:
        raise RuntimeError("Autocomplete result missing source_place_id.")

    created = get_or_create_address(client, source_place_id, referer=referer)
    matched = _match_saved_address(requested, available, prediction=prediction, created=created)
    if matched:
        result = update_default_address(client, matched["id"], referer=referer)
        default_address = result.get("defaultAddress") or matched
        return {
            "mode": "matched-after-get-or-create",
            "requested": requested,
            "prediction": prediction,
            "created": created,
            "default_address": default_address,
            "order_cart_id": (result.get("orderCart") or {}).get("id"),
        }

    payload = build_add_address_payload(requested, prediction, created)
    add_result = add_consumer_address(client, payload, referer=referer)
    default_address = add_result.get("defaultAddress")
    if not default_address:
        raise RuntimeError("addConsumerAddressV2 did not return defaultAddress.")

    # Re-run default-address update so the active cart picks up the new location.
    default_id = _first_str(default_address.get("id"))
    order_cart_id = None
    if default_id:
        update_result = update_default_address(client, default_id, referer=referer)
        default_address = update_result.get("defaultAddress") or default_address
        order_cart_id = (update_result.get("orderCart") or {}).get("id")

    return {
        "mode": "added-address",
        "requested": requested,
        "prediction": prediction,
        "created": created,
        "default_address": default_address,
        "order_cart_id": order_cart_id,
    }
