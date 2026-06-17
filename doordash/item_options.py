"""Fetch item option hierarchy and map flat selections to correct nesting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from doordash.web_client import DoorDashWebSession, load_query

ITEM_PAGE_QUERY = Path(__file__).resolve().parent / "graphql" / "item_page.graphql"
ITEM_PAGE_URL = "https://www.doordash.com/graphql/itemPage?operation=itemPage"


def fetch_item_page(
    client: DoorDashWebSession,
    store_id: str,
    item_id: str,
    referer: str,
) -> dict[str, Any] | None:
    try:
        data = client.graphql(
            ITEM_PAGE_URL,
            "itemPage",
            {
                "storeId": store_id,
                "itemId": item_id,
                "isNested": True,
                "isMerchantPreview": False,
            },
            load_query(ITEM_PAGE_QUERY),
            referer,
        )
    except Exception as exc:
        print(f"  [item_page] fetch failed: {exc}")
        return None
    if data.get("errors"):
        print(f"  [item_page] errors: {data['errors']}")
        return None
    return (data.get("data") or {}).get("itemPage")


def build_nested_from_item_page(
    flat_nested_json: str,
    item_page: dict[str, Any],
) -> str | None:
    """
    Re-nest flat selected option IDs using the item page option hierarchy.

    detailedCartItems stores nestedOptions flattened (all IDs at depth 0).
    addCartItem requires them nested according to the menu's optionList tree.
    This function maps each selected ID to the correct depth.
    """
    try:
        flat = json.loads(flat_nested_json) if flat_nested_json else []
    except (json.JSONDecodeError, ValueError):
        return None

    # Extract selected {id: quantity} from the flat list
    selected: dict[str, int] = {}
    for entry in flat:
        if not isinstance(entry, dict):
            continue
        opt_id = str(entry.get("id") or entry.get("id") or "")
        qty = int(entry.get("quantity") or 1)
        if opt_id:
            selected[opt_id] = qty

    if not selected:
        return None

    result: list[dict[str, Any]] = []
    placed: set[str] = set()

    for opt_list in item_page.get("optionLists") or []:
        for option in opt_list.get("options") or []:
            opt_id = str(option.get("id") or "")
            if opt_id not in selected or opt_id in placed:
                continue
            placed.add(opt_id)
            entry: dict[str, Any] = {
                "id": opt_id,
                "quantity": selected[opt_id],
            }

            # Gather nested sub-selections for this option
            sub: list[dict[str, Any]] = []
            for nested_extra in option.get("nestedExtrasList") or []:
                for nested_opt in nested_extra.get("options") or []:
                    nested_id = str(nested_opt.get("id") or "")
                    if nested_id in selected and nested_id not in placed:
                        placed.add(nested_id)
                        sub.append({
                            "id": nested_id,
                            "quantity": selected[nested_id],
                        })
            if sub:
                entry["options"] = sub
            result.append(entry)

    if not result:
        return None

    print(f"  [item_page] rebuilt nested: placed {len(placed)}/{len(selected)} option(s)")
    return json.dumps(result, separators=(",", ":"))
