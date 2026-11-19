#!/usr/bin/env python3
"""Build the traffic map's nightly OpenStreetMap data from a Geofabrik extract.

Run by .github/workflows/map-data.yml:
    python scripts/build_map_data.py florida-latest.osm.pbf out

Instead of asking the busy public Overpass servers hundreds of questions, this cuts each city out of the
Florida file with osmium-tool, keeps only the tags the map uses, and writes the same files the map expects
(identical in shape to Overpass "out body geom" / "out center" results):

  <out>/[orlando/]tiles/r{row}c{col}.json  everything for one 0.1-degree tile
  <out>/[orlando/]highways.json            interstates, U.S. and main state routes (zoomed-out views)
  <out>/[orlando/]points.json              place names, museums, colleges, venues, malls (zoomed-out views)
  <out>/[orlando/]airports.json            public airports
  <out>/[orlando/]counties.json            county boundaries
  <out>/[orlando/]meta.json                build time, tile grid, tiles with no data

Requires: osmium-tool (CLI) and pyosmium (pip install osmium). Grids must match TILE in traffic.html.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import osmium

CITIES = [
    {"name": "orlando", "dir": "orlando", "grid": {"lat0": 28.2, "lon0": -81.75, "step": 0.1, "rows": 7, "cols": 8},
     "region": (28.25, -81.70, 28.85, -81.00)},
    {"name": "tampa", "dir": "", "grid": {"lat0": 26.8, "lon0": -83.0, "step": 0.1, "rows": 15, "cols": 11},
     "region": (26.85, -82.95, 28.25, -81.95)},
]

# osmium tags-filter expressions: everything any map layer can use.
FILTERS = [
    "w/highway", "n/highway=stop,motorway_junction",
    "nwr/amenity=school,parking,cafe,bar,pub,nightclub,university,college,gym",
    "nwr/tourism=museum", "nwr/leisure=stadium,sports_centre,fitness_centre", "nwr/building=stadium",
    "nwr/shop=mall", "nwr/landuse=retail,residential", "n/place=city,town,suburb,village,hamlet,neighbourhood,quarter",
    "n/barrier=gate,lift_gate,swing_gate", "nwr/gated=yes", "nwr/aeroway=aerodrome", "r/boundary=administrative",
]
DROP_PREFIXES = ("tiger:", "source", "gnis:", "NHD:", "note", "fixme", "FIXME", "created_by", "check_date", "survey", "attribution",
                 "import", "wikimedia", "name:", "old_name", "alt_name:", "is_in", "ref:", "nhd", "building:levels", "roof:",
                 "addr:country", "addr:state", "addr:postcode")
ROAD_CLASSES = re.compile(r"^(motorway|trunk|primary|secondary|tertiary|motorway_link|trunk_link)$")
REF_ROUTE = re.compile(r"^(I|US|SR|FL|CR) ?[0-9]")
NAMED_RETAIL = re.compile(r"mall|town cent|outlets|shoppes|galleria|marketplace|square", re.I)
ARENA = re.compile(r"Arena|Stadium|Ballpark|Speedway", re.I)


def tags_of(o):
    return {t.k: t.v for t in o.tags if not t.k.startswith(DROP_PREFIXES)}


def r5(x):
    return round(x, 5)


def is_poi(t, kind):
    """The tile's 'out center' set (mirrors the old Overpass query)."""
    a, le = t.get("amenity", ""), t.get("leisure", "")
    if kind == "n" and t.get("highway") in ("stop", "motorway_junction"):
        return True
    if a in ("school", "parking", "cafe", "bar", "pub", "nightclub", "university", "college", "gym"):
        return True
    if t.get("tourism") == "museum" or le in ("stadium", "sports_centre", "fitness_centre") or t.get("building") == "stadium" or t.get("shop") == "mall":
        return True
    if kind == "w" and t.get("landuse") == "retail" and t.get("name"):
        return True
    if kind == "n" and t.get("place") in ("city", "town", "suburb", "village", "hamlet", "neighbourhood", "quarter"):
        return True
    if kind == "n" and t.get("barrier") in ("gate", "lift_gate", "swing_gate") and t.get("access") in ("private", "residents", "permit", "customers"):
        return True
    if t.get("gated") == "yes" or (kind == "w" and t.get("landuse") == "residential" and t.get("access") == "private"):
        return True
    return False


def is_point_of_region(t, kind):
    """points.json (zoomed-out place names and landmarks)."""
    if kind == "n" and t.get("place") in ("city", "town", "suburb", "village", "hamlet"):
        return True
    if not t.get("name"):
        return False
    return (t.get("tourism") == "museum" or t.get("amenity") in ("university", "college") or t.get("leisure") == "stadium" or
            t.get("building") == "stadium" or (t.get("leisure") == "sports_centre" and ARENA.search(t["name"])) or
            t.get("shop") == "mall" or (kind == "w" and t.get("landuse") == "retail" and NAMED_RETAIL.search(t["name"])))


def is_airport(t):
    return t.get("aeroway") == "aerodrome" and t.get("name") and not re.search(r"private|military", t.get("aerodrome:type", ""))


def is_county(t):
    return t.get("boundary") == "administrative" and t.get("admin_level") == "6"


class Pass1(osmium.SimpleHandler):
    """Collect stop-sign node ids and the member ways of relations we need (POI areas, counties)."""

    def __init__(self):
        super().__init__()
        self.stops, self.member_ways = set(), set()

    def node(self, n):
        if n.tags.get("highway") == "stop":
            self.stops.add(n.id)

    def relation(self, r):
        t = tags_of(r)
        if is_county(t) or is_poi(t, "r") or is_airport(t) or is_point_of_region(t, "r"):
            for m in r.members:
                if m.type == "w":
                    self.member_ways.add(m.ref)


class Pass2(osmium.SimpleHandler):
    def __init__(self, p1):
        super().__init__()
        self.p1 = p1
        self.lines, self.pois, self.region_pts, self.airports, self.highways, self.counties = [], [], [], [], [], []
        self.way_geom = {}

    def node(self, n):
        t = tags_of(n)
        if not t:
            return
        el = {"type": "node", "id": n.id, "lat": r5(n.location.lat), "lon": r5(n.location.lon), "tags": t}
        if is_poi(t, "n"):
            self.pois.append(el)
        if is_point_of_region(t, "n"):
            self.region_pts.append(el)
        if is_airport(t):
            self.airports.append(el)

    def way(self, w):
        geom, ids = [], []
        for nd in w.nodes:
            if nd.location.valid():
                geom.append({"lat": r5(nd.location.lat), "lon": r5(nd.location.lon)})
                ids.append(nd.ref)
        if not geom:
            return
        if w.id in self.p1.member_ways:
            self.way_geom[w.id] = geom
        t = tags_of(w)
        hw = t.get("highway", "")
        named_extra = hw in ("residential", "unclassified") and t.get("name", "").startswith("Bahia Vista")
        has_stop = hw and any(i in self.p1.stops for i in ids)
        if ROAD_CLASSES.match(hw) or named_extra or has_stop:
            self.lines.append({"type": "way", "id": w.id, "nodes": ids, "geometry": geom, "tags": t})
        if hw in ("motorway", "trunk", "primary") and REF_ROUTE.match(t.get("ref", "")):
            self.highways.append({"type": "way", "id": w.id, "geometry": geom, "tags": t})
        center = None
        for test, bucket in ((is_poi(t, "w"), self.pois), (is_point_of_region(t, "w"), self.region_pts), (is_airport(t), self.airports)):
            if test:
                center = center or bbox_center(geom)
                bucket.append({"type": "way", "id": w.id, "center": center, "tags": t})

    def relation(self, r):
        t = tags_of(r)
        members = [(m.ref, m.role) for m in r.members if m.type == "w" and m.ref in self.way_geom]
        if not members:
            return
        if is_county(t):
            self.counties.append({"type": "relation", "id": r.id, "tags": t,
                                  "members": [{"type": "way", "role": role, "geometry": self.way_geom[ref]} for ref, role in members]})
        pts = [g for ref, _ in members for g in self.way_geom[ref]]
        center = None
        for test, bucket in ((is_poi(t, "r"), self.pois), (is_point_of_region(t, "r"), self.region_pts), (is_airport(t), self.airports)):
            if test:
                center = center or bbox_center(pts)
                bucket.append({"type": "relation", "id": r.id, "center": center, "tags": t})


def bbox_center(geom):
    lats, lons = [g["lat"] for g in geom], [g["lon"] for g in geom]
    return {"lat": r5((min(lats) + max(lats)) / 2), "lon": r5((min(lons) + max(lons)) / 2)}


def in_box(lat, lon, box):
    s, w, n, e = box
    return s <= lat <= n and w <= lon <= e


def write(path, elements):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"elements": elements}, f, separators=(",", ":"))


def build_city(pbf, out, city, work):
    g, region = city["grid"], city["region"]
    out_dir = os.path.join(out, city["dir"]) if city["dir"] else out
    # Cut the city (whole tile grid) out of Florida, then keep only what the map uses.
    s, w = g["lat0"], g["lon0"]
    n, e = s + g["rows"] * g["step"], w + g["cols"] * g["step"]
    cut, small = os.path.join(work, city["name"] + "-cut.osm.pbf"), os.path.join(work, city["name"] + ".osm.pbf")
    subprocess.run(["osmium", "extract", "-b", f"{w},{s},{e},{n}", pbf, "-o", cut, "--overwrite"], check=True)
    subprocess.run(["osmium", "tags-filter", cut, *FILTERS, "-o", small, "--overwrite"], check=True)
    p1 = Pass1()
    p1.apply_file(small)
    p2 = Pass2(p1)
    p2.apply_file(small, locations=True, idx="flex_mem")
    print(f"{city['name']}: {len(p2.lines)} road ways, {len(p2.pois)} points, {len(p2.counties)} counties", flush=True)

    # Whole-area files.
    write(os.path.join(out_dir, "highways.json"), p2.highways)
    write(os.path.join(out_dir, "points.json"), [x for x in p2.region_pts if in_box(*point_of(x), region)])
    write(os.path.join(out_dir, "airports.json"), [x for x in p2.airports if in_box(*point_of(x), region)])
    write(os.path.join(out_dir, "counties.json"), p2.counties)

    # Tiles: points by location, roads by every tile their bounding box touches.
    tiles = {}

    def add(r, c, el):
        if 0 <= r < g["rows"] and 0 <= c < g["cols"]:
            tiles.setdefault((r, c), []).append(el)

    def rc(lat, lon):
        return int((lat - g["lat0"]) // g["step"]), int((lon - g["lon0"]) // g["step"])

    for el in p2.pois:
        add(*rc(*point_of(el)), el)
    for el in p2.lines:
        lats = [p["lat"] for p in el["geometry"]]
        lons = [p["lon"] for p in el["geometry"]]
        r0, c0 = rc(min(lats), min(lons))
        r1, c1 = rc(max(lats), max(lons))
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                add(r, c, el)
    for r in range(g["rows"]):
        for c in range(g["cols"]):
            write(os.path.join(out_dir, "tiles", f"r{r}c{c}.json"), tiles.get((r, c), []))
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "grid": g, "failed": [],
                   "source": "Geofabrik Florida extract"}, f)
    print(f"{city['name']}: wrote {g['rows'] * g['cols']} tiles", flush=True)


def point_of(el):
    if "lat" in el:
        return el["lat"], el["lon"]
    return el["center"]["lat"], el["center"]["lon"]


def main():
    pbf, out = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "out"
    work = os.path.join(os.path.dirname(os.path.abspath(pbf)), "work")
    os.makedirs(work, exist_ok=True)
    for city in CITIES:
        build_city(pbf, out, city, work)


if __name__ == "__main__":
    main()
