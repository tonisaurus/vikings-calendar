# Vintage Vikings Soccer Calendar

A subscribable calendar of Elliott's Vintage Vikings' games in the
[Golden Gate Women's Soccer League](https://www.ggwsl.org) (GGWSL), generated from the league website and
hosted on GitHub Pages.

**Subscribe:** https://tonisaurus.github.io/vikings-calendar/

## What the calendar shows

| Game state | Event title |
|---|---|
| This week's game | `Killer Tomatoes (1st, 2-0) vs Vintage Vikings (4th, 0-1-1)` plus our group's standings in the description |
| Later games | `Spitfires vs Vintage Vikings` |
| Played games | `Wasabi 3 - 2 Vintage Vikings (L)` plus the standings as they stood the week of that game |
| Forfeits | `Hot Flashes FF - 2 Vintage Vikings (W, forfeit)` |
| Called-off games | `RAINED OUT: Wasabi vs Vintage Vikings` (also `CANCELLED:` and `POSTPONED:`), marked cancelled in the calendar |
| No kickoff time yet | An all-day event on the game date |

The home team is always listed first. Records are wins-losses(-draws). Each event's location is the venue's
address from the league's venue page, and the description names the field (e.g. `Beach #4 (Turf)`). Games
are 90 minutes long in the calendar.

Standings are limited to the teams on our schedule. The league splits the Over-35 division into groups and
ranks each on its own. The team promoted from the lower group each season plays teams in both groups but is
listed in the lower one, so it is slotted into our group's table by points, then goal difference, then goals
for (after any team it ties with). Our group's own order is kept exactly as the league has it.

## How it works

- [`build_calendar.py`](build_calendar.py) reads the team page
  (`/LibLeague/Team/Team.php?PARAM_TEAM_CODE=VVI`), which without a season shows the league's current season.
  Its season selector lists every season the team has played, and each season's page (`+PARAM_SEASON_CODE=2026f`)
  has the schedule, results and division standings. Python 3.9+, no dependencies.
- The page is read strictly, and a run fails instead of publishing a wrong calendar when something does not
  add up: a missing table, a row with the wrong number of columns, a date on a different weekday than the
  page's own day column says (the year is inferred from the season, so this catches a wrong year), or a
  kickoff hour that is not 1-12. Times are printed without AM/PM, so 8-11 are read as morning and 12-7 as
  afternoon, all Pacific time. Parsing was checked against every season back to 2009.
- Home games are printed in bold, and the game code lists the home team first. If the two ever disagree
  (they never have), the game is listed as our home game and its description says home and away are unclear.
- The league's game code (e.g. `G0_WAS_VVI`: round, home team, away team) is the event `UID`, so a
  rescheduled game or a posted score updates the existing calendar entry instead of creating a new one.
- The league only publishes the current table. Each run stores it against every game whose result is posted
  and whose week is still running (up to 6 days after the game, or the day before our next game), so played
  games keep the table from their week. Seasons fetched for the first time after they ended only have the
  final table, which is shown on their last game.
- The current season is fetched every run. Other seasons are fetched until 14 days after their last game,
  then served from [`seasons/`](seasons/). `keep_seasons` in `config.json` (currently 4, two years) sets how
  many of the most recent seasons stay in the calendar; older ones drop out and their cache files are deleted
  (git history still has them). Remove the setting to keep every season.
- Venue addresses are fetched once per venue and kept in [`venues.json`](venues.json). Edit an entry there to
  fix an address the league has wrong; the script only fetches venues it has not seen.
- [`state.json`](state.json) remembers a content hash per event so `SEQUENCE` and `LAST-MODIFIED` only
  change when an event actually changes. That also means a run only commits when there is news.
- [`.github/workflows/update-calendar.yml`](.github/workflows/update-calendar.yml) runs the script three times
  a day (05:23, 14:23 and 20:23 UTC; the first is Sunday night Pacific, when game scores are usually in), and
  on any change to the config or script, then commits the result. GitHub Pages serves the `docs/` folder.
- If the site is unreachable the run retries a few times, then fails without committing, so subscribers keep
  the last good calendar. A run that would publish an empty calendar fails the same way.

## Alerting

- A failed run opens a GitHub issue labelled `calendar-alert` (or comments on the open one) with a link to
  the run log, and the next successful run closes it.
- Each run pings a [healthchecks.io](https://healthchecks.io) check stored in the `HEALTHCHECK_URL`
  repository secret: success pings `$URL`, failure pings `$URL/fail`. If no ping arrives for a day,
  healthchecks.io emails the owner. This also catches GitHub silently disabling the schedule, which it does
  after 60 days without commits; re-enable it from the Actions tab if that happens.

## Next season

Nothing to do. When the league makes the new season current, the team page switches to it and the calendar
picks it up. The only reason to touch `config.json` is the team changing its name (`team`, the display name)
or league team code (`team_code`, the `PARAM_TEAM_CODE` in the team page URL).

Requests per run: one for the current season's page, plus one per other season during its 14-day window
and one per venue the calendar has not seen before. So 1 request most of the year.

## Running locally

```bash
python3 build_calendar.py --dry-run   # print the calendar
python3 build_calendar.py             # write docs/vikings.ics, state.json, venues.json and seasons/
python3 -m unittest discover -s tests # run the tests
```

The test fixtures in [`tests/fixtures/`](tests/fixtures/) are real team and venue pages trimmed to the parts
the script reads (team contacts, news and the roster removed).
