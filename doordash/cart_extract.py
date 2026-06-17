"""Extract DoorDash cart items for rebuild via addCartItem."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class CartItemSpec:
    store_id: str
    menu_id: str
    item_id: str
    item_name: str
    item_description: str
    currency: str
    quantity: int
    nested_options: str
    special_instructions: str | None
    substitution_preference: str
    unit_price: int
    is_bundle: bool = False
    bundle_type: str = "BUNDLE_TYPE_UNSPECIFIED"
    fallback_nested_options: str | None = None

    def to_add_cart_input(self) -> dict[str, Any]:
        instructions = self.special_instructions
        if instructions is not None and not str(instructions).strip():
            instructions = None
        return {
            "storeId": self.store_id,
            "menuId": self.menu_id,
            "itemId": self.item_id,
            "itemName": self.item_name,
            "itemDescription": self.item_description or self.item_name,
            "currency": self.currency,
            "quantity": self.quantity,
            "nestedOptions": self.nested_options,
            "specialInstructions": instructions,
            "substitutionPreference": self.substitution_preference,
            "isBundle": self.is_bundle,
            "bundleType": self.bundle_type,
            "unitPrice": self.unit_price,
        }


def _proto_value(field: Any) -> Any:
    if isinstance(field, dict) and "value" in field:
        return field["value"]
    return field


def _opt_id(entry: dict[str, Any]) -> str | None:
    """Extract option ID from either stored format (itemExtraOptionId) or input format (id)."""
    item_extra = entry.get("itemExtraOption") or {}
    val = (
        _proto_value(item_extra.get("id"))
        or _proto_value(entry.get("id"))
        or entry.get("itemExtraOptionId")
    )
    return str(val) if val is not None else None


def _proto_options_to_add(raw_list: list[Any] | None) -> list[dict[str, Any]]:
    if not raw_list:
        return []
    result: list[dict[str, Any]] = []
    for entry in raw_list:
        opt_id = _opt_id(entry)
        if opt_id is None:
            continue
        sub_raw = entry.get("optionsList") or entry.get("options") or []
        converted = _proto_options_to_add(sub_raw)
        item: dict[str, Any] = {
            "id": opt_id,
            "quantity": int(_proto_value(entry.get("quantity")) or 1),
            "options": converted,  # always include, even empty
        }
        # Preserve itemExtraOption from proto data — required by addCartItem
        raw_extra = entry.get("itemExtraOption")
        if raw_extra and isinstance(raw_extra, dict):
            extra_id = _proto_value(raw_extra.get("id"))
            name_val = str(_proto_value(raw_extra.get("name")) or "")
            desc_val = str(_proto_value(raw_extra.get("description")) or "") or name_val
            price_val = _proto_value(raw_extra.get("price"))
            charge_val = _proto_value(raw_extra.get("chargeAbove"))
            dq_val = _proto_value(raw_extra.get("defaultQuantity"))
            rebuilt: dict[str, Any] = {
                "id": str(extra_id) if extra_id is not None else opt_id,
            }
            if name_val:
                rebuilt["name"] = name_val
            if desc_val:
                rebuilt["description"] = desc_val
            rebuilt["price"] = int(price_val) if price_val is not None else 0
            rebuilt["chargeAbove"] = int(charge_val) if charge_val is not None else 0
            if dq_val is not None:
                rebuilt["defaultQuantity"] = int(dq_val)
            item["itemExtraOption"] = rebuilt
        result.append(item)
    return result


def _rekey_nested(opts: list[Any]) -> list[dict[str, Any]]:
    """Convert stored itemExtraOptionId keys → id keys and preserve itemExtraOption metadata."""
    result: list[dict[str, Any]] = []
    for entry in opts:
        if not isinstance(entry, dict):
            continue
        opt_id = _opt_id(entry)
        if not opt_id:
            continue
        # Sub-options may be an array under "options" or a JSON string under "nestedOptions"
        sub_raw = entry.get("options") or entry.get("nestedOptions")
        if isinstance(sub_raw, str):
            try:
                sub_raw = json.loads(sub_raw)
            except (json.JSONDecodeError, ValueError):
                sub_raw = []
        sub = _rekey_nested(sub_raw if isinstance(sub_raw, list) else [])
        item: dict[str, Any] = {
            "id": opt_id,
            "quantity": int(entry.get("quantity") or 1),
            "options": sub,  # always include, even empty — DoorDash requires it
        }
        # Preserve itemExtraOption — DoorDash addCartItem requires it on every node
        raw_extra = entry.get("itemExtraOption")
        if raw_extra and isinstance(raw_extra, dict):
            extra_id = _proto_value(raw_extra.get("id"))
            rebuilt: dict[str, Any] = {"id": str(extra_id) if extra_id is not None else opt_id}
            for key in ("name", "description", "price", "chargeAbove", "defaultQuantity"):
                val = raw_extra.get(key)
                if val is not None:
                    rebuilt[key] = val
            item["itemExtraOption"] = rebuilt
        result.append(item)
    return result


def rekey_nested_options(nested_json: str) -> str:
    """Convert a stored nestedOptions JSON string to the addCartItem input format."""
    try:
        parsed = json.loads(nested_json)
        return json.dumps(_rekey_nested(parsed if isinstance(parsed, list) else []), separators=(",", ":"))
    except (json.JSONDecodeError, ValueError):
        return nested_json


def build_top_level_nested_options(order_item: dict[str, Any]) -> str:
    """Fallback: slot selections only (default sub-customizations)."""
    slots = [
        {"id": str(opt["id"]), "quantity": int(opt.get("quantity") or 1)}
        for opt in order_item.get("options") or []
    ]
    return json.dumps(slots, separators=(",", ":"))


def build_nested_options_from_order_item(order_item: dict[str, Any]) -> str:
    """Build addCartItem nestedOptions from detailedCartItems orderItem.options."""
    slots: list[dict[str, Any]] = []
    for opt in order_item.get("options") or []:
        opt_id = str(opt["id"])
        raw = opt.get("nestedOptions") or "[]"
        inner = json.loads(raw) if isinstance(raw, str) and raw.strip() not in ("", "[]") else []
        opt_name = opt.get("name") or ""
        entry: dict[str, Any] = {
            "id": opt_id,
            "quantity": int(opt.get("quantity") or 1),
            "options": _proto_options_to_add(inner),  # always include, even []
            "itemExtraOption": {
                "id": opt_id,
                "name": opt_name,
                "description": opt_name,
                "price": 0,
                "chargeAbove": 0,
                "defaultQuantity": 0,
            },
        }
        slots.append(entry)
    return json.dumps(slots, separators=(",", ":"))


def _options_to_nested(options: list[dict[str, Any]] | str | None) -> list[dict[str, Any]]:
    if not options:
        return []
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(options, list):
        return []
    result: list[dict[str, Any]] = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        entry: dict[str, Any] = {
            "id": str(opt["id"]),
            "quantity": opt.get("quantity") or 1,
            "options": _options_to_nested(opt.get("nestedOptions") or []),
        }
        result.append(entry)
    return result


def nested_options_string(options: list[dict[str, Any]] | None) -> str:
    return json.dumps(_options_to_nested(options), separators=(",", ":"))


def extract_cart_items(cart: dict[str, Any]) -> list[CartItemSpec]:
    """Pull all orderItems from a groupCart orderCart response."""
    restaurant = cart.get("restaurant") or {}
    store_id = str(restaurant.get("id") or "")
    menu = cart.get("menu") or {}
    menu_id = str(menu.get("id") or "")
    currency = cart.get("currencyCode") or "USD"

    if not store_id or not menu_id:
        raise ValueError("Cart missing restaurant.id or menu.id — cannot rebuild items")

    specs: list[CartItemSpec] = []
    for order in cart.get("orders") or []:
        for order_item in order.get("orderItems") or []:
            item = order_item.get("item") or {}
            item_id = str(item.get("id") or "")
            if not item_id:
                continue

            options = order_item.get("options") or []
            raw_nested = order_item.get("nestedOptions")
            fallback = None
            if isinstance(raw_nested, str) and raw_nested.strip() not in ("", "[]"):
                # Convert stored itemExtraOptionId format → id format for addCartItem
                nested = rekey_nested_options(raw_nested)
                fallback = build_top_level_nested_options(order_item) or None
            elif any((opt.get("nestedOptions") or "[]") not in ("", "[]") for opt in options):
                nested = build_nested_options_from_order_item(order_item)
                fallback = build_top_level_nested_options(order_item)
            else:
                nested = nested_options_string(options)

            unit_price = order_item.get("singlePrice")
            if unit_price is None:
                unit_price = item.get("price") or 0

            specs.append(
                CartItemSpec(
                    store_id=str(item.get("storeId") or store_id),
                    menu_id=menu_id,
                    item_id=item_id,
                    item_name=item.get("name") or "Item",
                    item_description=item.get("description") or "",
                    currency=currency,
                    quantity=int(order_item.get("quantity") or 1),
                    nested_options=nested,
                    special_instructions=order_item.get("specialInstructions"),
                    substitution_preference=order_item.get("substitutionPreference") or "substitute",
                    unit_price=int(unit_price),
                    is_bundle=bool(cart.get("isBundle")) or bool(order_item.get("isBundle")),
                    bundle_type=cart.get("bundleType") or "BUNDLE_TYPE_UNSPECIFIED",
                    fallback_nested_options=fallback,
                )
            )
    return specs


def summarize_cart(cart: dict[str, Any]) -> dict[str, Any]:
    restaurant = cart.get("restaurant") or {}
    items = extract_cart_items(cart)
    return {
        "cart_id": cart.get("id"),
        "restaurant": restaurant.get("name"),
        "store_id": restaurant.get("id"),
        "menu_id": (cart.get("menu") or {}).get("id"),
        "subtotal": cart.get("subtotal"),
        "total": cart.get("total"),
        "item_count": len(items),
        "items": [
            {
                "name": s.item_name,
                "quantity": s.quantity,
                "unit_price": s.unit_price,
                "options": s.nested_options,
            }
            for s in items
        ],
    }
