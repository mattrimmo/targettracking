#!/usr/bin/env python3
"""
Long Term Target Tracking - weekly / on-demand sync (v2, Spot On Track first).

What changed from the old version
---------------------------------
The old script asked Spotify about EVERY playlist to find its owner. Spotify's
quota on the newer app (roughly 90 lookups per 24h) can never keep up with that,
so most playlists stayed unclassified and the data stalled.

This version gets the owner / curator name straight from Spot On Track (SOT)
whenever SOT supplies one, and only falls back to Spotify for playlists SOT
cannot name, inside a small per-run budget. Spotify's own algorithmic/editorial
playlists (ids starting 37i9dQZF1) are classified as editorial from the id alone,
because Spotify's API answers 404 for them anyway.

Classification order for each current playlist:
  1. id starts with 37i9dQZF1                -> editorial (no call)
  2. in data/playlist_cache.json             -> use it (no call)
  3. SOT playlist object has an owner name   -> use it (no call), cache it
  4. otherwise Spotify lookup, budgeted      -> use it, cache it
  5. out of budget / rate limited            -> left unclassified, retried next run

Secrets (GitHub Actions): SOT_API_KEY (required), SPOTIFY_CLIENT_ID and
SPOTIFY_CLIENT_SECRET (optional, only used for step 4).
Optional env: SPOTIFY_LOOKUPS_PER_RUN (default 60, 0 disables Spotify entirely),
MIN_FOLLOWERS (default 100).
"""
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA        = os.path.join(REPO_ROOT, "data")
TRACKED_FP  = os.path.join(DATA, "tracked.json")
HISTORY_FP  = os.path.join(DATA, "history.json")
CURATORS_FP = os.path.join(DATA, "curators.json")
CACHE_FP    = os.path.join(DATA, "playlist_cache.json")
CALLLOG_FP  = os.path.join(DATA, "spotify_call_log.json")

SOT_KEY   = os.environ.get("SOT_API_KEY", "")
SP_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SP_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
IS_OFFICIAL = os.environ.get("IS_OFFICIAL", "false").lower() == "true"

MIN_FOLLOWERS = int(os.environ.get("MIN_FOLLOWERS", "100") or 100)
SPOTIFY_PER_RUN = int(os.environ.get("SPOTIFY_LOOKUPS_PER_RUN", "60") or 0)
SPOTIFY_DAILY_CAP = 90          # rolling 24h safety cap, same as before
SOT_DELAY = 0.4                 # seconds between SOT calls
EDITORIAL_ID_PREFIX = "37i9dQZF1"
UNKNOWN = "__UNKNOWN__"

OWNER_KEYS = ("owner_name", "owner", "curator_name", "curator", "user_name",
              "creator", "author", "user", "playlist_owner")
IMAGE_KEYS = ("image_url", "image", "cover_url", "cover", "artwork", "picture", "thumbnail", "img")
NAME_SUBKEYS = ("display_name", "name", "title", "username", "id")

stats = {"cache": 0, "sot": 0, "spotify": 0, "editorial_id": 0, "unresolved": 0,
         "not_found": 0, "spotify_blocked": False}
_logged_keys = [False]


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read().strip()
        return json.loads(txt) if txt else default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------- Spot On Track
def sot_get(path):
    for attempt in range(4):
        r = requests.get("https://www.spotontrack.com/api/v1" + path,
                         headers={"Authorization": "Bearer " + SOT_KEY}, timeout=30)
        if r.status_code == 429:
            wait = min(int(r.headers.get("Retry-After", "5") or 5), 60) + 1
            print(f"  [sot] 429, waiting {wait}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        time.sleep(SOT_DELAY)
        return r.json()
    raise RuntimeError("SOT kept rate limiting " + path)


def _name_from(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for k in NAME_SUBKEYS:
            if isinstance(value.get(k), str) and value[k].strip():
                return value[k].strip()
    return ""


def sot_owner(pl):
    """Owner / curator name from the SOT playlist object, or None if SOT has none."""
    for k in OWNER_KEYS:
        if k in pl:
            name = _name_from(pl[k])
            if name:
                return name
    return None


def sot_image(pl):
    for k in IMAGE_KEYS:
        v = pl.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("url"):
            return v[0]["url"]
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
    return None


# ---------------------------------------------------------------------- Spotify
_sp_token, _sp_exp = None, 0
_call_log = {"window_start": None, "calls": 0}
_run_spotify_calls = [0]


def sp_token():
    global _sp_token, _sp_exp
    if _sp_token and time.time() < _sp_exp:
        return _sp_token
    r = requests.post("https://accounts.spotify.com/api/token",
                      headers={"Authorization": "Basic " + base64.b64encode(f"{SP_ID}:{SP_SECRET}".encode()).decode(),
                               "Content-Type": "application/x-www-form-urlencoded"},
                      data={"grant_type": "client_credentials"}, timeout=30)
    r.raise_for_status()
    d = r.json()
    _sp_token, _sp_exp = d["access_token"], time.time() + d["expires_in"] - 30
    return _sp_token


def load_call_log():
    log = load_json(CALLLOG_FP, {}) or {}
    ws = log.get("window_start")
    try:
        start = datetime.fromisoformat(ws) if ws else None
    except ValueError:
        start = None
    now = datetime.now(timezone.utc)
    if not start or now - start >= timedelta(hours=24):
        start, log = now, {"calls": 0}
    _call_log.update({"window_start": start.isoformat(), "calls": int(log.get("calls", 0))})


def spotify_allowed():
    return bool(SP_ID and SP_SECRET) and not stats["spotify_blocked"] \
        and _run_spotify_calls[0] < SPOTIFY_PER_RUN and _call_log["calls"] < SPOTIFY_DAILY_CAP


def sp_lookup(spotify_id):
    """Returns ('ok', owner_id, owner_name, cover) | ('missing',) | ('blocked',) | ('error',)."""
    _run_spotify_calls[0] += 1
    _call_log["calls"] += 1
    try:
        r = requests.get(f"https://api.spotify.com/v1/playlists/{spotify_id}",
                         headers={"Authorization": "Bearer " + sp_token()},
                         params={"fields": "owner.id,owner.display_name,images"}, timeout=30)
    except requests.RequestException as e:
        print(f"  [spotify] request error {e}")
        return ("error",)
    if r.status_code == 429:
        ra = int(r.headers.get("Retry-After", "2") or 2)
        print(f"  [spotify] 429 Retry-After={ra}s -> stopping Spotify lookups for this run")
        stats["spotify_blocked"] = True      # quota wall: never cache this as an answer
        return ("blocked",)
    if r.status_code == 404:
        return ("missing",)
    if not r.ok:
        print(f"  [spotify] {r.status_code} {r.text[:120]}")
        return ("error",)
    d = r.json()
    o = d.get("owner") or {}
    imgs = d.get("images") or []
    return ("ok", (o.get("id") or "").lower(), o.get("display_name") or "", imgs[0]["url"] if imgs else None)


# --------------------------------------------------------------- classification
def classify(pl, cache):
    """Returns ('editorial'|'independent'|None, owner_name, cover)."""
    sid = pl["spotify_id"]
    sot_cover = sot_image(pl)

    if sid.startswith(EDITORIAL_ID_PREFIX):
        stats["editorial_id"] += 1
        cached = cache.get(sid)
        return "editorial", "Spotify", sot_cover or (cached[2] if cached else None)

    cached = cache.get(sid)
    if cached and isinstance(cached, list) and len(cached) == 3:
        owner, _t, cover = cached
        if owner == "":                       # legacy "blank owner" = editorial
            stats["cache"] += 1
            return "editorial", "Spotify", cover or sot_cover
        if owner == UNKNOWN:
            stats["cache"] += 1
            return "independent", "Unknown curator", cover or sot_cover
        stats["cache"] += 1
        return ("editorial" if owner.lower() == "spotify" else "independent"), owner, cover or sot_cover

    owner = sot_owner(pl)
    if owner:
        stats["sot"] += 1
        cache[sid] = [owner, None, sot_cover]
        return ("editorial" if owner.lower() == "spotify" else "independent"), owner, sot_cover

    if spotify_allowed():
        res = sp_lookup(sid)
        if res[0] == "ok":
            stats["spotify"] += 1
            _, oid, oname, cover = res
            if oid == "spotify":
                cache[sid] = ["Spotify", None, cover or sot_cover]
                return "editorial", "Spotify", cover or sot_cover
            cache[sid] = [oname or "Unknown curator", None, cover or sot_cover]
            return "independent", oname or "Unknown curator", cover or sot_cover
        if res[0] == "missing":
            stats["not_found"] += 1
            cache[sid] = [UNKNOWN, None, sot_cover]   # deleted/private: stop retrying
            return "independent", "Unknown curator", sot_cover

    stats["unresolved"] += 1
    return None, "", sot_cover


def build_curator_index(doc):
    return {c["owner_name"].strip().lower(): c for c in (doc or {}).get("curators", [])}


def fetch_shazam(isrc):
    total = daily = None
    charts = []
    try:
        sz = sot_get(f"/tracks/{isrc}/shazam/shazams")
        if sz:
            total, daily = sz[0].get("total"), sz[0].get("daily")
        for c in sot_get(f"/tracks/{isrc}/shazam/charts/current") or []:
            charts.append({k: c.get(k) for k in ("country_code", "type", "position", "previous_position", "genre", "city")})
    except Exception as e:
        print(f"  [shazam] skipped: {e}")
    return total, daily, charts


def process_track(entry, curator_idx, cache):
    isrc = entry["isrc"]
    current = sot_get(f"/tracks/{isrc}/spotify/playlists/current") or []
    current = [p for p in current if ((p.get("playlist") or {}).get("followers") or 0) >= MIN_FOLLOWERS]
    if current and not _logged_keys[0]:
        _logged_keys[0] = True
        print("  [sot] playlist object keys:", sorted(current[0]["playlist"].keys()))
        print("  [sot] owner field found:", sot_owner(current[0]["playlist"]) is not None,
              "| image field found:", sot_image(current[0]["playlist"]) is not None)

    editorial, independent, unclassified = [], [], 0
    for p in current:
        pl = p["playlist"]
        kind, owner, cover = classify(pl, cache)
        row = {"name": pl.get("name"), "spotify_id": pl["spotify_id"],
               "followers": pl.get("followers") or 0, "cover_url": cover}
        if kind == "editorial":
            row["owner_name"] = "Spotify"
            editorial.append(row)
        elif kind == "independent":
            row["owner_name"] = owner
            independent.append(row)
        else:
            unclassified += 1

    supporters = []
    for p in independent:
        m = curator_idx.get(p["owner_name"].strip().lower())
        if m:
            supporters.append({**p, "tier": m["tier"], "notes": m.get("notes", "")})
    supporters.sort(key=lambda k: (k["tier"], -k["followers"]))
    editorial.sort(key=lambda k: -k["followers"])
    independent.sort(key=lambda k: -k["followers"])

    total, daily, charts = fetch_shazam(isrc)
    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "official": IS_OFFICIAL,
        "editorial_count": len(editorial),
        "editorial_playlists": editorial,
        "independent_playlists": independent,
        "independent_followers_total": sum(p["followers"] for p in independent),
        "key_supporters": supporters,
        "unclassified_count": unclassified,
        "total_shazams": total,
        "daily_shazams": daily,
        "shazam_charts": charts,
    }


def last_official(snaps):
    o = [s for s in snaps if s.get("official")]
    return o[-1] if o else None


def delta(new, old):
    return None if new is None or old is None else new - old


def main():
    if not SOT_KEY:
        print("Missing SOT_API_KEY.")
        sys.exit(1)
    if not (SP_ID and SP_SECRET) or SPOTIFY_PER_RUN <= 0:
        print("Spotify fallback disabled: relying on Spot On Track owner names + cache only.")

    tracked = load_json(TRACKED_FP, {"tracks": []})
    history = load_json(HISTORY_FP, {"snapshots": {}})
    curator_idx = build_curator_index(load_json(CURATORS_FP, {}))
    cache = load_json(CACHE_FP, {}) or {}
    load_call_log()
    by_isrc = history.setdefault("snapshots", {})

    ok = failed = 0
    for entry in tracked.get("tracks", []):
        isrc = entry["isrc"]
        print(f"Syncing {entry.get('artist','?')} - {entry.get('track','?')} ({isrc})")
        try:
            snap = process_track(entry, curator_idx, cache)
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1
            continue
        ok += 1
        prior = by_isrc.setdefault(isrc, [])
        po = last_official(prior)
        snap["editorial_count_delta"] = delta(snap["editorial_count"], po and po.get("editorial_count"))
        snap["independent_followers_delta"] = delta(snap["independent_followers_total"], po and po.get("independent_followers_total"))
        snap["total_shazams_delta"] = delta(snap["total_shazams"], po and po.get("total_shazams"))
        snap["ai_read"] = None
        prior.append(snap)
        by_isrc[isrc] = prior[-52:]
        print(f"  editorial={snap['editorial_count']} independent={len(snap['independent_playlists'])} "
              f"unclassified={snap['unclassified_count']}")
        # checkpoint after every track so a late failure never loses earlier work
        save_json(HISTORY_FP, history)
        save_json(CACHE_FP, cache)
        save_json(CALLLOG_FP, _call_log)

    save_json(HISTORY_FP, history)
    save_json(CACHE_FP, cache)
    save_json(CALLLOG_FP, _call_log)
    print(f"\nSUMMARY tracks ok={ok} failed={failed} | classified from: id-prefix={stats['editorial_id']} "
          f"cache={stats['cache']} SOT={stats['sot']} Spotify={stats['spotify']} "
          f"not_found={stats['not_found']} | unresolved={stats['unresolved']} | "
          f"Spotify calls this run={_run_spotify_calls[0]} (24h window total {_call_log['calls']})"
          f"{' | SPOTIFY QUOTA WALL HIT' if stats['spotify_blocked'] else ''}")
    if ok == 0 and failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
