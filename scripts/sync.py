#!/usr/bin/env python3
"""
Long Term Target Tracking - weekly / on-demand sync (Songstats test version).

Songstats returns each playlist's owner, follower count, cover and a type
(Editorial / Personalized / Chart / Listener ...) in the same call, so there are
no Spotify lookups, no owner cache and no "not yet classified" backlog.

Classification:
  owner is Spotify          -> editorial (Editorial, Personalised, Chart, This Is, ...)
  anything else             -> independent (100+ followers)

Writes the same data/history.json shape as before, so index.html is unchanged.
Extra fields per snapshot: editorial_breakdown, spotify_streams_total, source="songstats".

Secrets (GitHub Actions): SONGSTATS_API_KEY.
Optional env: MIN_FOLLOWERS (default 100).
"""
import json
import os
import re
import sys
from datetime import datetime, timezone

import songstats_client as ss

REPO_ROOT   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA        = os.path.join(REPO_ROOT, "data")
TRACKED_FP  = os.path.join(DATA, "tracked.json")
HISTORY_FP  = os.path.join(DATA, "history.json")
CURATORS_FP = os.path.join(DATA, "curators.json")

IS_OFFICIAL = os.environ.get("IS_OFFICIAL", "false").lower() == "true"
MIN_FOLLOWERS = int(os.environ.get("MIN_FOLLOWERS", "100") or 100)

TYPE_LABEL = {"Editorial": "Editorial", "Personalized": "Personalised", "Chart": "Chart",
              "Artist Playlist": "This Is", "Artist Radio": "Artist Radio", "Written-by-format": "Songwriter"}


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


def norm(name):
    """Curator matching ignores case, spaces and punctuation ('selected.' == 'Selected')."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def build_curator_index(doc):
    return {norm(c["owner_name"]): c for c in (doc or {}).get("curators", [])}


def is_spotify_owned(p):
    return (p.get("spotify_userid") or "").lower() == "spotify" or (p.get("owner_name") or "").strip() == "Spotify"


def fill_missing_labels(tracked, infos):
    """Fill blank / INDEPENDENT labels from Songstats track info."""
    found = {}
    for t in tracked.get("tracks", []):
        lab = (t.get("label") or "").strip()
        if lab and lab.upper() != "INDEPENDENT":
            continue
        labels = [l.get("name") for l in (infos.get(t["isrc"]) or {}).get("labels") or [] if l.get("name")]
        if labels:
            found[t["isrc"]] = labels[0].upper()
    if found:
        fresh = load_json(TRACKED_FP, {"tracks": []})   # re-read so we never overwrite a UI edit
        for t in fresh.get("tracks", []):
            if t["isrc"] in found:
                t["label"] = found[t["isrc"]]
        save_json(TRACKED_FP, fresh)
        print(f"Labels filled from Songstats: {len(found)}")


def process_track(entry, playlists, totals, ident, curator_idx, prev_snap):
    editorial, independent, breakdown = [], [], {}
    for p in playlists:
        row = {"name": p.get("name"), "spotify_id": p.get("spotifyid"),
               "followers": p.get("followers_count") or 0, "cover_url": p.get("artwork"),
               "position": p.get("current_position"), "added_at": (p.get("added_at") or "")[:10]}
        if is_spotify_owned(p) and p.get("type") in ("Artist Playlist", "Artist Radio"):
            continue   # "This Is" / artist radio: not editorial support (same rule as the report)
        if is_spotify_owned(p):
            kind = TYPE_LABEL.get(p.get("type"), p.get("type") or "Editorial")
            row.update(owner_name="Spotify", type=kind)
            breakdown[kind] = breakdown.get(kind, 0) + 1
            editorial.append(row)
        else:
            row["owner_name"] = p.get("owner_name") or "Unknown curator"
            independent.append(row)

    supporters = []
    for p in independent:
        m = curator_idx.get(norm(p["owner_name"]))
        if m:
            supporters.append({**p, "tier": m["tier"], "notes": m.get("notes", "")})
    supporters.sort(key=lambda k: (k["tier"], -k["followers"]))
    editorial.sort(key=lambda k: -k["followers"])
    independent.sort(key=lambda k: -k["followers"])

    total, daily, charts = ss.shazam(ident)
    ss.fill_previous_positions(charts, (prev_snap or {}).get("shazam_charts"))
    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "official": IS_OFFICIAL,
        "source": "songstats",
        "editorial_count": len(editorial),
        "editorial_breakdown": breakdown,
        "editorial_playlists": editorial,
        "independent_playlists": independent,
        "independent_followers_total": sum(p["followers"] for p in independent),
        "key_supporters": supporters,
        "unclassified_count": 0,
        "spotify_streams_total": totals.get("streams_total"),
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
    if not ss.SS_KEY:
        print("Missing SONGSTATS_API_KEY.")
        sys.exit(1)

    tracked = load_json(TRACKED_FP, {"tracks": []})
    history = load_json(HISTORY_FP, {"snapshots": {}})
    curator_idx = build_curator_index(load_json(CURATORS_FP, {}))
    by_isrc = history.setdefault("snapshots", {})

    ok = failed = 0
    fetched, infos = {}, {}
    for entry in tracked.get("tracks", []):
        try:
            ident = ss.find_ids(entry["isrc"], f"{entry.get('artist','')} {entry.get('track','')}".strip())
            rows, totals, info = ss.current_spotify_playlists(ident, MIN_FOLLOWERS)
            fetched[entry["isrc"]] = (rows, totals, ident)
            infos[entry["isrc"]] = info
        except Exception as e:
            print(f"  ERROR fetching {entry.get('track','?')}: {e}")
    fill_missing_labels(tracked, infos)

    for entry in tracked.get("tracks", []):
        isrc = entry["isrc"]
        print(f"Syncing {entry.get('artist','?')} - {entry.get('track','?')} ({isrc})")
        if isrc not in fetched:
            failed += 1
            continue
        prior = by_isrc.setdefault(isrc, [])
        try:
            snap = process_track(entry, *fetched[isrc], curator_idx, prior[-1] if prior else None)
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1
            continue
        ok += 1
        po = last_official(prior)
        snap["editorial_count_delta"] = delta(snap["editorial_count"], po and po.get("editorial_count"))
        snap["independent_followers_delta"] = delta(snap["independent_followers_total"], po and po.get("independent_followers_total"))
        snap["total_shazams_delta"] = delta(snap["total_shazams"], po and po.get("total_shazams"))
        snap["ai_read"] = None
        # keep every official snapshot, but only the newest unofficial one (it's just a live check)
        prior = [x for x in prior if x.get("official")]
        prior.append(snap)
        by_isrc[isrc] = prior[-52:]
        print(f"  editorial={snap['editorial_count']} {snap['editorial_breakdown']} "
              f"independent={len(snap['independent_playlists'])} shazams={snap['total_shazams']}")
        save_json(HISTORY_FP, history)   # checkpoint after every track

    save_json(HISTORY_FP, history)
    print(f"\nSUMMARY tracks ok={ok} failed={failed} | Songstats calls this run={ss.calls[0]}")
    if ok == 0 and failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
