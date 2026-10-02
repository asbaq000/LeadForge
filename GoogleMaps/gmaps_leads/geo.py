"""Turn a city name into a grid of search points.

A single Google Maps search stops feeding results at roughly 120 listings, no
matter how many businesses actually exist. To reach 1,000 leads we geocode the
city, carve its bounding box into tiles, and run the same search centred on
each tile. Overlapping results get deduped later by place id.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim rejects requests without a descriptive User-Agent.
_UA = "gmaps-leads/1.0 (local lead-research tool)"

_KM_PER_DEG_LAT = 110.574


@dataclass
class CityBox:
    name: str
    display_name: str
    lat: float
    lng: float
    south: float
    north: float
    west: float
    east: float

    @property
    def width_km(self) -> float:
        return (self.east - self.west) * _km_per_deg_lng(self.lat)

    @property
    def height_km(self) -> float:
        return (self.north - self.south) * _KM_PER_DEG_LAT


def _km_per_deg_lng(lat: float) -> float:
    return 111.320 * math.cos(math.radians(lat))


class GeocodeError(RuntimeError):
    pass


def geocode_city(city: str, timeout: int = 20) -> CityBox:
    """Look up a city centre and bounding box via OpenStreetMap (free, no key)."""
    params = {"q": city, "format": "json", "limit": 1, "addressdetails": 0}
    try:
        resp = requests.get(
            NOMINATIM_URL, params=params, headers={"User-Agent": _UA}, timeout=timeout
        )
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as exc:
        raise GeocodeError(f"Could not reach the geocoder: {exc}") from exc
    except ValueError as exc:
        raise GeocodeError("Geocoder returned a response we could not read.") from exc

    if not results:
        raise GeocodeError(
            f"No place matched {city!r}. Try adding the country, "
            "for example: Karachi, Pakistan"
        )

    hit = results[0]
    south, north, west, east = (float(v) for v in hit["boundingbox"])
    # Be polite to the free Nominatim endpoint.
    time.sleep(1.0)
    return CityBox(
        name=city,
        display_name=hit.get("display_name", city),
        lat=float(hit["lat"]),
        lng=float(hit["lon"]),
        south=south,
        north=north,
        west=west,
        east=east,
    )


def build_grid(
    box: CityBox, tile_km: float = 3.0, max_tiles: int = 400
) -> list[tuple[float, float]]:
    """Lay a grid of search points over the city, ordered centre-outward.

    Points are sorted by distance from the city centre so the densest,
    highest-yield areas get scraped first. If you stop the run early you still
    come away with the leads that matter.
    """
    tile_km = max(0.5, tile_km)
    d_lat = tile_km / _KM_PER_DEG_LAT
    d_lng = tile_km / max(_km_per_deg_lng(box.lat), 1e-6)

    rows = max(1, math.ceil((box.north - box.south) / d_lat))
    cols = max(1, math.ceil((box.east - box.west) / d_lng))

    points: list[tuple[float, float]] = []
    for r in range(rows):
        lat = box.south + (r + 0.5) * d_lat
        for c in range(cols):
            lng = box.west + (c + 0.5) * d_lng
            points.append((round(lat, 6), round(lng, 6)))

    points.sort(key=lambda p: (p[0] - box.lat) ** 2 + (p[1] - box.lng) ** 2)

    if len(points) > max_tiles:
        points = points[:max_tiles]
    return points


def zoom_for_tile(tile_km: float) -> int:
    """Pick a map zoom that roughly frames one tile, so results stay local."""
    if tile_km <= 1.5:
        return 16
    if tile_km <= 3.0:
        return 15
    if tile_km <= 6.0:
        return 14
    if tile_km <= 12.0:
        return 13
    return 12
