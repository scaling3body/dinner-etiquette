# Alert quality and reliability

The October 2026 update implements requested suggestions 1, 2, 3, 7, 8, 9,
10, 11, 12, 13, 14 and 15. Yahoo activation remains off pending credential
confirmation. No waiver-pickup ranking was added to the digest.

## Notifications and screening

- NBA messages compare actual and projected counting stats, minutes and
  shooting, then identify the three largest positive score contributions.
- Shooting percentages use shot volume, with bounded contributions. Fewer
  than five field-goal attempts or three free throws do not contribute a
  percentage score. No attempts are neutral, rather than a shooting penalty.
- Default NBA screening requires 12 minutes played and 12 combined
  PTS/REB/AST/STL/BLK/3PM. Defensive specialists can qualify without high
  point totals. All limits live in `config.json` and its example.
- A new scoring version silently migrates existing NBA alert baselines to
  avoid comparing old percentage scores with the new weighted calculation.
- A matched ESPN scoreboard provides quarter, clock and final status.
  Both teams must match when Sleeper supplies an opponent. Unknown or
  mismatched progress is omitted; it never blocks an alert.

## Opportunity and trend signals

Opportunity alerts use observed game clock and player minutes, comparing
minutes pace with the player's projected minutes. They require at least
18 game minutes elapsed, eight player minutes, and the normal screening
floor. Estimated pace is labeled as an estimate. Increased shot/FT/turnover
rate is a usage **proxy**, not an official usage percentage. Starting-role
alerts require explicit actual/projected start flags; they are never
inferred from a player's name or a missing field. NFL snap-share signals
likewise require both actual and projected snap fields.

Sustained-breakout alerts require three qualifying results within the last
five completed games. Repeated checks update a game; they never count as
multiple games. NBA results older than 30 days and NFL results older than
70 days do not contribute. Trends build as observed completed games
accumulate; this release does not invent historical performances.

## Delivery and recovery

`delivery_state.json` stores messages before sending and checkpoints each
confirmed Telegram message. Only a fully delivered alert advances its
breakout baseline or enters the notified history. Failed parts remain
queued, honor Telegram retry delays, and retry on a later execution.
Confirmed parts are not resent during a normal partial retry. Receipts can
repair history and baselines if a process stops before the other files save.

Messages are packed and split conservatively below Telegram's length
limit, including emoji. The digest also checkpoints parts, freezes its
content during a retry, and groups each player's strongest result by game.
Its reporting date uses the structured game date, including late alerts.

An ambiguous network timeout can still produce a duplicate if Telegram
accepted a message but the acknowledgement was lost; Telegram offers no
send-message idempotency key. A process/runner lost before GitHub persists
its local files can also lose its newest receipts. Persistence errors now
fail visibly instead of being ignored.

Catch-up defaults to two previous NBA game days and two previous NFL
calendar days, configurable up to seven. NBA checks use the prior evening
until 6am Eastern. Historical slates remain eligible for recheck until at
least 30 minutes after the first observed final status, allowing final stat
updates to arrive. Catch-up and retried delivery messages are labeled.

## Commands and dashboard

- `/status`: completed/started checks, source freshness and failed deliveries.
- `/recent [nba|nfl] [1-10]`: confirmed alerts, newest first.
- `/settings`: scoring, screening, batching, catch-up and Yahoo status.
- `/help` or `/start`: commands and response timing.
- Existing `/check` and `/tier` commands remain available.

Commands are collected during bot runs, so GitHub scheduler delays also
delay replies. Commands do not enter the performance digest. The dashboard
shows check age, underlying NBA stat age, game progress and pending/failed
message parts. Its browser clock updates the overdue warning even when the
JSON snapshot stops updating. It refreshes data every minute and renders
untrusted text through DOM text nodes.

## Yahoo context (implemented, currently disabled)

When enabled after credential confirmation, alerts include availability,
own-roster status, active/bench capacity, positional coverage and roster
status flags. Free agents and waivers are scanned separately and cached
per run. Failed or ambiguous matches do not silently become actionable
roster suggestions. An empty IR-only slot does not make a healthy player
addable. Add buttons require a free agent and usable roster capacity, and
those conditions are checked again before the explicit button action.
There are no automatic drops or waiver bids.

## Checks and efficiency

Run `python -m unittest discover -v` and `node test_dashboard.js` for offline
regressions. CI runs them without secrets or live notification calls.
Public-data smoke tests use an isolated copy and mock all sends.

Player lists, Yahoo scans, scoreboard requests and identical Sleeper
requests are reused within a run. NBA category deviations are computed
once per slate, rather than for every player. Invalid API URL shapes are
not retried repeatedly. All three state-writing jobs share a concurrency
group, write JSON atomically, and use an explicit state-file allowlist with
visible Git push failures. Python dependencies are cached in Actions.
