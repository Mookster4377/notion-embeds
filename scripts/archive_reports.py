#!/usr/bin/env python3
"""Keep the map's police reports forever.

The map shares reports through free ntfy.sh channels, which only keep messages for about 12 hours.
.github/workflows/police-archive.yml runs this every 30 minutes: it reads each channel's recent
messages and adds any new ones to <out>/police-<city>.json (published on the "police-archive" branch),
which traffic.html loads alongside the live channel.

    python scripts/archive_reports.py out
"""
import json
import os
import sys
import urllib.request

TOPICS = {"tampa": "tbt-police-a49fdfc3a0", "orlando": "orl-police-7c31e58d24"}
KINDS = {"speed", "stop", "crash", "checkpoint", "other"}


def tag(tags, key):
    for t in tags or []:
        if t.startswith(key + ":"):
            return t[len(key) + 1:]
    return None


def fetch(topic):
    req = urllib.request.Request(f"https://ntfy.sh/{topic}/json?poll=1&since=all",
                                 headers={"User-Agent": "notion-embeds police archive"})
    with urllib.request.urlopen(req, timeout=60) as r:
        lines = r.read().decode("utf-8", "replace").splitlines()
    out = []
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("event") != "message":
            continue
        try:
            lat, lon = float(tag(m.get("tags"), "lat")), float(tag(m.get("tags"), "lon"))
        except (TypeError, ValueError):
            continue
        if not (24 < lat < 31.5 and -88 < lon < -79):  # Florida only
            continue
        kind = tag(m.get("tags"), "kind") or "other"
        msg = m.get("message") or ""
        note = msg.split(":", 1)[1].strip() if ":" in msg else ""
        out.append({"id": m["id"], "time": int(m["time"]), "lat": round(lat, 5), "lon": round(lon, 5),
                    "kind": kind if kind in KINDS else "other", "note": note[:140]})
    return out


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "out"
    os.makedirs(out, exist_ok=True)
    for city, topic in TOPICS.items():
        path = os.path.join(out, f"police-{city}.json")
        try:
            have = json.load(open(path))["reports"]
        except (OSError, ValueError, KeyError):
            have = []
        ids = {r["id"] for r in have}
        new = [r for r in fetch(topic) if r["id"] not in ids]
        reports = sorted(have + new, key=lambda r: r["time"])
        with open(path, "w") as f:
            json.dump({"reports": reports}, f, separators=(",", ":"))
        print(f"{city}: {len(new)} new, {len(reports)} total", flush=True)


if __name__ == "__main__":
    main()
