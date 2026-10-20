"""Tier 2 - what an auto-record rule matches (DESIGN-autoschedule.md §3.1-§3.3, §3.7 and the
matching half of §11; built in dev/changelog/1186).

  - TitleNormalizationTests (§3.3): the one mark-stripping helper, app/program_titles.py,
    table-driven over the providers' superscript marks, every code point of the ranges the
    spec names, a "Live:" prefix, and the two things it must leave alone - a title that starts
    "Live " and Japanese text whose `ー` and `々` are Unicode `Lm`. Then title_key()'s
    whitespace collapse and casefold.
  - ComparisonTitleAdoptsHelperTests (§3.3): epg_sources.comparison_title() runs on the same
    helper, so it no longer drops a bare "Live " or a Japanese `Lm` letter, and the source
    comparison's pairing still sees through the marks. tests/test_epg_source_compare.py is
    the comparison's own suite and must keep passing beside this.
  - SaveTimeRefusalTests (§3.1): what read_rule_values()/clean_search() refuse with a
    sentence, what they strip, and the canonical spelling a valid search is stored in.
  - RollingWhenTests (§3.1): a relative When window is answered against each evaluation's
    own moment; a custom range is not.
  - ProgramLengthBoundTests (§1, §3.1): a rule bounded by program length schedules only the
    showings inside the bound.
  - DayAndTimeRuleTests (§1): a rule for Sundays, or for weekend evenings, schedules only the
    showings starting then on the display timezone's clock (dev/changelog/1187).
  - NamesRenameTests / NamesDeletionTests (§3.2): a tag or group rename rewrites the rules
    naming it without bumping their revision; a deleted tag, group, channel or account, a
    vanished category, or a spelling that no longer parses stops the rule, says why, raises
    AUTO_RULE_BROKEN, and recovers by itself once the thing is back; a name already gone at
    save is reported by the save itself.
  - SameProgramTests (§3.3): six games under one series title at one start are six programs;
    an empty episode joins the slot's only episode and is filled in later, but is a program
    of its own at a crowded slot - crowded counting the library as well as the listings.
  - SimulcastTests (§3.7): the same program at the same start on a group and an unrelated
    channel is scheduled once, on the group, whatever order the search returned them, and the
    event names what it was chosen over; feeds offset by minutes stay apart.

Every test builds on make_test_app() and patches app.routes.recordings.schedule_recording, so
no scheduler job is ever armed. No network, no ffmpeg.

    python3 -m unittest tests.test_auto_rules_matching
"""
import json
import os
import sys
import unicodedata
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import auto_rules, db  # noqa: E402
from app.alerts import AUTO_RULE_BROKEN  # noqa: E402
from app.channel_search import (DimensionFilter, MAX_PAGE_SIZE, SearchContext,  # noqa: E402
                                search)
from app.database import (Alert, AutoRule, Channel, ChannelGroup,  # noqa: E402
                          M3uAccount, RECORDING_RULE_SCHEDULED, RULE_MATCH_SCHEDULED,
                          Recording, RecordingEvent, RecordingProfile, RuleMatch)
from app.epg_sources import (CMP_IDENTICAL, ROW_DIFFERENT, Listing,  # noqa: E402
                             compare_listings, comparison_title, normalize_name)
from app.program_titles import strip_marks, title_key  # noqa: E402

_REAL_DATETIME = datetime


def _future(hours=0, minutes=0):
    """A minute-aligned naive-UTC moment ahead of now - two showings that must share an exact
    start take the same value rather than two separate offsets from the clock."""
    return (datetime.utcnow() + timedelta(hours=hours, minutes=minutes)).replace(
        second=0, microsecond=0)


def _clock_moved_by(offset):
    """A datetime class whose utcnow() is `offset` ahead of the real clock - patched into the
    engine and the search engine together, so an evaluation runs as if time had moved."""
    class _Moved(_REAL_DATETIME):
        @classmethod
        def utcnow(cls):
            return _REAL_DATETIME.utcnow() + offset
    return _Moved


# ---------------------------------------------------------------------------
# §3.3 - the one mark-stripping helper
# ---------------------------------------------------------------------------

class TitleNormalizationTests(unittest.TestCase):
    """app/program_titles.py is the one definition of a provider's title marks (§3.3)."""

    def test_provider_marks_are_stripped(self):
        """Every mark shape the spec names comes off, and only the mark: the title's own
        words and case survive strip_marks()."""
        cases = [
            ('NASCAR Cup Series ᴸᶦᵛᵉ', 'NASCAR Cup Series'),
            ('The Good Side ᴺᵉʷ', 'The Good Side'),
            ('Formula 1 Grand Prix ᵁᴴᴰ', 'Formula 1 Grand Prix'),
            ('WWE ᴿᴬᵂ', 'WWE'),
            ('Monday Night Football¹', 'Monday Night Football'),
            ('Monday Night Football²', 'Monday Night Football'),
            ('Monday Night Football³', 'Monday Night Football'),
            ('Monday Night Football⁴', 'Monday Night Football'),   # U+2074
            ('Monday Night Football₂', 'Monday Night Football'),   # U+2082
            ('Monday Night Football ⱽ', 'Monday Night Football'),  # U+2C7D
            ('Live: U.S. Senate', 'U.S. Senate'),
            ('LIVE: U.S. Senate', 'U.S. Senate'),
            ('live: U.S. Senate', 'U.S. Senate'),
            ('Live:U.S. Senate', 'U.S. Senate'),
            ('Live: NASCAR Cup Series ᴸᶦᵛᵉ', 'NASCAR Cup Series'),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(strip_marks(raw), expected)

    def test_every_code_point_in_the_named_ranges_is_removed(self):
        """§3.3 names exact ranges, not "every Lm character": Spacing Modifier Letters
        U+02B0-02FF, Phonetic Extensions (and Supplement) U+1D00-1DBF, Superscripts and
        Subscripts U+2070-209F, U+2C7C-2C7D, and the three Latin-1 superscript digits."""
        ranges = [(0x02B0, 0x02FF), (0x1D00, 0x1DBF), (0x2070, 0x209F), (0x2C7C, 0x2C7D)]
        singles = [0x00B9, 0x00B2, 0x00B3]
        points = [cp for lo, hi in ranges for cp in range(lo, hi + 1)] + singles
        kept = [cp for cp in points if strip_marks(f'Race{chr(cp)}Day') != 'RaceDay']
        self.assertEqual(kept, [], 'code points the helper failed to strip: '
                         + ', '.join(f'U+{cp:04X}' for cp in kept))

    def test_neighbors_of_the_ranges_are_ordinary_text(self):
        """The ranges are exact: the characters just outside them are kept."""
        for cp in (0x02AF, 0x20A0, 0x2C7B, 0x2C7E, 0x00B4):
            with self.subTest(code_point=f'U+{cp:04X}'):
                self.assertEqual(strip_marks(f'Race{chr(cp)}Day'), f'Race{chr(cp)}Day')

    def test_a_title_starting_live_without_a_colon_is_unchanged(self):
        """Only "Live:" with its colon is a mark. A bare "Live " starts real titles, and
        a "Live:" that is not at the start is part of the title."""
        for raw in ('Live Wire', 'Live at the Apollo', 'Live PD',
                    'Saturday Night Live: The Best Of'):
            with self.subTest(raw=raw):
                self.assertEqual(strip_marks(raw), raw)
                self.assertEqual(title_key(raw), raw.casefold())

    def test_japanese_modifier_letters_are_ordinary_text(self):
        """`ー` and `々` are Unicode Lm, like the providers' marks, and are ordinary text: a
        title carrying them must come through untouched."""
        title = 'スーパー日々'
        self.assertEqual(unicodedata.category('ー'), 'Lm')
        self.assertEqual(unicodedata.category('々'), 'Lm')
        self.assertEqual(strip_marks(title), title)
        self.assertEqual(title_key(title), title)

    def test_title_key_collapses_whitespace_and_casefolds(self):
        """title_key() is the helper plus whitespace collapse, trim and casefold - casefold,
        not lower(), so a German sharp s compares equal to its capital spelling."""
        self.assertEqual(title_key('  NASCAR   Cup\tSeries \n'), 'nascar cup series')
        self.assertEqual(title_key('NASCAR Cup Series ᴸᶦᵛᵉ'), 'nascar cup series')
        self.assertEqual(title_key('Straße'), title_key('STRASSE'))
        self.assertEqual(title_key(None), '')
        self.assertEqual(title_key(''), '')

    def test_the_four_nascar_title_shapes_are_one_key(self):
        """The same race listed four ways on four channels is one title."""
        shapes = ['NASCAR Cup Series', 'NASCAR Cup Series ᴸᶦᵛᵉ', 'Live: NASCAR Cup Series',
                  'NASCAR Cup Series ᴺᵉʷ']
        self.assertEqual({title_key(s) for s in shapes}, {'nascar cup series'})

    def test_nothing_else_is_merged(self):
        """A title with the episode folded in stays a different title from the series."""
        self.assertNotEqual(title_key('NASCAR Cup Series - Coke Zero Sugar 400'),
                            title_key('NASCAR Cup Series'))


class ComparisonTitleAdoptsHelperTests(unittest.TestCase):
    """comparison_title() keeps its separator folding but takes its mark definition from
    program_titles (§3.3) - one definition, never a second copy."""

    def test_it_is_the_helper_then_the_separator_folding(self):
        for raw in ('U.S. Senate ᴸᶦᵛᵉ', 'Live: U.S. Senate', 'US| A&E: Live PD',
                    'Hot Bench - Timesharing Is Caring', 'スーパー日々', None):
            with self.subTest(raw=raw):
                self.assertEqual(comparison_title(raw), normalize_name(strip_marks(raw)))

    def test_a_bare_leading_live_is_no_longer_dropped(self):
        """The looser rule turned ':' into a space before matching and so also dropped a
        bare "Live " - "Live Wire" read as "Wire"."""
        self.assertEqual(comparison_title('Live Wire'), 'live wire')
        self.assertNotEqual(comparison_title('Live Wire'), comparison_title('Wire'))

    def test_japanese_lm_letters_survive_the_comparison(self):
        self.assertEqual(comparison_title('スーパー日々'), 'スーパー日々')

    def test_pairing_still_sees_through_the_marks(self):
        t0 = datetime(2026, 9, 27, 17, 0)

        def day(titles):
            return [Listing(t, None, None, t0 + timedelta(hours=i), t0 + timedelta(hours=i + 1))
                    for i, t in enumerate(titles)]

        c = compare_listings(day(['U.S. Senate ᴸᶦᵛᵉ', 'The Good Side ᴺᵉʷ', 'News']),
                             day(['Live: U.S. Senate', 'The Good Side', 'News']))
        self.assertEqual(c.verdict, CMP_IDENTICAL)
        c = compare_listings(day(['Live Wire', 'News']), day(['Wire', 'News']))
        self.assertEqual(c.same, 1)
        self.assertIn(ROW_DIFFERENT, {r.kind for r in c.rows})


# ---------------------------------------------------------------------------
# Shared fixture for everything that evaluates a rule
# ---------------------------------------------------------------------------

class _RuleCase(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.armed = patch('app.routes.recordings.schedule_recording').start()
        self.addCleanup(patch.stopall)
        self.acc = seed.make_account()
        self._next_stream = 100

    def tearDown(self):
        self.t.cleanup()

    def channel(self, name, account=None, **kw):
        # Stream ids unique across accounts, so no two seeded channels share a stream URL.
        self._next_stream += 1
        return seed.make_channel(account or self.acc, stream_id=self._next_stream, name=name,
                                 **kw)

    def showing(self, channel, title, start, minutes=60, sub_title=None):
        return seed.make_epg_entry(channel, title=title, sub_title=sub_title,
                                   start_time=start, duration_minutes=minutes)

    def rule(self, search, **kw):
        data = {'name': kw.pop('name', 'Test rule'), 'search': search}
        data.update(kw)
        return auto_rules.create_rule(auto_rules.read_rule_values(data))

    def evaluate(self, rule_id):
        out = auto_rules.evaluate_now(rule_id)
        db.session.expire_all()
        return out

    def recordings(self, rule_id):
        return (Recording.query.filter_by(rule_id=rule_id)
                .order_by(Recording.program_start_time, Recording.id).all())

    def ledger(self, rule_id):
        return (RuleMatch.query.filter_by(rule_id=rule_id)
                .order_by(RuleMatch.program_start_time, RuleMatch.id).all())

    def open_broken_alerts(self, rule_id):
        return Alert.query.filter_by(alert_type=AUTO_RULE_BROKEN,
                                     source=f'auto-rule:{rule_id}:broken',
                                     dismissed_at=None).all()


# ---------------------------------------------------------------------------
# §3.1 - refused at save, stripped, stored canonically
# ---------------------------------------------------------------------------

class SaveTimeRefusalTests(_RuleCase):
    """A rule runs with nobody watching, so a search that cannot be a rule is refused at
    save with a sentence saying why, never quietly ignored (§3.1)."""

    def assert_refused(self, data):
        with self.assertRaises(auto_rules.RuleError) as cm:
            auto_rules.read_rule_values(data)
        message = str(cm.exception).strip()
        self.assertTrue(message.endswith('.'), f'not a sentence: {message!r}')
        return message

    def test_a_channel_grain_search_is_refused(self):
        self.assert_refused({'name': 'r', 'search': 'q=nascar'})
        self.assert_refused({'name': 'r', 'search': 'q=nascar&grain=channels'})

    def test_on_now_is_refused(self):
        self.assert_refused({'name': 'r', 'search': 'grain=airings&q=nascar&f.when=now'})
        self.assert_refused({'name': 'r',
                             'search': 'grain=airings&q=nascar&f.when=today&f.when=now'})

    def test_one_row_per_channel_is_refused(self):
        self.assert_refused({'name': 'r', 'search':
                             'grain=airings&q=nascar&standing=grpdedup&standing=firstonly'})

    def test_show_airings_that_have_ended_is_refused(self):
        self.assert_refused({'name': 'r', 'search':
                             'grain=airings&q=nascar&standing=grpdedup&standing=showpast'})

    def test_a_when_value_that_does_not_parse_is_refused(self):
        for value in ('next:abc:days', 'next:3:weeks', 'next:0:days', 'next:3',
                      'custom:..', 'custom:garbage..', 'custom:2026-02-06'):
            with self.subTest(value=value):
                message = self.assert_refused(
                    {'name': 'r', 'search': f'grain=airings&q=nascar&f.when={value}'})
                self.assertIn(value, message)

    def test_an_incomplete_program_length_is_refused(self):
        for value in ('dur:..', 'dur:abc..', 'dur:60', 'dur:..-5'):
            with self.subTest(value=value):
                message = self.assert_refused(
                    {'name': 'r', 'search': f'grain=airings&q=nascar&f.duration={value}'})
                self.assertIn(value, message)

    def test_an_unknown_day_or_an_incomplete_time_range_is_refused(self):
        """A day that is not a day would stop the rule at its first evaluation, and a
        time range that does not parse would match nothing - both are refused at save,
        naming the value (dev/changelog/1187)."""
        for spelling in ('f.dow=funday', 'x.dow=Sunday', 'f.tod=tod:..', 'f.tod=tod:6pm..',
                         'f.tod=tod:18:00..25:00', 'f.tod=18:00..23:59', 'x.tod=tod:'):
            with self.subTest(spelling=spelling):
                message = self.assert_refused(
                    {'name': 'r', 'search': f'grain=airings&q=nascar&{spelling}'})
                self.assertIn(spelling.split('=', 1)[1], message)

    def test_complete_when_and_length_values_are_accepted(self):
        for extra in ('f.when=next:3:days', 'f.when=today', 'f.when=tomorrow',
                      'f.when=custom:2026-02-06T00:00..2026-02-22T23:59',
                      'f.when=custom:2026-02-06T00:00..', 'f.duration=dur:60..',
                      'f.duration=dur:..90', 'f.duration=dur:60..180',
                      'f.dow=sun', 'f.dow=sat&f.dow=sun&x.dow=mon',
                      'f.tod=tod:18:00..23:59', 'f.tod=tod:22:00..02:00',
                      'f.tod=tod:18:00..', 'x.tod=tod:..06:00'):
            with self.subTest(extra=extra):
                values = auto_rules.read_rule_values(
                    {'name': 'r', 'search': f'grain=airings&q=nascar&{extra}'})
                self.assertIn(('grain', 'airings'), parse_qsl(values.search_params))

    def test_page_only_params_are_stripped(self):
        values = auto_rules.read_rule_values({'name': 'r', 'search': (
            'grain=airings&q=nascar&sort=-title&page=3&per_page=250&facets='
            '&add_to_group=5&replace_rec=9')})
        keys = {k for k, _v in parse_qsl(values.search_params, keep_blank_values=True)}
        self.assertEqual(keys & {'sort', 'page', 'per_page', 'facets', 'add_to_group',
                                 'replace_rec'}, set())
        self.assertEqual(values.search_params, 'q=nascar&grain=airings')

    def test_a_valid_search_is_stored_in_the_canonical_spelling(self):
        """What is stored is what SearchState.to_params() writes: defaults left out, the
        parameters in its own order, and the stored text a fixed point of a re-parse."""
        cases = [
            ('grain=airings&match=all&q=NASCAR+Cup&in=epg-title&in=epg-sub&in=epg-desc'
             '&standing=grpdedup&standing=shownoepg&standing=showuntested',
             'q=NASCAR+Cup&grain=airings'),
            ('grain=airings&q=NASCAR+Cup&standing=grpdedup',
             'q=NASCAR+Cup&standing=grpdedup&grain=airings'),
            ('f.when=next%3A7%3Adays&grain=airings&q=NASCAR+Cup&x.tag=new',
             'q=NASCAR+Cup&f.when=next%3A7%3Adays&x.tag=new&grain=airings'),
            ('grain=airings&q=x&standing=', 'q=x&standing=&grain=airings'),
            ('grain=airings&in=epg-title&q=Cup&match=any',
             'q=Cup&in=epg-title&match=any&grain=airings'),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                stored = auto_rules.read_rule_values({'name': 'r', 'search': raw}).search_params
                self.assertEqual(stored, expected)
                self.assertEqual(urlencode(auto_rules.parse_search(stored).to_params()), stored)

    def test_the_limit_is_both_halves_or_neither(self):
        base = {'name': 'r', 'search': 'grain=airings&q=nascar'}
        self.assert_refused({**base, 'limit_count': 1})
        self.assert_refused({**base, 'limit_window_hours': 120})
        both = auto_rules.read_rule_values({**base, 'limit_count': 1, 'limit_window_hours': 120})
        self.assertEqual((both.limit_count, both.limit_window_hours), (1, 120))
        neither = auto_rules.read_rule_values(base)
        self.assertEqual((neither.limit_count, neither.limit_window_hours), (None, None))

    def test_a_rule_needs_a_name(self):
        self.assert_refused({'search': 'grain=airings&q=nascar'})
        self.assert_refused({'name': '   ', 'search': 'grain=airings&q=nascar'})

    def test_a_deleted_profile_is_refused(self):
        profile = RecordingProfile(name='Race padding', pre_padding_minutes=5,
                                   post_padding_minutes=60)
        db.session.add(profile)
        db.session.commit()
        pid = profile.id
        ok = auto_rules.read_rule_values({'name': 'r', 'search': 'grain=airings&q=nascar',
                                          'profile_id': pid})
        self.assertEqual(ok.profile_id, pid)
        db.session.delete(db.session.get(RecordingProfile, pid))
        db.session.commit()
        self.assert_refused({'name': 'r', 'search': 'grain=airings&q=nascar',
                             'profile_id': pid})

    def test_the_save_route_answers_a_refusal_with_400_and_writes_nothing(self):
        resp = self.t.client.post('/api/auto-rules', json={
            'name': 'r', 'search': 'grain=airings&q=nascar&f.when=now'})
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(resp.get_json().get('error'))
        self.assertEqual(AutoRule.query.count(), 0)


# ---------------------------------------------------------------------------
# §3.1 - relative When values roll forward; a custom range does not
# ---------------------------------------------------------------------------

class RollingWhenTests(_RuleCase):

    def _with_clock_moved(self, offset, rule_id):
        moved = _clock_moved_by(offset)
        with patch('app.channel_search.datetime', moved), \
                patch('app.auto_rules.datetime', moved):
            return self.evaluate(rule_id)

    def test_next_n_days_is_answered_against_each_evaluation(self):
        """"Next 1 day" in a rule means "anything starting within a day of whenever the
        rule checks": a showing past the window at one evaluation is scheduled by a later
        one that finds it inside (§3.1)."""
        ch = self.channel('Rolling One')
        soon = _future(hours=2)
        later = _future(hours=30)
        far = _future(hours=40)
        self.showing(ch, 'Rolling Show', soon)
        self.showing(ch, 'Rolling Show', later)
        self.showing(ch, 'Rolling Show', far)
        db.session.commit()
        rid = self.rule('grain=airings&q=Rolling+Show&f.when=next:1:days', max_upcoming=0)

        self.evaluate(rid)
        self.assertEqual([r.program_start_time for r in self.recordings(rid)], [soon])

        self._with_clock_moved(timedelta(hours=8), rid)
        self.assertEqual([r.program_start_time for r in self.recordings(rid)], [soon, later])

    def test_a_custom_range_is_fixed(self):
        """A `custom:` range does not roll: moving the clock brings nothing new into it."""
        from app.tz_utils import get_display_tz
        ch = self.channel('Fixed One')
        inside = _future(hours=2)
        outside = _future(hours=30)
        self.showing(ch, 'Olympics Tonight', inside)
        self.showing(ch, 'Olympics Tonight', outside)
        db.session.commit()
        tz = get_display_tz()

        def local(dt):
            return dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime('%Y-%m-%dT%H:%M')

        window = f'custom:{local(_future(hours=1))}..{local(_future(hours=25))}'
        rid = self.rule(f'grain=airings&q=Olympics+Tonight&f.when={window}', max_upcoming=0)
        self.evaluate(rid)
        self.assertEqual([r.program_start_time for r in self.recordings(rid)], [inside])
        self._with_clock_moved(timedelta(hours=8), rid)
        self.assertEqual([r.program_start_time for r in self.recordings(rid)], [inside])


# ---------------------------------------------------------------------------
# §1, §3.1 - a program-length bound bounds what a rule schedules
# ---------------------------------------------------------------------------

class ProgramLengthBoundTests(_RuleCase):

    def _seed_lengths(self):
        ch = self.channel('Length Channel')
        starts = {}
        for i, minutes in enumerate((30, 75, 100, 180)):
            start = _future(hours=2 + i * 4)
            self.showing(ch, 'Bounded Show', start, minutes=minutes)
            starts[minutes] = start
        db.session.commit()
        return starts

    def test_wire_the_program_length_bound_into_auto_schedule_rules(self):
        """A rule whose search carries a Program length bound schedules exactly the showings
        inside it, and exactly what the search page lists for the same spelling - the search
        is the rule's preview (§1, §3.1)."""
        starts = self._seed_lengths()
        rid = self.rule('grain=airings&q=Bounded+Show&f.duration=dur:60..120', max_upcoming=0)
        self.evaluate(rid)
        scheduled = [r.program_start_time for r in self.recordings(rid)]
        self.assertEqual(scheduled, [starts[75], starts[100]])
        self.assertEqual({m.program_start_time for m in self.ledger(rid)},
                         {starts[75], starts[100]})

        state = auto_rules.parse_search(db.session.get(AutoRule, rid).search_params)
        result = search(replace(state, page_size=MAX_PAGE_SIZE), SearchContext.build(),
                        want_counts=False, want_facets=False)
        self.assertEqual(sorted(e.start_time for e in result.rows), scheduled)

    def test_a_lower_bound_alone_bounds_too(self):
        starts = self._seed_lengths()
        rid = self.rule('grain=airings&q=Bounded+Show&f.duration=dur:120..', max_upcoming=0)
        self.evaluate(rid)
        self.assertEqual([r.program_start_time for r in self.recordings(rid)], [starts[180]])


# ---------------------------------------------------------------------------
# §1 - a day-of-week and time-of-day filter decides what a rule schedules
# ---------------------------------------------------------------------------

class DayAndTimeRuleTests(_RuleCase):
    """The motivating case for the two dimensions (dev/changelog/1187): a weekly race on
    Sunday afternoon, a replay of it on Wednesday evening, and a qualifying show on Saturday
    evening, all under one title. A rule for Sundays records the race and nothing else, and
    the search page lists exactly what it scheduled."""

    def _next_local(self, weekday, hour):
        """The next `weekday` (Monday 0) at `hour` on the display timezone's clock, at
        least a day out, as naive UTC - read through the same tz the engine reads."""
        from app.tz_utils import get_display_tz, to_naive_utc
        tz = get_display_tz()
        local = datetime.now(timezone.utc).astimezone(tz) + timedelta(days=1)
        local += timedelta(days=(weekday - local.weekday()) % 7)
        local = local.replace(hour=hour, minute=0, second=0, microsecond=0)
        return to_naive_utc(datetime(local.year, local.month, local.day, local.hour,
                                     tzinfo=tz))

    def _seed_week(self):
        ch = self.channel('Race Channel')
        starts = {'sun': self._next_local(6, 15), 'wed': self._next_local(2, 20),
                  'sat': self._next_local(5, 19)}
        for day, start in starts.items():
            self.showing(ch, 'Cup Race', start, minutes=180, sub_title=f'Round {day}')
        db.session.commit()
        return starts

    def test_a_rule_for_sundays_schedules_the_sunday_race_only(self):
        starts = self._seed_week()
        rid = self.rule('grain=airings&q=Cup+Race&f.dow=sun', max_upcoming=0)
        self.evaluate(rid)
        scheduled = [r.program_start_time for r in self.recordings(rid)]
        self.assertEqual(scheduled, [starts['sun']])

        state = auto_rules.parse_search(db.session.get(AutoRule, rid).search_params)
        result = search(replace(state, page_size=MAX_PAGE_SIZE), SearchContext.build(),
                        want_counts=False, want_facets=False)
        self.assertEqual(sorted(e.start_time for e in result.rows), scheduled)

    def test_a_rule_for_weekend_evenings_skips_the_afternoon_race(self):
        starts = self._seed_week()
        rid = self.rule('grain=airings&q=Cup+Race&f.dow=sat&f.dow=sun'
                        '&f.tod=tod:18:00..23:59', max_upcoming=0)
        self.evaluate(rid)
        self.assertEqual([r.program_start_time for r in self.recordings(rid)],
                         [starts['sat']])


# ---------------------------------------------------------------------------
# §3.2 - names the search depends on
# ---------------------------------------------------------------------------

class NamesRenameTests(_RuleCase):
    """A rename follows: every path that renames a tag or group rewrites each rule naming it
    in the same commit, by re-serializing the parsed search - never string replacement - and
    without bumping the rule's revision (§3.2)."""

    def _make_tag(self, name, pattern):
        resp = self.t.client.post('/api/tags', json={'name': name, 'patterns': [pattern],
                                                     'color': '#58a6ff'})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        return resp.get_json()['tag']['id']

    def test_renaming_a_tag_rewrites_rules_that_include_or_exclude_it(self):
        tag_id = self._make_tag('racing', 'Racing')
        ch = self.channel('Speed')
        self.showing(ch, 'Racing Tonight', _future(hours=3))
        db.session.commit()
        included = self.rule('grain=airings&q=racing&f.tag=racing&f.tag=live', name='inc')
        excluded = self.rule('grain=airings&q=Tonight&x.tag=racing', name='exc')
        unrelated = self.rule('grain=airings&q=racing&f.tag=new', name='other')
        before = db.session.get(AutoRule, unrelated).search_params

        resp = self.t.client.put(f'/api/tags/{tag_id}', json={
            'name': 'motorsport', 'patterns': ['Racing'], 'color': '#58a6ff'})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        db.session.expire_all()

        inc = db.session.get(AutoRule, included)
        exc = db.session.get(AutoRule, excluded)
        self.assertEqual(inc.search_params,
                         'q=racing&f.tag=motorsport&f.tag=live&grain=airings')
        self.assertEqual(exc.search_params, 'q=Tonight&x.tag=motorsport&grain=airings')
        self.assertEqual(auto_rules.parse_search(inc.search_params).q, 'racing',
                         'the typed text must not be string-replaced')
        self.assertEqual((inc.revision, exc.revision), (1, 1))
        self.assertEqual(db.session.get(AutoRule, unrelated).search_params, before)

        self.evaluate(included)
        self.assertIsNone(db.session.get(AutoRule, included).last_error)
        self.assertEqual(self.open_broken_alerts(included), [])
        self.assertEqual(len(self.recordings(included)), 1)

    def test_renaming_a_group_rewrites_rules_that_include_or_exclude_it(self):
        member = self.channel('Feed One')
        group = seed.make_group(name='Cup Feeds', members=[member])
        self.showing(member, 'Cup Race', _future(hours=3))
        db.session.commit()
        included = self.rule('grain=airings&q=Cup+Race&f.group=Cup+Feeds', name='inc')
        excluded = self.rule('grain=airings&q=Cup+Race&x.group=Cup+Feeds', name='exc')

        resp = self.t.client.post(f'/api/channel-groups/{group.id}/rename',
                                  json={'name': 'Cup Series Feeds'})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        db.session.expire_all()

        inc = db.session.get(AutoRule, included)
        exc = db.session.get(AutoRule, excluded)
        self.assertEqual(auto_rules.parse_search(inc.search_params).filter_for('group'),
                         DimensionFilter('group', ('Cup Series Feeds',), ()))
        self.assertEqual(auto_rules.parse_search(exc.search_params).filter_for('group'),
                         DimensionFilter('group', (), ('Cup Series Feeds',)))
        self.assertEqual(inc.search_params,
                         'q=Cup+Race&f.group=Cup+Series+Feeds&grain=airings')
        self.assertEqual((inc.revision, exc.revision), (1, 1))

        self.evaluate(included)
        self.assertIsNone(db.session.get(AutoRule, included).last_error)
        recs = self.recordings(included)
        self.assertEqual([r.group_id for r in recs], [group.id])


class NamesDeletionTests(_RuleCase):
    """A deletion breaks the rule visibly: it is not run, last_error names what is gone, and
    AUTO_RULE_BROKEN stands until an evaluation runs it again (§3.2, §6)."""

    def assert_breaks_then_recovers(self, rule_id, remove, restore, named):
        remove()
        db.session.commit()
        db.session.expire_all()
        out = self.evaluate(rule_id)
        rule = db.session.get(AutoRule, rule_id)
        self.assertEqual(self.recordings(rule_id), [], 'a broken rule must not run')
        self.assertEqual(out.get('scheduled'), 0)
        self.assertTrue(rule.last_error, 'last_error must say why the rule did not run')
        self.assertRegex(rule.last_error, named)
        self.assertEqual(len(self.open_broken_alerts(rule_id)), 1)

        restore()
        db.session.commit()
        db.session.expire_all()
        out = self.evaluate(rule_id)
        rule = db.session.get(AutoRule, rule_id)
        self.assertIsNone(rule.last_error)
        self.assertEqual(self.open_broken_alerts(rule_id), [])
        self.assertEqual(len(self.recordings(rule_id)), 1)
        self.assertEqual(out.get('scheduled'), 1)

    def test_a_deleted_tag(self):
        resp = self.t.client.post('/api/tags', json={'name': 'racing', 'patterns': ['Racing'],
                                                     'color': '#58a6ff'})
        tag_id = resp.get_json()['tag']['id']
        ch = self.channel('Speed')
        self.showing(ch, 'Racing Tonight', _future(hours=3))
        db.session.commit()
        rid = self.rule('grain=airings&q=Tonight&f.tag=racing')

        def remove():
            self.assertEqual(self.t.client.delete(f'/api/tags/{tag_id}').status_code, 200)

        def restore():
            self.t.client.post('/api/tags', json={'name': 'racing', 'patterns': ['Racing'],
                                                  'color': '#58a6ff'})

        self.assert_breaks_then_recovers(rid, remove, restore, r'racing')

    def test_an_excluded_tag_that_is_deleted_does_not_quietly_match_everything(self):
        """Excluding a tag that is gone would exclude nothing - the rule would schedule what
        it was told to leave out. It is reported broken instead."""
        resp = self.t.client.post('/api/tags', json={'name': 'replay', 'patterns': ['Replay'],
                                                     'color': '#58a6ff'})
        tag_id = resp.get_json()['tag']['id']
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race Replay', _future(hours=3))
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race&x.tag=replay')
        self.evaluate(rid)
        self.assertEqual(self.recordings(rid), [])

        self.t.client.delete(f'/api/tags/{tag_id}')
        db.session.expire_all()
        self.evaluate(rid)
        self.assertEqual(self.recordings(rid), [])
        self.assertRegex(db.session.get(AutoRule, rid).last_error, 'replay')
        self.assertEqual(len(self.open_broken_alerts(rid)), 1)

    def test_a_deleted_group(self):
        member = self.channel('Feed One')
        group = seed.make_group(name='Old Feeds', members=[member])
        self.showing(member, 'Cup Race', _future(hours=3))
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race&f.group=Old+Feeds')
        gid = group.id

        def remove():
            resp = self.t.client.post(f'/api/channel-groups/{gid}/delete', json={})
            self.assertEqual(resp.status_code, 200, resp.get_json())
            self.assertIsNone(db.session.get(ChannelGroup, gid))

        def restore():
            seed.make_group(name='Old Feeds', members=[db.session.get(Channel, member.id)])

        self.assert_breaks_then_recovers(rid, remove, restore, r'Old Feeds')

    def test_a_deleted_channel(self):
        ch = self.channel('Only Here')
        start = _future(hours=3)
        self.showing(ch, 'Cup Race', start)
        db.session.commit()
        cid, stream = ch.id, ch.stream_id
        rid = self.rule(f'grain=airings&q=Cup+Race&f.chan={cid}')

        def remove():
            # An ORM delete takes the channel's listings with it (cascade).
            db.session.delete(db.session.get(Channel, cid))

        def restore():
            back = seed.make_channel(self.acc, stream_id=stream, name='Only Here', id=cid)
            self.showing(back, 'Cup Race', start)

        self.assert_breaks_then_recovers(rid, remove, restore, rf'\b{cid}\b')

    def test_a_deleted_account(self):
        other = seed.make_account(name='Second Provider')
        ch = self.channel('Elsewhere', account=other)
        start = _future(hours=3)
        self.showing(ch, 'Cup Race', start)
        db.session.commit()
        aid = other.id
        rid = self.rule(f'grain=airings&q=Cup+Race&f.acct={aid}')

        def remove():
            # An ORM delete takes the account's channels and their listings with it.
            db.session.delete(db.session.get(M3uAccount, aid))

        def restore():
            back = seed.make_account(name='Second Provider', id=aid)
            self.showing(self.channel('Elsewhere', account=back), 'Cup Race', start)

        self.assert_breaks_then_recovers(rid, remove, restore, rf'\b{aid}\b')

    def test_a_vanished_category(self):
        """A category is provider text with no rename path, so one that no channel carries
        any more is reported the same way as a deletion."""
        ch = self.channel('Weather Now', category_name='Weather')
        self.showing(ch, 'Storm Watch', _future(hours=3))
        db.session.commit()
        cid = ch.id
        rid = self.rule('grain=airings&q=Storm+Watch&f.cat=Weather')

        def remove():
            db.session.get(Channel, cid).category_name = 'News'

        def restore():
            db.session.get(Channel, cid).category_name = 'Weather'

        self.assert_breaks_then_recovers(rid, remove, restore, r'Weather')

    def test_a_name_that_is_already_gone_is_reported_at_save(self):
        """The check runs at save as well as at every evaluation: the save route's own
        evaluation reports the missing name, and the rule is broken from the start."""
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race', _future(hours=3))
        db.session.commit()
        resp = self.t.client.post('/api/auto-rules', json={
            'name': 'r', 'search': 'grain=airings&q=Cup+Race&x.tag=nosuchtag'})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        rid = body['rule']['id']
        self.assertIn('nosuchtag', body['evaluation']['error'] or '')
        db.session.expire_all()
        self.assertIn('nosuchtag', db.session.get(AutoRule, rid).last_error or '')
        self.assertEqual(len(self.open_broken_alerts(rid)), 1)
        self.assertEqual(self.recordings(rid), [])

    def test_a_spelling_that_no_longer_parses(self):
        ch = self.channel('Speed')
        self.showing(ch, 'Cup Race', _future(hours=3))
        db.session.commit()
        rid = self.rule('grain=airings&q=Cup+Race')
        good = db.session.get(AutoRule, rid).search_params

        def remove():
            db.session.get(AutoRule, rid).search_params = good + '&f.nosuchdimension=1'

        def restore():
            db.session.get(AutoRule, rid).search_params = good

        self.assert_breaks_then_recovers(rid, remove, restore, r'nosuchdimension')


# ---------------------------------------------------------------------------
# §3.3 - "the same program"
# ---------------------------------------------------------------------------

class SameProgramTests(_RuleCase):

    def test_six_games_under_one_series_title_at_one_start_are_six_programs(self):
        """A series title alone is not one program: six different episodes at one start on
        six channels are six recordings when nothing else stops them (§3.3)."""
        start = _future(hours=4)
        games = ['Yankees at Red Sox', 'Phillies at Mets', 'Cubs at Cardinals',
                 'Dodgers at Giants', 'Astros at Rangers', 'Braves at Marlins']
        for i, game in enumerate(games):
            self.showing(self.channel(f'Ballpark {i}'), 'MLB Baseball', start, sub_title=game)
        db.session.commit()
        rid = self.rule('grain=airings&q=MLB+Baseball', max_upcoming=0)

        out = self.evaluate(rid)
        self.assertEqual(out['scheduled'], 6)
        recs = self.recordings(rid)
        self.assertEqual(sorted(r.program_sub_title for r in recs), sorted(games))
        rows = self.ledger(rid)
        self.assertEqual(sorted(m.episode_key for m in rows),
                         sorted(title_key(g) for g in games))
        self.assertEqual({m.action_taken for m in rows}, {RULE_MATCH_SCHEDULED})

    def test_an_empty_episode_at_a_slot_with_one_episode_is_that_program(self):
        start = _future(hours=4)
        self.showing(self.channel('Speed A'), 'NASCAR Cup Series', start,
                     sub_title='Coke Zero Sugar 400')
        self.showing(self.channel('Speed B'), 'NASCAR Cup Series', start)
        db.session.commit()
        rid = self.rule('grain=airings&q=NASCAR+Cup', max_upcoming=0)

        self.evaluate(rid)
        self.assertEqual(len(self.recordings(rid)), 1)
        rows = self.ledger(rid)
        self.assertEqual([(m.episode_key, m.action_taken) for m in rows],
                         [('coke zero sugar 400', RULE_MATCH_SCHEDULED)])

    def test_an_empty_episode_at_a_crowded_slot_is_a_program_of_its_own(self):
        """With several distinct episodes at one title and start, an episode-less listing
        cannot be told apart, so it absorbs none of them (§3.3, §11 simulcast order)."""
        start = _future(hours=4)
        self.showing(self.channel('Ballpark A'), 'MLB Baseball', start,
                     sub_title='Yankees at Red Sox')
        self.showing(self.channel('Ballpark B'), 'MLB Baseball', start,
                     sub_title='Phillies at Mets')
        self.showing(self.channel('Ballpark C'), 'MLB Baseball', start)
        db.session.commit()
        rid = self.rule('grain=airings&q=MLB+Baseball', max_upcoming=0)

        self.evaluate(rid)
        self.assertEqual(len(self.recordings(rid)), 3)
        self.assertEqual(sorted(m.episode_key for m in self.ledger(rid)),
                         ['', 'phillies at mets', 'yankees at red sox'])

    def test_the_library_counts_toward_a_crowded_slot(self):
        """The distinct episodes at a slot are counted over this run's listings, the rule's
        ledger and the library's recordings: a recording of a second game already in the
        library makes the slot crowded, so an episode-less listing is its own program
        rather than joining the one game still listed (§3.3)."""
        start = _future(hours=4)
        listed = self.channel('Ballpark A')
        recorded = self.channel('Ballpark B')
        self.showing(listed, 'MLB Baseball', start, sub_title='Yankees at Red Sox')
        self.showing(self.channel('Ballpark C'), 'MLB Baseball', start)
        seed.make_recording(status='SCHEDULED', name='by hand', channel_id=recorded.id,
                            start_time=start, stop_time=start + timedelta(hours=1),
                            program_title='MLB Baseball', program_sub_title='Phillies at Mets',
                            program_start_time=start,
                            program_stop_time=start + timedelta(hours=1))
        db.session.commit()
        rid = self.rule('grain=airings&q=MLB+Baseball', max_upcoming=0)

        self.evaluate(rid)
        self.assertEqual(sorted(m.episode_key for m in self.ledger(rid)),
                         ['', 'yankees at red sox'])
        self.assertEqual(len(self.recordings(rid)), 2)

    def test_a_series_title_with_the_episode_folded_in_is_a_different_program(self):
        start = _future(hours=4)
        self.showing(self.channel('Speed A'), 'NASCAR Cup Series - Coke Zero Sugar 400', start)
        self.showing(self.channel('Speed B'), 'NASCAR Cup Series', start,
                     sub_title='Coke Zero Sugar 400')
        db.session.commit()
        rid = self.rule('grain=airings&q=NASCAR+Cup', max_upcoming=0)
        self.evaluate(rid)
        self.assertEqual(len(self.recordings(rid)), 2)

    def test_an_empty_episode_row_is_filled_in_and_a_later_episode_told_apart(self):
        """A ledger row stored with no episode takes the slot's one distinct episode when it
        appears, keeping its id; a second, different episode arriving after that is its own
        program (§3.3)."""
        start = _future(hours=4)
        self.showing(self.channel('Speed A'), 'NASCAR Cup Series', start)
        db.session.commit()
        rid = self.rule('grain=airings&q=NASCAR+Cup', max_upcoming=0)
        self.evaluate(rid)
        rows = self.ledger(rid)
        self.assertEqual([m.episode_key for m in rows], [''])
        row_id = rows[0].id
        self.assertEqual(len(self.recordings(rid)), 1)

        self.showing(self.channel('Speed B'), 'NASCAR Cup Series ᴸᶦᵛᵉ', start,
                     sub_title='Coke Zero Sugar 400')
        db.session.commit()
        self.evaluate(rid)
        rows = self.ledger(rid)
        self.assertEqual([(m.id, m.episode_key) for m in rows],
                         [(row_id, 'coke zero sugar 400')])
        self.assertEqual(len(self.recordings(rid)), 1,
                         'the listing that filled the row in is the program already picked')

        self.showing(self.channel('Speed C'), 'NASCAR Cup Series', start,
                     sub_title='Pit Boss 250')
        db.session.commit()
        self.evaluate(rid)
        recs = self.recordings(rid)
        self.assertEqual(sorted((r.program_sub_title or '') for r in recs), ['', 'Pit Boss 250'])
        self.assertEqual(db.session.get(RuleMatch, row_id).episode_key, 'coke zero sugar 400')

    def test_an_empty_episode_row_is_not_filled_in_when_a_row_already_holds_the_episode(self):
        """The fill-in only happens when no row of the rule already holds the episode at that
        slot - filling it would make two rows one anchor (§3.3)."""
        start = _future(hours=4)
        ch = self.channel('Speed A')
        self.showing(ch, 'NASCAR Cup Series', start, sub_title='Coke Zero Sugar 400')
        db.session.commit()
        rid = self.rule('grain=airings&q=NASCAR+Cup', max_upcoming=0)
        bare = seed.make_recording(status='SCHEDULED', name='bare', channel_id=ch.id,
                                   start_time=start, stop_time=start + timedelta(hours=1),
                                   program_title='NASCAR Cup Series',
                                   program_start_time=start,
                                   program_stop_time=start + timedelta(hours=1), rule_id=rid)
        named = seed.make_recording(status='SCHEDULED', name='named', channel_id=ch.id,
                                    start_time=start, stop_time=start + timedelta(hours=1),
                                    program_title='NASCAR Cup Series',
                                    program_sub_title='Coke Zero Sugar 400',
                                    program_start_time=start,
                                    program_stop_time=start + timedelta(hours=1), rule_id=rid)
        for rec, episode in ((bare, ''), (named, 'coke zero sugar 400')):
            db.session.add(RuleMatch(
                rule_id=rid, channel_id=ch.id, title_key='nascar cup series',
                episode_key=episode, program_title='NASCAR Cup Series',
                program_start_time=start, program_stop_time=start + timedelta(hours=1),
                action_taken=RULE_MATCH_SCHEDULED, recording_id=rec.id,
                rec_start_time=rec.start_time, rec_stop_time=rec.stop_time))
        db.session.commit()

        out = self.evaluate(rid)
        self.assertFalse(out.get('error'), out)
        self.assertEqual(sorted(m.episode_key for m in self.ledger(rid)),
                         ['', 'coke zero sugar 400'])
        self.assertEqual(len(self.recordings(rid)), 2)


# ---------------------------------------------------------------------------
# §3.7 - the same program on several channels
# ---------------------------------------------------------------------------

class SimulcastTests(_RuleCase):

    def _seed(self, group_first):
        """A group of one member and an unrelated, healthier channel airing the same race at
        the same start under different provider marks. `group_first` decides which listing is
        seeded first, and so which one the search returns first."""
        self.start = _future(hours=5)
        self.member = self.channel('Cup Feed East')
        self.other = self.channel('Speed Simulcast', health_score=95)
        self.group = seed.make_group(name='Cup Feeds', members=[self.member])
        listings = [(self.member, 'Live: NASCAR Cup Series'),
                    (self.other, 'NASCAR Cup Series ᴸᶦᵛᵉ')]
        if not group_first:
            listings.reverse()
        for ch, title in listings:
            self.showing(ch, title, self.start, minutes=180, sub_title='Coke Zero Sugar 400')
        db.session.commit()
        self.rid = self.rule('grain=airings&q=NASCAR+Cup', max_upcoming=0)

    def _search_order(self):
        state = auto_rules.parse_search(db.session.get(AutoRule, self.rid).search_params)
        result = search(replace(state, page_size=MAX_PAGE_SIZE), SearchContext.build(),
                        want_counts=False, want_facets=False)
        return [e.channel_id for e in result.rows]

    def _assert_once_on_the_group(self):
        recs = self.recordings(self.rid)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].group_id, self.group.id)
        rows = self.ledger(self.rid)
        self.assertEqual([(m.action_taken, m.group_id) for m in rows],
                         [(RULE_MATCH_SCHEDULED, self.group.id)])
        event = RecordingEvent.query.filter_by(recording_id=recs[0].id,
                                               event_type=RECORDING_RULE_SCHEDULED).one()
        self.assertIn('Speed Simulcast', event.detail)
        self.assertIn(self.other.id, json.loads(event.extra_data)['chosen_over_channel_ids'])

    def test_the_group_wins_when_the_search_returns_the_channel_first(self):
        self._seed(group_first=False)
        self.assertEqual(self._search_order(), [self.other.id, self.member.id])
        self.evaluate(self.rid)
        self._assert_once_on_the_group()

    def test_the_group_wins_when_the_search_returns_the_group_first(self):
        self._seed(group_first=True)
        self.assertEqual(self._search_order(), [self.member.id, self.other.id])
        self.evaluate(self.rid)
        self._assert_once_on_the_group()

    def test_between_single_channels_the_healthier_one_wins(self):
        """With no group among them, the higher effective score is the simulcast winner,
        whichever order the listings arrived in."""
        for weak_first in (True, False):
            with self.subTest(weak_first=weak_first):
                start = _future(hours=6 if weak_first else 9)
                title = f'Simulcast Final {"A" if weak_first else "B"}'
                weak = self.channel(f'Weak {title}', health_score=40)
                strong = self.channel(f'Strong {title}', health_score=90)
                for ch in ((weak, strong) if weak_first else (strong, weak)):
                    self.showing(ch, title, start, sub_title='Game 7')
                db.session.commit()
                rid = self.rule(f'grain=airings&q={title.replace(" ", "+")}', max_upcoming=0,
                                name=title)
                self.evaluate(rid)
                self.assertEqual([r.channel_id for r in self.recordings(rid)], [strong.id])

    def test_feeds_offset_by_minutes_do_not_collapse(self):
        """Only an exact start makes a simulcast: a feed two minutes behind is its own
        program, with its own ledger anchor and its own recording."""
        start = _future(hours=5)
        member = self.channel('Cup Feed East')
        other = self.channel('Speed Delayed', health_score=95)
        group = seed.make_group(name='Cup Feeds', members=[member])
        self.showing(member, 'NASCAR Cup Series', start, minutes=180)
        self.showing(other, 'NASCAR Cup Series', start + timedelta(minutes=2), minutes=180)
        db.session.commit()
        rid = self.rule('grain=airings&q=NASCAR+Cup', max_upcoming=0)

        self.evaluate(rid)
        recs = self.recordings(rid)
        self.assertEqual([(r.program_start_time, r.group_id) for r in recs],
                         [(start, group.id), (start + timedelta(minutes=2), None)])
        self.assertEqual(recs[1].channel_id, other.id)
        self.assertEqual([m.program_start_time for m in self.ledger(rid)],
                         [start, start + timedelta(minutes=2)])


if __name__ == '__main__':
    unittest.main()
