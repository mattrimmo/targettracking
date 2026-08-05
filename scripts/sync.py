#!/usr/bin/env python3
"""
Campaign Dashboard — sync script.

Runs daily via GitHub Actions (see .github/workflows/sync.yml). Reads
data/campaigns.json (the tracked list — client, artist, track, ISRC,
status), pulls current metrics from Spot On Track and Spotify for every
"active" campaign, and appends a dated snapshot to data/history.json.

Campaigns with status != "active" (i.e. archived / stopped) are skipped
entirely — no API calls are made for them, and their existing history is
left untouched. This is the same discipline as the targettracking repo's
sync.py: once you're done working a record, archive it and it stops
costing API credits forever.

Secrets (set as GitHub Actions repo secrets):
  SOT_API_KEY            — Spot On Track bearer token
  SPOTIFY_CLIENT_ID       — Spotify app client id
  SPOTIFY_CLIENT_SECRET   — Spotify app client secret

Metric choices, deliberately:
  - "reach" is the summed follower count of playlists the track is
    CURRENTLY placed on (independent + editorial), not artist followers.
    Artist followers climb regardless of any one campaign's performance
    (Release Radar, general artist growth), so they're a weak signal for
    "did this campaign work." Playlist reach only grows when you land a
    new placement — same logic as targettracking's independent_followers_total.
  - "estimated_revenue_gbp" is exactly that — an ESTIMATE, built from Spot
    On Track's total stream count for the ISRC multiplied by a configurable
    blended per-stream rate (PER_STREAM_RATE_GBP below). Actual payouts
    depend on listener geography, subscription tier, and your distributor
    deal, none of which any API exposes — treat this as a directional
    figure, never a real royalty statement number.
"""
import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

REPO_ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAMPAIGNS_FP  = os.path.join(REPO_ROOT, "data", "campaigns.json")
HISTORY_FP    = os.path.join(REPO_ROOT, "data", "history.json")
PLAYLIST_CACHE_FP = os.path.join(REPO_ROOT, "data", "playlist_cache.json")

SOT_KEY   = os.environ.get("SOT_API_KEY", "")
SP_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SP_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

MIN_FOLLOWERS = 100  # same floor as the report generator / LTT

# Checked against multiple current 2026 sources: Spotify pays roughly $0.003-0.005
# per stream, averaging ~$0.004 in the US and ~$0.0044 in the UK — ~£0.003 at
# current USD/GBP either way, which is what this is set to. Still a blended
# average, not a real rate — adjust if you have better data on a client's
# actual distributor deal or audience geography.
#
# IMPORTANT CAVEAT: Spotify pays nothing at all on a track until it clears
# 1,000 streams in a rolling 12-month window. For a brand-new campaign with
# only a handful of streams so far, this estimate will show a small non-zero
# number that doesn't reflect any money that's actually arrived yet.
PER_STREAM_RATE_GBP = 0.003


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        return json.loads(content) if content else default


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
_playlist_cache = {}  # spotify_id -> owner — loaded from PLAYLIST_CACHE_FP at
                       # startup so it survives between runs, not just within
                       # one. Ownership rarely changes, so once checked a
                       # playlist is essentially never re-fetched from Spotify.


def load_playlist_cache():
    global _playlist_cache
    _playlist_cache = load_json(PLAYLIST_CACHE_FP, {})
    print(f"Loaded playlist cache: {len(_playlist_cache)} known playlists.")


def save_playlist_cache():
    save_json(PLAYLIST_CACHE_FP, _playlist_cache)


def git_checkpoint(message):
    """Commits + pushes whatever's currently on disk after every campaign,
    so a timeout only loses the current campaign's work, and the next run
    starts from a warmer playlist cache instead of from scratch."""
    try:
        subprocess.run(["git", "add", "data/history.json", "data/playlist_cache.json"], cwd=REPO_ROOT, check=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT)
        if result.returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", message], cwd=REPO_ROOT, check=True)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=REPO_ROOT, check=True)
        subprocess.run(["git", "push"], cwd=REPO_ROOT, check=True)
        print(f"  [checkpoint] {message}")
    except subprocess.CalledProcessError as e:
        print(f"  [checkpoint] git step failed, continuing anyway: {e}")


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


def sp_owner(spotify_id):
    """Returns playlist owner display name ('' means editorial/Spotify-owned).
    Cached per spotify_id for the lifetime of this run."""
    if spotify_id in _playlist_cache:
        return _playlist_cache[spotify_id]
    for attempt in range(2):
        tok = sp_token()
        r = requests.get(
            f"https://api.spotify.com/v1/playlists/{spotify_id}",
            headers={"Authorization": "Bearer " + tok},
            params={"fields": "owner.display_name"},
            timeout=30,
        )
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 0.5)
            continue
        if not r.ok:
            print(f"  [debug] Spotify playlist lookup failed for {spotify_id}: {r.status_code}")
            _playlist_cache[spotify_id] = ""
            return ""
        owner = (r.json().get("owner") or {}).get("display_name") or ""
        _playlist_cache[spotify_id] = owner
        return owner
    _playlist_cache[spotify_id] = ""
    return ""


def process_campaign(entry):
    isrc = entry["isrc"]

    streams = sot_get(f"/tracks/{isrc}/spotify/streams")
    current = sot_get(f"/tracks/{isrc}/spotify/playlists/current")
    current = [p for p in current if (p.get("playlist", {}).get("followers") or 0) >= MIN_FOLLOWERS]

    total_streams = streams[0]["total"] if streams else None
    daily_streams = streams[0]["daily"] if streams else None

    editorial_reach = 0
    independent_reach = 0
    editorial_count = 0
    independent_count = 0

    for p in current:
        pl = p["playlist"]
        followers = pl.get("followers") or 0
        owner = sp_owner(pl["spotify_id"])
        if owner.strip() == "":
            editorial_reach += followers
            editorial_count += 1
        else:
            independent_reach += followers
            independent_count += 1

    estimated_revenue_gbp = (
        round(total_streams * PER_STREAM_RATE_GBP, 2) if total_streams is not None else None
    )

    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_streams": total_streams,
        "daily_streams": daily_streams,
        "editorial_count": editorial_count,
        "independent_count": independent_count,
        "editorial_reach": editorial_reach,
        "independent_reach": independent_reach,
        "estimated_revenue_gbp": estimated_revenue_gbp,
        "estimated_revenue_rate_gbp": PER_STREAM_RATE_GBP,
    }


def main():
    if not (SOT_KEY and SP_ID and SP_SECRET):
        print("Missing one or more required secrets (SOT_API_KEY / SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET).")
        sys.exit(1)

    load_playlist_cache()
    campaigns = load_json(CAMPAIGNS_FP, [])
    history = load_json(HISTORY_FP, {})

    for entry in campaigns:
        if entry.get("status") != "active":
            continue  # archived — no calls, history left as-is

        cid = entry["id"]
        cache_size_before = len(_playlist_cache)
        print(f"Syncing {entry.get('client','?')} / {entry.get('artist','?')} - {entry.get('track','?')} ({entry.get('isrc')})")
        try:
            snap = process_campaign(entry)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        history.setdefault(cid, [])
        # Avoid duplicate snapshots if the workflow runs twice in a day
        history[cid] = [h for h in history[cid] if h["date"] != snap["date"]]
        history[cid].append(snap)
        print(f"  -> {snap}")

        # Checkpoint after every campaign — see git_checkpoint() docstring.
        new_playlists = len(_playlist_cache) - cache_size_before
        save_json(HISTORY_FP, history)
        save_playlist_cache()
        git_checkpoint(f"Sync checkpoint: {entry.get('client','?')} - {entry.get('track','?')} ({new_playlists} new playlist lookups)")

    print(f"Done. Playlist cache now holds {len(_playlist_cache)} known playlists.")


if __name__ == "__main__":
    main()
