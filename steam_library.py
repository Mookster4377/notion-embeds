#!/usr/bin/env python3
"""Fetch your Steam library (games, playtime, last played) as JSON.

Claude reads this output and updates the "Steam Library" database in Notion.

Usage:
  python3 steam_library.py                 # uses the saved Steam profile
  python3 steam_library.py PROFILE         # profile URL, custom name, or SteamID64

Setup (once):
  1. Get a key at https://steamcommunity.com/dev/apikey
  2. Save it:  mkdir -p ~/.config/steam-notion && pbpaste > ~/.config/steam-notion/api_key
     (copy the key first), then: chmod 600 ~/.config/steam-notion/api_key
     Or set the STEAM_API_KEY environment variable instead.
  3. On Steam, set Profile > Privacy Settings > "Game details" to Public.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CONF_DIR = os.path.expanduser("~/.config/steam-notion")
KEY_FILE = os.path.join(CONF_DIR, "api_key")
ID_FILE = os.path.join(CONF_DIR, "steam_id")
API = "https://api.steampowered.com"
# App IDs left out of the library (not games): 920490 = Driver Booster for Steam.
EXCLUDED_APP_IDS = {920490}


def fail(msg):
    print(json.dumps({"error": msg}))
    sys.exit(1)


def load_key():
    key = os.environ.get("STEAM_API_KEY", "").strip()
    if not key and os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            key = f.read().strip()
    if not re.fullmatch(r"[0-9A-Fa-f]{32}", key or ""):
        fail(f"No valid Steam API key. Save the 32-character key to {KEY_FILE}.")
    return key


def call(key, path, **params):
    params.update(key=key, format="json")
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r).get("response", {})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            fail("Steam rejected the API key. Check the key you saved.")
        fail(f"Steam API error {e.code} on {path}.")
    except urllib.error.URLError as e:
        fail(f"Could not reach Steam: {e.reason}")


PLATFORM_CACHE = os.path.join(CONF_DIR, "platforms.json")
PLATFORM_NAMES = (("windows", "Windows"), ("mac", "Mac"), ("linux", "Linux"))


def load_platforms(app_ids):
    """Return {app_id: ["Windows", "Mac", ...]} from the public Steam store API.

    Results are cached in PLATFORM_CACHE so daily runs only look up new games.
    Games the store no longer lists (playtests, delisted) get an empty list.
    """
    cache = {}
    if os.path.exists(PLATFORM_CACHE):
        with open(PLATFORM_CACHE) as f:
            cache = json.load(f)
    missing = [a for a in app_ids if str(a) not in cache]
    for i, appid in enumerate(missing):
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=platforms"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                entry = json.load(r).get(str(appid), {})
        except (urllib.error.URLError, ValueError):
            continue  # try again next run
        plats = entry.get("data", {}).get("platforms", {}) if entry.get("success") else {}
        cache[str(appid)] = [label for key, label in PLATFORM_NAMES if plats.get(key)]
        if i % 20 == 19:  # stay well under the store's rate limit
            time.sleep(2)
    if missing:
        os.makedirs(CONF_DIR, exist_ok=True)
        with open(PLATFORM_CACHE, "w") as f:
            json.dump(cache, f)
    return {a: cache.get(str(a)) for a in app_ids}


def resolve_steam_id(key, profile):
    profile = profile.strip().rstrip("/")
    m = re.search(r"(\d{17})$", profile)
    if m:
        return m.group(1)
    m = re.search(r"/id/([^/?#]+)", profile)
    name = m.group(1) if m else profile
    res = call(key, "/ISteamUser/ResolveVanityURL/v1/", vanityurl=name)
    if res.get("success") != 1:
        fail(f"Could not find a Steam profile named '{name}'.")
    return res["steamid"]


def main():
    key = load_key()
    if len(sys.argv) > 1:
        steam_id = resolve_steam_id(key, sys.argv[1])
        os.makedirs(CONF_DIR, exist_ok=True)
        with open(ID_FILE, "w") as f:
            f.write(steam_id)
    elif os.path.exists(ID_FILE):
        with open(ID_FILE) as f:
            steam_id = f.read().strip()
    else:
        fail("No Steam profile yet. Run: python3 steam_library.py <your profile URL>")

    owned = call(key, "/IPlayerService/GetOwnedGames/v1/",
                 steamid=steam_id, include_appinfo=1, include_played_free_games=1)
    if not owned.get("games"):
        fail("Steam returned no games. Set Privacy Settings > 'Game details' to Public, then try again.")

    recent = call(key, "/IPlayerService/GetRecentlyPlayedGames/v1/", steamid=steam_id)
    recent_mins = {g["appid"]: g.get("playtime_2weeks", 0) for g in recent.get("games", [])}

    games = []
    for g in owned["games"]:
        appid = g["appid"]
        if appid in EXCLUDED_APP_IDS:
            continue
        last =g.get("rtime_last_played") or 0
        games.append({
            "app_id": appid,
            "name": g.get("name", f"App {appid}"),
            "hours_played": round(g.get("playtime_forever", 0) / 60, 1),
            "hours_last_2_weeks": round(recent_mins.get(appid, 0) / 60, 1),
            "last_played": (datetime.fromtimestamp(last, timezone.utc).strftime("%Y-%m-%d")
                            if last else None),
            "store_url": f"https://store.steampowered.com/app/{appid}/",
            "header_image": f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
        })
    platforms = load_platforms([g["app_id"] for g in games])
    for g in games:
        g["platforms"] = platforms[g["app_id"]]  # None = lookup failed, retry next run
    games.sort(key=lambda x: x["hours_played"], reverse=True)
    print(json.dumps({"steam_id": steam_id, "game_count": len(games), "games": games}, indent=1))


if __name__ == "__main__":
    main()
