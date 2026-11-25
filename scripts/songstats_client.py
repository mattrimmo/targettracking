"""
Small Songstats helper shared by sync.py and pulse.py.
Key comes from the SONGSTATS_API_KEY repo secret. Docs: https://docs.songstats.com
"""
import os
import time

import requests

SS_BASE = "https://api.songstats.com/enterprise/v1"
SS_KEY = os.environ.get("SONGSTATS_API_KEY", "")
SS_DELAY = 0.3          # seconds between calls, to stay polite
PAGE = 100
MAX_PAGES = 30

calls = [0]


def ss_get(path, params):
    for attempt in range(4):
        r = requests.get(f"{SS_BASE}/{path}", params={k: v for k, v in params.items() if v not in (None, "")},
                         headers={"apikey": SS_KEY, "accept": "application/json"}, timeout=60)
        calls[0] += 1
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            wait = r.headers.get("Retry-After")
            wait = min(int(float(wait)), 60) + 1 if wait and wait.replace(".", "", 1).isdigit() else 3 * (attempt + 1)
            print(f"  [songstats] {r.status_code}, waiting {wait}s")
            time.sleep(wait)
            continue
        if not r.ok:
            raise RuntimeError(f"Songstats {r.status_code} on {path}: {r.text[:200]}")
        time.sleep(SS_DELAY)
        body = r.json()
        body = body.get("response", body)
        if isinstance(body, dict) and body.get("result") == "error":
            raise RuntimeError(f"Songstats error on {path}: {body.get('message')}")
        return body
    raise RuntimeError(f"Songstats kept failing on {path}")


def find_ids(isrc, hint=""):
    """Songstats track + artist IDs for an ISRC (artist-scoped keys need the artist on every call)."""
    for q in [isrc, hint]:
        if not q:
            continue
        for res in ss_get("tracks/search", {"q": q, "limit": 5}).get("results") or []:
            for a in [x.get("songstats_artist_id") for x in res.get("artists") or []] or [None]:
                ident = {k: v for k, v in {"songstats_track_id": res.get("songstats_track_id"),
                                           "songstats_artist_id": a}.items() if v}
                try:
                    body = ss_get("tracks/info", dict(ident, source="all"))
                except RuntimeError:
                    continue
                isrcs = {l.get("isrc") for l in ((body or {}).get("track_info") or {}).get("links") or [] if l.get("isrc")}
                if q != isrc or isrc in isrcs or not isrcs:
                    return ident
    raise RuntimeError(f"could not find {isrc} on Songstats")


def source_data(body, source):
    for s in (body or {}).get("stats", []) or []:
        if s.get("source") == source:
            return s.get("data") or {}
    return {}


def current_spotify_playlists(ident, min_followers):
    """Current Spotify placements, biggest first, stopping once below min_followers.
    Returns (rows, totals, track_info)."""
    rows, totals, info = [], {}, {}
    for page in range(MAX_PAGES):
        body = ss_get("tracks/stats", dict(ident, source="spotify", with_playlists="true",
                                           only_current="true", limit=PAGE, offset=page * PAGE))
        data = source_data(body, "spotify")
        if page == 0:
            totals = {k: v for k, v in data.items() if not isinstance(v, (list, dict))}
            info = body.get("track_info") or {}
        pl = data.get("playlists") or []
        rows.extend(pl)
        if len(pl) < PAGE or (pl[-1].get("followers_count") or 0) < min_followers:
            break
    return [p for p in rows if (p.get("followers_count") or 0) >= min_followers], totals, info


def shazam(ident):
    """(total, daily, charts) with charts in the shape the page already renders."""
    body = ss_get("tracks/stats", dict(ident, source="shazam", with_charts="true", only_current="true", limit=PAGE))
    data = source_data(body, "shazam")
    total = data.get("shazams_total")
    charts = [chart_row(c) for c in data.get("charts") or []]
    daily = None
    try:
        from datetime import date, timedelta
        end = date.today()
        hb = ss_get("tracks/historic_stats", {**ident, "source": "shazam",
                                              "start_date": (end - timedelta(days=6)).isoformat(),
                                              "end_date": end.isoformat()})
        hist = [h for h in source_data(hb, "shazam").get("history") or [] if h.get("shazams_total") is not None]
        hist.sort(key=lambda h: h["date"])
        # Songstats totals don't always move every day, so use the latest real increase
        for a, b in zip(reversed(hist[:-1]), reversed(hist)):
            if b["shazams_total"] != a["shazams_total"]:
                daily = b["shazams_total"] - a["shazams_total"]
                break
        if daily is None and len(hist) >= 2:
            daily = 0
    except Exception as e:
        print(f"  [shazam] daily history skipped: {e}")
    return total, daily, charts


# ── Shazam chart names -> country codes ─────────────────────────────────────
_EXTRA = {"uk": "GB", "united kingdom": "GB", "england": "GB", "usa": "US", "united states": "US",
          "south korea": "KR", "russia": "RU", "vietnam": "VN", "czech republic": "CZ", "czechia": "CZ",
          "turkey": "TR", "türkiye": "TR", "taiwan": "TW", "bolivia": "BO", "venezuela": "VE",
          "iran": "IR", "tanzania": "TZ", "moldova": "MD", "uae": "AE", "united arab emirates": "AE"}
_NAMES = None


def _country_names():
    global _NAMES
    if _NAMES is None:
        _NAMES = dict(_EXTRA)
        try:
            import pycountry
            for c in pycountry.countries:
                for n in (c.name, getattr(c, "common_name", None), getattr(c, "official_name", None)):
                    if n:
                        _NAMES.setdefault(n.lower(), c.alpha_2)
        except ImportError:
            pass
    return _NAMES


def _match_country(text):
    t = (text or "").strip().lower()
    names = _country_names()
    if t in names:
        return names[t], ""
    # City charts look like "Toronto Canada": find the longest country name at the end
    best = None
    for n, code in names.items():
        if t.endswith(" " + n) and (best is None or len(n) > len(best[0])):
            best = (n, code)
    if best:
        return best[1], text[: len(text) - len(best[0])].strip()
    return None, ""


def chart_row(c):
    name = c.get("name") or ""
    kind = (c.get("location_type") or "").lower()
    code, city, genre = None, None, None
    if "global" in name.lower() or "world" in name.lower():
        code = "GLOBAL"
        genre = name.split(" - ", 1)[1] if " - " in name else None
    elif kind == "city":
        code, city = _match_country(name)
        city = city or name
    else:
        country = name.split(" - ", 1)[0]
        genre = name.split(" - ", 1)[1] if " - " in name else None
        code, _ = _match_country(country)
    return {"country_code": code or "", "type": "shazams", "position": c.get("current_position"),
            "previous_position": None, "genre": genre, "city": city, "name": name,
            "peak": c.get("top_position"), "url": c.get("external_url")}


def fill_previous_positions(charts, previous_charts):
    """Songstats gives no 'previous position', so take it from the last stored snapshot."""
    prev = {(p.get("name") or (p.get("country_code"), p.get("genre"), p.get("city"))): p.get("position")
            for p in previous_charts or []}
    for c in charts:
        c["previous_position"] = prev.get(c.get("name"))
    return charts
