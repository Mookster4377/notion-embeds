#!/usr/bin/env python3
"""Find a YouTube video for every song in youtube-finder.html and save the results into it.

Reads the song list built into youtube-finder.html, searches YouTube for
"artist title" for each song, and writes the top 3 results into the page's
<script id="video-data"> block. The page uses the top result for any song you
have not set yourself, and offers the other two under "Other picks".

Searches are cached in .youtube-cache.json so the script can be stopped and
re-run; only songs not looked up yet are searched again.

    python3 scripts/find_youtube.py            # look up everything not cached
    python3 scripts/find_youtube.py --limit 20 # try a few first
"""
import argparse
import json
import os
import random
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, 'youtube-finder.html')
CACHE = os.path.join(ROOT, '.youtube-cache.json')
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/130.0 Safari/537.36')
EMBEDDED_ID = re.compile(r'\s*\(([\w-]{11})\)\s*$')


def read_songs(html):
    m = re.search(r'<script id="song-data" type="text/plain">\n(.*?)\n\s*</script>', html, re.S)
    if not m:
        sys.exit('Could not find the song list in youtube-finder.html')
    songs, seen = [], {}
    for line in m.group(1).split('\n'):
        if '|' not in line:
            continue
        title, artist = line.split('|')[:2]
        k = title + '\u0001' + artist
        seen[k] = seen.get(k, 0) + 1
        songs.append({'title': title, 'artist': artist,
                      'key': k if seen[k] == 1 else k + '\u0001' + str(seen[k])})
    return songs


def query_for(song):
    title = EMBEDDED_ID.sub('', song['title'])
    return (song['artist'] + ' ' + title).strip() if song['artist'] else title


def search(q, n=3):
    url = 'https://www.youtube.com/results?' + urllib.parse.urlencode({'search_query': q, 'sp': 'EgIQAQ=='})
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9', 'Cookie': 'CONSENT=YES+1; SOCS=CAI'})
    html = urllib.request.urlopen(req, timeout=20).read().decode('utf-8', 'replace')
    m = re.search(r'var ytInitialData = (\{.*?\});</script>', html, re.S)
    if not m:
        raise RuntimeError('no results data in page')
    found = []

    def walk(o):
        if len(found) >= n:
            return
        if isinstance(o, dict):
            v = o.get('videoRenderer')
            if v and v.get('videoId'):
                found.append({
                    'id': v['videoId'],
                    't': ''.join(r.get('text', '') for r in v.get('title', {}).get('runs', [])),
                    'c': ''.join(r.get('text', '') for r in v.get('ownerText', {}).get('runs', [])),
                })
                return
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(json.loads(m.group(1)))
    return found


def words(s):
    s = unicodedata.normalize('NFKD', s.lower())
    return set(w for w in re.findall(r'[a-z0-9぀-ヿ一-鿿]+', s) if w not in {'feat', 'ft', 'the', 'a'})


def weak(song, top):
    """True when the top result's title shares under half the song title's words."""
    want = words(EMBEDDED_ID.sub('', song['title']))
    if not want:
        return False
    got = words(top['t'] + ' ' + top['c'])
    return len(want & got) / len(want) < 0.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0, help='only look up this many new songs')
    ap.add_argument('--workers', type=int, default=4)
    args = ap.parse_args()

    html = open(PAGE, encoding='utf-8').read()
    songs = read_songs(html)
    cache = json.load(open(CACHE, encoding='utf-8')) if os.path.exists(CACHE) else {}
    todo = [s for s in songs if s['key'] not in cache and not EMBEDDED_ID.search(s['title'])]
    if args.limit:
        todo = todo[:args.limit]
    print(f'{len(songs)} songs, {len(cache)} already looked up, {len(todo)} to search', flush=True)

    lock = threading.Lock()
    state = {'done': 0, 'failed': 0}

    def work(song):
        results = None
        for attempt in range(4):
            try:
                results = search(query_for(song))
                break
            except Exception:
                time.sleep(2 + attempt * 4 + random.random() * 2)
        with lock:
            state['done'] += 1
            if results is None:
                state['failed'] += 1
            else:
                cache[song['key']] = results
            if state['done'] % 50 == 0 or state['done'] == len(todo):
                json.dump(cache, open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False)
                print(f"  {state['done']}/{len(todo)} searched, {state['failed']} failed", flush=True)
        time.sleep(0.3 + random.random() * 0.4)

    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(work, todo))
    json.dump(cache, open(CACHE, 'w', encoding='utf-8'), ensure_ascii=False)

    out, n_weak = {}, 0
    for s in songs:
        r = cache.get(s['key'])
        if not r:
            continue
        top = r[0]
        rec = {'v': top['id'], 't': top['t'], 'c': top['c'], 'alts': r[1:]}
        if weak(s, top):
            rec['w'] = 1
            n_weak += 1
        out[s['key']] = rec
    blob = json.dumps(out, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    block = '<script id="video-data" type="application/json">' + blob + '</script>'
    if '<script id="video-data"' in html:
        html = re.sub(r'<script id="video-data" type="application/json">.*?</script>',
                      lambda _: block, html, count=1, flags=re.S)
    else:
        html = html.replace('  <script id="song-data"', '  ' + block + '\n  <script id="song-data"', 1)
    open(PAGE, 'w', encoding='utf-8').write(html)
    print(f'Saved {len(out)} videos into youtube-finder.html '
          f'({n_weak} marked "check this", {state["failed"]} searches failed).')


if __name__ == '__main__':
    main()
