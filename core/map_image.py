"""Generate Mapbox static map images for order tracking."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

_DIRECTIONS_URL = "https://api.mapbox.com/directions/v5/mapbox/driving"
_GEOCODE_URL = "https://api.mapbox.com/geocoding/v5/mapbox.places"
_STATIC_URL = "https://api.mapbox.com/styles/v1/mapbox/streets-v12/static"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

_geocode_cache: dict[str, tuple[float, float] | None] = {}


def _get_token() -> str:
    tok = os.getenv("MAPBOX_TOKEN", "").strip()
    if not tok:
        raise ValueError("MAPBOX_TOKEN is not set in .env")
    return tok


def geocode(
    query: str,
    *,
    types: str | None = None,
    proximity_lat: float | None = None,
    proximity_lng: float | None = None,
) -> tuple[float, float] | None:
    """Returns (lat, lng) via Mapbox Geocoding. Result is cached per unique args."""
    prox = f"@{proximity_lat:.4f},{proximity_lng:.4f}" if proximity_lat is not None else ""
    cache_key = f"{query}[{types or ''}]{prox}"
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    encoded = urllib.parse.quote(query)
    params = ["limit=1", f"access_token={_get_token()}"]
    if types:
        params.append(f"types={types}")
    if proximity_lat is not None and proximity_lng is not None:
        params.append(f"proximity={proximity_lng},{proximity_lat}")
    url = f"{_GEOCODE_URL}/{encoded}.json?{'&'.join(params)}"

    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        features = data.get("features") or []
        if features:
            lng, lat = features[0]["geometry"]["coordinates"]
            result: tuple[float, float] | None = (float(lat), float(lng))
        else:
            result = None
    except Exception:
        result = None
    _geocode_cache[cache_key] = result
    return result


def _fetch_route(
    origin_lng: float, origin_lat: float,
    dest_lng: float, dest_lat: float,
) -> str:
    url = (
        f"{_DIRECTIONS_URL}/{origin_lng},{origin_lat};{dest_lng},{dest_lat}"
        f"?geometries=polyline&overview=full&access_token={_get_token()}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data["routes"][0]["geometry"]


def generate_tracking_map(
    *,
    store_lat: float,
    store_lng: float,
    delivery_lat: float,
    delivery_lng: float,
    dasher_lat: float | None = None,
    dasher_lng: float | None = None,
    picked_up: bool = False,
    width: int = 500,
    height: int = 280,
) -> bytes:
    """
    Returns PNG bytes of the tracking map.

    Route line only shown when dasher is assigned:
      - Not picked up → dasher to store
      - Picked up     → dasher to delivery address

    Markers:
      - store (black grocery pin)
      - delivery address (black home pin)
      - dasher position (red car pin) — omitted if lat/lng are None
    """
    overlays = []

    if dasher_lat is not None and dasher_lng is not None:
        if not picked_up:
            polyline = _fetch_route(dasher_lng, dasher_lat, store_lng, store_lat)
        else:
            polyline = _fetch_route(dasher_lng, dasher_lat, delivery_lng, delivery_lat)
        encoded = urllib.parse.quote(polyline, safe="")
        overlays.append(f"path-2+1a1a1a-0.9({encoded})")

    overlays += [
        f"pin-l-grocery+1a1a1a({store_lng},{store_lat})",
        f"pin-l-home+1a1a1a({delivery_lng},{delivery_lat})",
    ]
    if dasher_lat is not None and dasher_lng is not None:
        overlays.append(f"pin-l-car+e53935({dasher_lng},{dasher_lat})")

    static_url = (
        f"{_STATIC_URL}/{','.join(overlays)}"
        f"/auto/{width}x{height}"
        f"?padding=60&access_token={_get_token()}"
    )

    req = urllib.request.Request(static_url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


if __name__ == "__main__":
    import os
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    # Test with dasher heading to delivery (picked_up=True)
    png = generate_tracking_map(
        store_lat=41.55351,    store_lng=-73.027885,
        delivery_lat=41.56994, delivery_lng=-73.03026,
        dasher_lat=41.5620,    dasher_lng=-73.0340,
        picked_up=True,
    )
    out = Path(__file__).resolve().parent.parent / "tracking_map_preview.png"
    out.write_bytes(png)
    print(f"Saved {len(png):,} bytes → {out}")
    os.startfile(out)
