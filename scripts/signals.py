#!/usr/bin/env python3
"""
Long Term Target Tracking - daily signals (Songstats).

For every tracked record this looks at EVERYTHING Songstats has, so the page can show
whether a record is moving anywhere worth acting on:

  Growth (this week vs last week, from daily history)
    Streaming : Spotify streams, Spotify playlist reach
    Discovery : Shazams, radio plays
    Social    : TikTok videos + views, Instagram reels + views, YouTube Shorts + views
    DJ & club : 1001Tracklists DJ supports, SoundCloud plays

  New support in the last 7 days (from current placements and their dates)
    Spotify / Apple Music / Deezer / Amazon editorial adds, Beatport Top 100 + features,
    Beatport DJ charts, new Shazam chart entries

Nothing is cut off by a growth threshold: every movement is listed, biggest first.
The only rule is a small volume floor per metric so tiny numbers (3 -> 6 videos = +100%)
are marked "low volume" and don't drive the ranking or the momentum label.

Writes data/signals.json. Runs daily from .github/workflows/pulse.yml.
Secret: SONGSTATS_API_KEY
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import songstats_client as ss

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKED_FP = os.path.join(REPO_ROOT, "data", "tracked.json")
SIGNALS_FP = os.path.join(REPO_ROOT, "data", "signals.json")

WEEKS = 8                 # sparkline length
NEW_DAYS = 7              # "new this week" window
UP, DOWN = 10, -10        # what counts as moving up / down for the momentum label
COUNT_DAYS_KEEP = 70      # daily editorial counts kept for week-on-week comparisons

# key, label, group, source, field, kind ("flow" = weekly gain of a running total, "level" = value now), floor
METRICS = [
    ("spotify_streams", "Spotify streams",       "Streaming", "spotify",    "streams_total",          "flow",  2000),
    ("spotify_reach",   "Spotify playlist reach", "Streaming", "spotify",    "playlist_reach_current", "level", 10000),
    ("shazams",         "Shazams",                "Discovery", "shazam",     "shazams_total",          "flow",  300),
    ("radio_plays",     "Radio plays",            "Discovery", "radio",      "radio_plays_total",      "flow",  30),
    ("tiktok_videos",   "TikTok videos",          "Social",    "tiktok",     "videos_total",           "flow",  20),
    ("tiktok_views",    "TikTok views",           "Social",    "tiktok",     "views_total",            "flow",  20000),
    ("ig_reels",        "Instagram reels",        "Social",    "instagram",  "videos_total",           "flow",  20),
    ("ig_views",        "Instagram views",        "Social",    "instagram",  "views_total",            "flow",  20000),
    ("yt_shorts",       "YouTube Shorts",         "Social",    "youtube",    "shorts_total",           "flow",  10),
    ("yt_views",        "YouTube views",          "Social",    "youtube",    "video_views_total",      "flow",  10000),
    ("dj_supports",     "DJ supports (1001Tracklists)", "DJ & club", "tracklist", "total_supports_total", "flow", 3),
    ("sc_plays",        "SoundCloud plays",       "DJ & club", "soundcloud", "streams_total",          "flow",  1000),
]
HIST_SOURCES = sorted({m[3] for m in METRICS})
SKIP_SPOTIFY_TYPES = {"Artist Playlist", "Artist Radio"}


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        txt = f.read().strip()
        return json.loads(txt) if txt else default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")


def d(s):
    try:
        return date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def fmt(n):
    return f"{int(round(n)):,}" if n is not None else "—"


# ── history ────────────────────────────────────────────────────────────────
def fetch_history(ident):
    start = date.today() - timedelta(days=7 * (WEEKS + 1) + 2)
    params = dict(ident, start_date=start.isoformat(), end_date=date.today().isoformat())
    out = {}
    try:
        body = ss.ss_get("tracks/historic_stats", dict(params, source=",".join(HIST_SOURCES), with_aggregates="true"))
        for s in body.get("stats") or []:
            data = s.get("data")
            out[s.get("source")] = (data.get("history") if isinstance(data, dict) else None) or []
    except Exception as e:  # one source not on the plan shouldn't sink the rest
        print(f"  [history] combined call failed ({e}); trying each source")
        for src in HIST_SOURCES:
            try:
                body = ss.ss_get("tracks/historic_stats", dict(params, source=src))
                out[src] = ss.source_data(body, src).get("history") or []
            except Exception as e2:
                print(f"  [history] {src} unavailable: {e2}")
    return out


def series(hist, field):
    pts = sorted((d(h.get("date")), h.get(field)) for h in hist or [] if d(h.get("date")) and h.get(field) is not None)
    return pts


def value_at(pts, day):
    v = None
    for dt, val in pts:
        if dt <= day:
            v = val
        else:
            break
    return v


def metric(pts, kind):
    """Weekly figures for one metric. Flow = gained that week; level = value at week end."""
    if len(pts) < 2:
        return None
    # last real update (totals can sit still for a day or two before Songstats refreshes them)
    last = pts[-1][0]
    for i in range(len(pts) - 1, 0, -1):
        if pts[i][1] != pts[i - 1][1]:
            last = pts[i][0]
            break
    ends = [last - timedelta(days=7 * i) for i in range(WEEKS + 1)]
    vals = [value_at(pts, e) for e in ends]
    if kind == "level":
        weekly = vals[:WEEKS]
    else:
        weekly = [(vals[i] - vals[i + 1]) if vals[i] is not None and vals[i + 1] is not None else None
                  for i in range(WEEKS)]
    cur, prev = weekly[0], weekly[1]
    pct = round((cur - prev) / prev * 100) if cur is not None and prev not in (None, 0) else None
    return dict(week=cur, prev=prev, pct=pct, total=pts[-1][1], as_of=last.isoformat(),
                spark=list(reversed(weekly)))


# ── current snapshot: what's new this week ───────────────────────────────
def new_support(ident):
    cutoff = date.today() - timedelta(days=NEW_DAYS)
    events, counts = [], {}

    def recent(s):
        x = d(s)
        return x is not None and x > cutoff

    try:
        body = ss.ss_get("tracks/stats", dict(ident, source="spotify", with_playlists="true",
                                              only_current="true", limit=100))
        pl = ss.source_data(body, "spotify").get("playlists") or []
        ed = [p for p in pl if ((p.get("spotify_userid") or "").lower() == "spotify" or p.get("owner_name") == "Spotify")
              and p.get("type") not in SKIP_SPOTIFY_TYPES]
        counts["spotify_editorial"] = len(ed)
        new = [p for p in ed if recent(p.get("added_at"))]
        if new:
            events.append(dict(group="Streaming", kind="editorial", strength=300 + len(new),
                               text=f"New Spotify editorial: {names(new)}"))
    except Exception as e:
        print(f"  [spotify] {e}")

    try:
        body = ss.ss_get("tracks/stats", dict(ident, source="apple_music,deezer,amazon", with_playlists="true",
                                              only_current="true", limit=100))
        ap = [p for p in ss.source_data(body, "apple_music").get("playlists") or [] if p.get("type") == "Editorial"]
        dz = [p for p in ss.source_data(body, "deezer").get("playlists") or [] if deezer_editorial(p.get("owner_name"))]
        am = [p for p in ss.source_data(body, "amazon").get("playlists") or [] if "amazon" in (p.get("owner_name") or "").lower()]
        am_names = {}
        for p in am:
            am_names.setdefault(p.get("name"), []).append(p)
        counts.update(apple_editorial=len(ap), deezer_editorial=len(dz), amazon_editorial=len(am_names))
        for label, rows in (("Apple Music", ap), ("Deezer", dz)):
            new = [p for p in rows if recent(p.get("added_at"))]
            if new:
                events.append(dict(group="Streaming", kind="editorial", strength=280 + len(new),
                                   text=f"New {label} editorial: {names(new)}"))
        new_am = [v[0] for v in am_names.values() if any(recent(p.get("added_at")) for p in v)]
        if new_am:
            events.append(dict(group="Streaming", kind="editorial", strength=270 + len(new_am),
                               text=f"New Amazon Music editorial: {names(new_am)}"))
    except Exception as e:
        print(f"  [apple/deezer/amazon] {e}")

    try:
        body = ss.ss_get("tracks/stats", dict(ident, source="beatport,shazam", with_charts="true",
                                              with_playlists="true", limit=100))
        bp = ss.source_data(body, "beatport")
        charts = [c for c in (bp.get("top_100") or []) + (bp.get("features") or []) if not c.get("removed_at")]
        counts["beatport_charts"] = len(charts)
        new = [c for c in charts if recent(c.get("added_at"))]
        if new:
            events.append(dict(group="DJ & club", kind="chart", strength=250,
                               text="New on Beatport: " + ", ".join(
                                   f"{c.get('name')}{' #' + str(c['current_position']) if c.get('current_position') else ''}"
                                   for c in new[:3])))
        djc = [c for c in bp.get("dj_charts") or [] if recent(c.get("release_date"))]
        if djc:
            events.append(dict(group="DJ & club", kind="dj", strength=25 + 15 * len(djc),
                               text=f"{len(djc)} new Beatport DJ chart{'s' if len(djc) != 1 else ''}: "
                                    + ", ".join(c.get("owner_name") or "?" for c in djc[:4])
                                    + (f" +{len(djc) - 4} more" if len(djc) > 4 else "")))
        sz = [c for c in ss.source_data(body, "shazam").get("charts") or [] if not c.get("removed_at")]
        counts["shazam_charts"] = len(sz)
        new = [c for c in sz if recent(c.get("added_at"))]
        if new:
            events.append(dict(group="Discovery", kind="chart", strength=200 + len(new),
                               text=f"New Shazam chart{'s' if len(new) != 1 else ''}: "
                                    + ", ".join(f"{c.get('name')} #{c.get('current_position')}" for c in new[:3])
                                    + (f" +{len(new) - 3} more" if len(new) > 3 else "")))
    except Exception as e:
        print(f"  [beatport/shazam] {e}")
    return events, counts


def deezer_editorial(owner):
    o = (owner or "").lower()
    return o != "deezer charts" and ("deezer" in o or "editor" in o or "editrice" in o or "editora" in o)


def names(rows, n=3):
    shown = [r.get("name") or "?" for r in rows[:n]]
    return ", ".join(shown) + (f" +{len(rows) - n} more" if len(rows) > n else "")


# ── one record ───────────────────────────────────────────────────────────
def build_record(entry, prev_rec):
    hint = f"{entry.get('artist', '')} {entry.get('track', '')}".strip()
    ident = ss.find_ids(entry["isrc"], hint)
    hist = fetch_history(ident)
    metrics = []
    for key, label, group, src, field, kind, floor in METRICS:
        m = metric(series(hist.get(src), field), kind)
        if not m:
            continue
        low = (m["week"] or 0) < floor and (m["prev"] or 0) < floor
        metrics.append(dict(key=key, label=label, group=group, kind=kind, floor=floor, low_volume=low, **m))

    events, counts = new_support(ident)

    # editorial counts against a week ago (from our own daily log) for drops
    today = date.today().isoformat()
    log = [c for c in (prev_rec or {}).get("count_log", []) if c.get("date") != today]
    log.append(dict(date=today, **counts))
    log = log[-COUNT_DAYS_KEEP:]
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    base = next((c for c in reversed(log) if c["date"] <= week_ago), None)
    if base:
        for k, label in (("spotify_editorial", "Spotify"), ("apple_editorial", "Apple Music"),
                         ("deezer_editorial", "Deezer"), ("amazon_editorial", "Amazon Music")):
            if k in counts and k in base and counts[k] < base[k]:
                events.append(dict(group="Streaming", kind="drop", strength=-100,
                                   text=f"{label} editorial down {base[k] - counts[k]} on last week ({base[k]} → {counts[k]})"))

    signals = [dict(group=e["group"], text=e["text"], strength=e["strength"], positive=e["strength"] > 0, kind=e["kind"])
               for e in events]
    for m in metrics:
        if m["pct"] is None:
            continue
        if m["kind"] == "flow":
            detail = f"{fmt(m['week'])} this week vs {fmt(m['prev'])} last week"
        else:
            detail = f"{fmt(m['week'])} now vs {fmt(m['prev'])} a week ago"
        text = f"{m['label']} {'+' if m['pct'] >= 0 else ''}{m['pct']}% ({detail})"
        signals.append(dict(group=m["group"], text=text, strength=m["pct"], positive=m["pct"] > 0,
                            kind="growth", low_volume=m["low_volume"], metric=m["key"]))
    signals.sort(key=lambda s: (s.get("low_volume", False), -s["strength"]))

    ups = sum(1 for s in signals if not s.get("low_volume") and (s["kind"] != "growth" and s["positive"]
                                                                   or s["kind"] == "growth" and s["strength"] >= UP))
    downs = sum(1 for s in signals if not s.get("low_volume") and (s["kind"] == "drop"
                                                                    or s["kind"] == "growth" and s["strength"] <= DOWN))
    score = ups - downs
    momentum = "accelerating" if score >= 2 else "cooling" if downs > ups else "steady"
    tags = sorted({t for s in signals if s["positive"] and not s.get("low_volume") for t in tags_for(s)})
    return dict(isrc=entry["isrc"], updated=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                momentum=momentum, ups=ups, downs=downs, signal_count=ups, tags=tags,
                metrics=metrics, signals=signals, counts=counts, count_log=log, songstats=ident)


def tags_for(s):
    if s["kind"] == "editorial":
        yield "editorial"
    if s["group"] == "Social" and (s["kind"] != "growth" or s["strength"] >= UP):
        yield "social"
    if s["group"] == "DJ & club" and (s["kind"] != "growth" or s["strength"] >= UP):
        yield "dj"


def main():
    if not ss.SS_KEY:
        print("Missing SONGSTATS_API_KEY.")
        sys.exit(1)
    tracked = load_json(TRACKED_FP, {"tracks": []})
    doc = load_json(SIGNALS_FP, {"records": {}})
    recs = doc.setdefault("records", {})
    ok = failed = 0
    for entry in tracked.get("tracks", []):
        print(f"Signals: {entry.get('artist', '?')} - {entry.get('track', '?')} ({entry['isrc']})")
        try:
            rec = build_record(entry, recs.get(entry["isrc"]))
        except Exception as e:
            print(f"  ERROR: {e}")
            failed += 1
            continue
        recs[entry["isrc"]] = rec
        ok += 1
        top = [s["text"] for s in rec["signals"][:2]]
        print(f"  {rec['momentum']} ups={rec['ups']} downs={rec['downs']} | {' | '.join(top)}")
    live = {t["isrc"] for t in tracked.get("tracks", [])}
    doc["records"] = {k: v for k, v in recs.items() if k in live}
    doc["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_json(SIGNALS_FP, doc)
    print(f"\nSUMMARY signals ok={ok} failed={failed} | Songstats calls={ss.calls[0]}")
    if ok == 0 and failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
