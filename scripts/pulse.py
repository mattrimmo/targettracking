#!/usr/bin/env python3
"""
Long Term Target Tracking — daily pulse script.

Separate and much lighter than scripts/sync.py. Runs every day (not just
Wednesdays) via .github/workflows/pulse.yml. Deliberately makes ZERO calls
to Spotify's API — only Spot On Track — so it stays cheap enough to run
daily indefinitely regardless of how many tracks are being watched:
  - Shazam total/daily counts
  - Shazam current chart positions (every country, not just UK)
  - A raw current-playlist COUNT (just len() of the SOT response — no
    per-playlist Spotify owner lookup, which is what the Wednesday
    official sync needs Spotify for and this script doesn't do at all)

From the accumulated daily history, works out a simple trend classification
(accelerating / steady / plateauing / declining) per track. AI-read
generation used to live here too, but has moved to scripts/sync.py, which
has the full weekly picture (editorial vs independent vs Shazam) rather
than just what this daily script sees — this file no longer touches
Anthropic at all.

Secrets:
  SOT_API_KEY  — required (same one sync.py uses)
"""
import json
import os
import sys
from datetime import datetime, timezone

import requests

REPO_ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKED_FP = os.path.join(REPO_ROOT, "data", "tracked.json")
PULSE_FP   = os.path.join(REPO_ROOT, "data", "pulse.json")

SOT_KEY = os.environ.get("SOT_API_KEY", "")

MIN_FOLLOWERS = 100  # same floor used elsewhere
DAYS_TO_KEEP = 60    # ~2 months of daily granularity is plenty for trend detection
TREND_WINDOW = 4     # compare the last N days' avg growth vs the N days before that


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


def fetch_pulse_metrics(isrc):
    shazams = sot_get(f"/tracks/{isrc}/shazam/shazams")
    total_shazams = shazams[0]["total"] if shazams else None
    daily_shazams = shazams[0]["daily"] if shazams else None

    charts = sot_get(f"/tracks/{isrc}/shazam/charts/current")
    shazam_charts = [
        {
            "country_code": c.get("country_code"),
            "type": c.get("type"),
            "position": c.get("position"),
            "previous_position": c.get("previous_position"),
            "genre": c.get("genre"),
            "city": c.get("city"),
        }
        for c in charts
    ]

    playlists = sot_get(f"/tracks/{isrc}/spotify/playlists/current")
    playlist_count = sum(1 for p in playlists if (p.get("playlist", {}).get("followers") or 0) >= MIN_FOLLOWERS)

    return total_shazams, daily_shazams, shazam_charts, playlist_count


def classify_trend(daily_series):
    """
    daily_series: list of {"date":..., "daily_shazams": int|None}, oldest first.
    Compares the average of the most recent TREND_WINDOW days against the
    TREND_WINDOW days before that. Needs at least 2*TREND_WINDOW days of
    real (non-null) data, otherwise returns None — not enough history yet
    to say anything meaningful.
    """
    vals = [d["daily_shazams"] for d in daily_series if d.get("daily_shazams") is not None]
    if len(vals) < TREND_WINDOW * 2:
        return None, None

    recent = vals[-TREND_WINDOW:]
    prior = vals[-TREND_WINDOW * 2:-TREND_WINDOW]
    recent_avg = sum(recent) / len(recent)
    prior_avg = sum(prior) / len(prior) if prior else 0

    if prior_avg == 0:
        pct_change = None
    else:
        pct_change = round(((recent_avg - prior_avg) / prior_avg) * 100)

    if prior_avg == 0 and recent_avg > 0:
        return "accelerating", pct_change
    if pct_change is None:
        return "steady", 0
    if pct_change >= 15:
        return "accelerating", pct_change
    if pct_change <= -15:
        return "declining", pct_change
    if -15 < pct_change < 0:
        return "plateauing", pct_change
    return "steady", pct_change


def generate_ai_read(*args, **kwargs):
    """Retired — AI read generation now lives in scripts/sync.py, which has
    the full weekly picture (editorial vs independent vs Shazam) rather
    than just the daily Shazam trend this script sees. Kept as a no-op
    stub rather than deleting the call site below, to avoid a bigger diff
    than necessary — always returns None."""
    return None


def main():
    if not SOT_KEY:
        print("Missing SOT_API_KEY.")
        sys.exit(1)

    tracked = load_json(TRACKED_FP, {"tracks": []})
    pulse = load_json(PULSE_FP, {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for entry in tracked.get("tracks", []):
        isrc = entry["isrc"]
        print(f"Pulse: {entry.get('artist','?')} - {entry.get('track','?')} ({isrc})")
        try:
            total_shazams, daily_shazams, shazam_charts, playlist_count = fetch_pulse_metrics(isrc)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        series = pulse.setdefault(isrc, [])
        series[:] = [d for d in series if d["date"] != today]  # avoid dupes if run twice today

        trend, pct_change = classify_trend(
            series + [{"date": today, "daily_shazams": daily_shazams}]
        )

        gb_charts = [c for c in shazam_charts if c["country_code"] == "GB"]
        chart_summary = ", ".join(
            f"#{c['position']} {c.get('genre') or 'overall'} UK" for c in gb_charts
        ) or "not currently charting in the UK"

        ai_read = generate_ai_read(
            entry.get("artist", ""), entry.get("track", ""), entry.get("label", "INDEPENDENT"),
            trend, pct_change, total_shazams, playlist_count, chart_summary,
        )

        series.append({
            "date": today,
            "total_shazams": total_shazams,
            "daily_shazams": daily_shazams,
            "playlist_count": playlist_count,
            "shazam_charts": shazam_charts,
            "trend": trend,
            "trend_pct_change": pct_change,
            "ai_read": ai_read,
        })
        pulse[isrc] = series[-DAYS_TO_KEEP:]

    save_json(PULSE_FP, pulse)
    print("Done.")


if __name__ == "__main__":
    main()
