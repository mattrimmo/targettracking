# Listen Up — Long Term Target Tracking

A live, shared dashboard for tracking specific records over time: how many
editorial playlists they're currently in (and the week-on-week movement),
which key independent curators are supporting them, and total independent
follower movement.

## How it works (quick version)

- **The page (`index.html`)** is a static viewer. It reads two data files
  (`data/tracked.json`, `data/history.json`) and renders them. It also lets
  you add/remove tracked tracks and trigger a sync — but it never calls Spot
  On Track directly.
- **A GitHub Actions workflow** does the actual data pulling. It runs
  automatically every Wednesday morning (the "official" weekly snapshot used
  for the up/down numbers), and can also be triggered on demand from the page
  via the **"Check live now"** button (shows current standing without
  disturbing the official weekly delta).
- Your Spot On Track and Spotify API keys live only in **GitHub Actions
  Secrets** — never in the page, never visible to anyone browsing the site.

This keeps the whole thing free (GitHub Pages + Actions, both free for a repo
like this) while keeping your proprietary Spot On Track data feed private,
even though the page itself will be publicly viewable.

## One-time setup

### 1. Create the repo
Create a new GitHub repository (public — GitHub Pages is only free on public
repos) and push everything in this folder to it, keeping the folder structure
exactly as-is:

```
your-repo/
  index.html
  data/tracked.json
  data/history.json
  data/curators.json
  scripts/sync.py
  scripts/requirements.txt
  .github/workflows/sync.yml
```

### 2. Add your API keys as repo secrets
In the repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add three:

- `SOT_API_KEY` — your Spot On Track bearer token
- `SPOTIFY_CLIENT_ID` — your Spotify app's client ID
- `SPOTIFY_CLIENT_SECRET` — your Spotify app's client secret

(These are the same three values already sitting in `ListenUp_Report_Generator.html` if you want to copy them straight across.)

### 3. Turn on GitHub Pages
**Settings → Pages → Source: Deploy from a branch → Branch: `main` / root.**
Save. Your team's URL will be `https://your-username.github.io/your-repo/`.

### 4. Make a token so the page can add/remove tracks and trigger syncs
This is the one thing every team member needs, so create it once and share it
with the team over Slack/1Password/etc — not by pasting it anywhere public.

- Go to **github.com/settings/personal-access-tokens/new** (fine-grained token)
- Resource owner: your account/org · Repository access: **only this repository**
- Permissions needed: **Contents: Read and write**, **Actions: Read and write**
- Set an expiry you're comfortable with (you'll just generate a new one and
  re-share it when it expires)
- Generate, copy it, share it with the team

### 5. First run
Open the site → click **edit** under Settings → fill in:
- GitHub owner (your username/org) and repo name
- The token from step 4
- Your Spotify Client ID/Secret (same ones from step 2 — this pair is only
  used for the in-browser search-to-add feature, so it's a separate paste)

Then search for a track, add it, and hit **Check live now** to pull its first
snapshot.

Each team member does the Settings step once in their own browser — it's
saved to `localStorage` there, never uploaded anywhere.

## Editing the master curator list

`data/curators.json` is the list that decides who counts as a "Key
Supporter." It ships with just one seeded entry — you'll want to build this
out yourself, since curator reputation is exactly the kind of judgement call
that needs a human who knows the scene. Edit it straight in GitHub (or
locally + push): add an entry per curator with their exact Spotify display
name, a tier (1 = top trust, 2 = solid, 3 = watching), and a note. The
next sync will pick up any independent placement matching those names.

Playlists from curators *not* in the list still show up in the "All
independent placements" section on each track's card — they just won't be
flagged as Key Supporters until you add them.

## Adjusting the schedule

The cron in `.github/workflows/sync.yml` is set to `0 7 * * 3` (07:00 UTC
every Wednesday — 8am UK time in summer, 7am in winter, since UK clocks
change but UTC cron doesn't). Change the `cron` line if you want a different
day/time, or trigger a run manually any time from **Actions → Sync Long Term
Target Tracking → Run workflow** in GitHub itself.

## Troubleshooting

- **"Set up GitHub owner/repo/token in Settings first"** — you haven't filled
  in Settings in this browser yet.
- **Add/remove seems to hang** — check the debug box bottom-left; a 401/403
  from the GitHub API usually means the token has expired or is missing the
  Contents/Actions write permissions from step 4.
- **"Check live now" times out** — the workflow can take a minute or two,
  especially with lots of tracked tracks (each playlist needs a separate
  Spotify lookup). Check the **Actions** tab in GitHub directly to see progress.
- **A track shows "No snapshot yet"** — it's been added but a sync hasn't run
  since. Click Check live now, or wait for Wednesday.
