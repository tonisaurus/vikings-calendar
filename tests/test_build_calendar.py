import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_calendar as bc

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TZ = ZoneInfo("America/Los_Angeles")
TEAM, CODE = "Vintage Vikings", "VVI"
NOW = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)  # Sunday 11am PDT
CONFIG = {
    "team": TEAM, "team_code": CODE, "calendar_name": "Vintage Vikings Soccer", "timezone": "America/Los_Angeles",
    "site": "https://www.ggwsl.org", "game_minutes": 90, "keep_seasons": 4, "output": "docs/vikings.ics",
    "teamsnap_output": "docs/vikings-teamsnap.csv", "state_file": "state.json", "venues_file": "venues.json", "cache_dir": "seasons",
}


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def game(code="G0_VVI_WAS", start=datetime(2026, 9, 27, 15, 0, tzinfo=TZ), home=True, opponent="Wasabi", opponent_code="WAS",
         result="", ours=None, theirs=None, status="scheduled", season="2026f", venue_code="BEA", **kwargs):
    return bc.Game(code=code, season=season, start=start, field="Beach #4 (Turf)", venue_code=venue_code, opponent=opponent,
                   opponent_code=opponent_code, home=home, result=result, our_score=ours, their_score=theirs,
                   status=status, **kwargs)


def row(code, name, rank, wins=0, losses=0, draws=0, gf=0, ga=0, points=None):
    played = wins + losses + draws
    return bc.Standing(code, name, rank, played, wins, losses, draws, 0, gf, ga, gf - ga,
                       wins * 3 + draws if points is None else points)


STANDINGS = bc.Standings([
    row("KIL", "Killer Tomatoes", 1, wins=2, gf=15, ga=1),
    row("WAS", "Wasabi", 2, wins=2, gf=8, ga=4),
    row("VVI", "Elliott's Vintage Vikings", 4, losses=1, draws=1, gf=2, ga=3),
])


def season(games, standings=STANDINGS, snapshots=None, code="2026f"):
    return bc.Season(code=code, name="Fall 2026", games=games, standings=standings, snapshots=snapshots or {})


# ------------------------------------------------------------------------------ parsing


class TeamPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = bc.parse_team_page(fixture("team_2026f.html"), CODE, TZ)

    def test_season_and_listing(self):
        self.assertEqual(self.page.current, "2026f")
        self.assertEqual(self.page.season.name, "Fall 2026")
        self.assertEqual(self.page.seasons[:3], [("2026f", "Fall 2026"), ("2026s", "Spring 2026"), ("2025f", "Fall 2025")])
        self.assertEqual(self.page.seasons[-1], ("2009f", "Fall 2009"))

    def test_schedule_rows(self):
        games = self.page.season.games
        self.assertEqual(len(games), 10)
        first = games[0]
        self.assertEqual(first.code, "G0_WAS_VVI")
        self.assertEqual(first.start, datetime(2026, 9, 13, 13, 0, tzinfo=TZ))
        self.assertFalse(first.home)
        self.assertEqual((first.opponent, first.opponent_code), ("Wasabi", "WAS"))
        self.assertEqual((first.field, first.venue_code), ("Beach #4 (Turf)", "BEA"))
        self.assertEqual((first.result, first.our_score, first.their_score), ("L", "2", "3"))
        self.assertEqual((first.competition, first.status, first.forfeit, first.all_day), ("league", "scheduled", False, False))

    def test_bold_rows_are_home_games(self):
        self.assertEqual([g.home for g in self.page.season.games],
                         [False, True, False, False, False, True, True, False, True, True])

    def test_morning_and_afternoon_kickoffs(self):
        self.assertEqual([g.start.strftime("%H:%M") for g in self.page.season.games[:5]],
                         ["13:00", "11:00", "15:00", "15:00", "09:00"])

    def test_unplayed_game_has_no_result(self):
        game = self.page.season.games[2]
        self.assertEqual((game.result, game.our_score, game.their_score), ("", None, None))
        self.assertEqual(game.venue_code, "ALA")

    def test_standings_keep_only_teams_on_our_schedule_in_league_order(self):
        rows = self.page.season.standings.rows
        self.assertEqual([r.code for r in rows], ["KIL", "WAS", "FRU", "VVI", "SPI", "OFL"])
        self.assertEqual([r.rank for r in rows], [1, 2, 3, 4, 5, 6])
        kil = rows[0]
        # The league caps goal difference, so GD is taken from the page rather than computed.
        self.assertEqual((kil.played, kil.wins, kil.goals_for, kil.goals_against, kil.goal_diff, kil.points), (2, 2, 15, 1, 10, 6))
        self.assertEqual(rows[3].record, "0-1-1")

    def test_promoted_opponent_from_another_group_is_slotted_in_by_points(self):
        rows = bc.parse_team_page(fixture("team_2024f.html"), CODE, TZ).season.standings.rows
        # Hot Flashes (17 pts) are listed in the lower group; our group keeps the league's order.
        self.assertEqual([(r.code, r.points, r.rank) for r in rows],
                         [("FRU", 23, 1), ("WAS", 22, 2), ("KIL", 19, 3), ("HFL", 17, 4), ("VVI", 14, 5), ("OFL", 6, 6), ("SPI", 2, 7)])

    def test_slotting_ties_go_after_and_fall_back_to_goal_difference(self):
        def cells(code, pts, gd, gf):
            values = ["5.0", code, "10", "0", "0", "0", "0", str(gf), "0", str(gd), str(pts)]
            row = [bc.Cell(text=v) for v in values]
            row[1].links.append([f"Team.php?PARAM_TEAM_CODE={code}", code])
            return row
        spacer = [bc.Cell(header=True)]
        rows = [cells("VVI", 10, 3, 9), cells("AAA", 10, 1, 9), cells("BBB", 4, 0, 5), spacer,
                cells("TIE", 10, 1, 9), cells("MID", 10, 2, 1), cells("OUT", 30, 9, 9)]
        standings = bc.parse_standings(rows, "VVI", {"VVI", "AAA", "BBB", "TIE", "MID"})
        self.assertEqual([r.code for r in standings.rows], ["VVI", "MID", "AAA", "TIE", "BBB"])

    def test_forfeit_and_noon_kickoff(self):
        games = bc.parse_team_page(fixture("team_2024f.html"), CODE, TZ).season.games
        forfeit = next(g for g in games if g.forfeit)
        self.assertEqual((forfeit.result, forfeit.our_score, forfeit.their_score), ("W", "2", "FF"))
        self.assertEqual(forfeit.start, datetime(2024, 10, 27, 12, 0, tzinfo=TZ))

    def test_wrong_team_fails(self):
        with self.assertRaisesRegex(bc.PageError, "not for team WAS"):
            bc.parse_team_page(fixture("team_2026f.html"), "WAS", TZ)

    def test_error_page_fails(self):
        with self.assertRaises(bc.PageError):
            bc.parse_team_page("<html><body>No such team</body></html>", CODE, TZ)

    def test_missing_schedule_fails_unless_allowed(self):
        html = fixture("team_2026f.html").replace(">\nOPPONENT\n<", ">\nFOE\n<")
        with self.assertRaisesRegex(bc.PageError, "schedule table"):
            bc.parse_team_page(html, CODE, TZ)
        self.assertEqual(bc.parse_team_page(html, CODE, TZ, require_schedule=False).season.games, [])

    def test_home_marking_that_disagrees_with_game_code_is_flagged(self):
        html = fixture("team_2026f.html").replace("G0_WAS_VVI", "G1_VVI_WAS", 2)
        first = bc.parse_team_page(html, CODE, TZ).season.games[0]
        self.assertTrue(first.home)
        self.assertTrue(first.home_unclear)
        self.assertFalse(any(g.home_unclear for g in bc.parse_team_page(fixture("team_2026f.html"), CODE, TZ).season.games))

    def test_unexpected_row_shape_fails(self):
        html = fixture("team_2026f.html").replace(">league</a></td>", ">league</a></td><td>extra</td>", 1)
        with self.assertRaisesRegex(bc.PageError, "unexpected schedule row"):
            bc.parse_team_page(html, CODE, TZ)


class FieldParsingTests(unittest.TestCase):
    def test_time_am_pm(self):
        self.assertEqual(bc.parse_time("9:00"), (9, 0))
        self.assertEqual(bc.parse_time("11:00"), (11, 0))
        self.assertEqual(bc.parse_time("12:00"), (12, 0))
        self.assertEqual(bc.parse_time("1:00"), (13, 0))
        self.assertEqual(bc.parse_time("10:15"), (10, 15))
        self.assertEqual(bc.parse_time("7:30"), (19, 30))
        self.assertIsNone(bc.parse_time(""))

    def test_implausible_time_fails(self):
        for value in ("13:00", "0:30", "noon"):
            with self.assertRaises(bc.PageError):
                bc.parse_time(value)

    def test_date_is_checked_against_weekday(self):
        self.assertEqual(bc.parse_date("Sun", "Sep. 13", 2026), date(2026, 9, 13))
        self.assertEqual(bc.parse_date("Sat", "May. 16", 2026), date(2026, 5, 16))
        self.assertEqual(bc.parse_date("Sun", "Sept. 13", 2026), date(2026, 9, 13))
        with self.assertRaisesRegex(bc.PageError, "Sunday"):
            bc.parse_date("Sat", "Sep. 13", 2026)
        with self.assertRaises(bc.PageError):
            bc.parse_date("Sun", "Smarch 3", 2026)

    def test_scores(self):
        self.assertEqual(bc.parse_score("2-3"), ("2", "3"))
        self.assertEqual(bc.parse_score("2-FF"), ("2", "FF"))
        self.assertEqual(bc.parse_score(""), (None, None))
        self.assertEqual(bc.parse_score("--"), (None, None))
        self.assertEqual(bc.parse_score("CCD"), (None, None))
        with self.assertRaises(bc.PageError):
            bc.parse_score("2:3")

    def test_results(self):
        self.assertEqual(bc.parse_result("W", "2", "1", "scheduled"), ("W", False))
        self.assertEqual(bc.parse_result("", "2", "2", "scheduled"), ("D", False))
        self.assertEqual(bc.parse_result("", None, None, "scheduled"), ("", False))
        self.assertEqual(bc.parse_result("--", None, None, "rainout"), ("", False))
        self.assertEqual(bc.parse_result("W", "2", "FF", "scheduled"), ("W", True))
        self.assertEqual(bc.parse_result("FF", "0", "2", "earlyforfeit"), ("L", True))
        self.assertEqual(bc.parse_result("W", "5", "2", "earlyforfeit"), ("W", True))
        self.assertEqual(bc.parse_result("CCD", "1", "0", "scheduled"), ("CCD", False))

    def test_venue_address(self):
        venue = bc.parse_venue(fixture("venue_BEA.html"), "BEA")
        self.assertEqual(venue, bc.Venue("Beach Chalet", "1500 John F Kennedy, Golden Gate Park, San Francisco, CA"))
        self.assertEqual(venue.location, "Beach Chalet, 1500 John F Kennedy, Golden Gate Park, San Francisco, CA")
        self.assertEqual(bc.parse_venue('<a name="#X"></a><h3>Kezar Stadium</h3>', "X"), bc.Venue("Kezar Stadium", ""))
        self.assertEqual(bc.Venue("Kezar Stadium", "").location, "Kezar Stadium")
        self.assertIsNone(bc.parse_venue(fixture("venue_BEA.html"), "ALA"))


# ----------------------------------------------------------------------------- rendering


class SummaryTests(unittest.TestCase):
    def test_upcoming_home_and_away(self):
        self.assertEqual(bc.build_summary(game(), TEAM, CODE, False, None), "Vintage Vikings vs Wasabi")
        self.assertEqual(bc.build_summary(game(home=False), TEAM, CODE, False, None), "Wasabi vs Vintage Vikings")

    def test_featured_game_shows_rank_and_record_home_first(self):
        g = game(home=False, opponent="Killer Tomatoes", opponent_code="KIL")
        self.assertEqual(bc.build_summary(g, TEAM, CODE, True, STANDINGS), "Killer Tomatoes (1st, 2-0) vs Vintage Vikings (4th, 0-1-1)")

    def test_result_home_first_with_our_perspective_letter(self):
        g = game(home=False, result="L", ours="2", theirs="3")
        self.assertEqual(bc.build_summary(g, TEAM, CODE, False, None), "Wasabi 3 - 2 Vintage Vikings (L)")
        g = game(result="D", ours="0", theirs="0")
        self.assertEqual(bc.build_summary(g, TEAM, CODE, False, None), "Vintage Vikings 0 - 0 Wasabi (D)")

    def test_forfeit(self):
        g = game(home=False, result="W", ours="2", theirs="FF", forfeit=True)
        self.assertEqual(bc.build_summary(g, TEAM, CODE, False, None), "Wasabi FF - 2 Vintage Vikings (W, forfeit)")

    def test_result_without_score(self):
        g = game(result="CCD")
        self.assertEqual(bc.build_summary(g, TEAM, CODE, False, None), "Vintage Vikings vs Wasabi (CCD)")

    def test_called_off_games(self):
        self.assertEqual(bc.build_summary(game(status="rainout"), TEAM, CODE, False, None), "RAINED OUT: Vintage Vikings vs Wasabi")
        self.assertEqual(bc.build_summary(game(status="cancelled"), TEAM, CODE, True, STANDINGS), "CANCELLED: Vintage Vikings (4th, 0-1-1) vs Wasabi (2nd, 2-0)")
        self.assertEqual(bc.build_summary(game(status="postpnd"), TEAM, CODE, False, None), "POSTPONED: Vintage Vikings vs Wasabi")
        self.assertEqual(bc.build_summary(game(status="mystery"), TEAM, CODE, False, None), "MYSTERY: Vintage Vikings vs Wasabi")

    def test_non_league_competition_is_labelled(self):
        self.assertEqual(bc.build_summary(game(competition="cup"), TEAM, CODE, False, None), "Cup: Vintage Vikings vs Wasabi")


class DescriptionTests(unittest.TestCase):
    def test_upcoming(self):
        text = bc.build_description(game(), TEAM, CODE, None, "Standings and records are added the week of the game.")
        self.assertEqual(text, "Vintage Vikings (home) vs Wasabi (away)\nBeach #4 (Turf)\n\nStandings and records are added the week of the game.")

    def test_played_with_table_uses_display_name(self):
        g = game(result="W", ours="2", theirs="1")
        text = bc.build_description(g, TEAM, CODE, STANDINGS)
        self.assertIn("Final: Vintage Vikings 2 - 1 Wasabi (W)", text)
        self.assertIn("Standings after this game:\n1. Killer Tomatoes 2-0, 6 pts, GD +14", text)
        self.assertIn("4. Vintage Vikings 0-1-1, 1 pts, GD -1", text)  # rank as the table has it
        self.assertNotIn("Elliott", text)

    def test_unclear_home_team_is_explained(self):
        text = bc.build_description(game(home_unclear=True), TEAM, CODE, None)
        self.assertIn("Vintage Vikings (home) vs Wasabi (away)\nHome and away are unclear", text)

    def test_rained_out_and_time_tbd(self):
        text = bc.build_description(game(status="rainout", all_day=True), TEAM, CODE, None)
        self.assertEqual(text, "Status: Rained out\nVintage Vikings (home) vs Wasabi (away)\nBeach #4 (Turf)\nKickoff time not set yet.")


class StandingsChoiceTests(unittest.TestCase):
    def test_played_game_uses_its_snapshot(self):
        played = game(result="W", ours="1", theirs="0")
        snap = bc.Standings([row("VVI", "V", 1, wins=1)])
        table, note = bc.standings_for(played, season([played, game(code="G1", start=datetime(2026, 10, 4, 15, tzinfo=TZ))], snapshots={played.code: snap}), False, NOW)
        self.assertIs(table, snap)
        self.assertIsNone(note)

    def test_played_game_without_snapshot_has_no_table(self):
        played = game(result="W", ours="1", theirs="0")
        self.assertEqual(bc.standings_for(played, season([played, game(code="G1", start=datetime(2026, 10, 4, 15, tzinfo=TZ))]), False, NOW), (None, None))

    def test_last_game_of_season_shows_final_table_even_over_its_snapshot(self):
        first = game(code="G0", start=datetime(2026, 5, 3, 9, tzinfo=TZ), result="W", ours="1", theirs="0")
        last = game(code="G1", start=datetime(2026, 5, 10, 9, tzinfo=TZ), result="L", ours="0", theirs="1")
        week_of = bc.Standings([row("VVI", "V", 1, wins=1, losses=1)])
        self.assertIs(bc.table_after(last, season([first, last])), STANDINGS)
        self.assertIs(bc.table_after(last, season([first, last], snapshots={"G1": week_of})), STANDINGS)
        self.assertIsNone(bc.table_after(first, season([first, last])))

    def test_final_table_goes_on_last_game_played_when_later_ones_were_called_off(self):
        last = game(code="G0", start=datetime(2026, 5, 3, 9, tzinfo=TZ), result="W", ours="1", theirs="0")
        rained = game(code="G1", start=datetime(2026, 5, 10, 9, tzinfo=TZ), status="rainout")
        self.assertIs(bc.table_after(last, season([last, rained])), STANDINGS)

    def test_no_final_table_while_games_remain(self):
        played = game(code="G0", start=datetime(2026, 9, 20, 9, tzinfo=TZ), result="W", ours="1", theirs="0")
        upcoming = game(code="G1", start=datetime(2026, 10, 4, 9, tzinfo=TZ))
        self.assertIsNone(bc.table_after(played, season([played, upcoming])))

    def test_featured_game_notes(self):
        g = game()
        self.assertEqual(bc.standings_for(g, season([g]), True, NOW), (STANDINGS, None))
        empty = bc.Standings([row("VVI", "V", 1), row("WAS", "W", 2)])
        self.assertEqual(bc.standings_for(g, season([g], standings=empty), True, NOW), (None, "No games played yet this season."))

    def test_other_game_notes(self):
        past = game(start=datetime(2026, 9, 20, 11, tzinfo=TZ))
        future = game(start=datetime(2026, 10, 25, 15, tzinfo=TZ))
        self.assertEqual(bc.standings_for(past, season([past]), False, NOW), (None, "Result not posted yet."))
        self.assertEqual(bc.standings_for(future, season([future]), False, NOW), (None, "Standings and records are added the week of the game."))
        self.assertEqual(bc.standings_for(game(status="rainout"), season([]), False, NOW), (None, None))

    def test_pick_featured(self):
        done = game(code="A", start=datetime(2026, 9, 20, 11, tzinfo=TZ), result="D", ours="0", theirs="0")
        rained = game(code="B", start=datetime(2026, 9, 27, 9, tzinfo=TZ), status="rainout")
        today = game(code="C", start=datetime(2026, 9, 27, 15, tzinfo=TZ))
        later = game(code="D", start=datetime(2026, 10, 4, 15, tzinfo=TZ))
        self.assertIs(bc.pick_featured([done, rained, today, later], NOW), today)
        self.assertIsNone(bc.pick_featured([later], NOW - timedelta(days=10)))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.first = game(code="G0", start=datetime(2026, 9, 13, 13, tzinfo=TZ), result="L", ours="2", theirs="3")
        self.second = game(code="G1", start=datetime(2026, 9, 20, 11, tzinfo=TZ), result="D", ours="0", theirs="0")
        self.third = game(code="G2", start=datetime(2026, 9, 27, 15, tzinfo=TZ))
        self.season = season([self.first, self.second, self.third])

    def test_records_table_for_this_weeks_result_only(self):
        result = bc.record_snapshots(self.season, {}, date(2026, 9, 21))
        self.assertEqual(set(result.snapshots), {"G1"})

    def test_window_ends_the_day_before_our_next_game(self):
        self.assertEqual(bc.record_snapshots(self.season, {}, date(2026, 9, 26)).snapshots.keys(), {"G1"})
        self.assertEqual(bc.record_snapshots(self.season, {}, date(2026, 9, 27)).snapshots, {})

    def test_frozen_snapshots_are_kept_and_removed_games_dropped(self):
        old = bc.Standings([row("VVI", "V", 1)])
        result = bc.record_snapshots(self.season, {"G0": old, "GONE": old}, date(2026, 9, 21))
        self.assertIs(result.snapshots["G0"], old)
        self.assertNotIn("GONE", result.snapshots)
        self.assertIs(result.snapshots["G1"], STANDINGS)


class LiveSeasonTests(unittest.TestCase):
    def test_rules(self):
        finished = season([game(start=datetime(2026, 6, 7, 13, tzinfo=TZ))], code="2026s")
        today = date(2026, 6, 21)
        self.assertTrue(bc.season_is_live("2026f", "2026f", finished, today))  # the current season
        self.assertTrue(bc.season_is_live("2026s", "2026f", None, today))  # not cached yet
        self.assertTrue(bc.season_is_live("2026s", "2026f", season([], code="2026s"), today))  # no schedule yet
        self.assertTrue(bc.season_is_live("2026s", "2026f", finished, today))  # within 14 days of its last game
        self.assertFalse(bc.season_is_live("2026s", "2026f", finished, today + timedelta(days=1)))


class CacheTests(unittest.TestCase):
    def test_round_trip(self):
        original = bc.parse_team_page(fixture("team_2026f.html"), CODE, TZ).season
        original = bc.Season(original.code, original.name, original.games + [game(code="G9", all_day=True, start=datetime(2026, 12, 6, tzinfo=TZ))],
                             original.standings, {"G0_WAS_VVI": original.standings})
        original.games[1] = replace(original.games[1], home_unclear=True)
        restored = bc.season_from_json(json.loads(json.dumps(bc.season_to_json(original))), TZ)
        self.assertEqual(restored, original)


class IcsTests(unittest.TestCase):
    def test_fold_respects_octets_and_utf8(self):
        line = "DESCRIPTION:" + "é" * 80
        folded = bc.ics_fold(line)
        self.assertTrue(all(len(part.encode("utf-8")) <= 75 for part in folded))
        self.assertEqual("".join(p[1:] if i else p for i, p in enumerate(folded)), line)

    def test_escape(self):
        self.assertEqual(bc.ics_escape("a, b; c\\d\ne"), "a\\, b\\; c\\\\d\\ne")

    def test_timed_and_all_day_events(self):
        length = timedelta(minutes=90)
        self.assertEqual(bc.event_times(game(), length), ["DTSTART:20260927T220000Z", "DTEND:20260927T233000Z"])
        all_day = game(all_day=True, start=datetime(2026, 12, 6, tzinfo=TZ))
        self.assertEqual(bc.event_times(all_day, length), ["DTSTART;VALUE=DATE:20261206", "DTEND;VALUE=DATE:20261207"])

    def test_pst_after_daylight_saving_ends(self):
        g = game(start=datetime(2026, 11, 22, 15, tzinfo=TZ))
        self.assertEqual(bc.event_times(g, timedelta(minutes=90))[0], "DTSTART:20261122T230000Z")


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.played = game(code="G0_WAS_VVI", home=False, start=datetime(2026, 9, 20, 11, tzinfo=TZ), result="L", ours="2", theirs="3")
        self.today = game(code="G0_KIL_VVI", home=False, opponent="Killer Tomatoes", opponent_code="KIL", venue_code="ALA")
        self.seasons = [season([self.played, self.today])]
        self.venues = {"BEA": bc.Venue("Beach Chalet", "1500 John F Kennedy, San Francisco, CA")}

    def events(self, calendar):
        unfolded = calendar.replace("\r\n ", "")
        return [dict(line.split(":", 1) for line in block.split("\r\n") if ":" in line)
                for block in unfolded.split("BEGIN:VEVENT")[1:]]

    def test_calendar(self):
        calendar, state = bc.build(CONFIG, self.seasons, self.venues, {}, NOW)
        self.assertTrue(calendar.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertIn("X-WR-CALNAME:Vintage Vikings Soccer\r\n", calendar)
        played, today = self.events(calendar)
        self.assertEqual(played["UID"], "ggwsl-2026f-G0_WAS_VVI@vikings-calendar")
        self.assertEqual(played["SUMMARY"], "Wasabi 3 - 2 Vintage Vikings (L)")
        self.assertEqual(played["LOCATION"], "Beach Chalet\\, 1500 John F Kennedy\\, San Francisco\\, CA")
        self.assertEqual(today["SUMMARY"], "Killer Tomatoes (1st\\, 2-0) vs Vintage Vikings (4th\\, 0-1-1)")
        self.assertEqual(today["LOCATION"], "Beach #4 (Turf)")  # venue address not known: field name
        self.assertEqual(today["DTEND"], "20260927T233000Z")
        self.assertEqual(set(state), {played["UID"], today["UID"]})

    def test_sequence_only_bumps_on_change(self):
        _, state = bc.build(CONFIG, self.seasons, self.venues, {}, NOW)
        later = NOW + timedelta(hours=6)
        _, same = bc.build(CONFIG, self.seasons, self.venues, state, later)
        self.assertEqual(same, state)
        moved = game(code="G0_KIL_VVI", home=False, opponent="Killer Tomatoes", opponent_code="KIL", start=datetime(2026, 9, 27, 13, tzinfo=TZ))
        _, changed = bc.build(CONFIG, [season([self.played, moved])], self.venues, state, later)
        uid = "ggwsl-2026f-G0_KIL_VVI@vikings-calendar"
        self.assertEqual(changed[uid]["sequence"], 1)
        self.assertEqual(changed[uid]["last_modified"], later.isoformat())

    def test_teamsnap_csv(self):
        spring = season([game(code="OLD", start=datetime(2026, 5, 3, 9, tzinfo=TZ), season="2026s")], code="2026s")
        fall = season([
            self.played,
            game(code="RAIN", status="rainout", start=datetime(2026, 10, 4, 9, tzinfo=TZ)),
            game(code="TBD", opponent="Old Flames, Jr.", venue_code="TBD", all_day=True, start=datetime(2026, 12, 6, tzinfo=TZ)),
        ])
        rows = bc.build_teamsnap_csv(CONFIG, [spring, fall], self.venues).split("\r\n")
        self.assertEqual(rows[0].split(","), bc.TEAMSNAP_COLUMNS)
        self.assertEqual(rows[1], '09/20/2026,11:00 AM,1:30,,,Wasabi,,,,Beach Chalet,"1500 John F Kennedy, San Francisco, CA",'
                                  'Beach #4 (Turf),,a,,,League game (Fall 2026)')
        self.assertEqual(rows[2], '12/06/2026,,1:30,,,"Old Flames, Jr.",,,,Beach #4 (Turf),,Beach #4 (Turf),,h,,,League game (Fall 2026)')
        self.assertEqual(rows[3:], [""])  # the rained-out game and the spring season are left out

    def test_teamsnap_csv_template_headings(self):
        # The template's first column is an instruction to delete it; the rest must match exactly.
        template = ("Delete this column before saving for import!,Date,Time,Duration (HH:MM),Arrival Time (Minutes),Name,"
                    "Opponent Name,Opponent Contact Name,Opponent Contact Phone Number,Opponent Contact E-mail Address,"
                    "Location Name,Location Address,Location Details,Location URL,Home or Away,Uniform,Extra Label,Notes")
        self.assertEqual(bc.TEAMSNAP_COLUMNS, template.split(",")[1:])

    def test_cancelled_status(self):
        calendar, _ = bc.build(CONFIG, [season([game(status="rainout")])], {}, {}, NOW)
        self.assertEqual(self.events(calendar)[0]["STATUS"], "CANCELLED")


VENUES = {"BEA": bc.Venue("Beach Chalet", "1500 John F Kennedy, San Francisco, CA"),
          "ALA": bc.Venue("Alameda Estuary Park", "230-200 Mosley Ave, Alameda, CA")}


class ChangeTests(unittest.TestCase):
    TODAY = date(2026, 9, 27)

    def setUp(self):
        self.played = game(code="G0_WAS_VVI", home=False, start=datetime(2026, 9, 20, 11, tzinfo=TZ), result="L", ours="2", theirs="3")
        self.next = game(code="G0_OFL_VVI", home=False, opponent="Old Flames", opponent_code="OFL", start=datetime(2026, 10, 18, 9, tzinfo=TZ))
        self.before = {"2026f": season([self.played, self.next])}

    def changes(self, *games, before=None):
        return bc.detect_changes(self.before if before is None else before, [season(list(games))], VENUES, self.TODAY)

    def test_nothing_changed(self):
        self.assertEqual(self.changes(self.played, self.next), [])

    def test_posted_scores_are_not_schedule_changes(self):
        scored = replace(self.next, result="W", our_score="1", their_score="0")
        self.assertEqual(self.changes(self.played, scored), [])

    def test_past_games_are_ignored(self):
        moved = replace(self.played, start=datetime(2026, 9, 20, 13, tzinfo=TZ), venue_code="ALA")
        self.assertEqual(self.changes(moved, self.next), [])

    def test_time_and_field_change(self):
        moved = replace(self.next, start=datetime(2026, 10, 18, 11, tzinfo=TZ), field="Beach #2 (Turf)")
        self.assertEqual(self.changes(self.played, moved), [
            "Changed: Sun Oct 18 vs Old Flames (away)\n  Time: 9:00 AM -> 11:00 AM\n  Field: Beach #4 (Turf) -> Beach #2 (Turf)"])

    def test_moved_to_another_day_and_venue(self):
        moved = replace(self.next, start=datetime(2026, 10, 17, 15, tzinfo=TZ), venue_code="ALA", field="Alameda Estuary Park")
        self.assertEqual(self.changes(self.played, moved), [(
            "Changed: Sat Oct 17 vs Old Flames (away)\n  Date: Sun Oct 18 -> Sat Oct 17\n  Time: 9:00 AM -> 3:00 PM\n"
            "  Location: Beach Chalet, 1500 John F Kennedy, San Francisco, CA -> Alameda Estuary Park, 230-200 Mosley Ave, Alameda, CA\n"
            "  Field: Beach #4 (Turf) -> Alameda Estuary Park"
        )])

    def test_rained_out_and_home_away_swap(self):
        self.assertEqual(self.changes(self.played, replace(self.next, status="rainout")), [
            "Changed: Sun Oct 18 vs Old Flames (away)\n  Status: Scheduled -> Rained out"])
        self.assertEqual(self.changes(self.played, replace(self.next, home=True)), [])  # a home/away swap is not worth an email

    def test_added_and_removed(self):
        makeup = game(code="G2_VVI_WAS", start=datetime(2026, 12, 6, tzinfo=TZ), all_day=True)
        self.assertEqual(self.changes(self.played, makeup), [
            "Removed: Sun Oct 18 vs Old Flames (away), no longer on the league schedule",
            "Added: Sun Dec 6 vs Wasabi (home)\n  not set yet at Beach Chalet, 1500 John F Kennedy, San Francisco, CA, Beach #4 (Turf)"])

    def test_new_season_schedule(self):
        changes = self.changes(self.played, self.next, before={})
        self.assertEqual(changes, ["The Fall 2026 schedule is up: 1 upcoming game.\n  Sun Oct 18 vs Old Flames (away), 9:00 AM, Beach #4 (Turf)"])

    def test_finished_new_season_is_not_announced(self):
        self.assertEqual(self.changes(self.played, before={}), [])

    def test_report(self):
        report = bc.change_report({**CONFIG, "page_url": "https://example.test/"}, ["A", "B"])
        self.assertEqual(report["subject"], "[automated] Vintage Vikings schedule update")
        self.assertEqual(report["body"], "A\n\nB\n\nThe calendar subscription updates on its own. TeamSnap does not, "
                                         "so update it by hand.\n\nCalendar and TeamSnap CSV: https://example.test/\n")


# ---------------------------------------------------------------------------- loading


class FakeSite:
    """Serves fixture pages by URL and records what was fetched."""

    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    def __call__(self, url):
        self.fetched.append(url)
        if url not in self.pages:
            raise AssertionError(f"unexpected fetch {url}")
        return self.pages[url]


CURRENT_URL = "https://www.ggwsl.org/LibLeague/Team/Team.php?PARAM_TEAM_CODE=VVI"


def season_url(code):
    return f"{CURRENT_URL}+PARAM_SEASON_CODE={code}"


class LoadSeasonsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cache = Path(self.dir.name)
        self.addCleanup(self.dir.cleanup)
        self.today = date(2026, 9, 27)
        self.config = {**CONFIG, "keep_seasons": 2}
        # Stand-in for the spring page: the fall page relabelled. Same year, so its dates still check out.
        self.spring = fixture("team_2026f.html").replace('value="2026f" selected>', 'value="2026f">').replace('value="2026s">', 'value="2026s" selected>')

    def cached(self, code, last_game):
        s = season([game(season=code, start=datetime.combine(last_game, datetime.min.time(), TZ).replace(hour=13))], code=code)
        (self.cache / f"{code}.json").write_text(json.dumps(bc.season_to_json(s)))

    def test_fetches_current_and_uncached_seasons(self):
        site = FakeSite({CURRENT_URL: fixture("team_2026f.html"), season_url("2026s"): self.spring})
        with mock.patch.object(bc, "fetch_text", site):
            seasons, stale = bc.load_seasons(self.config, bc.read_cache(self.cache, TZ), self.today, TZ)
        self.assertEqual(site.fetched, [CURRENT_URL, season_url("2026s")])
        self.assertEqual(stale, [])
        self.assertEqual({s.code for s in seasons}, {"2026f", "2026s"})

    def test_serves_finished_seasons_from_cache_and_drops_old_ones(self):
        self.cached("2026s", date(2026, 6, 7))
        self.cached("2025f", date(2025, 11, 23))
        site = FakeSite({CURRENT_URL: fixture("team_2026f.html")})
        with mock.patch.object(bc, "fetch_text", site):
            seasons, stale = bc.load_seasons(self.config, bc.read_cache(self.cache, TZ), self.today, TZ)
        self.assertEqual(site.fetched, [CURRENT_URL])
        self.assertEqual(stale, ["2025f"])
        self.assertEqual({s.code for s in seasons}, {"2026f", "2026s"})

    def test_page_for_the_wrong_season_fails(self):
        site = FakeSite({CURRENT_URL: fixture("team_2026f.html"), season_url("2026s"): fixture("team_2024f.html")})
        with mock.patch.object(bc, "fetch_text", site), self.assertRaisesRegex(bc.PageError, "shows 2024f"):
            bc.load_seasons(self.config, bc.read_cache(self.cache, TZ), self.today, TZ)

    def test_venues_fetched_once(self):
        site = FakeSite({"https://www.ggwsl.org/LibLeague/Direct.php?PARAM_VENUE_CODE=BEA": fixture("venue_BEA.html"),
                         "https://www.ggwsl.org/LibLeague/Direct.php?PARAM_VENUE_CODE=ALA": "<html></html>"})
        games = [game(), game(code="X", venue_code="ALA"), game(code="Y", venue_code="OLD")]
        with mock.patch.object(bc, "fetch_text", site), redirect_stderr(io.StringIO()) as err:
            venues = bc.load_venues(CONFIG, {"OLD": {"name": "Old Field", "address": "1 Main St"}}, games)
        self.assertEqual(venues, {"OLD": bc.Venue("Old Field", "1 Main St"),
                                  "BEA": bc.Venue("Beach Chalet", "1500 John F Kennedy, Golden Gate Park, San Francisco, CA")})
        self.assertIn("ALA", err.getvalue())


class MainTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({**CONFIG, "keep_seasons": 1, "page_url": "https://example.test/"}))

    def run_main(self, site):
        with mock.patch.object(bc, "fetch_text", site), redirect_stderr(io.StringIO()) as err, redirect_stdout(io.StringIO()):
            code = bc.main(["--config", str(self.config)])
        return code, err.getvalue()

    def test_writes_calendar_cache_and_state(self):
        site = FakeSite({CURRENT_URL: fixture("team_2026f.html"),
                         "https://www.ggwsl.org/LibLeague/Direct.php?PARAM_VENUE_CODE=BEA": fixture("venue_BEA.html"),
                         "https://www.ggwsl.org/LibLeague/Direct.php?PARAM_VENUE_CODE=ALA": "<html></html>"})
        code, _ = self.run_main(site)
        self.assertEqual(code, 0)
        calendar = (self.root / "docs/vikings.ics").read_bytes()
        self.assertEqual(calendar.count(b"BEGIN:VEVENT"), 10)
        self.assertIn(b"\r\n", calendar)
        self.assertTrue((self.root / "seasons/2026f.json").exists())
        self.assertEqual(len(json.loads((self.root / "state.json").read_text())), 10)
        self.assertEqual(json.loads((self.root / "venues.json").read_text())["BEA"]["name"], "Beach Chalet")
        csv_bytes = (self.root / "docs/vikings-teamsnap.csv").read_bytes()
        self.assertTrue(csv_bytes.startswith(b"Date,Time,Duration (HH:MM),"))
        self.assertEqual(csv_bytes.count(b"\r\n"), 11)

    def test_changes_file_written_only_when_something_changed(self):
        site = FakeSite({CURRENT_URL: fixture("team_2026f.html"),
                         "https://www.ggwsl.org/LibLeague/Direct.php?PARAM_VENUE_CODE=BEA": fixture("venue_BEA.html"),
                         "https://www.ggwsl.org/LibLeague/Direct.php?PARAM_VENUE_CODE=ALA": "<html></html>"})
        changes = self.root / "changes.json"
        now = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)
        with mock.patch.object(bc, "datetime", wraps=datetime) as clock:
            clock.now.return_value = now
            with mock.patch.object(bc, "fetch_text", site), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(bc.main(["--config", str(self.config), "--changes-file", str(changes)]), 0)
                report = json.loads(changes.read_text())
                self.assertEqual(report["subject"], "[automated] Vintage Vikings schedule update")
                self.assertIn("The Fall 2026 schedule is up: 8 upcoming games.", report["body"])
                changes.unlink()
                self.assertEqual(bc.main(["--config", str(self.config), "--changes-file", str(changes)]), 0)
        self.assertFalse(changes.exists())

    def test_broken_page_fails_without_writing(self):
        code, err = self.run_main(FakeSite({CURRENT_URL: "<html>maintenance</html>"}))
        self.assertEqual(code, 1)
        self.assertIn("failed to read the league site", err)
        self.assertFalse((self.root / "docs").exists())
        self.assertFalse((self.root / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
