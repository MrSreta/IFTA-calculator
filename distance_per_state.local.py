"""
Uses:
  - OSRM       (Open Source Routing Machine)  — routing + polyline
  - Shapely    + US States GeoJSON            — instant offline state lookup
  - Nominatim  (OpenStreetMap)                — only for place name → coords

Requirements:
    pip install requests shapely

Accepted coordinate formats:
    Decimal degrees:   35.2271,-80.8431
    DMS with symbols:  37°11'59.9"N 85°56'05.8"W
    DMS with letters:  37d11m59.9sN 85d56m5.8sW
    Place name:        Charlotte, NC
"""

import argparse
import json
import math
import os
import re
import sys
import time
from collections import defaultdict

import requests
from shapely.geometry import Point, shape


OSRM_BASE      = "http://router.project-osrm.org"
NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
USER_AGENT     = "distance-per-state-script/2.0 (personal use)"

GEOJSON_URL    = "https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/data/geojson/us-states.json"
GEOJSON_CACHE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "us_states_cache.geojson")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


def load_state_geometries() -> list[tuple[str, object]]:
    if os.path.exists(GEOJSON_CACHE):
        with open(GEOJSON_CACHE, "r") as f:
            data = json.load(f)
    else:
        print("  Downloading US state boundaries (one-time, ~200 KB) ...")
        resp = SESSION.get(GEOJSON_URL, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        with open(GEOJSON_CACHE, "w") as f:
            json.dump(data, f)
        print(f"  Saved to {GEOJSON_CACHE}")

    states = []
    for feature in data["features"]:
        name = feature["properties"]["name"]
        geom = shape(feature["geometry"])
        states.append((name, geom))
    return states


_STATE_GEOMETRIES: list[tuple[str, object]] | None = None


def get_state_for_point(lat: float, lng: float) -> str:

    global _STATE_GEOMETRIES
    if _STATE_GEOMETRIES is None:
        _STATE_GEOMETRIES = load_state_geometries()

    pt = Point(lng, lat)   
    for name, geom in _STATE_GEOMETRIES:
        if geom.contains(pt):
            return name
        
    for name, geom in _STATE_GEOMETRIES:
        if geom.distance(pt) < 0.05:   
            return name

    return "Unknown / Outside US"

def try_parse_dms(location: str) -> tuple[float, float] | None:

    s = location.strip()
    s = s.replace("°", "d").replace("′", "m").replace("'", "m").replace("\u2019", "m")
    s = s.replace("″", "s").replace('"', "s")

    pat_trail = r"([NSEWnsew])?\s*(\d{1,3})[dD\s]\s*(\d{1,2})[mM\s]\s*([\d.]+)\s*[sS]?\s*([NSEWnsew])?"
    matches = re.findall(pat_trail, s)
    if len(matches) >= 2 and all((m[0] or m[4]) for m in matches):
        parts = []
        for lead, deg, mn, sec, trail in matches:
            direction = (lead or trail).upper()
            val = float(deg) + float(mn) / 60 + float(sec) / 3600
            if direction in ("S", "W"):
                val = -val
            parts.append(val)
        return (parts[0], parts[1])

    pat_lead = r"([NSEWnsew])\s*(\d{1,3})[dD]\s*(\d{1,2})[mM]\s*([\d.]+)\s*[sS]?"
    matches = re.findall(pat_lead, s)
    if len(matches) >= 2:
        parts = []
        for direction, deg, mn, sec in matches:
            val = float(deg) + float(mn) / 60 + float(sec) / 3600
            if direction.upper() in ("S", "W"):
                val = -val
            parts.append(val)
        return (parts[0], parts[1])

    return None


def try_parse_decimal(location: str) -> tuple[float, float] | None:
    s = location.strip().replace(";", ",").replace("  ", " ")
    for sep in (",", " "):
        parts = s.split(sep, 1)
        if len(parts) == 2:
            try:
                return float(parts[0].strip()), float(parts[1].strip())
            except ValueError:
                pass
    return None


def parse_or_geocode(location: str) -> tuple[float, float]:

    result = try_parse_decimal(location)
    if result:
        return result

    result = try_parse_dms(location)
    if result:
        lat, lng = result
        print(f"  Parsed DMS {location!r} → ({lat:.5f}, {lng:.5f})")
        return lat, lng

    print(f"  Geocoding {location!r} via Nominatim ...")
    resp = SESSION.get(
        f"{NOMINATIM_BASE}/search",
        params={"q": location, "format": "json", "limit": 1},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        sys.exit(f"Could not geocode location: {location!r}")
    lat = float(results[0]["lat"])
    lng = float(results[0]["lon"])
    print(f"  → ({lat:.4f}, {lng:.4f})")
    time.sleep(1)   
    return lat, lng


# ---------------------------------------------------------------------------
# Routing — OSRM
# ---------------------------------------------------------------------------

def get_route_osrm(origin: tuple[float, float], destination: tuple[float, float]) -> dict:

    coords = f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
    resp = SESSION.get(
        f"{OSRM_BASE}/route/v1/driving/{coords}",
        params={"overview": "full", "geometries": "polyline", "steps": "false"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok":
        sys.exit(f"OSRM error: {data.get('code')} — {data.get('message', '')}")
    return data["routes"][0]


def decode_polyline(encoded: str) -> list[tuple[float, float]]:
    points = []
    index, lat, lng = 0, 0, 0
    while index < len(encoded):
        for is_lng in (False, True):
            result, shift = 0, 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lng:
                lng += delta
            else:
                lat += delta
        points.append((lat / 1e5, lng / 1e5))
    return points


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def sample_polyline(points: list[tuple[float, float]], every_km: float) -> list[tuple[float, float]]:

    if not points:
        return []
    sampled = [points[0]]
    accumulated = 0.0
    for i in range(1, len(points)):
        accumulated += haversine_km(*points[i - 1], *points[i])
        if accumulated >= every_km:
            sampled.append(points[i])
            accumulated = 0.0
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def calculate_distance_per_state(
    sampled_points: list[tuple[float, float]],
) -> tuple[dict[str, float], list[str]]:

    state_km: dict[str, float] = defaultdict(float)
    state_order: list[str] = []
    seen: set[str] = set()
    total = len(sampled_points) - 1

    for i in range(total):
        a = sampled_points[i]
        b = sampled_points[i + 1]
        seg_km = haversine_km(a[0], a[1], b[0], b[1])
        mid_lat = (a[0] + b[0]) / 2
        mid_lng = (a[1] + b[1]) / 2
        state = get_state_for_point(mid_lat, mid_lng)
        state_km[state] += seg_km
        if state not in seen:
            seen.add(state)
            state_order.append(state)
        print(f"  Segment {i+1:>3}/{total}  →  {state:<35} (+{seg_km:.1f} km)", end="\r", flush=True)

    print()
    return dict(state_km), state_order


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

STATE_ABBREV: dict[str, str] = {
    "Alabama": "US_AL", "Alaska": "US_AK", "Arizona": "US_AZ", "Arkansas": "US_AR",
    "California": "US_CA", "Colorado": "US_CO", "Connecticut": "US_CT", "Delaware": "US_DE",
    "Florida": "US_FL", "Georgia": "US_GA", "Hawaii": "US_HI", "Idaho": "US_ID",
    "Illinois": "US_IL", "Indiana": "US_IN", "Iowa": "US_IA", "Kansas": "US_KS",
    "Kentucky": "US_KY", "Louisiana": "US_LA", "Maine": "US_ME", "Maryland": "US_MD",
    "Massachusetts": "US_MA", "Michigan": "US_MI", "Minnesota": "US_MN", "Mississippi": "US_MS",
    "Missouri": "US_MO", "Montana": "US_MT", "Nebraska": "US_NE", "Nevada": "US_NV",
    "New Hampshire": "US_NH", "New Jersey": "US_NJ", "New Mexico": "US_NM", "New York": "US_NY",
    "North Carolina": "US_NC", "North Dakota": "US_ND", "Ohio": "US_OH", "Oklahoma": "US_OK",
    "Oregon": "US_OR", "Pennsylvania": "US_PA", "Rhode Island": "US_RI", "South Carolina": "US_SC",
    "South Dakota": "US_SD", "Tennessee": "US_TN", "Texas": "US_TX", "Utah": "US_UT",
    "Vermont": "US_VT", "Virginia": "US_VA", "Washington": "US_WA", "West Virginia": "US_WV",
    "Wisconsin": "US_WI", "Wyoming": "US_WY", "District of Columbia": "US_DC",
}


def format_state_label(state: str) -> str:

    abbrev = STATE_ABBREV.get(state)
    snake  = state.upper().replace(" ", "_")
    return f"{abbrev}_{snake}" if abbrev else snake


def print_results(
    origin_raw: str,
    destination_raw: str,
    state_distances_km: dict[str, float],
    state_order: list[str],
    total_route_km: float,
    unit: str,
) -> None:
    factor = 0.621371 if unit == "miles" else 1.0
    label  = "mi" if unit == "miles" else "km"
    estimated_total = round(sum(state_distances_km.values()) * factor)

    rows = [(format_state_label(s), round(state_distances_km[s] * factor)) for s in state_order]
    col_width = max(len(lbl) for lbl, _ in rows) + 2

    divider = "=" * (col_width + 16)
    line    = "-" * (col_width + 16)

    print("\n" + divider)
    print(f"  {origin_raw}  →  {destination_raw}")
    print(f"  Total route distance (OSRM): {round(total_route_km * factor)} {label}")
    print(divider)
    print(f"  {'STATE':<{col_width}} {'DISTANCE':>10}")
    print(line)
    for label_str, dist in rows:
        print(f"  {label_str:<{col_width}} {dist:>8} {label}")
    print(line)
    print(f"  {'ESTIMATED_TOTAL':<{col_width}} {estimated_total:>8} {label}")
    print(divider)
    print()
    print("Note: Per-state values are estimated from polyline sampling.")
    print("Small discrepancies vs. OSRM total are normal (~1–2%).\n")

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Calculate driving distance per US state between two locations.\n"
            "No API key required. State lookup is fully offline via shapely.\n\n"
            "Run with no arguments for interactive prompts.\n\n"
            "Accepted location formats:\n"
            "  Decimal degrees:   35.2271,-80.8431\n"
            "  DMS with symbols:  37°11'59.9\"N 85°56'05.8\"W\n"
            "  DMS with letters:  37d11m59.9sN 85d56m5.8sW\n"
            "  Place name:        Charlotte, NC\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--origin",      default=None, help="Start location (prompted if omitted)")
    parser.add_argument("--destination", default=None, help="End location (prompted if omitted)")
    parser.add_argument(
        "--sample-km", type=float, default=5.0,
        help="Sample a midpoint every N km (default 5). Lower = more accurate. "
             "No speed penalty since lookups are now local."
    )
    parser.add_argument(
        "--unit", choices=["km", "miles"], default="miles",
        help="Output unit (default: miles)"
    )
    return parser.parse_args()


def prompt_location(label: str) -> str:
    print(f"\n  {label}")
    print("  Accepted formats:")
    print("    • Place name:        Charlotte, NC")
    print("    • Decimal degrees:   35.2271,-80.8431")
    print("    • DMS coordinates:   37°11'59.9\"N 85°56'05.8\"W")
    while True:
        value = input("  > ").strip()
        if value:
            return value
        print("  Please enter a location.")


def main() -> None:
    args = parse_args()

    print("\n╔══════════════════════════════════════════╗")
    print("║      Distance Per State Calculator       ║")
    print("╚══════════════════════════════════════════╝")

    origin_raw      = args.origin      or prompt_location("ORIGIN — where are you starting?")
    destination_raw = args.destination or prompt_location("DESTINATION — where are you going?")

    print("\nResolving locations ...")
    origin_coords      = parse_or_geocode(origin_raw)
    destination_coords = parse_or_geocode(destination_raw)

    print("\nLoading US state boundaries ...")
    _ = get_state_for_point(0, 0)   

    print("\nFetching route via OSRM ...")
    route = get_route_osrm(origin_coords, destination_coords)
    total_route_km = route["distance"] / 1000.0
    print(f"Route found: {total_route_km:.1f} km  ({total_route_km * 0.621371:.1f} mi)")

    print(f"\nDecoding polyline and sampling every ~{args.sample_km} km ...")
    all_points     = decode_polyline(route["geometry"])
    sampled_points = sample_polyline(all_points, every_km=args.sample_km)
    print(f"Decoded {len(all_points)} points → {len(sampled_points)} samples → {len(sampled_points)-1} segments")

    print("\nCalculating distance per state (local lookup — no rate limit) ...")
    state_distances, state_order = calculate_distance_per_state(sampled_points)

    print_results(origin_raw, destination_raw, state_distances, state_order, total_route_km, args.unit)


if __name__ == "__main__":
    main()