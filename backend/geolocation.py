"""
Genome Geolocation

Derives a geohash from the device's public IP address so that
public_local networks are automatically scoped to a ~50 km radius
without the user having to enter any location information.

Public surface:
    geohash = await get_local_geohash()   # e.g. "9q8y" (4-char ≈ 50 km)
    info    = await get_location_info()   # full dict with city, country, lat/lon, geohash

Geohash precision:
    4 chars ≈ ±20 km  (good for "local area" discovery)
    5 chars ≈ ±2.4 km (neighbourhood-level — too tight for public_local)

The 4-char geohash becomes the networkId for public_local:
    topic = "genome-local-<geohash>"
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from functools import lru_cache
from typing import TypedDict

_log = logging.getLogger(__name__)

# Free, no-key IP geolocation service (returns JSON with lat/lon/city/country)
_GEO_URL = "http://ip-api.com/json/?fields=status,lat,lon,city,regionName,country,countryCode"
_TIMEOUT_S = 5.0

# ── Geohash encoder ────────────────────────────────────────────────────────────
# Pure-Python geohash — no external dependency.

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def encode_geohash(lat: float, lon: float, precision: int = 4) -> str:
    """Encode (lat, lon) to a geohash string of given precision."""
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    bits = [16, 8, 4, 2, 1]
    bit_idx = 0
    char_idx = 0
    is_lon = True
    chars: list[str] = []

    while len(chars) < precision:
        if is_lon:
            mid = (lon_range[0] + lon_range[1]) / 2
            if lon >= mid:
                char_idx |= bits[bit_idx]
                lon_range[0] = mid
            else:
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2
            if lat >= mid:
                char_idx |= bits[bit_idx]
                lat_range[0] = mid
            else:
                lat_range[1] = mid

        is_lon = not is_lon
        bit_idx += 1
        if bit_idx == 5:
            chars.append(_BASE32[char_idx])
            bit_idx = 0
            char_idx = 0

    return "".join(chars)


# ── Location info ──────────────────────────────────────────────────────────────


class LocationInfo(TypedDict):
    lat: float
    lon: float
    city: str
    region: str
    country: str
    country_code: str
    geohash: str


def _fetch_location_sync() -> LocationInfo | None:
    """Blocking HTTP call — run in a thread via asyncio.to_thread."""
    try:
        with urllib.request.urlopen(_GEO_URL, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status") != "success":
            _log.warning("IP geolocation returned status=%s", data.get("status"))
            return None
        lat = float(data["lat"])
        lon = float(data["lon"])
        return LocationInfo(
            lat=lat,
            lon=lon,
            city=str(data.get("city") or ""),
            region=str(data.get("regionName") or ""),
            country=str(data.get("country") or ""),
            country_code=str(data.get("countryCode") or ""),
            geohash=encode_geohash(lat, lon, precision=4),
        )
    except Exception as exc:
        _log.warning("IP geolocation failed: %s", exc)
        return None


# Module-level cache — only fetch once per process lifetime
_cached_location: LocationInfo | None = None
_location_lock = asyncio.Lock()
_location_fetched = False


async def get_location_info() -> LocationInfo | None:
    """
    Return the device's location info derived from public IP.
    Result is cached for the lifetime of the process.
    Returns None if the lookup fails (e.g. offline or VPN).
    """
    global _cached_location, _location_fetched
    async with _location_lock:
        if not _location_fetched:
            _cached_location = await asyncio.to_thread(_fetch_location_sync)
            _location_fetched = True
            if _cached_location:
                _log.info(
                    "Local geohash: %s (%s, %s)",
                    _cached_location["geohash"],
                    _cached_location["city"],
                    _cached_location["country"],
                )
        return _cached_location


async def get_local_geohash() -> str | None:
    """
    Return the 4-character geohash for the device's public IP location.
    This becomes the networkId for the public_local gossipsub topic.
    Returns None if geolocation is unavailable.
    """
    info = await get_location_info()
    return info["geohash"] if info else None
