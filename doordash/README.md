# doordash/ — DoorDash API Client

Everything in this folder talks to DoorDash. The rest of the bot (core/, views/) calls into here.

---

## How the /price flow works (big picture)

1. User pastes a group order link + delivery address
2. **Source account** joins the group order and reads the cart (what items are in it)
3. **Build account** recreates that exact cart under its own session
4. Build account sets the delivery address and fetches checkout pricing
5. Bot replies with the full price breakdown

Two separate DoorDash accounts are needed because you can only get checkout pricing
(delivery fee, taxes, etc.) on a cart that belongs to your own session.

- Source account cookies → `config/doordash_cookies.txt`
- Build account cookies → `config/doordash_build_cookies.txt`

---

## Files

### `constants.py`
Static values that don't change.
- `IMPERSONATE = "chrome120"` — tells curl_cffi to mimic Chrome's TLS fingerprint so Cloudflare doesn't block requests
- `DOORDASH_AUTHORIZATION` — Android OAuth token (only needed if you ever hit the mobile API; unused in the current flow)

### `web_client.py`
The HTTP layer. Every other file goes through this.
- `DoorDashWebSession` — one curl_cffi session per operation
  - `.warm(url)` — do a browser GET first to get cookies/CSRF set before any API call
  - `.graphql(url, operation, variables, query, referer)` — send a GraphQL POST
- `load_cookies(path)` — reads a cookie .txt file into a dict
- Helper functions at the bottom: `fetch_group_cart`, `fetch_detailed_cart`, `add_cart_item`, `remove_cart_item`, `list_detailed_carts`

> Requests go through a residential proxy (DOORDASH_PROXY in .env) to avoid IP blocks.

### `order_link.py`
Parses any DoorDash share link into a usable cart ID.
- Handles: full cart URLs, `drd.sh/cart/...` short links, raw UUIDs
- `parse_cart_reference(link)` — returns either a UUID or a short URL code
- `is_url_code(ref)` — True if it's a short code, False if it's a UUID
- `group_cart_referer(ref)` — builds the `doordash.com/cart/<ref>/` URL (used as Referer header)

### `group_order.py`
Joins a group order so the source account can read the cart.
- `join_group_order(cookies, order_link)` — navigates the invite URL(s), lets the session
  pick up the right cookies, then calls `detailedCartItems` GraphQL
- Returns `(cart_uuid, client, source_cart_dict)`

### `cart_extract.py`
Parses the raw cart response into structured specs ready for rebuild.
- `CartItemSpec` — dataclass: store ID, item ID, name, quantity, modifiers (nested options), price, etc.
- `extract_cart_items(cart)` — turns a raw `orderCart` dict into a list of `CartItemSpec`
- The tricky part: DoorDash stores modifier selections in a different format than what
  `addCartItem` expects. Several functions handle the translation:
  - `build_nested_options_from_order_item()` — full modifiers with sub-options
  - `rekey_nested_options()` — converts stored `itemExtraOptionId` keys → `id` keys
  - `build_top_level_nested_options()` — fallback, top-level slots only (drops sub-customizations)

### `rebuild.py`
Recreates the source cart on the build account item by item.
- `_clear_existing_carts()` — removes leftover items from a previous build for this store
- `rebuild_cart(specs, restaurant, menu_id, build_cookies)` — main function:
  - Loops through each `CartItemSpec` and calls `addCartItem`
  - Each item gets up to 3 attempts with different modifier formats:
    1. Full nested options (exact customizations)
    2. Top-level options only (fallback)
    3. Empty `[]` (bare item, no modifiers)
  - On a "wrong level" GraphQL error → fetches the item page to re-nest correctly (see `item_options.py`)
  - Returns `(rebuilt_cart, failures_list, client, cart_id)`

### `item_options.py`
Fixes modifier nesting when DoorDash rejects an `addCartItem` call with "wrong level" error.
- `fetch_item_page(client, store_id, item_id, referer)` — fetches the menu's canonical option tree for one item
- `build_nested_from_item_page(flat_json, item_page)` — re-nests selected option IDs according to
  the menu hierarchy and injects this corrected attempt into the rebuild loop

### `address.py`
Resolves a plain-text address and sets it on the build account.
- `validate_address()` — quick pre-check (just autocomplete, no mutations). Called before rebuild so
  you fail fast on a bad address instead of wasting time rebuilding the cart.
- `set_delivery_address()` — full flow:
  1. Check if address is already saved on account → set as default if so
  2. Otherwise: autocomplete → get-or-create → `addConsumerAddressV2` → set as default
- Uses two DoorDash endpoints:
  - `unified-gateway/geo-intelligence/v2/address/autocomplete` (REST, not GraphQL)
  - `unified-gateway/geo-intelligence/v2/address/get-or-create` (REST)
  - `getAvailableAddresses`, `updateConsumerDefaultAddressV2`, `addConsumerAddressV2` (GraphQL)

### `checkout.py`
Gets the final pricing for the rebuilt cart.
- `fetch_checkout(client, cart_id, lat, lng)` — calls the `checkout` GraphQL query
- `PriceBreakdown.from_cart(cart)` — parses subtotal, fees+tax, delivery fee, discounts, total
- `summarize_order_items(cart)` — pulls item names + quantities from the checkout response

---

## graphql/
All `.graphql` query files. Each one matches a GraphQL operation name used in the code.

| File | Operation |
|---|---|
| `group_cart.graphql` | `groupCart` |
| `detailed_cart_items.graphql` | `detailedCartItems` |
| `add_cart_item.graphql` | `addCartItem` |
| `remove_cart_item.graphql` | `removeCartItem` |
| `list_detailed_carts.graphql` | `listDetailedCarts` |
| `checkout.graphql` | `checkout` |
| `item_page.graphql` | `itemPage` |
| `get_available_addresses.graphql` | `getAvailableAddresses` |
| `add_consumer_address.graphql` | `addConsumerAddressV2` |
| `update_consumer_default_address.graphql` | `updateConsumerDefaultAddressV2` |
| `storepage_feed.graphql` | `storepageFeed` (unused, kept for reference) |

---

## Common gotchas

**Cookies expire.** If you get auth errors or empty cart responses, the cookie files need to be refreshed. Log in to each account in a browser, export cookies, paste into the txt files.

**Nested options are the hardest part.** DoorDash uses a multi-level modifier system (e.g. "Sauce" → "Ranch" → "Extra"). The format stored in `detailedCartItems` is not the same format `addCartItem` accepts. `cart_extract.py` and `item_options.py` handle the translation with fallbacks.

**Always warm before GraphQL.** Never call `.graphql()` without calling `.warm()` first on the same session. The warm GET establishes the session cookies and CSRF token that DoorDash requires on every subsequent request.

**Two accounts, two sessions.** Never mix cookies. Source cookies read the cart, build cookies do everything else (rebuild, set address, checkout). Mixing them will break things silently.
