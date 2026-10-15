#!/usr/bin/env python3
"""Prints what Spot On Track returns for one playlist, so we can see which owner/image fields exist."""
import json, os, sys, requests
KEY = os.environ.get("SOT_API_KEY", "")
tracked = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tracked.json")))
isrc = tracked["tracks"][0]["isrc"]
r = requests.get(f"https://www.spotontrack.com/api/v1/tracks/{isrc}/spotify/playlists/current",
                 headers={"Authorization": "Bearer " + KEY}, timeout=30)
print("status", r.status_code)
if not r.ok:
    print(r.text[:500]); sys.exit(1)
d = r.json()
print("playlists returned:", len(d))
if d:
    print("top-level keys:", sorted(d[0].keys()))
    print("playlist keys:", sorted(d[0]["playlist"].keys()))
    print(json.dumps(d[0], indent=2)[:3000])
    for i in (1, 2):
        if len(d) > i: print(json.dumps(d[i]["playlist"], indent=2)[:1200])

print("\n--- label lookup test via Deezer (Spot On Track has no label field) ---")
for t in tracked["tracks"][:4]:
    code = t["isrc"]
    r1 = requests.get(f"https://api.deezer.com/track/isrc:{code}", timeout=20)
    tr = r1.json() if r1.ok else {}
    alb = (tr.get("album") or {}).get("id")
    lab = None
    if alb:
        lab = requests.get(f"https://api.deezer.com/album/{alb}", timeout=20).json().get("label")
    print(t.get("artist"), "-", t.get("track"), "| Deezer album id:", alb, "| label:", lab, "| typed label:", t.get("label"))
