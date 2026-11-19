#!/usr/bin/env python3
"""Download OpenStreetMap data for the Tampa Bay traffic map (traffic.html).

Run nightly by .github/workflows/map-data.yml. Writes, into the output folder:
  tiles/r{row}c{col}.json  everything the map needs for one 0.1-degree tile
  highways.json            interstates, U.S. and main state routes (for zoomed-out views)
  points.json              place names, museums, colleges, venues and malls (for zoomed-out views)
  airports.json            public airports
  counties.json            county boundaries
  meta.json                when it ran, the tile grid, and any tiles that failed

The grid must match TILE in traffic.html.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

OUT = sys.argv[1] if len(sys.argv) > 1 else "out"
GRID = {"lat0": 26.8, "lon0": -83.0, "step": 0.1, "rows": 15, "cols": 11}
REGION = (26.85, -82.95, 28.25, -81.95)  # south, west, north, east: Tampa Bay to North Port
SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "notion-embeds-traffic-map/1.0 (nightly map data; github.com/Mookster4377/notion-embeds)"
# Tags the map never uses; dropped to keep files small.
DROP_PREFIXES = ("tiger:", "source", "gnis:", "NHD:", "note", "fixme", "FIXME", "created_by", "check_date", "survey", "attribution",
                 "import", "wikimedia", "name:", "old_name", "alt_name:", "is_in", "ref:", "nhd", "building:levels", "roof:", "addr:country", "addr:state", "addr:postcode")


def bb(s, w, n, e):
    return f"{s:.4f},{w:.4f},{n:.4f},{e:.4f}"


def tile_query(box):
    return f"""[out:json][timeout:180];
node["highway"="stop"]({box})->.stops;
way(bn.stops)["highway"]->.stopways;
(
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|motorway_link|trunk_link)$"]({box});
  way["highway"~"^(residential|unclassified)$"]["name"~"^Bahia Vista"]({box});
  .stopways;
);
out body geom qt;
(
  node["highway"~"^(stop|motorway_junction)$"]({box});
  nwr["amenity"~"^(school|parking|cafe|bar|pub|nightclub|university|college|gym)$"]({box});
  nwr["tourism"="museum"]({box});
  nwr["leisure"~"^(stadium|sports_centre|fitness_centre)$"]({box});
  nwr["building"="stadium"]({box});
  nwr["shop"="mall"]({box});
  way["landuse"="retail"]["name"]({box});
  node["place"~"^(city|town|suburb|village|hamlet|neighbourhood|quarter)$"]({box});
  node["barrier"~"^(gate|lift_gate|swing_gate)$"]["access"~"^(private|residents|permit|customers)$"]({box});
  nwr["gated"="yes"]({box});
  way["landuse"="residential"]["access"="private"]({box});
);
out center qt;"""


REGION_FILES = {
    "highways.json": f"""[out:json][timeout:240];
way["highway"~"^(motorway|trunk|primary)$"]["ref"~"^(I|US|SR|FL|CR) ?[0-9]"]({bb(*REGION)});
out tags geom qt;""",
    "points.json": f"""[out:json][timeout:240];
(
  node["place"~"^(city|town|suburb|village|hamlet)$"]({bb(*REGION)});
  nwr["tourism"="museum"]["name"]({bb(*REGION)});
  nwr["amenity"~"^(university|college)$"]["name"]({bb(*REGION)});
  nwr["leisure"="stadium"]["name"]({bb(*REGION)});
  nwr["building"="stadium"]["name"]({bb(*REGION)});
  nwr["leisure"="sports_centre"]["name"~"Arena|Stadium|Ballpark|Speedway",i]({bb(*REGION)});
  nwr["shop"="mall"]["name"]({bb(*REGION)});
  way["landuse"="retail"]["name"~"mall|town cent|outlets|shoppes|galleria|marketplace|square",i]({bb(*REGION)});
);
out center tags qt;""",
    "airports.json": f"""[out:json][timeout:120];
nwr["aeroway"="aerodrome"]["name"]["aerodrome:type"!~"private|military"]({bb(*REGION)});
out center tags qt;""",
    "counties.json": f"""[out:json][timeout:240];
relation["boundary"="administrative"]["admin_level"="6"]({bb(*REGION)});
out geom qt;""",
}


def fetch(query, attempts=6):
    last = None
    for i in range(attempts):
        url = SERVERS[i % len(SERVERS)]
        try:
            req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": query}).encode(),
                                         headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.load(r)
            if "remark" in data and "error" in data.get("remark", "").lower():
                raise RuntimeError(data["remark"][:200])
            return data
        except Exception as e:  # busy server, timeout, bad reply: wait and try the next server
            last = e
            print(f"  attempt {i + 1} on {url} failed: {e}", flush=True)
            time.sleep(min(60, 5 * (i + 1)))
    raise RuntimeError(f"gave up: {last}")


def r5(x):
    return round(x, 5)


def slim(el):
    tags = {k: v for k, v in (el.get("tags") or {}).items() if not k.startswith(DROP_PREFIXES)}
    out = {"type": el["type"], "id": el["id"]}
    if tags:
        out["tags"] = tags
    if "lat" in el:
        out["lat"], out["lon"] = r5(el["lat"]), r5(el["lon"])
    if "center" in el:
        out["center"] = {"lat": r5(el["center"]["lat"]), "lon": r5(el["center"]["lon"])}
    if "nodes" in el:
        out["nodes"] = el["nodes"]
    if "geometry" in el:
        out["geometry"] = [{"lat": r5(g["lat"]), "lon": r5(g["lon"])} if g else None for g in el["geometry"]]
    if "members" in el:
        out["members"] = [{"type": m["type"], "role": m.get("role", ""),
                           "geometry": [{"lat": r5(g["lat"]), "lon": r5(g["lon"])} for g in m.get("geometry") or [] if g]}
                          for m in el["members"] if m["type"] == "way"]
    return out


def save(path, data):
    els = [slim(e) for e in data.get("elements", [])]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump({"elements": els}, f, separators=(",", ":"))
    return len(els)


def main():
    os.makedirs(os.path.join(OUT, "tiles"), exist_ok=True)
    failed = []
    for name, q in REGION_FILES.items():
        print(f"{name} ...", flush=True)
        try:
            print(f"  {save(os.path.join(OUT, name), fetch(q))} elements", flush=True)
        except Exception as e:
            print(f"  FAILED {name}: {e}", flush=True)
            failed.append(name)
        time.sleep(3)
    g = GRID
    total = g["rows"] * g["cols"]
    for r in range(g["rows"]):
        for c in range(g["cols"]):
            tid = f"r{r}c{c}"
            s, w = g["lat0"] + r * g["step"], g["lon0"] + c * g["step"]
            box = bb(s, w, s + g["step"], w + g["step"])
            try:
                n = save(os.path.join(OUT, "tiles", tid + ".json"), fetch(tile_query(box)))
                print(f"{tid}: {n} elements", flush=True)
            except Exception as e:
                print(f"{tid}: FAILED {e}", flush=True)
                failed.append(tid)
            time.sleep(2)  # be polite to the shared servers
    with open(os.path.join(OUT, "meta.json"), "w") as f:
        json.dump({"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "grid": GRID, "failed": failed}, f)
    print(f"done: {total} tiles, {len(failed)} failed: {failed}", flush=True)
    # Don't publish a mostly-empty set over yesterday's good data.
    if len([x for x in failed if x.startswith("r")]) > total * 0.2:
        sys.exit("too many tiles failed; keeping the previous data")


if __name__ == "__main__":
    main()
