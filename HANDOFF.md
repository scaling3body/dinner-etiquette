# Handoff: dinner-etiquette (fantasy sports alert bot)

Written 2026-09-19 for whichever AI assistant picks this project up next.
This is a status snapshot, not permanent documentation — update or delete
sections as they go stale. See `README.md` for the actual user-facing
setup/usage docs; this file is about *state and history*, not features.

## What this project is

A Telegram bot that watches Sleeper (NFL + NBA) for player "breakout"
performances vs. projection, alerts on Telegram, and optionally checks
Yahoo Fantasy roster status (free agent / waivers / rostered) to suggest
adds. Runs on a GitHub Actions schedule (`.github/workflows/fantasy_alerts.yml`,
every 5 min during game hours), not a long-lived server. State (alert
history, health tracking, caches) is committed back to the repo by the
workflow itself after each run, since there's no persistent host.

Key files: `main.py` (orchestrator), `scoring.py` (breakout math + escalation),
`sleeper_client.py` / `yahoo_client.py` (API wrappers), `health.py`
(failure tracking + Yahoo circuit breaker), `telegram_notifier.py`,
`tier_scraper.py` (`/check` command backing), `game_schedule_checker.py`
+ `daily_digest.py` (separate daily-cron workflows).

## Current live state (as of this writing)

- `config.json`: `roster_management.enabled` is **false**. Yahoo integration
  is fully off — no Yahoo API calls happen, no Yahoo-related alerts fire.
- The main `fantasy_alerts.yml` workflow is running successfully every
  ~5-8 min on `main` and persisting state correctly (verify with
  `git log --oneline -5` on `main` — should show recent "Update alert
  state [skip ci]" commits close together).
- Branch `claude/fantasy-bot-memory-import-sabdyo` and `main` are kept in
  sync (this session pushed directly to both after each fix — see "Branch
  situation" below for why).

## 🔴 Outstanding: Yahoo credentials still need rotation

`oauth2.json` was committed to git history (commit `e632a99`) with a
**real, live** Yahoo OAuth `access_token`, `consumer_key`,
`consumer_secret`, and `refresh_token` in plaintext. This session:

1. Scrubbed it from all git history with `git filter-repo` and force-pushed
   both `main` and the feature branch.
2. Added `.gitignore` for `oauth2.json` going forward.

**What's NOT done, and can't be done by an AI session**: the actual Yahoo
app credentials themselves were never rotated. Scrubbing git history does
not invalidate already-exposed secrets — anyone who cloned before the
scrub, or GitHub's own object cache, may still have them. The repo owner
needs to:

- Regenerate the Yahoo app's consumer key/secret in the Yahoo Developer
  console.
- Re-run `yahoo_auth_setup.py` to get a fresh refresh token.
- Update the `YAHOO_OAUTH_JSON` GitHub secret.
- Only then set `roster_management.enabled: true` in `config.json`.

Two one-shot reminders were scheduled via this session's `CronCreate` for
2026-09-18 9am and 2026-09-19 9pm (Vancouver time, fixed UTC-7 — this user
says their timezone no longer observes DST). **Those reminders are
session-scoped and almost certainly did not fire** if this handoff is
being read in a new session — don't assume the user was reminded.
If you're a future assistant reading this: ask the user directly whether
they've rotated the credentials before touching `roster_management`.

## What this session fixed (chronological, all on `main`)

1. **State persistence was completely broken.** The workflow's `git add`
   line included `tier_cache/*.json`, a glob that matched nothing until
   `/check` had run once. `git add` aborts *entirely* (stages nothing at
   all) if any pathspec matches zero files — so `health_state.json`,
   `telegram_offset.json`, `alerted.json`, etc. never got committed,
   ever, across 300+ runs. Effects: the Yahoo circuit breaker never
   persisted, Telegram never learned an old command had been handled (so
   it kept re-replying to stale `/check` messages every run), and the
   breakout escalation counter (`alerted.json`) reset every run, so a
   player's second breakout showed as a fresh "BREAKOUT" instead of
   "DOUBLE BREAKOUT". Fixed with `shopt -s nullglob` before the `git add`.
2. **`[Final]` tag showing on live games.** `get_schedule()`'s endpoint
   URL has no week segment, so it likely returns the whole season, and
   `get_team_game_status()` returned the *first* schedule entry matching
   a team abbreviation with no week filter — often an old, already-
   completed game for that team. Fixed by threading `week` through and
   filtering to it, with a documented fallback for schedules that don't
   carry a `week` field, and a fail-safe (return no tag, not a guess) if
   `week` values are present but none match.
3. **`oauth2.json` secret leak** — see above.
4. **Player cache and tier cache never actually refreshed.** Both used
   `os.path.getmtime()` to judge staleness, but GitHub Actions does a
   fresh `git checkout` every run, which sets every file's mtime to "now"
   regardless of commit history — so the staleness check always read as
   fresh. Fixed by tracking an explicit stored timestamp instead (a
   `.fetched_at` sidecar file for player caches; tier cache already wrote
   `fetched_at` into its own JSON but wasn't reading it back).
5. **UTC vs. Eastern day-boundary bugs**, three places:
   - `game_schedule_checker.py` defined an `ET_OFFSET_HOURS` constant and
     never applied it — replaced with real `zoneinfo`-based Eastern time
     (stdlib, no new dependency).
   - `main.py`'s `run_basketball()` used UTC `date.today()` to pick which
     day's NBA slate to fetch — wrong day during US evening hours where
     UTC has already rolled over. Now uses Eastern date.
   - `daily_digest.py` compared UTC date strings against UTC timestamps to
     decide "today" — missed most of a real game day's evening activity.
     Fixed to compare Eastern calendar days, with a 6-hour lookback buffer
     to handle the cron's DST-dependent Eastern landing time (~11:45pm ET
     under EST vs. ~00:45am ET, already past ET midnight, under EDT).

All of the above were verified against reproduced failure scenarios
(simulated mtime-vs-fetched_at mismatches, simulated EDT/EST cron timing,
week-mismatch schedule payloads) before pushing, not just read through.

## Branch situation (read before pushing anything)

This session was originally instructed to develop on
`claude/fantasy-bot-memory-import-sabdyo` and never push elsewhere without
permission. In practice:

- Direct `git push` and GitHub API branch-creation were both denied
  (permissions) until partway through the session, when the user
  installed the Claude GitHub App.
- Even after that, writes to `.github/workflows/*` specifically require a
  separate "Workflows" permission scope on the GitHub App, distinct from
  "Contents" — this tripped up an early attempt to push a workflow-only
  fix via the Contents API.
- The user explicitly asked, more than once, to push fixes straight to
  `main` (since that's what GitHub Actions actually runs off), so both
  branches now carry identical history. If you're continuing this work,
  confirm with the user whether they still want that pattern (dev branch
  + immediate push to `main`) or want a normal PR-based flow now that
  push access works cleanly.

## Known non-urgent gaps (not yet acted on)

- FAAB (waiver-bid) leagues aren't supported by `yahoo_client.add_player()`
  — documented in that file's docstring, not fixed, low priority unless
  the user's league uses FAAB.
- No automated tests exist for any of this. All verification in this
  session was manual/ad-hoc (temp scripts, monkeypatched network calls).
- The code-review skill flagged (and this session fixed) a narrow gap in
  the week-filter fix regarding type mismatches — worth a similar level
  of scrutiny on any future schedule/API-shape assumptions in this file,
  since `get_schedule()`'s endpoint is explicitly unofficial and
  unverified per its own docstring.

## User context worth knowing

- User is in Vancouver, on a fixed UTC-7 offset (their stated timezone no
  longer switches DST) — convert times accordingly, never state UTC to
  them directly.
- User prefers direct, no-preamble answers, wants uncertainty flagged
  explicitly with a confidence level, and wants recommendations led with
  a conclusion first.
- This session runs in an ephemeral cloud container with no access to the
  user's local machine or iCloud Drive — don't assume file operations
  reach anywhere outside `/home/user/dinner-etiquette` and its git remote.
