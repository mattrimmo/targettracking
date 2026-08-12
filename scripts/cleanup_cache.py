#!/usr/bin/env python3
"""
One-time cleanup for data/playlist_cache.json — strips entries that were
mis-cached as "editorial" (owner == "") by a bug in sp_enrich's quota-wall
and retry-exhausted handling (now fixed), plus the older pre-existing
behaviour that did the same thing for genuine 404s.

Detection: a REAL editorial classification always has real `total` and
`cover` values from a successful API response. The bugged entries have
owner == "" with BOTH total and cover as null — that combination only
happens from these bugs, never from a genuine successful check. Anything
matching this gets removed entirely, not just re-labelled, so the next
sync run treats it as never-checked and gives it a real classification.

Separately reports (but does NOT remove) any "__UNKNOWN__" entries —
that's the correct, intentional marker for genuinely-deleted playlists
going forward, not a bug to clean up.

Run once via the temporary workflow, then this file (and its workflow)
can be deleted — it's not meant to be a permanent part of the repo.
"""
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_FP = os.path.join(REPO_ROOT, "data", "playlist_cache.json")


def main():
    with open(CACHE_FP, "r", encoding="utf-8") as f:
        cache = json.load(f)

    print(f"Loaded {len(cache)} cached playlists.")

    suspect = {
        pid: v for pid, v in cache.items()
        if isinstance(v, list) and len(v) == 3
        and v[0] == "" and v[1] is None and v[2] is None
    }
    print(f"Found {len(suspect)} suspect entries (owner='', total=None, cover=None).")

    if suspect:
        for pid in list(suspect.keys())[:10]:
            print(f"  removing: {pid}")
        if len(suspect) > 10:
            print(f"  ...and {len(suspect) - 10} more")

    cleaned = {pid: v for pid, v in cache.items() if pid not in suspect}

    unknown_count = sum(1 for v in cleaned.values() if isinstance(v, list) and len(v) == 3 and v[0] == "__UNKNOWN__")
    if unknown_count:
        print(f"Note: {unknown_count} entries are marked __UNKNOWN__ (genuinely deleted playlists) — "
              f"these are correct and left as-is, not part of the cleanup.")

    with open(CACHE_FP, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Done. Cache now holds {len(cleaned)} entries (removed {len(suspect)}).")
    print("These will get a genuine re-check — respecting the normal budget/circuit "
          "breaker — on the next sync run.")


if __name__ == "__main__":
    main()
