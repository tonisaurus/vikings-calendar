#!/usr/bin/env python3
"""Build an iCalendar (.ics) feed of one GGWSL team's games from the league website.

The Golden Gate Women's Soccer League has no API: its team pages are server-rendered PHP. Each
season's team page carries the team's schedule, results and division standings, and its season
selector lists every season the team has played, so one page per season is all this needs.
Finished seasons are cached in the repository so their results stay in the calendar without
being refetched every run.

The page is read strictly. Anything unexpected (a missing table, a row with the wrong number of
columns, a time with no plausible AM/PM reading, a date that falls on a different weekday than
the page says) fails the run instead of publishing a wrong calendar.

Each game keeps a stable UID (the league's game code), so rescheduled games, posted scores and
standings changes show up as updates to existing calendar events rather than duplicates.

Stdlib only, so it runs anywhere Python 3.9+ is installed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

# A game this far in the future counts as "this week's game" and gets the
# standings/record annotation. A game that started less than FEATURED_GRACE ago
# but has no result yet keeps the annotation until the result is posted.
FEATURED_WINDOW = timedelta(days=8)
FEATURED_GRACE = timedelta(hours=24)

# A season other than the league's current one is refetched until this long after its last game
# (late score corrections), then served from the cache.
LIVE_AFTER_LAST_GAME = timedelta(days=14)

# The league only publishes the current table, so the table shown on a played game is recorded
# while it still describes that week: from when the result is posted until this many days after
# the game (or the day before our next game, if that comes sooner).
SNAPSHOT_DAYS = 6

# Kickoff times are printed without AM/PM ("1:00"). GGWSL plays between morning and early
# evening, so 8-11 are morning and 12-7 afternoon. Anything else fails the run rather than
# guessing.
MORNING_HOURS = range(8, 12)
AFTERNOON_HOURS = (12, 1, 2, 3, 4, 5, 6, 7)

LEAGUE = "league"
SCHEDULED = "scheduled"
FORFEIT = "FF"

# Game states the league uses besides "scheduled". Called-off games stay in the calendar, marked
# cancelled. Unknown states are shown as-is in the event title.
STATUS_LABELS = {"scheduled": "Scheduled", "cancelled": "Cancelled", "rainout": "Rained out", "postpnd": "Postponed", "earlyforfeit": "Forfeit"}
CALLED_OFF = {"cancelled", "canceled", "rainout", "postpnd"}
EARLY_FORFEIT = "earlyforfeit"

ICS_LINE_LIMIT = 75  # octets, per RFC 5545 section 3.1

SCHEDULE_HEADER = ["DAY", "DATE", "TIME", "VENUE", "OPPONENT", "RES", "SCO", "COMP", "STAT"]
STANDINGS_HEADER = ["TC", "TEAM NAME", "P", "W", "L", "D", "F", "GF", "GA", "GD", "PTS"]
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class PageError(ValueError):
    """The league page did not look the way this script expects."""


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Game:
    code: str  # the league's game code, e.g. "G0_WAS_VVI" (round, home team, away team)
    season: str
    start: datetime  # local time; midnight for an all-day game
    field: str
    venue_code: str | None
    opponent: str
    opponent_code: str
    home: bool
    result: str  # "W", "L" or "D" from our point of view, another code the league uses (e.g. "CCD"), or "" until posted
    our_score: str | None  # a number, or "FF" for the side that forfeited; None until posted
    their_score: str | None
    forfeit: bool = False  # either side forfeited
    competition: str = LEAGUE
    status: str = SCHEDULED
    all_day: bool = False  # no kickoff time on the page yet
    home_unclear: bool = False  # the page's bold (home) marking disagrees with the game code; shown as our home game

    @property
    def has_result(self) -> bool:
        return bool(self.result)

    @property
    def is_cancelled(self) -> bool:
        return self.status in CALLED_OFF

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


@dataclass(frozen=True)
class Standing:
    code: str
    name: str
    rank: int
    played: int
    wins: int
    losses: int
    draws: int
    forfeits: int
    goals_for: int
    goals_against: int
    goal_diff: int  # as the league publishes it, which is not always goals_for - goals_against
    points: int

    @property
    def record(self) -> str:
        record = f"{self.wins}-{self.losses}"
        return f"{record}-{self.draws}" if self.draws else record


@dataclass(frozen=True)
class Standings:
    rows: list[Standing]

    def for_team(self, code: str) -> Standing | None:
        return next((row for row in self.rows if row.code == code), None)

    @property
    def has_results(self) -> bool:
        """False before the first game of a season, when every row is still zeroes."""
        return any(row.played for row in self.rows)


@dataclass(frozen=True)
class Season:
    code: str
    name: str
    games: list[Game]
    standings: Standings  # the league's table as of the last fetch
    snapshots: dict[str, Standings] = field(default_factory=dict)  # game code -> table after that game

    @property
    def last_game_day(self) -> date | None:
        return max((g.start.date() for g in self.games), default=None)


@dataclass(frozen=True)
class Venue:
    name: str  # e.g. "Beach Chalet"
    address: str  # e.g. "1500 John F Kennedy, Golden Gate Park, San Francisco, CA"; "" when the league gives none

    @property
    def location(self) -> str:
        return f"{self.name}, {self.address}" if self.address else self.name


@dataclass(frozen=True)
class TeamPage:
    season: Season
    current: str  # the season the page shows, which is the league's current one when none is asked for
    seasons: list[tuple[str, str]]  # (code, name) for every season the team has played, newest first


# ------------------------------------------------------------------------ parsing


@dataclass
class Cell:
    text: str = ""
    header: bool = False
    bold: bool = False
    links: list[list[str]] = field(default_factory=list)  # [href, text]


@dataclass
class Option:
    value: str
    selected: bool
    text: str = ""


class PageParser(HTMLParser):
    """Collect every table as rows of cells, plus the options of every <select>.

    The site nests layout tables, so text is only credited to the innermost open table's cell.
    It also leaves some cells unclosed, so a new cell or row closes the previous cell.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[Cell]]] = []
        self.selects: list[list[Option]] = []
        self._open: list[dict] = []  # open tables: {"rows": [...], "cell": Cell | None}
        self._link: list[str] | None = None
        self._option: Option | None = None

    @property
    def _cell(self) -> Cell | None:
        return self._open[-1]["cell"] if self._open else None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attributes = dict(attrs)
        if tag == "table":
            rows: list[list[Cell]] = []
            self.tables.append(rows)
            self._open.append({"rows": rows, "cell": None})
        elif tag == "tr" and self._open:
            self._open[-1]["cell"] = None
            self._open[-1]["rows"].append([])
        elif tag in ("td", "th") and self._open:
            table = self._open[-1]
            if not table["rows"]:
                table["rows"].append([])
            style = (attributes.get("style") or "").replace(" ", "").lower()
            table["cell"] = Cell(header=tag == "th", bold="font-weight:bold" in style)
            table["rows"][-1].append(table["cell"])
        elif tag == "a" and attributes.get("href") and self._cell is not None:
            self._link = [attributes["href"], ""]
            self._cell.links.append(self._link)
        elif tag == "br" and self._cell is not None:
            self._cell.text += " "
        elif tag == "select":
            self.selects.append([])
        elif tag == "option" and self.selects:
            self._option = Option(value=attributes.get("value") or "", selected="selected" in attributes)
            self.selects[-1].append(self._option)

    def handle_endtag(self, tag: str) -> None:
        if tag == "table" and self._open:
            self._open.pop()
        elif tag in ("tr", "td", "th") and self._open:
            self._open[-1]["cell"] = None
        elif tag == "a":
            self._link = None
        elif tag in ("option", "select"):
            self._option = None

    def handle_data(self, data: str) -> None:
        if self._option is not None:
            self._option.text += data
        if self._cell is not None:
            self._cell.text += data
        if self._link is not None:
            self._link[1] += data


def text(cell: Cell) -> str:
    return " ".join(cell.text.split())


def link_param(cell: Cell, param: str) -> str | None:
    """The value of `param` in the cell's first link that has it (the site joins params with '+')."""
    for href, _ in cell.links:
        match = re.search(rf"{param}=([A-Za-z0-9_]+)", href)
        if match:
            return match.group(1)
    return None


def find_table(tables: list[list[list[Cell]]], header: list[str], what: str, required: bool = True) -> list[list[Cell]] | None:
    """The rows (header excluded) of the one table whose first row is `header`, or None when it is
    missing and not `required`."""
    matches = [t for t in tables if t and [text(c) for c in t[0]] == header]
    if not matches and not required:
        return None
    if len(matches) != 1:
        raise PageError(f"expected one {what} table with columns {header}, found {len(matches)}")
    return matches[0][1:]


def season_name(label: str) -> str:
    """'2026, Fall' -> 'Fall 2026'."""
    parts = [p.strip() for p in label.split(",")]
    return f"{parts[1]} {parts[0]}" if len(parts) == 2 else label.strip()


def parse_date(day: str, value: str, year: int) -> date:
    match = re.fullmatch(r"([A-Za-z]{3})[A-Za-z]*\.?\s*(\d{1,2})", value)
    if not match or match.group(1).lower() not in MONTHS:
        raise PageError(f"unreadable date {value!r}")
    parsed = date(year, MONTHS[match.group(1).lower()], int(match.group(2)))
    if parsed.strftime("%a").lower() != day[:3].lower():
        raise PageError(f"{value!r} {year} is a {parsed.strftime('%A')}, but the page says {day!r}")
    return parsed


def parse_time(value: str) -> tuple[int, int] | None:
    """(hour, minute) on the 24-hour clock, or None when the kickoff time is not set yet."""
    if not value:
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if not match:
        raise PageError(f"unreadable time {value!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour in MORNING_HOURS:
        return hour, minute
    if hour in AFTERNOON_HOURS:
        return (hour % 12) + 12, minute
    raise PageError(f"kickoff {value!r} has no plausible AM/PM reading; update MORNING_HOURS/AFTERNOON_HOURS")


def parse_score(value: str) -> tuple[str | None, str | None]:
    """'2-3' -> ('2', '3'), our goals first; '2-FF' for a forfeit; '' before the score is posted and
    '--' for a game that was called off. A bare code such as 'CCD' repeats the result column."""
    if value in ("", "--") or re.fullmatch(r"[A-Z]+", value):
        return None, None
    match = re.fullmatch(r"(\d+|FF)\s*-\s*(\d+|FF)", value, re.IGNORECASE)
    if not match:
        raise PageError(f"unreadable score {value!r}")
    return match.group(1).upper(), match.group(2).upper()


def parse_result(value: str, ours: str | None, theirs: str | None, status: str) -> tuple[str, bool]:
    """(W/L/D, whether either side forfeited). The league posts "FF" as the result when we forfeit,
    and "FF" in place of the other team's goals when they do; a game forfeited ahead of time can
    also carry an awarded score. When only a score is posted, the result is worked out from it.
    Rare codes the league has used (e.g. "CCD") are kept as they are."""
    value = value.upper()
    result = {"T": "D", "--": "", FORFEIT: "L"}.get(value, value)
    if not re.fullmatch(r"[A-Z]*", result):
        raise PageError(f"unreadable result {value!r}")
    if not result and ours is not None and ours.isdigit() and theirs.isdigit():
        result = "W" if int(ours) > int(theirs) else "L" if int(ours) < int(theirs) else "D"
    return result, FORFEIT in (value, ours, theirs) or (bool(result) and status == EARLY_FORFEIT)


def parse_schedule(rows: list[list[Cell]], team_code: str, season: str, tz: ZoneInfo) -> list[Game]:
    year = int(season[:4])
    games = []
    for row in rows:
        values = [text(c) for c in row]
        if len(row) != len(SCHEDULE_HEADER):
            raise PageError(f"unexpected schedule row {values}")
        day, when, kickoff, venue, opponent, result, score, competition, status = values
        game_code = link_param(row[7], "PARAM_GAME_CODE")
        opponent_code = link_param(row[4], "PARAM_TEAM_CODE")
        teams = re.fullmatch(r"G\d+_([A-Za-z0-9]+)_([A-Za-z0-9]+)", game_code or "")
        if not teams or not opponent_code:
            raise PageError(f"schedule row without a game code or opponent link: {values}")
        if {teams.group(1), teams.group(2)} != {team_code, opponent_code}:
            raise PageError(f"game code {game_code} does not match {team_code} vs {opponent_code}")
        # Home games are printed in bold, and the game code lists the home team first. They have always
        # agreed; if they ever do not, list the game as ours and say so in the event.
        home = teams.group(1) == team_code
        home_unclear = len({c.bold for c in row}) != 1 or row[0].bold != home
        if home_unclear:
            home = True
        kickoff_time = parse_time(kickoff)
        hour, minute = kickoff_time or (0, 0)
        ours, theirs = parse_score(score)
        status = status.lower() or SCHEDULED
        result, forfeit = parse_result(result, ours, theirs, status)
        games.append(Game(
            code=game_code,
            season=season,
            start=datetime(*parse_date(day, when, year).timetuple()[:3], hour, minute, tzinfo=tz),
            field=venue,
            venue_code=link_param(row[3], "PARAM_VENUE_CODE"),
            opponent=opponent,
            opponent_code=opponent_code,
            home=home,
            result=result,
            our_score=ours,
            their_score=theirs,
            forfeit=forfeit,
            competition=competition.lower() or LEAGUE,
            status=status,
            all_day=kickoff_time is None,
            home_unclear=home_unclear,
        ))
    if len({g.code for g in games}) != len(games):
        raise PageError("duplicate game codes in the schedule")
    return sorted(games, key=lambda g: g.start)


def parse_int(value: str, what: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise PageError(f"unreadable {what} {value!r} in the standings") from None


def parse_standings(rows: list[list[Cell]], team_code: str, keep: set[str]) -> Standings:
    """The division table, limited to the teams in `keep` (ours and our opponents), in the league's order.

    The league splits a division into groups with spacer rows and ranks each group on its own. Our
    group keeps the league's order. Opponents listed in another group (the team promoted from the
    lower group plays both) are slotted in by points, then goal difference, then goals for, after any
    team they tie with.
    """
    groups: list[list[tuple[str, list[str]]]] = [[]]
    for row in rows:
        if len(row) == 1 and row[0].header:
            groups.append([])  # spacer between the division's groups
            continue
        values = [text(c) for c in row]
        code = link_param(row[1], "PARAM_TEAM_CODE") if len(row) > 1 else None
        if len(row) != len(STANDINGS_HEADER) or not code:
            raise PageError(f"unexpected standings row {values}")
        groups[-1].append((code, values))

    def standing(code: str, values: list[str]) -> Standing:
        return Standing(code, values[1], 0, *(parse_int(v, h) for v, h in zip(values[2:], STANDINGS_HEADER[2:])))

    def strength(row: Standing) -> tuple[int, int, int]:
        return row.points, row.goal_diff, row.goals_for

    ours = next((g for g in groups if any(code == team_code for code, _ in g)), [])
    rows = [standing(code, values) for code, values in ours if code in keep]
    for group in groups:
        if group is ours:
            continue
        for extra in (standing(code, values) for code, values in group if code in keep):
            at = next((i for i, row in enumerate(rows) if strength(extra) > strength(row)), len(rows))
            rows.insert(at, extra)
    return Standings([replace(row, rank=rank) for rank, row in enumerate(rows, start=1)])


def parse_team_page(html: str, team_code: str, tz: ZoneInfo, require_schedule: bool = True) -> TeamPage:
    """Read a team page. Some old seasons' pages have standings but no schedule section at all;
    `require_schedule=False` reads those as seasons without games."""
    parser = PageParser()
    parser.feed(html)
    parser.close()

    season_select = next((s for s in parser.selects if s and all(re.fullmatch(r"\d{4}[a-z]", o.value) for o in s)), None)
    team_select = next((s for s in parser.selects if any(o.value == team_code for o in s)), None)
    if not season_select or not team_select:
        raise PageError("season or team selector not found; is the team code right?")
    if not any(o.value == team_code and o.selected for o in team_select):
        raise PageError(f"the page is not for team {team_code}")
    selected = [o for o in season_select if o.selected]
    if len(selected) != 1:
        raise PageError("could not tell which season the page shows")
    code = selected[0].value

    schedule = find_table(parser.tables, SCHEDULE_HEADER, "schedule", required=require_schedule)
    games = parse_schedule(schedule, team_code, code, tz) if schedule is not None else []
    standings = parse_standings(
        find_table(parser.tables, STANDINGS_HEADER, "standings"),
        team_code,
        {team_code} | {g.opponent_code for g in games},
    )
    if not standings.for_team(team_code):
        raise PageError(f"team {team_code} is missing from its own standings table")
    return TeamPage(
        season=Season(code=code, name=season_name(selected[0].text), games=games, standings=standings),
        current=code,
        seasons=[(o.value, season_name(o.text)) for o in season_select],
    )


def parse_venue(html: str, venue_code: str) -> Venue | None:
    """The venue page heading, 'Beach Chalet - 1500 John F Kennedy, Golden Gate Park, San Francisco, CA, '."""
    match = re.search(rf'<a name="#?{re.escape(venue_code)}">\s*</a>\s*<h3[^>]*>(.*?)</h3>', html, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    heading = " ".join(re.sub(r"<[^>]+>", "", match.group(1)).split()).strip(" ,")
    name, _, address = heading.partition(" - ")
    return Venue(name.strip(), address.strip(" ,")) if name.strip() else None


# ------------------------------------------------------------------------ fetching


FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 5
USER_AGENT = "vikings-calendar/1.0 (+https://github.com/tonisaurus/vikings-calendar)"


def fetch_text(url: str) -> str:
    """GET a page, retrying transient failures (network errors, 5xx) with backoff."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == FETCH_ATTEMPTS:
                raise
            error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == FETCH_ATTEMPTS:
                raise
            error = exc
        delay = FETCH_BACKOFF_SECONDS * attempt
        print(f"warning: {url} failed ({error}); retrying in {delay}s", file=sys.stderr)
        time.sleep(delay)
    raise AssertionError("unreachable")


def team_url(site: str, team_code: str, season: str | None = None) -> str:
    # The site separates query parameters with '+' rather than '&'.
    url = f"{site}/LibLeague/Team/Team.php?PARAM_TEAM_CODE={team_code}"
    return f"{url}+PARAM_SEASON_CODE={season}" if season else url


def venue_url(site: str, venue_code: str) -> str:
    return f"{site}/LibLeague/Direct.php?PARAM_VENUE_CODE={venue_code}"


# ------------------------------------------------------------------------- caching


def game_to_json(game: Game) -> dict:
    return {
        "code": game.code, "date": game.start.date().isoformat(), "time": "" if game.all_day else game.start.strftime("%H:%M"),
        "field": game.field, "venue_code": game.venue_code, "opponent": game.opponent,
        "opponent_code": game.opponent_code, "home": game.home, "result": game.result,
        "our_score": game.our_score, "their_score": game.their_score, "forfeit": game.forfeit, "home_unclear": game.home_unclear,
        "competition": game.competition, "status": game.status,
    }


def game_from_json(raw: dict, season: str, tz: ZoneInfo) -> Game:
    start = datetime.fromisoformat(f"{raw['date']}T{raw['time'] or '00:00'}").replace(tzinfo=tz)
    fields = {k: v for k, v in raw.items() if k not in ("date", "time")}
    return Game(season=season, start=start, all_day=not raw["time"], **fields)


def standings_to_json(standings: Standings) -> list[dict]:
    return [row.__dict__.copy() for row in standings.rows]


def standings_from_json(raw: list[dict]) -> Standings:
    return Standings([Standing(**row) for row in raw])


def season_to_json(season: Season) -> dict:
    return {
        "code": season.code,
        "name": season.name,
        "games": [game_to_json(g) for g in season.games],
        "standings": standings_to_json(season.standings),
        "snapshots": {code: standings_to_json(s) for code, s in sorted(season.snapshots.items())},
    }


def season_from_json(raw: dict, tz: ZoneInfo) -> Season:
    return Season(
        code=raw["code"],
        name=raw["name"],
        games=[game_from_json(g, raw["code"], tz) for g in raw["games"]],
        standings=standings_from_json(raw["standings"]),
        snapshots={code: standings_from_json(rows) for code, rows in raw.get("snapshots", {}).items()},
    )


def record_snapshots(season: Season, previous: dict[str, Standings], today: date) -> Season:
    """Keep each played game's table as it stood the week of that game.

    The current table is stored against every game whose result is posted and whose week is still
    running; once the week is over the stored table stops changing. Tables for games that have
    left the schedule are dropped.
    """
    snapshots = {code: table for code, table in previous.items() if any(g.code == code for g in season.games)}
    for i, game in enumerate(season.games):
        if not game.has_result:
            continue
        freeze = game.start.date() + timedelta(days=SNAPSHOT_DAYS)
        if i + 1 < len(season.games):
            freeze = min(freeze, season.games[i + 1].start.date() - timedelta(days=1))
        if today <= freeze:
            snapshots[game.code] = season.standings
    return Season(season.code, season.name, season.games, season.standings, snapshots)


def season_is_live(code: str, current: str, cached: Season | None, today: date) -> bool:
    if code == current or cached is None or cached.last_game_day is None:
        return True
    return today <= cached.last_game_day + LIVE_AFTER_LAST_GAME


def read_cache(cache_dir: Path, tz: ZoneInfo) -> dict[str, Season]:
    return {p.stem: season_from_json(json.loads(p.read_text(encoding="utf-8")), tz) for p in cache_dir.glob("*.json")}


def load_seasons(config: dict, cached: dict[str, Season], today: date, tz: ZoneInfo) -> tuple[list[Season], list[str]]:
    """The seasons that belong in the calendar, plus the codes of cached seasons to delete.

    The team page for the current season comes first: it is both the current season's data and the
    list of every season the team has played. Other seasons are fetched while they are live and
    served from the cache afterwards.
    """
    site, team_code = config["site"], config["team_code"]

    current = parse_team_page(fetch_text(team_url(site, team_code)), team_code, tz)
    keep = config.get("keep_seasons")
    kept = [code for code, _ in current.seasons][: keep if keep is not None else None]

    seasons = []
    for code in kept:
        previous = cached.get(code)
        if not season_is_live(code, current.current, previous, today):
            seasons.append(previous)
            continue
        # The current season's schedule must be there; a missing one means the page changed.
        page = current if code == current.current else parse_team_page(fetch_text(team_url(site, team_code, code)), team_code, tz, require_schedule=False)
        if page.current != code:
            raise PageError(f"asked for season {code} but the page shows {page.current}")
        seasons.append(record_snapshots(page.season, previous.snapshots if previous else {}, today))

    stale = sorted(code for code in cached if code not in kept)
    return sorted(seasons, key=lambda s: min((g.start for g in s.games), default=datetime.max.replace(tzinfo=tz))), stale


def load_venues(config: dict, known: dict[str, dict], games: list[Game]) -> dict[str, Venue]:
    """Venue code -> venue, fetching only venues not seen before. A venue page that cannot be read
    is skipped (the event falls back to the field name) and retried on the next run."""
    venues = {code: Venue(**raw) for code, raw in known.items()}
    for code in sorted({g.venue_code for g in games if g.venue_code} - set(venues)):
        venue = parse_venue(fetch_text(venue_url(config["site"], code)), code)
        if venue:
            venues[code] = venue
        else:
            print(f"warning: no address found on the venue page for {code}", file=sys.stderr)
    return venues


# ----------------------------------------------------------------- standings choice


def table_after(game: Game, season: Season) -> Standings | None:
    """The table as it stood after `game`: its recorded snapshot, or the final table for the last
    game of a finished season (seasons cached before snapshots existed have only that one)."""
    if game.code in season.snapshots:
        return season.snapshots[game.code]
    played = [g for g in season.games if g.has_result]
    if len(played) == len(season.games) and played and game is played[-1]:
        return season.standings
    return None


def standings_for(game: Game, season: Season, featured: bool, now: datetime) -> tuple[Standings | None, str | None]:
    """The table to show on an event, or the note to show in its place."""
    if game.has_result:
        return table_after(game, season), None
    if game.is_cancelled or game.status != SCHEDULED:
        return None, None
    if featured:
        table = season.standings if season.standings.has_results else None
        return table, None if table else "No games played yet this season."
    if game.start < now:
        return None, "Result not posted yet."
    return None, "Standings and records are added the week of the game."


def pick_featured(games: list[Game], now: datetime) -> Game | None:
    """The single upcoming game that gets the standings/record annotation."""
    for game in games:  # already sorted by start
        if game.has_result or game.is_cancelled:
            continue
        if now - FEATURED_GRACE <= game.start <= now + FEATURED_WINDOW:
            return game
    return None


# ------------------------------------------------------------------------ rendering


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def sides(game: Game, team: str, team_code: str) -> list[tuple[str, str, str | None]]:
    """[(name, code, score)] for the home side, then the away side."""
    ours = (team, team_code, game.our_score)
    theirs = (game.opponent, game.opponent_code, game.their_score)
    return [ours, theirs] if game.home else [theirs, ours]


def annotate(name: str, code: str, standings: Standings | None) -> str:
    """'Vintage Vikings (4th, 0-1-1)' when standings are known, else just the name."""
    row = standings.for_team(code) if standings else None
    if row is None:
        return name
    return f"{name} ({ordinal(row.rank)}, {row.record})"


def prefix(game: Game) -> str:
    """'RAINED OUT: ' and the like. A played or forfeited game's result already says what happened."""
    parts = []
    if game.status != SCHEDULED and not game.has_result:
        parts.append(f"{game.status_label.upper()}: ")
    if game.competition != LEAGUE:
        parts.append(f"{game.competition.title()}: ")
    return "".join(parts)


def result_text(game: Game, team: str, team_code: str) -> str:
    """'Wasabi 3 - 2 Vintage Vikings (L)', home team first."""
    (home, _, home_score), (away, _, away_score) = sides(game, team, team_code)
    tags = game.result + (", forfeit" if game.forfeit else "")
    if home_score is None:
        return f"{home} vs {away} ({tags})"
    return f"{home} {home_score} - {away_score} {away} ({tags})"


def build_summary(game: Game, team: str, team_code: str, featured: bool, standings: Standings | None) -> str:
    (home, home_code, _), (away, away_code, _) = sides(game, team, team_code)
    if game.has_result:
        return prefix(game) + result_text(game, team, team_code)
    if featured:
        return f"{prefix(game)}{annotate(home, home_code, standings)} vs {annotate(away, away_code, standings)}"
    return f"{prefix(game)}{home} vs {away}"


def standings_table(standings: Standings, team: str, team_code: str, heading: str) -> list[str]:
    # Calendar apps render descriptions in proportional fonts, so keep rows compact rather than column-aligned.
    lines = [heading]
    for row in standings.rows:
        name = team if row.code == team_code else row.name
        lines.append(f"{row.rank}. {name} {row.record}, {row.points} pts, GD {row.goal_diff:+d}")
    return lines


def build_description(game: Game, team: str, team_code: str, standings: Standings | None, note: str | None = None) -> str:
    """`standings` is the table to show: pre-game for this week's game, after-the-game for played games.
    `note` stands in for it when there is no table worth showing."""
    (home, _, _), (away, _, _) = sides(game, team, team_code)
    lines = []
    if game.has_result:
        lines.append(f"Final: {result_text(game, team, team_code)}")
    else:
        if game.status != SCHEDULED:
            lines.append(f"Status: {game.status_label}")
        lines.append(f"{home} (home) vs {away} (away)")
    if game.home_unclear:
        lines.append("Home and away are unclear: the league site lists this game inconsistently.")
    if game.competition != LEAGUE:
        lines.append(f"Competition: {game.competition}")
    if game.field:
        lines.append(game.field)
    if game.all_day and not game.has_result:
        lines.append("Kickoff time not set yet.")

    if standings:
        lines.append("")
        lines.extend(standings_table(standings, team, team_code, "Standings after this game:" if game.has_result else "Standings:"))
    elif note:
        lines.append("")
        lines.append(note)
    return "\n".join(lines)


def ics_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line: str) -> list[str]:
    """Fold a content line at 75 octets without splitting a UTF-8 character."""
    encoded = line.encode("utf-8")
    if len(encoded) <= ICS_LINE_LIMIT:
        return [line]
    out: list[str] = []
    limit = ICS_LINE_LIMIT
    start = 0
    while start < len(encoded):
        end = min(start + limit, len(encoded))
        while end < len(encoded) and (encoded[end] & 0xC0) == 0x80:  # inside a multibyte char
            end -= 1
        out.append(("" if start == 0 else " ") + encoded[start:end].decode("utf-8"))
        start = end
        limit = ICS_LINE_LIMIT - 1  # continuation lines start with a space
    return out


def ics_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def event_uid(game: Game) -> str:
    return f"ggwsl-{game.season}-{game.code}@vikings-calendar"


def event_times(game: Game, length: timedelta) -> list[str]:
    if game.all_day:
        day = game.start.date()
        return [f"DTSTART;VALUE=DATE:{day:%Y%m%d}", f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}"]
    return [f"DTSTART:{ics_datetime(game.start)}", f"DTEND:{ics_datetime(game.start + length)}"]


def render_event(game: Game, length: timedelta, summary: str, description: str, location: str, sequence: int, modified: datetime) -> list[str]:
    status = "CANCELLED" if game.is_cancelled else "CONFIRMED"
    props = [
        "BEGIN:VEVENT",
        f"UID:{event_uid(game)}",
        f"DTSTAMP:{ics_datetime(modified)}",
        f"LAST-MODIFIED:{ics_datetime(modified)}",
        f"SEQUENCE:{sequence}",
        *event_times(game, length),
        f"SUMMARY:{ics_escape(summary)}",
        f"DESCRIPTION:{ics_escape(description)}",
        f"LOCATION:{ics_escape(location)}",
        f"STATUS:{status}",
        "TRANSP:OPAQUE",
        "END:VEVENT",
    ]
    return [folded for prop in props for folded in ics_fold(prop)]


def render_calendar(name: str, tz: str, events: list[list[str]]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//vikings-calendar//GGWSL team calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)}",
        f"X-WR-TIMEZONE:{tz}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
    ]
    for event in events:
        lines.extend(event)
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------- state


def content_hash(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def next_state(previous: dict | None, digest: str, now: datetime) -> dict:
    """Bump SEQUENCE and LAST-MODIFIED only when an event's rendered content changed."""
    if previous and previous.get("hash") == digest:
        return previous
    sequence = previous["sequence"] + 1 if previous else 0
    return {"hash": digest, "sequence": sequence, "last_modified": now.isoformat()}


# ---------------------------------------------------------------------- change alerts


def game_facts(game: Game, venues: dict[str, Venue]) -> dict[str, str]:
    """What a player needs to show up at the right place and time, as the alert email words it."""
    return {
        "Date": f"{game.start:%a %b} {game.start.day}",
        "Time": "not set yet" if game.all_day else game.start.strftime("%-I:%M %p"),
        "Opponent": game.opponent,
        "Home/away": "home" if game.home else "away",
        "Location": location(game, venues),
        "Field": game.field,
        "Status": game.status_label,
    }


def game_heading(game: Game) -> str:
    return f"{game.start:%a %b} {game.start.day} vs {game.opponent} ({'home' if game.home else 'away'})"


def detect_changes(before: dict[str, Season], after: list[Season], venues: dict[str, Venue], today: date) -> list[str]:
    """Paragraphs describing schedule changes to games that have not been played yet: a new season's
    schedule appearing, games added or removed, and any change to a game's facts (see game_facts).
    Scores and standings are not schedule changes."""
    def upcoming(game: Game) -> bool:
        return game.start.date() >= today

    changes = []
    for season in after:
        games = [g for g in season.games if upcoming(g)]
        old_season = before.get(season.code)
        if old_season is None or not old_season.games:
            if games:
                lines = [f"The {season.name} schedule is up: {len(games)} upcoming game{'s' if len(games) != 1 else ''}."]
                lines += [f"  {game_heading(g)}, {game_facts(g, venues)['Time']}, {g.field}" for g in games]
                changes.append("\n".join(lines))
            continue
        old = {g.code: g for g in old_season.games}
        new = {g.code: g for g in season.games}
        for code in sorted(set(old) | set(new), key=lambda c: (new.get(c) or old[c]).start):
            was, now = old.get(code), new.get(code)
            if not any(g is not None and upcoming(g) for g in (was, now)):
                continue
            if was is None:
                facts = game_facts(now, venues)
                changes.append(f"Added: {game_heading(now)}\n  {facts['Time']} at {facts['Location']}, {now.field}")
            elif now is None:
                changes.append(f"Removed: {game_heading(was)}, no longer on the league schedule")
            else:
                old_facts, new_facts = game_facts(was, venues), game_facts(now, venues)
                diffs = [f"  {k}: {old_facts[k]} -> {v}" for k, v in new_facts.items() if old_facts[k] != v]
                if diffs:
                    changes.append("\n".join([f"Changed: {game_heading(now)}"] + diffs))
    return changes


def change_report(config: dict, changes: list[str]) -> dict[str, str]:
    subject = f"{config['team']} schedule: {len(changes)} change{'s' if len(changes) != 1 else ''}"
    body = "\n\n".join(changes + [
        "The calendar subscription updates on its own. TeamSnap does not, so update it by hand.",
        f"Calendar and TeamSnap CSV: {config['page_url']}",
    ])
    return {"subject": subject, "body": body + "\n"}


# ----------------------------------------------------------------------------- main


def location(game: Game, venues: dict[str, Venue]) -> str:
    venue = venues.get(game.venue_code or "")
    return venue.location if venue else game.field


def build(config: dict, seasons: list[Season], venues: dict[str, Venue], state: dict, now: datetime) -> tuple[str, dict]:
    team, team_code = config["team"], config["team_code"]
    tz = ZoneInfo(config["timezone"])
    length = timedelta(minutes=config["game_minutes"])
    by_code = {season.code: season for season in seasons}
    games = sorted((g for season in seasons for g in season.games), key=lambda g: g.start)
    featured = pick_featured(games, now)

    events = []
    new_state = {}
    for game in games:
        is_featured = game is featured
        table, note = standings_for(game, by_code[game.season], is_featured, now)
        summary = build_summary(game, team, team_code, is_featured, table)
        description = build_description(game, team, team_code, table, note)
        where = location(game, venues)
        uid = event_uid(game)
        digest = content_hash(summary, description, event_times(game, length), game.status, where)
        entry = next_state(state.get(uid), digest, now)
        new_state[uid] = entry
        modified = datetime.fromisoformat(entry["last_modified"])
        description += f"\n\nUpdated {modified.astimezone(tz).strftime('%b %-d, %Y %-I:%M %p %Z')}"
        events.append(render_event(game, length, summary, description, where, entry["sequence"], modified))

    return render_calendar(config["calendar_name"], config["timezone"], events), new_state


# TeamSnap's team schedule import template (https://go.teamsnap.com/files/teamsnap_schedule_template.csv),
# minus its first column, which the template says to delete. Headings must match it exactly.
TEAMSNAP_COLUMNS = [
    "Date", "Time", "Duration (HH:MM)", "Arrival Time (Minutes)", "Name", "Opponent Name", "Opponent Contact Name",
    "Opponent Contact Phone Number", "Opponent Contact E-mail Address", "Location Name", "Location Address",
    "Location Details", "Location URL", "Home or Away", "Uniform", "Extra Label", "Notes",
]


def build_teamsnap_csv(config: dict, seasons: list[Season], venues: dict[str, Venue]) -> str:
    """The latest season's games in TeamSnap's schedule import format, for importing at the start of a
    season. Called-off games are left out. A game without a kickoff time yet has an empty time."""
    season = next((s for s in reversed(seasons) if s.games), None)
    minutes = config["game_minutes"]
    out = io.StringIO()
    writer = csv.DictWriter(out, TEAMSNAP_COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for game in season.games if season else []:
        if game.is_cancelled:
            continue
        venue = venues.get(game.venue_code or "")
        writer.writerow({
            "Date": game.start.strftime("%m/%d/%Y"),
            "Time": "" if game.all_day else game.start.strftime("%-I:%M %p"),
            "Duration (HH:MM)": f"{minutes // 60}:{minutes % 60:02d}",
            "Opponent Name": game.opponent,
            "Location Name": venue.name if venue else game.field,
            "Location Address": venue.address if venue else "",
            "Location Details": game.field,
            "Home or Away": "h" if game.home else "a",
            "Notes": f"{game.competition.title()} game ({season.name})",
        })
    return out.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config.json", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="print the calendar instead of writing files")
    parser.add_argument("--changes-file", type=Path, help="write schedule changes here (JSON with subject and body), if there are any")
    args = parser.parse_args(argv)

    root = args.config.resolve().parent
    config = json.loads(args.config.read_text(encoding="utf-8"))
    tz = ZoneInfo(config["timezone"])
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cache_dir = root / config["cache_dir"]
    venues_path = root / config["venues_file"]

    today = now.astimezone(tz).date()
    cached = read_cache(cache_dir, tz)
    try:
        seasons, stale = load_seasons(config, cached, today, tz)
        venues = load_venues(config, load_json(venues_path), [g for s in seasons for g in s.games])
    except (urllib.error.URLError, PageError) as exc:
        print(f"error: failed to read the league site: {exc}", file=sys.stderr)
        return 1

    state_path = root / config["state_file"]
    calendar, new_state = build(config, seasons, venues, load_json(state_path), now)

    # An empty feed would delete every event from every subscriber's calendar, so treat it as an
    # error (most likely a wrong team code in config.json) rather than publishing it.
    if not new_state:
        print(f"error: found no games for {config['team_code']!r}; refusing to publish an empty calendar", file=sys.stderr)
        return 1

    changes = detect_changes(cached, seasons, venues, today)
    if args.dry_run:
        sys.stdout.write(calendar)
        for change in changes:
            print(change, file=sys.stderr)
        return 0

    for season in seasons:
        write_json(cache_dir / f"{season.code}.json", season_to_json(season))
    for code in stale:
        (cache_dir / f"{code}.json").unlink()
    write_json(venues_path, {code: venue.__dict__ for code, venue in sorted(venues.items())})
    write_json(state_path, new_state)
    output = root / config["output"]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(calendar.encode("utf-8"))  # bytes, so CRLF line endings survive on every platform
    (root / config["teamsnap_output"]).write_bytes(build_teamsnap_csv(config, seasons, venues).encode("utf-8"))
    if changes:
        print("schedule changes:\n" + "\n".join(changes))
        if args.changes_file:
            write_json(args.changes_file, change_report(config, changes))
    print(f"wrote {output.relative_to(root)} with {len(new_state)} events from {len(seasons)} season(s): "
          + ", ".join(s.name for s in seasons))
    return 0


if __name__ == "__main__":
    sys.exit(main())
