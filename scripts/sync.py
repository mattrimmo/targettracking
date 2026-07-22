#!/usr/bin/env python3
"""
Long Term Target Tracking — sync script.

Runs inside GitHub Actions (scheduled every Wednesday, or on-demand via the
"Check live now" button in the app). Reads the tracked track list, pulls
current playlist placements from Spot On Track, enriches each placement with
its Spotify owner name + follower count, classifies editorial vs independent,
matches independent curators against the master list, works out week-on-week
movement, and writes the result back to data/history.json.

Secrets (set as GitHub Actions repo secrets, never committed):
  SOT_API_KEY            — Spot On Track bearer token
  SPOTIFY_CLIENT_ID       — Spotify app client id
  SPOTIFY_CLIENT_SECRET   — Spotify app client secret
"""
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKED_FP  = os.path.join(REPO_ROOT, "data", "tracked.json")
HISTORY_FP  = os.path.join(REPO_ROOT, "data", "history.json")
CURATORS_FP = os.path.join(REPO_ROOT, "data", "curators.json")

SOT_KEY   = os.environ.get("SOT_API_KEY", "")
SP_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SP_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
# "true" on the scheduled Wednesday run, "false" on an ad-hoc manual run
IS_OFFICIAL = os.environ.get("IS_OFFICIAL", "false").lower() == "true"

MIN_FOLLOWERS = 100  # same floor as the report generator


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def sot_get(path):
    r = requests.get(
        "https://www.spotontrack.com/api/v1" + path,
        headers={"Authorization": "Bearer " + SOT_KEY},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


_sp_token = None
_sp_exp = 0


def sp_token():
    global _sp_token, _sp_exp
    if _sp_token and time.time() < _sp_exp:
        return _sp_token
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={
            "Authorization": "Basic "
            + base64.b64encode(f"{SP_ID}:{SP_SECRET}".encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    _sp_token = data["access_token"]
    _sp_exp = time.time() + data["expires_in"] - 30
    return _sp_token


def sp_enrich(spotify_id):
    """Returns (owner_display_name, total_tracks). Retries once on 429."""
    for attempt in range(2):
        tok = sp_token()
        r = requests.get(
            f"https://api.spotify.com/v1/playlists/{spotify_id}",
            headers={"Authorization": "Bearer " + tok},
            params={"fields": "owner.display_name,tracks.total"},
            timeout=30,
        )
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 0.5)
            continue
        if not r.ok:
            return "", None
        d = r.json()
        owner = (d.get("owner") or {}).get("display_name") or ""
        total = (d.get("tracks") or {}).get("total")
        return owner, total
    return "", None


def build_curator_index(curators_doc):
    idx = {}
    for c in curators_doc.get("curators", []):
        idx[c["owner_name"].strip().lower()] = c
    return idx


def process_track(entry, curator_idx):
    isrc = entry["isrc"]
    current = sot_get(f"/tracks/{isrc}/spotify/playlists/current")
    current = [p for p in current if (p.get("playlist", {}).get("followers") or 0) >= MIN_FOLLOWERS]

    editorial_playlists = []
    independent_playlists = []

    for p in current:
        pl = p["playlist"]
        owner, _total = sp_enrich(pl["spotify_id"])
        row = {
            "name": pl["name"],
            "spotify_id": pl["spotify_id"],
            "followers": pl.get("followers") or 0,
        }
        if owner.strip() == "":
            editorial_playlists.append(row)
        else:
            row["owner_name"] = owner
            independent_playlists.append(row)

    independent_followers_total = sum(p["followers"] for p in independent_playlists)

    key_supporters = []
    for p in independent_playlists:
        match = curator_idx.get(p["owner_name"].strip().lower())
        if match:
            key_supporters.append({**p, "tier": match["tier"], "notes": match.get("notes", "")})
    key_supporters.sort(key=lambda k: (k["tier"], -k["followers"]))

    editorial_playlists.sort(key=lambda k: -k["followers"])
    independent_playlists.sort(key=lambda k: -k["followers"])

    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "official": IS_OFFICIAL,
        "editorial_count": len(editorial_playlists),
        "editorial_playlists": editorial_playlists,
        "independent_playlists": independent_playlists,
        "independent_followers_total": independent_followers_total,
        "key_supporters": key_supporters,
    }


def last_official(snapshots):
    officials = [s for s in snapshots if s.get("official")]
    return officials[-1] if officials else None


def main():
    if not (SOT_KEY and SP_ID and SP_SECRET):
        print("Missing one or more required secrets (SOT_API_KEY / SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET).")
        sys.exit(1)

    tracked   = load_json(TRACKED_FP)
    history   = load_json(HISTORY_FP)
    curators  = load_json(CURATORS_FP)
    curator_idx = build_curator_index(curators)

    snapshots_by_isrc = history.setdefault("snapshots", {})

    for entry in tracked.get("tracks", []):
        isrc = entry["isrc"]
        print(f"Syncing {entry.get('artist','?')} - {entry.get('track','?')} ({isrc})")
        try:
            snap = process_track(entry, curator_idx)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        prior_list = snapshots_by_isrc.setdefault(isrc, [])
        prior_official = last_official(prior_list)
        if prior_official:
            snap["editorial_count_delta"] = snap["editorial_count"] - prior_official["editorial_count"]
            snap["independent_followers_delta"] = (
                snap["independent_followers_total"] - prior_official["independent_followers_total"]
            )
        else:
            snap["editorial_count_delta"] = None
            snap["independent_followers_delta"] = None

        prior_list.append(snap)
        # Keep at most 52 snapshots per track (a year of weekly history) to keep the file small
        snapshots_by_isrc[isrc] = prior_list[-52:]

    save_json(HISTORY_FP, history)
    print("Done.")


if __name__ == "__main__":
    main()
