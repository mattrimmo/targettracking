# Listen Up: Long Term Target Tracking

Live team dashboard for records Listen Up is watching but not yet working:
editorial playlist count, key independent curators, independent follower movement,
and Shazam trend, with week-on-week change.

## How it works
- `index.html` is a static page (GitHub Pages). It only reads `data/*.json`.
- `scripts/sync.py` runs in GitHub Actions every Wednesday (official snapshot) or on
  demand from the page ("Check live now", unofficial). It pulls current playlists
  and Shazam data from Spot On Track, works out editorial vs independent, and writes
  `data/history.json`.
- `scripts/pulse.py` runs daily, Spot On Track only, for the Shazam trend (`data/pulse.json`).
- API keys live only in repo Secrets: `SOT_API_KEY`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`.

## Classification (no Spotify call unless unavoidable)
1. Playlist id starts `37i9dQZF1` -> editorial (Spotify answers 404 for these anyway)
2. Already in `data/playlist_cache.json` -> reuse
3. Spot On Track supplies an owner name -> use and cache
4. Otherwise a Spotify lookup, capped by `SPOTIFY_LOOKUPS_PER_RUN` (default 60; 0 = never call Spotify)
5. Anything still unresolved is retried next run and shown as "not yet classified"

## Files
```
index.html  logo.png
data/       tracked.json  history.json  pulse.json  curators.json  playlist_cache.json  spotify_call_log.json
scripts/    sync.py  pulse.py  sot_diag.py  requirements.txt
.github/workflows/  sync.yml  pulse.yml  sot_diag.yml
```
`sot_diag` is a diagnostic: it prints which fields Spot On Track returns for a playlist. Delete it once not needed.

## Troubleshooting
- Open Actions, open the latest "Sync" run, and read the last line starting `SUMMARY`. It says where each classification came from, how many Spotify calls were used, and whether Spotify's quota wall was hit.
- Many "not yet classified": Spot On Track gives no owner names and Spotify's quota is the limit. It clears over several runs.
- Add/remove or "Check live now" fails with 401/403: the shared GitHub token expired or lacks Contents + Actions write.
- Key Supporters are matched on exact owner name in `data/curators.json`.
