#!/usr/bin/env python3
"""Build "who represents here" data for traffic.html: district shapes plus current members.

Run by .github/workflows/map-data.yml after the map build:
    python scripts/build_reps.py out

Writes <out>/[orlando/]reps.json for each city in build_map_data.CITIES:
  {"updated": "...", "senators": [member], "districts": [{"c": "us"|"fls"|"flh", "d": "14", "p": polygons, "m": member|null,
   "covers": ["Tampa", ...], "counties": ["Hillsborough", ...]}], "counties": [{"n", "p"}], "places": [{"n", "p"}]}
  polygons are Leaflet-style [[[[lat, lon], ...ring], ...rings], ...polygons]; member = {"n", "party", "url", "phone", "email"}.

Sources (all public, refreshed nightly):
  Districts, counties, cities  U.S. Census TIGER/Line shapefiles (newest year that has each layer)
  U.S. House and Senate        github.com/unitedstates/congress-legislators (legislators-current.json)
  Florida Senate and House     github.com/openstates/people (data/fl/legislature)

If anything fails, the previous day's reps.json is kept (copied from the published map-data branch), so a
source outage never breaks the rest of the map data.
"""
import datetime
import glob
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_map_data import CITIES  # noqa: E402

PUBLISHED = "https://raw.githubusercontent.com/{repo}/map-data/{path}"
CONGRESS = "https://raw.githubusercontent.com/unitedstates/congress-legislators/gh-pages/legislators-current.json"
OPENSTATES = "https://github.com/openstates/people.git"
TIGER = "https://www2.census.gov/geo/tiger/TIGER{year}/{layer}/"
FL = "12"
UA = {"User-Agent": "notion-embeds map builder (github.com/Mookster4377/notion-embeds)"}


def fetch(url, timeout=120):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def tiger_zip(layer, pattern):
    """Newest TIGER/Line file in a layer folder whose name matches pattern (tries this year back to 2022)."""
    for year in range(datetime.date.today().year, 2021, -1):
        try:
            listing = fetch(TIGER.format(year=year, layer=layer), 60).decode("utf-8", "replace")
        except Exception:
            continue
        names = sorted(set(re.findall(r'href="(' + pattern.replace("{year}", str(year)) + r')"', listing)))
        if names:
            url = TIGER.format(year=year, layer=layer) + names[-1]
            print(f"  {layer}: {url}", flush=True)
            return zipfile.ZipFile(io.BytesIO(fetch(url, 600)))
    raise RuntimeError(f"no TIGER file for {layer}")


def read_shapes(zf, keep=lambda rec: True):
    import shapefile
    from shapely.geometry import shape
    base = [n[:-4] for n in zf.namelist() if n.endswith(".shp")][0]
    r = shapefile.Reader(shp=io.BytesIO(zf.read(base + ".shp")), dbf=io.BytesIO(zf.read(base + ".dbf")),
                         shx=io.BytesIO(zf.read(base + ".shx")))
    fields = [f[0] for f in r.fields[1:]]
    out = []
    for sr in r.iterShapeRecords():
        rec = dict(zip(fields, sr.record))
        if keep(rec):
            out.append((rec, shape(sr.shape.__geo_interface__)))
    return out


def district_no(rec, keys):
    for k in keys:
        if k in rec and str(rec[k]).strip():
            v = str(rec[k]).strip().lstrip("0")
            return v or "0"
    return None


def congress_members():
    data = json.loads(fetch(CONGRESS))
    house, senators = {}, []
    for p in data:
        t = p["terms"][-1]
        if t.get("state") != "FL":
            continue
        m = {"n": p["name"].get("official_full") or (p["name"]["first"] + " " + p["name"]["last"]), "party": t.get("party", ""),
             "url": t.get("url", ""), "phone": t.get("phone", "")}
        if t["type"] == "sen":
            senators.append(m)
        else:
            house[str(t.get("district"))] = m
    return house, senators


def state_members(work):
    import yaml
    repo = os.path.join(work, "openstates-people")
    if not os.path.isdir(repo):
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", OPENSTATES, repo], check=True)
        subprocess.run(["git", "-C", repo, "sparse-checkout", "set", "data/fl/legislature"], check=True)
    upper, lower = {}, {}
    for path in glob.glob(os.path.join(repo, "data/fl/legislature/*.yml")):
        p = yaml.safe_load(open(path))
        roles = [r for r in p.get("roles", []) if r.get("type") in ("upper", "lower") and not r.get("end_date")]
        if not roles:
            continue
        role = roles[-1]
        party = (p.get("party") or [{}])[-1].get("name", "")
        office = next((o for o in p.get("offices", []) if o.get("classification") == "district" and o.get("voice")), None) or \
            next((o for o in p.get("offices", []) if o.get("voice")), {})
        link = next((ln["url"] for ln in p.get("links", []) if "flsenate.gov" in ln["url"] or "flhouse.gov" in ln["url"]
                     or "myfloridahouse.gov" in ln["url"]), (p.get("links") or [{}])[0].get("url", ""))
        m = {"n": p["name"], "party": party, "url": link, "phone": office.get("voice", ""), "email": p.get("email", "")}
        (upper if role["type"] == "upper" else lower)[str(role["district"]).lstrip("0")] = m
    return upper, lower


def to_leaflet(geom):
    polys = [geom] if geom.geom_type == "Polygon" else [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    out = []
    for poly in polys:
        rings = [poly.exterior] + list(poly.interiors)
        out.append([[[round(y, 4), round(x, 4)] for x, y in ring.coords] for ring in rings if len(ring.coords) >= 4])
    return out


def clipped(geom, box):
    from shapely.geometry import box as mkbox
    s, w, n, e = box
    g = geom.intersection(mkbox(w - 0.05, s - 0.05, e + 0.05, n + 0.05))
    if g.is_empty:
        return None
    return g.simplify(0.0004, preserve_topology=True)


def build(out, work):
    print("Representatives:", flush=True)
    house, senators = congress_members()
    upper, lower = state_members(work)
    print(f"  members: {len(house)} U.S. House, {len(senators)} U.S. Senate, {len(upper)} FL Senate, {len(lower)} FL House", flush=True)
    layers = [
        ("us", read_shapes(tiger_zip("CD", r"tl_{year}_" + FL + r"_cd\d+\.zip")), ("CD119FP", "CD118FP", "CDFP"), house),
        ("fls", read_shapes(tiger_zip("SLDU", r"tl_{year}_" + FL + r"_sldu\.zip")), ("SLDUST",), upper),
        ("flh", read_shapes(tiger_zip("SLDL", r"tl_{year}_" + FL + r"_sldl\.zip")), ("SLDLST",), lower),
    ]
    for k in list(r for r in layers[0][1][0][0] if re.match(r"CD\d+FP", r)):
        layers[0] = (layers[0][0], layers[0][1], (k,) + layers[0][2], layers[0][3])
    counties = read_shapes(tiger_zip("COUNTY", r"tl_{year}_us_county\.zip"), lambda r: r.get("STATEFP") == FL)
    places = read_shapes(tiger_zip("PLACE", r"tl_{year}_" + FL + r"_place\.zip"), lambda r: str(r.get("CLASSFP", "")).startswith("C"))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    for city in CITIES:
        box = city["region"]
        cnty = [(r["NAME"], g) for r, g in counties if clipped(g, box) is not None]
        plc = [(r["NAME"], g) for r, g in places if clipped(g, box) is not None]
        districts = []
        for chamber, shapes, keys, members in layers:
            for rec, geom in shapes:
                d = district_no(rec, keys)
                if d in (None, "0", "ZZZ"):
                    continue
                g = clipped(geom, box)
                if g is None or g.area < 1e-6:
                    continue
                covers = sorted(n for n, pg in plc if pg.intersection(geom).area > 0.2 * pg.area)
                in_counties = sorted(n for n, cg in cnty if cg.intersection(geom).area > 0.02 * min(cg.area, geom.area))
                districts.append({"c": chamber, "d": d, "p": to_leaflet(g), "m": members.get(d), "covers": covers, "counties": in_counties})
        doc = {"updated": stamp, "senators": senators, "districts": districts,
               "counties": [{"n": n, "p": to_leaflet(clipped(g, box))} for n, g in cnty],
               "places": [{"n": n, "p": to_leaflet(clipped(g, box))} for n, g in plc]}
        out_dir = os.path.join(out, city["dir"]) if city["dir"] else out
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "reps.json")
        with open(path, "w") as f:
            json.dump(doc, f, separators=(",", ":"))
        counts = {c: sum(1 for x in districts if x["c"] == c) for c in ("us", "fls", "flh")}
        vacant = [x["c"] + x["d"] for x in districts if not x["m"]]
        print(f"  {city['name']}: {counts}, {len(cnty)} counties, {len(plc)} cities, vacant/unmatched {vacant}, "
              f"{os.path.getsize(path) // 1024} KB", flush=True)


def keep_previous(out):
    repo = os.environ.get("GITHUB_REPOSITORY", "Mookster4377/notion-embeds")
    for city in CITIES:
        rel = (city["dir"] + "/" if city["dir"] else "") + "reps.json"
        try:
            data = fetch(PUBLISHED.format(repo=repo, path=rel), 60)
            os.makedirs(os.path.dirname(os.path.join(out, rel)) or out, exist_ok=True)
            with open(os.path.join(out, rel), "wb") as f:
                f.write(data)
            print(f"  kept yesterday's {rel}", flush=True)
        except Exception as e:
            print(f"  no previous {rel} to keep ({e})", flush=True)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "out"
    work = tempfile.mkdtemp(prefix="reps-")
    try:
        build(out, work)
    except Exception as e:
        print(f"::warning::Couldn't rebuild representatives ({e!r}); keeping yesterday's data.", flush=True)
        keep_previous(out)


if __name__ == "__main__":
    main()
