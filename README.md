# Fantasy Breakout Alert Bot

Messages you on Telegram when an NFL or NBA player massively outperforms
their projection in a game -- football uses your league's point scoring,
basketball uses a 9-cat z-score (since there's no single "points" number in
a categories league).

## How it works

- Runs every 10 minutes via GitHub Actions (free, no server needed).
- Pulls player projections + live/actual stats from Sleeper's API.
- Football: alerts when actual points >= 130% of projection AND >= 12 points.
- Basketball: alerts when a player's combined deviation across all 9
  categories crosses a z-score threshold (see `scoring.py` for the math).
- Sends the alert via a Telegram bot. Remembers who it already alerted on so
  you don't get the same message every 10 minutes.
- Optionally (see step 6), if there's an open roster spot, the message
  includes tappable **Add** / **No thanks** buttons.

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
every 10 minutes automatically once it's on GitHub's default branch. To test
it immediately without waiting: go to the **Actions** tab -> "Fantasy Breakout
Alerts" -> **Run workflow**.

### 6. (Optional) Turn on add/drop suggestions
This adds tappable **Add** / **No thanks** buttons to a breakout message
whenever there's an open bench/IR spot on your Yahoo roster. It never picks
who to drop -- it only acts when a spot is already open, and only after you
tap Add.

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
   key), and put them in `config.json` under `roster_management`.
5. Set `roster_management.enabled` to `true` in `config.json`.

**Known limitations of this feature:**
- Player matching between Sleeper (stats source) and Yahoo (your roster) is
  done by name, since the two platforms don't share player IDs. This is
  reliable almost all the time but could rarely mismatch two players with
  identical names -- double-check the name in the message before tapping Add.
- If your league uses FAAB (dollar-bid waivers) rather than plain waiver
  priority, the add call doesn't currently submit a bid amount -- see the
  comment in `yahoo_client.py` for where to extend it.
- "Open bench/IR spot" detection compares your roster's filled positions
  against your league's roster settings. It's been kept intentionally simple
  -- if it ever suggests an add when you don't actually have room (or misses
  a spot that is open), check `get_open_bench_or_ir_slots()` in
  `yahoo_client.py`.
- Button taps are picked up on the next scheduled run (every 10 min), not
  instantly -- there's no live webhook server involved, so expect up to a
  ~10 minute delay between tapping Add and the roster move going through.

### 7. (Optional) Turn on the dashboard
A simple read-only webpage showing recent breakouts and any pending add
suggestions, hosted free by GitHub. No login, no server to run.

1. In your repo: Settings -> Pages -> under "Build and deployment", set
   Source to "Deploy from a branch", branch `main`, folder `/docs`. Save.
2. GitHub gives you a URL like `https://yourusername.github.io/your-repo/`.
   Bookmark it on your phone.
3. It updates automatically each time the bot runs (every 10 min).

**Important:** if your repo is private, GitHub Pages on a free/Pro personal
account still publishes the page itself *publicly* -- anyone with the exact
URL could view it, even though your repo's code stays private. Nothing on
the dashboard is sensitive (just player names and pending-add codes), and a
random stranger can't act on a pending suggestion since button taps are only
honored from your specific Telegram chat_id (see security note below) --
but don't share the dashboard URL if you'd rather keep it fully private.

## Important things to know

- **Button taps are only honored from your own Telegram chat.** Every
  callback (button tap) is checked against your configured
  `TELEGRAM_CHAT_ID` before anything happens -- see `_is_authorized()` in
  `telegram_notifier.py`.
- **GitHub disables scheduled workflows after 60 days of repo inactivity.**
  If you don't touch the repo for 2 months, go back into the Actions tab and
  re-enable it (one click).
- **Private repo Actions minutes are limited** (2,000 free minutes/month on
  GitHub's free tier). Running every 10 minutes, 24/7, all season could use
  a meaningful chunk of that. If you hit the limit, either make the repo
  public (unlimited free minutes) or narrow the cron schedule in the workflow
  file to only cover typical game windows (evenings/weekends).
- **Sleeper's stats/projections endpoints are unofficial and undocumented.**
  They're widely used by the fantasy dev community and have been stable, but
  Sleeper could change them without warning. If alerts stop working, check
  `sleeper_client.py` first -- set `DEBUG = True` at the top of that file and
  look at the raw API responses in the Actions log.
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
- `sleeper_client.py` -- all Sleeper API calls
- `scoring.py` -- breakout detection logic for both sports
- `telegram_notifier.py` -- Telegram message sending + button-tap polling
- `telegram_setup.py` -- one-time local script to find your chat_id
- `yahoo_client.py` -- Yahoo roster/free-agent lookups and adds
- `yahoo_auth_setup.py` -- one-time local script to authorize Yahoo access
- `config.example.json` -- copy to `config.json` and edit
- `docs/index.html` -- the optional read-only dashboard page
- `.github/workflows/fantasy_alerts.yml` -- the scheduler
