# Fantasy Breakout Alert Bot

Messages you on Telegram when an NFL or NBA player massively outperforms
their projection in a game -- football uses your league's point scoring,
basketball uses a 9-cat z-score (since there's no single "points" number in
a categories league).

## How it works

- Runs every 5 minutes via GitHub Actions (free, no server needed).
- Pulls player projections + live/actual stats from Sleeper's API.
- Football: alerts when actual points >= 130% of projection AND >= 12 points.
- Basketball: alerts when a player's combined deviation across all 9
  categories crosses a z-score threshold (see `scoring.py` for the math).
- Sends the alert via a Telegram bot. A player isn't just alerted once and
  ignored for the rest of the game -- each alert resets that player's
  "baseline" to their current level, and the next alert only fires once they
  clear the same threshold again relative to that new baseline. Plateauing
  after a breakout doesn't spam repeat texts, but a player who keeps climbing
  gets escalating labels: **BREAKOUT** -> **DOUBLE BREAKOUT** -> **3x
  BREAKOUT** and so on. (Football: baseline * pct_threshold again. Basketball:
  baseline + zscore_alert_threshold again, since z-scores can be negative.)
- Optionally (see step 6), if there's an open roster spot, the message
  includes tappable **Add** / **No thanks** buttons.
- **Extreme outliers get a \U0001F525 fire emoji** on the label -- a performance
  clearing 1.5x the normal breakout bar (tunable via `extreme_multiplier`)
  stands out from routine breakouts at a glance.
- **Multiple breakouts in the same check get combined into ONE message**
  instead of a flood of separate pings, with one Add/No-thanks button row
  per addable player.
- **If a Yahoo manager is set up, game status (Live/Final) gets tagged onto
  the alert** when resolvable, so you know whether the number could still
  climb. (No quarter/clock data is available -- just whether the game is
  still going or over; see the note in step 6.)
- **An optional once-daily digest** (step 7) sends a single end-of-day
  summary of everything that broke out, for a catch-up read instead of
  piecing together scattered pings.

## One-time setup

### 1. Create your Telegram bot
1. In Telegram, message **@BotFather** -> send `/newbot` -> follow the
   prompts (pick any name/username). It gives you a **bot token** like
   `123456789:AAExampleTokenHere`. Keep it private.
2. Search for your new bot's username in Telegram and open a chat with it.
   Send it any message, like `/start`. This is required -- a bot can't
   message you first until you've messaged it.
3. On your own computer: `pip install requests`, then run:
   ```
   python telegram_setup.py YOUR_BOT_TOKEN
   ```
   This prints your `chat_id`.

### 2. Put this code in a GitHub repo
1. Create a new **private** GitHub repo.
2. Push these files to it (this folder is a complete repo -- just
   `git init`, `git add .`, `git commit`, and push to your new repo).

### 3. Add your secrets
In your repo: Settings -> Secrets and variables -> Actions -> New repository secret.
Add:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### 4. Add your config
Copy `config.example.json` to `config.json`, adjust:
- `football.scoring_settings` to match your league's actual point values
  (check your league settings page).
- Thresholds, if you want to change them later.
- Commit `config.json` to the repo (it has no secrets in it, just settings).

### 5. Turn it on
The workflow in `.github/workflows/fantasy_alerts.yml` is already set to run
every 5 minutes automatically once it's on GitHub's default branch. To test
it immediately without waiting: go to the **Actions** tab -> "Fantasy Breakout
Alerts" -> **Run workflow**.

**To confirm your Telegram setup works without waiting for a real breakout:**
check the "Send a test Telegram message" box when you click **Run workflow**.
This runs `test_telegram.py` instead of the real check and sends a fake
breakout alert through your actual bot token/chat_id -- if it arrives, your
Telegram setup is confirmed working end-to-end. (You can also run
`python test_telegram.py` locally the same way, as long as `config.json`
exists in that folder.)

### 6. (Optional) Turn on Yahoo availability + add/drop suggestions
Once this is on, **every** breakout alert gets a line showing that player's
status in your Yahoo league: **Available as FA**, **On waivers**, or
**Rostered**. By default (`quiet_rostered_players: true`), rostered players
are silently skipped from notification entirely -- since the whole point is
catching players you might actually add, a breakout from someone already on
another team isn't actionable and just adds noise. They're still logged to
the dashboard if you're curious; set `quiet_rostered_players` to `false` in
`config.json` if you'd rather see every breakout regardless of availability.

On top of that, whenever there's an open bench/IR spot on your roster AND
the player is actually available, the message also gets tappable **Add** /
**No thanks** buttons. It never picks who to drop -- it only offers Add when
a spot is already open, and only acts after you tap Add.

1. Go to https://developer.yahoo.com/apps/create/ and create an app.
   Check "Fantasy Sports" under API Permissions, with Read/Write access.
   Note the Client ID and Client Secret.
2. On your own computer (not in GitHub Actions): `pip install yahoo_oauth`,
   then run:
   ```
   python yahoo_auth_setup.py YOUR_CLIENT_ID YOUR_CLIENT_SECRET
   ```
   Follow the printed link, log into Yahoo, approve access, paste the code
   back into the terminal. This creates `oauth2.json` locally.
3. Copy the entire contents of that `oauth2.json` file into a new GitHub
   secret called `YAHOO_OAUTH_JSON`. Do not commit the file itself.
4. Find your league key and team key (visible in your league's Yahoo URL,
   e.g. `nfl.l.123456` is the league key and `nfl.l.123456.t.4` is the team
   key). Add them as GitHub secrets rather than in `config.json` -- this
   keeps the repo from revealing which specific league/team you're in,
   which matters if you ever make the repo public:
   - `YAHOO_NFL_LEAGUE_KEY`, `YAHOO_NFL_TEAM_KEY`
   - `YAHOO_NBA_LEAGUE_KEY`, `YAHOO_NBA_TEAM_KEY`
5. Set `roster_management.enabled` to `true` in `config.json`.

**Known limitations of this feature:**
- Player matching between Sleeper (stats source) and Yahoo (your roster) is
  done by name, since the two platforms don't share player IDs. This is
  reliable almost all the time but could rarely mismatch two players with
  identical names -- double-check the name in the message before tapping Add.
- The availability status only distinguishes what Yahoo's API actually
  reports: "Available as FA" (free agent, claimable now), "On waivers"
  (in the locked claim period), or "Rostered" (assumed -- the player wasn't
  found in the free-agent/waiver scan, most likely because someone owns
  them, though this shares the name-matching caveat above).
- One Yahoo connection is built per sport per run (not per player), and its
  free-agent list is cached for that run -- checking availability on every
  single breakout is efficient even on a heavy NFL Sunday.
- If your league uses FAAB (dollar-bid waivers) rather than plain waiver
  priority, the add call doesn't currently submit a bid amount -- see the
  comment in `yahoo_client.py` for where to extend it.
- "Open bench/IR spot" detection compares your roster's filled positions
  against your league's roster settings. It's been kept intentionally simple
  -- if it ever suggests an add when you don't actually have room (or misses
  a spot that is open), check `get_open_bench_or_ir_slots()` in
  `yahoo_client.py`.
- Button taps are picked up on the next scheduled run (every 5 min), not
  instantly -- there's no live webhook server involved, so expect up to a
  ~5 minute delay between tapping Add and the roster move going through.
- Game-status tagging (Live/Final) relies on an unofficial Sleeper schedule
  endpoint. It's confirmed to work for NFL; NBA support is unverified and
  may just silently not show a tag if Sleeper's shape differs there. Either
  way, only whether the game is underway or over is available -- no
  quarter/clock detail exists in this data at all.
- Batched messages (multiple breakouts in one check) don't support editing
  on confirm/decline, since editing would erase the other players' info in
  that same message -- tapping Add or No thanks on a batched suggestion
  sends a short new follow-up message instead, leaving the original message
  as-is.

### 7. (Optional) Turn on the end-of-day digest
Sends one summary message (see `daily_digest.py`) of everything that broke
out today, for a single catch-up read instead of piecing together scattered
pings throughout the day. Only includes what was actually sent to you --
anything `quiet_rostered_players` suppressed doesn't clutter the digest
either. Sends nothing on a day with no breakouts.

Nothing to configure -- `.github/workflows/daily_digest.yml` runs once near
the end of a typical game day and will start working once it's on the
default branch. "End of day" is an approximation (a fixed daily time, not
truly "after the last game") -- very late West Coast NBA games might finish
after it runs; check the dashboard for anything after that point.

### 8. (Optional) Turn on the dashboard
A simple read-only webpage showing recent breakouts and any pending add
suggestions, hosted free by GitHub. No login, no server to run.

1. In your repo: Settings -> Pages -> under "Build and deployment", set
   Source to "Deploy from a branch", branch `main`, folder `/docs`. Save.
2. GitHub gives you a URL like `https://yourusername.github.io/your-repo/`.
   Bookmark it on your phone.
3. It updates automatically each time the bot runs (every 5 min).

**Important:** if your repo is private, GitHub Pages on a free/Pro personal
account still publishes the page itself *publicly* -- anyone with the exact
URL could view it, even though your repo's code stays private. Nothing on
the dashboard is sensitive (just player names and pending-add codes), and a
random stranger can't act on a pending suggestion since button taps are only
honored from your specific Telegram chat_id (see security note below) --
but don't share the dashboard URL if you'd rather keep it fully private.

### 9. (Optional but recommended) Turn on schedule-aware checking
Without this, the bot checks every 5 minutes, 24/7/365 -- including at
4am in the middle of July when nothing's happening. This adds a second,
lightweight workflow that runs once a day (~3am ET) and figures out
whether today is actually an NFL day and/or NBA day, so the main checker
can skip whichever sport isn't in play.

Nothing to configure -- `.github/workflows/daily_schedule.yml` is already
set up and will start running once it's on the default branch. It writes
`schedule_windows.json`, which `main.py` reads automatically.

**How "today is a game day" is determined:**
- **NFL**: a day-of-week heuristic (Thu/Sun/Mon, +Sat from week 15 on) --
  Sleeper doesn't expose an actual schedule endpoint. For a one-off game
  that falls outside that pattern (e.g. a Christmas Day game on a
  Friday), add the date to `scheduling.extra_nfl_game_dates` in
  `config.json`.
- **NBA**: a real check, not a guess -- it asks Sleeper for today's
  projections and treats a non-empty result as "games are happening."

The main workflow's cron window is also narrowed to `16:00-05:59 UTC`
(roughly noon-1am Eastern) rather than running truly 24/7, since games
never happen outside that window regardless of sport or day.

**Manually re-checking or overriding the schedule:** go to the Actions tab
-> "Daily Game Schedule Check" -> **Run workflow** any time, not just at
3am. Two checkboxes there let you force NFL and/or NBA to be treated as
active today, bypassing the normal detection entirely -- useful if you know
something changed after the 3am check ran (a weather-postponed game moved
to an unexpected day, for instance). Note that running it with both boxes
unchecked re-runs the normal checks fresh: this is a real re-check for NBA
(it re-queries live data), but will reproduce the same result for NFL,
since that heuristic is date-based rather than data-based -- use the force
checkbox or add the date to `extra_nfl_game_dates` instead.

## Important things to know

- **Button taps are only honored from your own Telegram chat.** Every
  callback (button tap) is checked against your configured
  `TELEGRAM_CHAT_ID` before anything happens -- see `_is_authorized()` in
  `telegram_notifier.py`.
- **GitHub disables scheduled workflows after 60 days of repo inactivity.**
  If you don't touch the repo for 2 months, go back into the Actions tab and
  re-enable it (one click).
- **Private repo Actions minutes are limited** (2,000 free minutes/month on
  GitHub's free tier). At the current 5-minute interval, checks only run
  during the ~14-hour daily window when games are ever actually happening
  (step 9), but that still adds up to roughly 5,000 minutes/month -- well
  over the free tier. If you're on a private repo, either make it public
  (unlimited free minutes, and the code has no identifying info in it --
  see the earlier note on league/team keys living in secrets) or space the
  interval back out in `fantasy_alerts.yml`'s cron. The daily scheduler and
  digest workflows are a single run each per day and barely register.
- **Sleeper's stats/projections endpoints are unofficial and undocumented.**
  Community sources disagree on the exact URL shape (whether `/v1/` is
  included, whether `season_type` is a path segment or query param), so
  `sleeper_client.py` tries several known shapes each call and uses
  whichever one actually returns data -- see `_candidate_urls()`. If alerts
  ever stop working again, set `DEBUG = True` at the top of that file; the
  Actions log will show every URL shape it tried and which one (if any)
  succeeded.
- The basketball z-score formula is a reasonable heuristic, not gospel --
  feel free to tune `zscore_alert_threshold` in `config.json` after seeing a
  week or two of real alerts (lower it if you want more, raise it for fewer).
- **Football scoring now covers offense, kickers, and D/ST** (including
  tiered field goals by distance and points-allowed brackets) -- see
  `config.example.json`. One category from a typical league rules page,
  "Extra Point Returned" (defense returns a blocked PAT for 2), doesn't have
  a confirmed Sleeper stat field and isn't included; it's a rare enough play
  that it's unlikely to matter, but flagging it for completeness.

## Files

- `main.py` -- entry point, runs each cycle
- `game_schedule_checker.py` -- daily job that determines today's active sports
- `sleeper_client.py` -- all Sleeper API calls
- `scoring.py` -- breakout detection logic for both sports
- `telegram_notifier.py` -- Telegram message sending + button-tap polling
- `telegram_setup.py` -- one-time local script to find your chat_id
- `test_telegram.py` -- sends a fake breakout alert to confirm Telegram setup works
- `daily_digest.py` -- sends the once-daily breakout summary
- `yahoo_client.py` -- Yahoo roster/free-agent lookups and adds
- `yahoo_auth_setup.py` -- one-time local script to authorize Yahoo access
- `config.example.json` -- copy to `config.json` and edit
- `docs/index.html` -- the optional read-only dashboard page
- `.github/workflows/fantasy_alerts.yml` -- the main 5-min scheduler
- `.github/workflows/daily_schedule.yml` -- the once-a-day schedule check
- `.github/workflows/daily_digest.yml` -- the once-a-day digest
