#!/usr/bin/env python3
"""
Standalone Spotify auth test — nothing to do with the real sync scripts.
Tries to get a token, then fetches ONE well-known public playlist
(Spotify's own "Today's Top Hits"). Times out fast (15s per request) and
prints exactly what happened, so this finishes in seconds, not minutes.

Run this before touching sync.py again — it isolates "can this app talk to
Spotify at all" from everything else (rate limits, caching, checkpointing).
"""
import base64
import os
import sys
import time

import requests

SP_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SP_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
TEST_PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"  # Spotify's own "Today's Top Hits" — always exists

if not SP_ID or not SP_SECRET:
    print("FAIL: SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET is not set at all.")
    sys.exit(1)

print(f"Client ID starts with: {SP_ID[:6]}... (length {len(SP_ID)})")
print(f"Client Secret length: {len(SP_SECRET)}")

print("\n--- Step 1: requesting a token ---")
t0 = time.time()
try:
    r = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={
            "Authorization": "Basic " + base64.b64encode(f"{SP_ID}:{SP_SECRET}".encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
except requests.exceptions.RequestException as e:
    print(f"FAIL: request itself errored after {time.time()-t0:.1f}s: {e}")
    sys.exit(1)

print(f"Took {time.time()-t0:.1f}s. Status: {r.status_code}")
print(f"Raw response body: {r.text[:500]}")

if not r.ok:
    print("\nFAIL: could not get a token at all. The error body above is the real reason —")
    print("common causes: wrong Client ID/Secret, extra whitespace when pasting, or the app")
    print("not actually being enabled/created correctly on developer.spotify.com.")
    sys.exit(1)

token = r.json().get("access_token")
print(f"\nSUCCESS: got a token. Length: {len(token) if token else 0}")

print("\n--- Step 2: fetching one known public playlist ---")
t0 = time.time()
try:
    r2 = requests.get(
        f"https://api.spotify.com/v1/playlists/{TEST_PLAYLIST_ID}",
        headers={"Authorization": f"Bearer {token}"},
        params={"fields": "name,followers.total,owner.display_name"},
        timeout=15,
    )
except requests.exceptions.RequestException as e:
    print(f"FAIL: request itself errored after {time.time()-t0:.1f}s: {e}")
    sys.exit(1)

print(f"Took {time.time()-t0:.1f}s. Status: {r2.status_code}")
print(f"Raw response body: {r2.text[:500]}")

if r2.ok:
    print("\nSUCCESS: the app can authenticate AND fetch playlist data. This means the")
    print("original problem is genuinely about volume/rate-limiting under load, not a")
    print("broken or misconfigured app — worth knowing before we touch sync.py again.")
elif r2.status_code == 429:
    retry_after = r2.headers.get("Retry-After", "?")
    print(f"\nRATE LIMITED already, on the very first real request. Retry-After: {retry_after}s.")
    print("This is a strong signal the new app's rate ceiling is genuinely very low —")
    print("worth checking its quota tier on developer.spotify.com directly.")
else:
    print(f"\nFAIL: got a token fine, but the actual data request failed with {r2.status_code}.")
    print("The response body above is the real reason.")
