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
- `scripts/signals.py` runs daily straight after the pulse and writes `data/signals.json`: everything else
  Songstats has on each record (see Signals below).
- `scripts/songstats_client.py` is the small Songstats helper all the scripts use.
- The API key lives only in the repo secret `SONGSTATS_API_KEY`.

## Signals
The board at the top of the page ranks every tracked record by what it is doing this week, across
everything Songstats has. Each record compares the last 7 days with the 7 before.

- Growth: Spotify streams and playlist reach, Shazams, radio plays, TikTok videos and views, Instagram
  reels and views, YouTube Shorts and views, 1001Tracklists DJ supports, SoundCloud plays. Each one shows
  this week, the change, an 8-week sparkline and the running total.
- New support in the last 7 days: Spotify, Apple Music, Deezer and Amazon editorial adds, Beatport Top 100
  and features, Beatport DJ charts, new Shazam chart entries. Editorial counts that drop against a week ago
  show as a red signal.
- No growth cut-off. Every change is listed, biggest first, and the colour gets stronger the bigger the move.
- Volume floor: if both weeks are under a small floor (for example 20 TikTok videos, 300 Shazams, 3 DJ
  supports) the change still shows, marked "low volume", but it does not count towards the ranking.
  The floors are the last number on each line of `METRICS` in `scripts/signals.py`.
- Momentum: Accelerating (at least 2 more things up than down), Cooling (more down than up), otherwise
  Steady. "Up" means +10% or more on a metric, or a new placement. "Down" means -10% or worse, or an
  editorial drop. Change `UP` and `DOWN` in `scripts/signals.py` to tune it.
- About 6 Songstats calls per record per day.

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
