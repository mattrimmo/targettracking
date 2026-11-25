# Listen Up: Long Term Target Tracking

Live team dashboard for records Listen Up is watching but not yet working:
editorial playlist count, key independent curators, independent follower movement,
and Shazam trend, with week-on-week change. Data comes from Songstats.

## How it works
- `index.html` is a static page (GitHub Pages). It only reads `data/*.json`.
- `scripts/sync.py` runs in GitHub Actions every Wednesday 05:17 UTC (official snapshot) or on demand
  from the page ("Check live now"). It pulls current Spotify playlists and Shazam data from Songstats,
  works out editorial vs independent and writes `data/history.json`.
- `scripts/pulse.py` runs daily for the Shazam trend and playlist count (`data/pulse.json`).
- `scripts/songstats_client.py` is the small Songstats helper both scripts use.
- The API key lives only in the repo secret `SONGSTATS_API_KEY`.

## Classification
- Spotify-owned playlists are editorial: Editorial, Personalised, Chart, Songwriter. Each card shows its type.
- "This Is" and artist radio playlists are left out (algorithmic / artist lists).
- Everything else with 100+ followers is independent.
- Key Supporters (`data/curators.json`) match on owner name, ignoring case and punctuation.

## Labels
Type the label when adding a track, or leave it blank and the sync fills it from Songstats.

## Troubleshooting
- Open Actions, open the latest "Sync" run, and read the last line starting `SUMMARY`: tracks ok / failed and Songstats calls used.
- "Missing SONGSTATS_API_KEY": the secret is missing or misnamed.
- "could not find ... on Songstats": that record isn't matched. Check the ISRC.
- Add/remove or "Check live now" fails with 401/403: the shared GitHub token expired or lacks Contents + Actions write.
- History carries on from the Spot On Track snapshots. The first official Songstats week can show a one-off jump in
  editorial counts, because "This Is" / artist radio lists are no longer counted.
