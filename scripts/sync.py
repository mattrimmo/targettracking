#!/usr/bin/env python3
"""
Long Term Target Tracking — sync script (rebuilt).

Runs weekly (official Wednesday snapshot) or on-demand via "Check live now"
(unofficial). Reads the tracked track list, pulls current playlist
placements from Spot On Track, classifies each as editorial or independent
via Spotify, tracks week-on-week movement, and layers in Shazam data +
an AI pitch-readiness read.

Built against the original grandfathered Spotify app — shared with the
report generator and (paused) Campaign Dashboard — so this is deliberately
throttled to be a good citizen of that shared quota, not just fast.

Secrets:
  SOT_API_KEY            — Spot On Track bearer token
  SPOTIFY_CLIENT_ID       — Spotify app client id
  SPOTIFY_CLIENT_SECRET   — Spotify app client secret
  ANTHROPIC_API_KEY       — optional; AI reads skip gracefully without it

Quota protection, two layers:
  1. PACING_DELAY_SECONDS — a small fixed wait before every Spotify call,
     all the time, not just after getting rate-limited. Keeps us well
     under the ceiling instead of sprinting into it.
  2. A persistent playlist-ownership cache (data/playlist_cache.json) —
     once a playlist's owner is known, it's essentially never re-fetched.
     This is the real long-term protection: as the cache fills in over
     the coming weeks, the number of genuinely new Spotify calls per run
     should trend toward zero.
  3. DAILY_CALL_CIRCUIT_BREAKER is a generous safety net (a bug runaway
     stopper), not active rationing — it should basically never trigger
     under normal use against the old app's known-good quota.
"""
import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests

REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKED_FP  = os.path.join(REPO_ROOT, "data", "tracked.json")
HISTORY_FP  = os.path.join(REPO_ROOT, "data", "history.json")
CURATORS_FP = os.path.join(REPO_ROOT, "data", "curators.json")
PLAYLIST_CACHE_FP = os.path.join(REPO_ROOT, "data", "playlist_cache.json")
CALL_LOG_FP = os.path.join(REPO_ROOT, "data", "spotify_call_log.json")

SOT_KEY   = os.environ.get("SOT_API_KEY", "")
SP_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "")
SP_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
IS_OFFICIAL = os.environ.get("IS_OFFICIAL", "false").lower() == "true"

MIN_FOLLOWERS = 100
PACING_DELAY_SECONDS = 0.2          # deliberate throttle, every Spotify call

# Set conservatively below the ~4 tracks that fully succeeded before SOT's
# limit walled off completely in the last real run. Adjust upward once
# there's a clearer sense of the actual ceiling — this is a safe starting
# guess, not a measured number.
MAX_TRACKS_PER_RUN = 3
# Was a rough guess of 3000 originally. Real evidence from an actual run:
# ~115 new lookups succeeded before Spotify's own quota wall kicked in
# (cache grew 609 -> ~724 before the 429s started, then every subsequent
# call failed instantly). Set with a margin below that measured number so
# this circuit breaker trips before Spotify's real wall does, rather than
# continuing to hammer a wall we already know is there. One track landing
# on an unusually large number of playlists (as happened here) can burn
# the whole day's allowance on its own — this is a total-calls limit,
# not a per-track one, which is why it's separate from MAX_TRACKS_PER_RUN.
DAILY_CALL_CIRCUIT_BREAKER = 90
CALL_LOG_WINDOW_HOURS = 24

_logged_sample_keys = [False]


# ─── generic json helpers ───────────────────────────────────────────────
def load_json(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else {}
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        return json.loads(content) if content else (default if default is not None else {})


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ─── persistent playlist cache ──────────────────────────────────────────
_playlist_cache = {}


def load_playlist_cache():
    global _playlist_cache
    raw = load_json(PLAYLIST_CACHE_FP, {})
    _playlist_cache = {k: tuple(v) for k, v in raw.items()}
    print(f"Loaded playlist cache: {len(_playlist_cache)} known playlists.")


def save_playlist_cache():
    save_json(PLAYLIST_CACHE_FP, {k: list(v) for k, v in _playlist_cache.items()})


# ─── circuit breaker (safety net, not rationing) ────────────────────────
_call_log = {"window_start": None, "calls": 0}


def load_call_log():
    global _call_log
    raw = load_json(CALL_LOG_FP, {})
    window_start = raw.get("window_start")
    calls = raw.get("calls", 0)
    if window_start:
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(window_start)).total_seconds() / 3600
        if age_h >= CALL_LOG_WINDOW_HOURS:
            window_start, calls = None, 0
    if not window_start:
        window_start = datetime.now(timezone.utc).isoformat()
        calls = 0
    _call_log = {"window_start": window_start, "calls": calls}
    print(f"Spotify calls this window: {calls}/{DAILY_CALL_CIRCUIT_BREAKER} (safety net, not a ration).")
    if calls >= DAILY_CALL_CIRCUIT_BREAKER:
        hours_left = CALL_LOG_WINDOW_HOURS - (datetime.now(timezone.utc) - datetime.fromisoformat(window_start)).total_seconds() / 3600
        print(f"WARNING: stored call count already meets/exceeds the current cap — this ENTIRE run will make "
              f"zero new-lookup progress (cache hits still work fine). This happens if DAILY_CALL_CIRCUIT_BREAKER "
              f"was just lowered below an already-accumulated count. Window resets in ~{hours_left:.1f}h, or "
              f"manually reset data/spotify_call_log.json to {{}} to unblock immediately.")


def save_call_log():
    save_json(CALL_LOG_FP, _call_log)


def circuit_breaker_tripped():
    return _call_log["calls"] >= DAILY_CALL_CIRCUIT_BREAKER


def log_call():
    _call_log["calls"] += 1


# ─── git checkpointing ──────────────────────────────────────────────────
def git_checkpoint(message):
    try:
        subprocess.run(
            ["git", "add", "data/history.json", "data/playlist_cache.json", "data/spotify_call_log.json"],
            cwd=REPO_ROOT, check=True,
        )
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=REPO_ROOT).returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", message], cwd=REPO_ROOT, check=True)
        subprocess.run(["git", "pull", "--rebase", "origin", "main"], cwd=REPO_ROOT, check=True)
        subprocess.run(["git", "push"], cwd=REPO_ROOT, check=True)
        print(f"  [checkpoint] {message}")
    except subprocess.CalledProcessError as e:
        print(f"  [checkpoint] git step failed, continuing anyway: {e}")


# ─── Spot On Track ───────────────────────────────────────────────────────
SOT_PACING_DELAY_SECONDS = 0.15  # same reasoning as the Spotify pacing delay


def sot_get(path):
    for attempt in range(2):
        time.sleep(SOT_PACING_DELAY_SECONDS)
        r = requests.get(
            "https://www.spotontrack.com/api/v1" + path,
            headers={"Authorization": "Bearer " + SOT_KEY},
            timeout=30,
        )
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "2"))
            if wait > 30:
                # Same rule as the Spotify side: never blindly sleep out a
                # long server-specified cooldown inside a CI job — that's
                # what caused the multi-hour hangs before. Fail this call
                # fast instead.
                r.raise_for_status()
            time.sleep(wait + 0.5)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


# ─── Spotify ─────────────────────────────────────────────────────────────
_sp_token = None
_sp_exp = 0


def sp_token():
    global _sp_token, _sp_exp
    if _sp_token and time.time() < _sp_exp:
        return _sp_token
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={
            "Authorization": "Basic " + base64.b64encode(f"{SP_ID}:{SP_SECRET}".encode()).decode(),
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
    """Returns (owner_display_name, total_tracks, cover_url).
    Cache hit -> free, no call, no delay. Otherwise: pace, call, cache."""
    if spotify_id in _playlist_cache:
        return _playlist_cache[spotify_id]
    if circuit_breaker_tripped():
        print(f"  [debug] circuit breaker tripped ({DAILY_CALL_CIRCUIT_BREAKER} calls this window) — skipping {spotify_id}, will retry next run")
        return (None, None, None)  # not cached — genuinely not checked yet

    for attempt in range(2):
        time.sleep(PACING_DELAY_SECONDS)  # deliberate pacing, every call
        tok = sp_token()
        r = requests.get(
            f"https://api.spotify.com/v1/playlists/{spotify_id}",
            headers={"Authorization": "Bearer " + tok},
            params={"fields": "owner.display_name,tracks.total,images"},
            timeout=30,
        )
        log_call()
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", "2"))
            if wait > 30:
                # This means "we don't know yet", not "confirmed editorial" —
                # must NOT write "" (empty owner) to the cache here, since ""
                # is exactly what a real editorial playlist looks like once
                # genuinely checked. Doing that previously mis-classified
                # every playlist hit by a quota wall as editorial, permanently,
                # since it got baked into the persistent cache. Return the
                # same uncached "pending" sentinel the circuit breaker uses
                # instead, so it gets a real check on a future run.
                print(f"  [debug] Spotify quota signal for {spotify_id} — Retry-After {wait}s, treating as pending (not cached)")
                return (None, None, None)
            time.sleep(wait + 0.5)
            continue
        if not r.ok:
            # Genuinely gone (404) or some other real error — worth caching
            # permanently, since there's no point re-checking a deleted
            # playlist every run, but "" is the wrong value to use: that's
            # exactly what a confirmed editorial playlist looks like. Use a
            # distinct marker so this gets excluded from both counts
            # instead of silently miscounted as editorial.
            print(f"  [debug] Spotify playlist lookup failed for {spotify_id}: {r.status_code} {r.text[:150]}")
            _playlist_cache[spotify_id] = ("__UNKNOWN__", None, None)
            return _playlist_cache[spotify_id]
        d = r.json()
        owner = (d.get("owner") or {}).get("display_name") or ""
        total = (d.get("tracks") or {}).get("total")
        images = d.get("images") or []
        cover = images[0]["url"] if images else None
        _playlist_cache[spotify_id] = (owner, total, cover)
        return _playlist_cache[spotify_id]
    return (None, None, None)  # retries exhausted — pending, not confirmed editorial


def sp_label(album_id):
    """Best-effort label lookup — Spotify removed this field from the API
    in Feb 2026 and it may return nothing. Left in in case it's restored."""
    try:
        time.sleep(PACING_DELAY_SECONDS)
        tok = sp_token()
        r = requests.get(
            f"https://api.spotify.com/v1/albums/{album_id}",
            headers={"Authorization": "Bearer " + tok},
            params={"fields": "label"},
            timeout=30,
        )
        log_call()
        if r.ok:
            label = r.json().get("label")
            if label and label != "INDEPENDENT":
                return label.upper()
    except Exception:
        pass
    return None


# ─── curators ────────────────────────────────────────────────────────────
def build_curator_index(curators_doc):
    idx = {}
    for c in curators_doc.get("curators", []):
        idx[c["owner_name"].strip().lower()] = c
    return idx


# ─── Shazam (Spot On Track only, no Spotify cost) ───────────────────────
def sot_shazam(isrc):
    shazams = sot_get(f"/tracks/{isrc}/shazam/shazams")
    total = shazams[0]["total"] if shazams else None
    daily = shazams[0]["daily"] if shazams else None
    charts = sot_get(f"/tracks/{isrc}/shazam/charts/current")
    chart_positions = [
        {
            "country_code": c.get("country_code"), "type": c.get("type"),
            "position": c.get("position"), "previous_position": c.get("previous_position"),
            "genre": c.get("genre"), "city": c.get("city"),
        }
        for c in charts
    ]
    return total, daily, chart_positions


# ─── AI read — pitch-readiness, not just trend description ─────────────
def generate_ai_read(artist, track, label, snap, prior_official):
    if not ANTHROPIC_KEY:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        editorial_delta = snap.get("editorial_count_delta")
        indie_delta = snap.get("independent_followers_delta")
        shazam_delta = snap.get("total_shazams_delta")

        prompt = f"""Track: {artist} - {track} (label: {label}).

This week: {snap['editorial_count']} editorial playlists (change: {editorial_delta}),
{snap['independent_followers_total']:,} independent playlist followers (change: {indie_delta}),
{snap.get('total_shazams') or 0:,} total Shazams (change: {shazam_delta}).
{snap.get('unclassified_count', 0)} playlists not yet classified (budget/cache pending).

You're advising a UK dance/house playlist promotion company on whether this
track is ready to approach the artist/label about working it. Their actual
judgment criteria: a track holding steady or growing on INDEPENDENT
placements (not just editorial, which is algorithmic and can fade fast) is
a strong positive sign. A track with zero editorial support but real
independent traction is often the best kind of opportunity, not a gap.
A drop-off after week 1-2 driven specifically by editorial playlists
falling away (independent holding) is a normal pattern, not a red flag.
Genuine decline across independent AND Shazam together is the real warning
sign.

In one direct sentence (under 30 words), tell them: is this worth
approaching now, worth watching a bit longer, or not there yet — and why,
referencing the actual shape of editorial vs independent vs Shazam, not a
generic trend summary."""

        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"  [debug] AI read failed: {e}")
        return None


# ─── core per-track processing ──────────────────────────────────────────
def process_track(entry, curator_idx):
    isrc = entry["isrc"]
    current = sot_get(f"/tracks/{isrc}/spotify/playlists/current")
    current = [p for p in current if (p.get("playlist", {}).get("followers") or 0) >= MIN_FOLLOWERS]

    editorial_playlists, independent_playlists = [], []
    unclassified_count = 0
    lookups_since_checkpoint = 0

    for p in current:
        pl = p["playlist"]
        if not _logged_sample_keys[0]:
            print(f"  [debug] sample SOT playlist object keys: {list(pl.keys())}")
            _logged_sample_keys[0] = True

        was_cached = pl["spotify_id"] in _playlist_cache
        owner, _total, cover = sp_enrich(pl["spotify_id"])
        if owner is None:
            unclassified_count += 1
            print(f"  [debug] playlist='{pl['name']}' -> pending (lookup budget exhausted this window)")
            continue
        if owner == "__UNKNOWN__":
            unclassified_count += 1
            print(f"  [debug] playlist='{pl['name']}' -> excluded (Spotify says this playlist no longer exists)")
            continue

        classification = "editorial" if owner.strip() == "" else "independent"
        print(f"  [debug] playlist='{pl['name']}' owner='{owner}' -> {classification}")
        row = {"name": pl["name"], "spotify_id": pl["spotify_id"], "followers": pl.get("followers") or 0, "cover_url": cover}
        if owner.strip() == "":
            row["owner_name"] = "Spotify"
            editorial_playlists.append(row)
        else:
            row["owner_name"] = owner
            independent_playlists.append(row)

        # Mid-track checkpoint every 15 NEW lookups (cache hits are free and
        # don't count) — a single track can have 100+ playlists, so a run
        # cut off mid-track still keeps everything found so far, and the
        # next run starts from a warmer cache instead of from scratch.
        if not was_cached:
            lookups_since_checkpoint += 1
            if lookups_since_checkpoint >= 15:
                save_playlist_cache()
                save_call_log()
                git_checkpoint(f"Mid-track checkpoint: {len(_playlist_cache)} playlists cached")
                lookups_since_checkpoint = 0

    independent_followers_total = sum(p["followers"] for p in independent_playlists)

    key_supporters = []
    for p in independent_playlists:
        match = curator_idx.get(p["owner_name"].strip().lower())
        if match:
            key_supporters.append({**p, "tier": match["tier"], "notes": match.get("notes", "")})
    key_supporters.sort(key=lambda k: (k["tier"], -k["followers"]))
    editorial_playlists.sort(key=lambda k: -k["followers"])
    independent_playlists.sort(key=lambda k: -k["followers"])

    try:
        total_shazams, daily_shazams, shazam_charts = sot_shazam(isrc)
    except Exception as e:
        print(f"  [debug] Shazam lookup failed: {e}")
        total_shazams, daily_shazams, shazam_charts = None, None, []

    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "official": IS_OFFICIAL,
        "editorial_count": len(editorial_playlists),
        "editorial_playlists": editorial_playlists,
        "independent_playlists": independent_playlists,
        "independent_followers_total": independent_followers_total,
        "key_supporters": key_supporters,
        "unclassified_count": unclassified_count,
        "total_shazams": total_shazams,
        "daily_shazams": daily_shazams,
        "shazam_charts": shazam_charts,
    }


def last_official(snapshots):
    officials = [s for s in snapshots if s.get("official")]
    return officials[-1] if officials else None


def main():
    if not (SOT_KEY and SP_ID and SP_SECRET):
        print("Missing one or more required secrets (SOT_API_KEY / SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET).")
        sys.exit(1)
    if not ANTHROPIC_KEY:
        print("No ANTHROPIC_API_KEY set — AI reads will be skipped, everything else still runs.")

    load_playlist_cache()
    load_call_log()
    tracked = load_json(TRACKED_FP, {"tracks": []})
    history = load_json(HISTORY_FP, {})
    curators = load_json(CURATORS_FP, {"curators": []})
    curator_idx = build_curator_index(curators)

    snapshots_by_isrc = history.setdefault("snapshots", {})

    # Never-snapshotted tracks first. Spot On Track calls aren't cached or
    # rationed the way Spotify calls are — every track gets fully re-queried
    # every run, in whatever order the list is in. If SOT's own rate limit
    # gets hit partway through a run (as observed), whatever's positioned
    # after that point never gets reached — and since new tracks always get
    # appended to the END of the list, they'd be the ones silently starved,
    # every single run, not just occasionally. Processing brand-new tracks
    # first means a partial run costs an already-established track its
    # update, not a track that's never had one at all.
    all_tracks = tracked.get("tracks", [])

    # The playlist_cache.json cleanup (2026-08-12) fixed the underlying
    # cache, but never touched already-recorded history.json snapshots —
    # a track's dashboard card shows whatever its LATEST snapshot says,
    # frozen at whenever that track last actually ran. Any track not
    # re-synced since the fix is still displaying pre-fix, potentially
    # mis-classified data, even though the cache underneath it is clean
    # now. Treat those the same as genuinely never-synced tracks so the
    # whole list gets a fresh pass, not just brand-new additions. Safe to
    # remove this cutoff once satisfied every track has had a post-fix run.
    CACHE_FIX_DATE = "2026-08-12"
    def needs_priority(t):
        snaps = snapshots_by_isrc.get(t["isrc"])
        if not snaps:
            return True
        return snaps[-1].get("date", "") < CACHE_FIX_DATE

    never_synced = [t for t in all_tracks if needs_priority(t)]
    already_synced = [t for t in all_tracks if not needs_priority(t)]
    ordered_tracks = never_synced + already_synced
    if never_synced:
        print(f"Prioritising {len(never_synced)} track(s) needing a fresh pass (never-synced or pre-fix data): "
              + ", ".join(f"{t.get('artist','?')} - {t.get('track','?')}" for t in never_synced))

    # SOT's own limit looks like a hard quota-per-period, not a smooth
    # per-second throttle — a run works fine for several tracks then walls
    # off completely and stays walled for the rest of the run. Pacing
    # alone can't fix that, so cap how many tracks get FULLY processed in
    # one run and leave the rest for the next one. Combined with the
    # never-synced-first ordering above, new tracks get covered well
    # within this cap; already-synced tracks just take a couple of extra
    # runs to all get refreshed, which is fine at weekly/daily cadence.
    if len(ordered_tracks) > MAX_TRACKS_PER_RUN:
        deferred = ordered_tracks[MAX_TRACKS_PER_RUN:]
        ordered_tracks = ordered_tracks[:MAX_TRACKS_PER_RUN]
        print(f"Capping this run to {MAX_TRACKS_PER_RUN} tracks — deferring to next run: "
              + ", ".join(f"{t.get('artist','?')} - {t.get('track','?')}" for t in deferred))

    for entry in ordered_tracks:
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
            snap["independent_followers_delta"] = snap["independent_followers_total"] - prior_official["independent_followers_total"]
            if snap.get("total_shazams") is not None and prior_official.get("total_shazams") is not None:
                snap["total_shazams_delta"] = snap["total_shazams"] - prior_official["total_shazams"]
            else:
                snap["total_shazams_delta"] = None
        else:
            snap["editorial_count_delta"] = None
            snap["independent_followers_delta"] = None
            snap["total_shazams_delta"] = None

        snap["ai_read"] = generate_ai_read(
            entry.get("artist", ""), entry.get("track", ""), entry.get("label", "INDEPENDENT"),
            snap, prior_official,
        )

        prior_list.append(snap)
        snapshots_by_isrc[isrc] = prior_list[-52:]

        save_json(HISTORY_FP, history)
        save_playlist_cache()
        save_call_log()
        git_checkpoint(f"Sync checkpoint: {entry.get('artist','?')} - {entry.get('track','?')}")

    print(f"Done. Playlist cache: {len(_playlist_cache)} known. Spotify calls this window: {_call_log['calls']}.")


if __name__ == "__main__":
    main()
