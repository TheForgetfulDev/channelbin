"""Tier 2 - a channel's guide continued past the end of its first source
(DESIGN-epg-sources.md §14, whose §14.7 is this module's list; dev/changelog/1722).

  - DecideTests: decide_stretches() is pure - each outcome, the seam, a refused source
    passed over, and a recorded judgment kept until six listings overturn it.
  - SeamTests: one timeline in epg_entries with no program doubled and none missing, after
    a first import, a first-source refresh that grows, one that shrinks, a refresh of the
    filling source, a reorder, an override, a stop, a delete - and no moment inside a
    first-source import where the hours it grew by hold two sources' programs.
  - RefusalTests: a source whose listings are not the first source's schedule, one listing
    one program all day, one read through a shared id, and one judged on too little.
  - RerunTests: the placement run again from the top, from rows left in the wrong table,
    ends in the same rows; run twice it moves nothing.
  - ReaderTests: the guide API and both search grains see the one timeline.
  - RanOutTests: retire_expired_listings() leaves the stretches consistent.
  - RuleTests: a rule's recording made from a filled listing is neither orphaned nor made a
    second time when the first source later lists the program itself.
  - CopyTests: a key write that copies a donor's listings copies both halves and places them.
  - TeardownTests: a hidden channel, a deleted channel and a deleted source take their rows.
  - EveryMoverPlacesTests: every function that moves or copies listings ends in the placement.

No network: every feed is a local byte string (CLAUDE.md §Testing).

    python3 -m unittest tests.test_epg_fill
"""
import ast
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auto_rules, db  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import (CHANNEL_EPG_FILL_CHANGED, Channel, ChannelEvent, EPGEntry,  # noqa: E402
                          EPG_FINDING_SHARED_ID, EPG_STRETCH_FILLS, EPG_STRETCH_NO_OVERLAP,
                          EPG_STRETCH_NO_SCHEDULE, EPG_STRETCH_NOTHING_PAST_END,
                          EPG_STRETCH_SHARED_ID, EPG_STRETCH_UNRELATED, EPG_STRETCH_WAITING,
                          EpgAlternateEntry, EpgChannelStretch, EpgFinding, EpgSource,
                          EpgSourceChannel, EpgSourceSubscription, Recording)
from app.epg_sources import (Coverage, FILL_MIN_JUDGED, Listing, Span, decide_stretches,  # noqa: E402
                             delete_url_source, move_source, place_stretches,
                             retire_expired_listings, set_channel_key, set_source_override,
                             stop_using_source)
from tests.test_epg_sources import CFG, _Fixture  # noqa: E402

_APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app')


def _run(xml_id, first, last, title='Show', start=60, minutes=30):
    """Slots `first`..`last` of one channel's day: slot n is '<title> n', starting
    `start` + n * `minutes` minutes from now. Two sources given the same title agree."""
    return [(xml_id, start + n * minutes, minutes, f'{title} {n}')
            for n in range(first, last + 1)]


def _feed(base, listings):
    """An XMLTV payload whose times are offsets from `base`, never from the clock at the
    moment of the call: two imports a second apart must agree on when a slot starts, or a
    seam test passes or fails on how busy the box is."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for xml_id, offset, dur, title in listings:
        start = base + timedelta(minutes=offset)
        stop = start + timedelta(minutes=dur)
        parts.append(f'<programme start="{start:%Y%m%d%H%M%S} +0000" '
                     f'stop="{stop:%Y%m%d%H%M%S} +0000" channel="{xml_id}">'
                     f'<title>{title}</title></programme>')
    parts.append('</tv>')
    return ''.join(parts).encode()


def _titles(first, last, title='Show'):
    return [f'{title} {n}' for n in range(first, last + 1)]


# ── Pure ─────────────────────────────────────────────────────────────────────

T0 = datetime(2026, 11, 25, 12, 0)


def _listings(first, last, title='Show'):
    return [Listing(f'{title} {n}', None, None, T0 + timedelta(minutes=30 * n),
                    T0 + timedelta(minutes=30 * (n + 1))) for n in range(first, last + 1)]


def _span(rows):
    return Span(rows[-1].start, rows[-1].stop)


class DecideTests(unittest.TestCase):
    REAL = Coverage(20, 20)

    def _decide(self, held, later=(2,), covers=None, flagged=(), stored=None):
        covers = covers if covers is not None else {sid: self.REAL for sid in held}
        return decide_stretches(1, list(later), covers, {s: _span(r) for s, r in held.items() if r},
                                set(flagged), stored or {}, held)

    def test_a_source_that_agrees_and_reaches_further_fills_from_the_first_ones_end(self):
        (s,) = self._decide({1: _listings(0, 9), 2: _listings(0, 19)})
        self.assertEqual((s.source_id, s.position, s.outcome, s.after),
                         (2, 1, EPG_STRETCH_FILLS, 1))
        self.assertEqual(s.seam, T0 + timedelta(minutes=300))

    def test_a_source_that_reaches_no_further(self):
        (s,) = self._decide({1: _listings(0, 9), 2: _listings(0, 9)})
        self.assertEqual(s.outcome, EPG_STRETCH_NOTHING_PAST_END)
        self.assertIsNone(s.seam)

    def test_a_program_running_at_the_seam_is_not_something_past_the_end(self):
        # The later source's last program starts before the first's last stop.
        other = _listings(0, 8) + [Listing('Long', None, None, T0 + timedelta(minutes=290),
                                           T0 + timedelta(minutes=400))]
        (s,) = self._decide({1: _listings(0, 9), 2: other})
        self.assertEqual(s.outcome, EPG_STRETCH_NOTHING_PAST_END)

    def test_a_verdict_it_does_not_know_is_never_read_as_agreement(self):
        from app import epg_sources
        weak = epg_sources.compare_listings(_listings(0, 9), _listings(0, 19))._replace(
            verdict='something-new')
        with mock.patch('app.epg_sources.compare_listings', return_value=weak):
            with self.assertRaises(ValueError):
                self._decide({1: _listings(0, 9), 2: _listings(0, 19)})

    def test_another_channels_schedule_is_unrelated(self):
        (s,) = self._decide({1: _listings(0, 9), 2: _listings(0, 19, 'Other')})
        self.assertEqual(s.outcome, EPG_STRETCH_UNRELATED)

    def test_one_program_all_day_never_extends_a_real_schedule(self):
        (s,) = self._decide({1: _listings(0, 9), 2: _listings(0, 19)},
                            covers={1: self.REAL, 2: Coverage(20, 1)})
        self.assertEqual(s.outcome, EPG_STRETCH_NO_SCHEDULE)

    def test_one_program_all_day_may_extend_another_such_source_it_agrees_with(self):
        (s,) = self._decide({1: _listings(0, 9), 2: _listings(0, 19)},
                            covers={1: Coverage(10, 1), 2: Coverage(20, 1)})
        self.assertEqual(s.outcome, EPG_STRETCH_FILLS)

    def test_a_covering_source_with_no_listings_here_is_waiting(self):
        (s,) = self._decide({1: _listings(0, 9), 2: []}, covers={1: self.REAL, 2: self.REAL})
        self.assertEqual(s.outcome, EPG_STRETCH_WAITING)

    def test_a_source_that_does_not_cover_the_channel_gets_no_row(self):
        self.assertEqual(self._decide({1: _listings(0, 9)}, covers={1: self.REAL}), [])

    def test_a_shared_id_on_either_side_is_not_filled(self):
        held = {1: _listings(0, 9), 2: _listings(0, 19)}
        for flagged in ((2,), (1,)):
            (s,) = self._decide(held, flagged=flagged)
            self.assertEqual(s.outcome, EPG_STRETCH_SHARED_ID, flagged)

    def test_a_first_judgment_over_too_few_listings_is_no_overlap(self):
        few = FILL_MIN_JUDGED - 1
        (s,) = self._decide({1: _listings(0, few - 1), 2: _listings(0, 19)})
        self.assertEqual(s.outcome, EPG_STRETCH_NO_OVERLAP)

    def test_a_recorded_judgment_stands_until_enough_listings_overturn_it(self):
        thin = {1: _listings(0, 2), 2: _listings(0, 19, 'Other')}
        for kept in (EPG_STRETCH_FILLS, EPG_STRETCH_UNRELATED):
            (s,) = self._decide(thin, stored={2: (kept, 1)})
            self.assertEqual(s.outcome, kept)
        (s,) = self._decide({1: _listings(0, 9), 2: _listings(0, 19, 'Other')},
                            stored={2: (EPG_STRETCH_FILLS, 1)})
        self.assertEqual(s.outcome, EPG_STRETCH_UNRELATED, 'six listings overturn it')

    def test_a_judgment_against_another_source_is_not_kept(self):
        (s,) = self._decide({1: _listings(0, 2), 2: _listings(0, 19)},
                            stored={2: (EPG_STRETCH_FILLS, 7)})
        self.assertEqual(s.outcome, EPG_STRETCH_NO_OVERLAP)

    def test_a_third_source_fills_behind_the_second(self):
        a, b = self._decide({1: _listings(0, 9), 2: _listings(0, 19), 3: _listings(0, 29)},
                            later=(2, 3))
        self.assertEqual((a.outcome, b.outcome), (EPG_STRETCH_FILLS, EPG_STRETCH_FILLS))
        self.assertEqual((b.after, b.seam), (2, T0 + timedelta(minutes=600)))

    def test_a_refused_source_is_passed_over_and_the_next_judged_against_the_same_stretch(self):
        a, b = self._decide({1: _listings(0, 9), 2: _listings(0, 19, 'Other'),
                             3: _listings(0, 29)}, later=(2, 3))
        self.assertEqual((a.outcome, b.outcome), (EPG_STRETCH_UNRELATED, EPG_STRETCH_FILLS))
        self.assertEqual((b.position, b.after, b.seam), (2, 1, T0 + timedelta(minutes=300)))


# ── Over a database ──────────────────────────────────────────────────────────

class _Fill(_Fixture):
    """Alpha reads self.src first and self.ext second. `ch` reads the id 'c.test' in both:
    the first source lists slots 0-9 of its day and the second, the same day, slots 0-19."""

    FIRST = (0, 9)
    SECOND = (0, 19)

    def setUp(self):
        super().setUp()
        self.base = datetime.utcnow().replace(second=0, microsecond=0)
        self.ext = self._second_source()
        self.ch = self._channel('Chan', 'c.test')
        self._import(self.src, _run('c.test', *self.FIRST))
        if self.SECOND:
            self._import(self.ext, self._second_feed())

    def _second_feed(self):
        return _run('c.test', *self.SECOND)

    def _import(self, source, listings):
        import_source(db.session.get(EpgSource, source.id), _feed(self.base, listings),
                      epg_days=3, cfg=CFG)
        db.session.expire_all()

    def _guide(self, ch=None):
        """[(source_id, title)] of epg_entries by start - what every guide reader sees."""
        return self._active(ch or self.ch)

    def _outcomes(self, ch=None):
        db.session.expire_all()
        return {r.source_id: r.outcome for r in
                EpgChannelStretch.query.filter_by(channel_id=(ch or self.ch).id)}

    def _assert_one_timeline(self, ch=None):
        """No two listings in epg_entries overlap, and no listing is in both tables."""
        ch = ch or self.ch
        rows = EPGEntry.query.filter_by(channel_id=ch.id).order_by(EPGEntry.start_time).all()
        for a, b in zip(rows, rows[1:]):
            self.assertLessEqual(a.stop_time, b.start_time,
                                 f'{a.title} (source {a.source_id}) overlaps {b.title} '
                                 f'(source {b.source_id})')
        both = ({(r.source_id, r.start_time) for r in rows}
                & {(r.source_id, r.start_time)
                   for r in EpgAlternateEntry.query.filter_by(channel_id=ch.id)})
        self.assertFalse(both, 'a listing sits in both tables')

    def _assert_guide(self, first_slots, second_slots, first=None, second=None):
        first, second = first or self.src, second or self.ext
        want = ([(first.id, t) for t in _titles(*first_slots)] if first_slots else [])
        want += [(second.id, t) for t in _titles(*second_slots)] if second_slots else []
        self.assertEqual(self._guide(), want)
        self._assert_one_timeline()


class SeamTests(_Fill):

    def test_a_first_import_continues_the_guide_from_the_second_source(self):
        self._assert_guide((0, 9), (10, 19))
        self.assertEqual(self._winner(self.ch), self.src.id, 'the first source is unchanged')
        self.assertEqual(self._outcomes(), {self.ext.id: EPG_STRETCH_FILLS})
        self.assertEqual([t for _s, t in self._alternates(self.ch)], _titles(0, 9),
                         "the second source's earlier listings stay aside")
        ev = ChannelEvent.query.filter_by(channel_id=self.ch.id,
                                          event_type=CHANNEL_EPG_FILL_CHANGED).one()
        self.assertIn(self.ext.name, ev.detail)
        self.assertIn(self.src.name, ev.detail)

    def test_a_first_source_refresh_that_grows_moves_the_seam_forward(self):
        self._import(self.src, _run('c.test', 0, 13))
        self._assert_guide((0, 13), (14, 19))
        self.assertEqual(ChannelEvent.query.filter_by(
            channel_id=self.ch.id, event_type=CHANNEL_EPG_FILL_CHANGED).count(), 1,
            'a seam that only moved writes nothing')

    def test_a_first_source_refresh_that_shrinks_moves_the_seam_back(self):
        self._import(self.src, _run('c.test', 0, 6))
        self._assert_guide((0, 6), (7, 19))

    def test_a_refresh_of_the_filling_source_keeps_the_seam(self):
        self._import(self.ext, _run('c.test', 0, 25))
        self._assert_guide((0, 9), (10, 25))

    def test_a_first_source_that_overtakes_the_second_ends_the_fill(self):
        self._import(self.src, _run('c.test', 0, 22))
        self._assert_guide((0, 22), None)
        self.assertEqual(self._outcomes(), {self.ext.id: EPG_STRETCH_NOTHING_PAST_END})
        ev = ChannelEvent.query.filter_by(
            channel_id=self.ch.id, event_type=CHANNEL_EPG_FILL_CHANGED).order_by(
            ChannelEvent.id.desc()).first()
        self.assertIn('no longer continues', ev.detail)

    def test_a_gap_at_the_seam_stays_a_gap(self):
        # The second source has a hole of its own right after the first one ends.
        self._import(self.ext, _run('c.test', 0, 9) + _run('c.test', 13, 19))
        self._assert_guide((0, 9), (13, 19))

    def test_a_program_running_at_the_seam_is_left_out(self):
        # The second source's slots are an hour long, so one straddles the first's end.
        self._import(self.ext, [('c.test', 60 + n * 30, 30, f'Show {n}') for n in range(0, 9)]
                     + [('c.test', 60 + 9 * 30 - 15, 60, 'Straddler')]
                     + _run('c.test', 11, 19))
        self.assertNotIn('Straddler', [t for _s, t in self._guide()])
        self._assert_guide((0, 9), (11, 19))

    def test_a_reorder_starts_the_guide_with_the_other_source(self):
        self.assertTrue(move_source(self.acct.id, self.src.id, 1, False).moved)
        self._assert_guide(None, (0, 19))
        self.assertEqual(self._winner(self.ch), self.ext.id)
        self.assertEqual(self._outcomes(), {self.src.id: EPG_STRETCH_NOTHING_PAST_END})
        move_source(self.acct.id, self.src.id, -1, False)
        self._assert_guide((0, 9), (10, 19))

    def test_an_override_starts_the_guide_with_the_pinned_source_and_the_other_fills(self):
        self._import(self.src, _run('c.test', 0, 25))      # now the first source is longer
        self._assert_guide((0, 25), None)
        ch = db.session.get(Channel, self.ch.id)
        set_source_override(ch, db.session.get(EpgSource, self.ext.id), case_sensitive=False)
        db.session.commit()
        self.assertEqual(self._guide(), [(self.ext.id, t) for t in _titles(0, 19)]
                         + [(self.src.id, t) for t in _titles(20, 25)])
        self._assert_one_timeline()
        self.assertEqual(self._outcomes(), {self.src.id: EPG_STRETCH_FILLS})
        set_source_override(db.session.get(Channel, self.ch.id), None, case_sensitive=False)
        db.session.commit()
        self._assert_guide((0, 25), None)

    def test_stopping_the_filling_source_ends_the_guide_at_the_first_ones_end(self):
        stop_using_source(self.acct.id, self.ext.id, False)
        self._assert_guide((0, 9), None)
        self.assertEqual(self._outcomes(), {})
        self.assertEqual(self._alternates(self.ch), [])

    def test_deleting_the_filling_source_ends_the_guide_at_the_first_ones_end(self):
        delete_url_source(self.ext.id, False)
        self._assert_guide((0, 9), None)
        self.assertEqual(self._outcomes(), {})

    def test_stopping_the_first_source_starts_the_guide_with_the_second(self):
        stop_using_source(self.acct.id, self.src.id, False)
        self._assert_guide(None, (0, 19))
        self.assertEqual(self._outcomes(), {})

    def test_a_third_source_continues_behind_the_second(self):
        third = self._second_source(priority=3, name='Third')
        self._import(third, _run('c.test', 0, 29))
        self.assertEqual(self._guide(), [(self.src.id, t) for t in _titles(0, 9)]
                         + [(self.ext.id, t) for t in _titles(10, 19)]
                         + [(third.id, t) for t in _titles(20, 29)])
        self._assert_one_timeline()
        # With the second gone, the third continues from the first's end.
        stop_using_source(self.acct.id, self.ext.id, False)
        self._assert_guide((0, 9), (10, 29), second=third)

    def test_a_change_of_first_source_never_leaves_two_sources_programs_before_it_places(self):
        """What a restart between the row moves and the placement leaves: the new first
        source's listings alone, never beside the ones that continued the old one."""
        with mock.patch('app.epg_sources.place_stretches') as placed:
            move_source(self.acct.id, self.src.id, 1, False)
        placed.assert_called()
        self.assertEqual(self._guide(), [(self.ext.id, t) for t in _titles(0, 19)])
        self._assert_one_timeline()
        with mock.patch('app.epg_sources.place_stretches'):
            move_source(self.acct.id, self.src.id, -1, False)
        self.assertEqual(self._guide(), [(self.src.id, t) for t in _titles(0, 9)],
                         'short until it is placed, and nothing doubled')
        place_stretches([self.ch.id], False)
        self._assert_guide((0, 9), (10, 19))

    def test_an_import_that_changes_the_first_source_moves_the_others_out_first(self):
        """The first source starts listing one program all day, so an import of it hands
        the guide to the second. Its rows and the old fill leave before the new start
        arrives; seen at the moment the import places."""
        seen = []
        real = place_stretches

        def watched(ids, cs, now=None):
            self._assert_one_timeline()
            seen.append(self._winner(self.ch))
            return real(ids, cs, now)

        with mock.patch('app.accounts.place_stretches', side_effect=watched):
            self._import(self.src, [('c.test', 60 + n * 30, 30, 'Banner') for n in range(10)])
        self.assertEqual(seen, [self.ext.id])
        self.assertEqual(self._guide(), [(self.ext.id, t) for t in _titles(0, 19)])
        self._assert_one_timeline()

    def test_no_moment_of_a_growing_first_import_holds_two_sources_programs(self):
        """The filling source's listings inside the hours the new file reaches leave
        epg_entries before the file's own are written, not after."""
        seen = []
        real = place_stretches

        def watched(ids, cs, now=None):
            seen.append(list(self._guide()))
            self._assert_one_timeline()
            return real(ids, cs, now)

        with mock.patch('app.accounts.place_stretches', side_effect=watched):
            self._import(self.src, _run('c.test', 0, 13))
        self.assertTrue(seen, 'the import placed')
        self.assertEqual(seen[0], [(self.src.id, t) for t in _titles(0, 13)]
                         + [(self.ext.id, t) for t in _titles(14, 19)])


class RefusalTests(_Fill):

    def _second_feed(self):
        return _run('c.test', *self.SECOND, title='Other')

    def test_a_source_listing_another_schedule_does_not_extend_the_guide(self):
        self._assert_guide((0, 9), None)
        self.assertEqual(self._outcomes(), {self.ext.id: EPG_STRETCH_UNRELATED})
        self.assertFalse(ChannelEvent.query.filter_by(
            channel_id=self.ch.id, event_type=CHANNEL_EPG_FILL_CHANGED).count())

    def test_it_fills_once_its_listings_match(self):
        self._import(self.ext, _run('c.test', 0, 19))
        self._assert_guide((0, 9), (10, 19))

    def test_one_program_all_day_does_not_extend_a_real_schedule(self):
        self._import(self.ext, [('c.test', 60 + n * 30, 30, 'Banner') for n in range(20)])
        self._assert_guide((0, 9), None)
        self.assertEqual(self._outcomes(), {self.ext.id: EPG_STRETCH_NO_SCHEDULE})

    def test_a_channel_reading_a_shared_id_is_not_filled(self):
        self._import(self.ext, _run('c.test', 0, 19))
        self._assert_guide((0, 9), (10, 19))
        db.session.add(EpgFinding(account_id=self.acct.id, source_id=self.ext.id,
                                  kind=EPG_FINDING_SHARED_ID, subject='C.TEST', count=2,
                                  found_at=datetime.utcnow()))
        db.session.commit()
        place_stretches([self.ch.id], False)
        self._assert_guide((0, 9), None)
        self.assertEqual(self._outcomes(), {self.ext.id: EPG_STRETCH_SHARED_ID})

    def test_the_epg_check_places_an_account_again_when_its_shared_ids_change(self):
        from app import epg_scorecard
        self._import(self.ext, _run('c.test', 0, 19))
        before = epg_scorecard._shared_id_subjects()
        db.session.add(EpgFinding(account_id=self.acct.id, source_id=self.ext.id,
                                  kind=EPG_FINDING_SHARED_ID, subject='c.test', count=2,
                                  found_at=datetime.utcnow()))
        db.session.commit()
        after = epg_scorecard._shared_id_subjects()
        self.assertEqual(after, {self.acct.id: {(self.ext.id, 'c.test')}})
        epg_scorecard._place_where_flags_moved(before, after)
        self.assertEqual(self._outcomes(), {self.ext.id: EPG_STRETCH_SHARED_ID})
        self._assert_guide((0, 9), None)
        # Unchanged findings place nothing.
        with mock.patch('app.epg_sources.place_stretches') as placed:
            epg_scorecard._place_where_flags_moved(after, after)
        placed.assert_not_called()

    def test_a_fill_stands_while_the_first_source_runs_down(self):
        """The first source stops refreshing: what both list shrinks to nothing, which is
        when the fill is carrying the guide (§14.3)."""
        self._import(self.ext, _run('c.test', 0, 19))
        later = self.base + timedelta(minutes=60 + 8 * 30 + 5)   # inside slot 8
        placed = place_stretches([self.ch.id], False, now=later)
        self.assertEqual(placed.channels, 0)
        self.assertEqual(self._outcomes(), {self.ext.id: EPG_STRETCH_FILLS})
        self._assert_guide((0, 9), (10, 19))


class ThinTests(_Fill):
    FIRST = (0, FILL_MIN_JUDGED - 2)
    SECOND = None

    def test_too_little_listed_by_both_is_not_trusted(self):
        self._import(self.ext, _run('c.test', 0, 19))
        self._assert_guide(self.FIRST, None)
        self.assertEqual(self._outcomes(), {self.ext.id: EPG_STRETCH_NO_OVERLAP})

    def test_a_source_not_refreshed_yet_is_waiting(self):
        db.session.add(EpgSourceChannel(source_id=self.ext.id, xml_id='c.test', entry_count=20,
                                        distinct_titles=20))
        db.session.commit()
        place_stretches([self.ch.id], False)
        self.assertEqual(self._outcomes(), {self.ext.id: EPG_STRETCH_WAITING})
        self._assert_guide(self.FIRST, None)


class RerunTests(_Fill):

    def _rows(self):
        return (sorted((r.source_id, r.title) for r in
                       EPGEntry.query.filter_by(channel_id=self.ch.id)),
                sorted((r.source_id, r.title) for r in
                       EpgAlternateEntry.query.filter_by(channel_id=self.ch.id)))

    def test_run_twice_it_moves_nothing(self):
        self.assertEqual(place_stretches([self.ch.id], False), (0, 0, 0))

    def test_rows_left_in_the_wrong_table_are_put_back(self):
        """What a move interrupted by a restart leaves: every row of the filling source in
        epg_entries, every program doubled; and the reverse, every row in the alternates."""
        from app.epg_sources import demote, promote
        want = self._rows()
        promote([self.ch.id], self.ext.id)
        self.assertEqual(len(self._guide()), 30, 'doubled')
        placed = place_stretches([self.ch.id], False)
        self.assertEqual(self._rows(), want)
        self.assertEqual((placed.moved_in, placed.moved_out), (0, 10))
        demote([self.ch.id], self.ext.id)
        demote([self.ch.id], self.src.id)
        self.assertEqual(self._guide(), [])
        place_stretches([self.ch.id], False)
        self.assertEqual(self._rows(), want)
        self._assert_one_timeline()

    def test_each_batch_takes_the_write_lock_before_it_reads(self):
        """The order only: what the lock prevents (a plan read before another writer's
        commit and applied after it) needs two writers, which one thread cannot stage."""
        from app import epg_sources
        order = []
        real_lock, real_plan = epg_sources._take_write_lock, epg_sources._plan_stretches
        with mock.patch.object(epg_sources, '_take_write_lock',
                               side_effect=lambda: (order.append('lock'), real_lock())[1]), \
             mock.patch.object(epg_sources, '_plan_stretches',
                               side_effect=lambda *a, **k: (order.append('plan'),
                                                            real_plan(*a, **k))[1]):
            place_stretches([self.ch.id], False)
        self.assertEqual(order, ['lock', 'plan'])

    def test_a_channel_with_no_first_source_keeps_no_stretch_rows(self):
        db.session.execute(db.update(Channel).where(Channel.id == self.ch.id)
                           .values(epg_source_id=None))
        db.session.commit()
        place_stretches([self.ch.id], False)
        self.assertEqual(self._outcomes(), {})


class ReaderTests(_Fill):

    def test_the_guide_api_shows_each_program_once(self):
        client = self.t.app.test_client()
        now = datetime.utcnow()
        r = client.get('/api/guide/epg', query_string={
            'start': (now - timedelta(hours=1)).isoformat(),
            'end': (now + timedelta(hours=24)).isoformat(), 'channel_id': self.ch.id})
        self.assertEqual(r.status_code, 200, r.get_json())
        titles = [p['title'] for row in r.get_json()['channels'] for p in row['programs']]
        self.assertEqual(sorted(titles), sorted(_titles(0, 19)))

    def test_both_search_grains_see_one_timeline(self):
        from app.search_index import rebuild_search_indexes
        rebuild_search_indexes('test')
        client = self.t.app.test_client()
        r = client.get('/api/channels/search', query_string={'grain': 'airings', 'q': 'Show',
                                                             'limit': 100})
        self.assertEqual(r.status_code, 200, r.get_json())
        titles = [row['title'] for row in r.get_json()['rows']]
        self.assertEqual(sorted(titles), sorted(_titles(0, 19)))
        r = client.get('/api/channels/search', query_string={'grain': 'channels', 'q': 'Chan'})
        self.assertEqual([row['id'] for row in r.get_json()['rows']], [self.ch.id])


class LongFill(_Fill):
    """The second source runs two days past the first, so the two end on different days."""
    SECOND = (0, 99)


class WhatReadsItTests(LongFill):
    """Everything that asked "what does this channel's source hold" of one table."""

    def _tz(self):
        from app.tz_utils import get_display_tz
        return get_display_tz()

    def test_an_accounts_reach_counts_a_continued_guide_at_the_later_source(self):
        from app.epg_sources import accounts_reach, source_reach
        days = {sid: r.day for sid, r in
                source_reach([self.src.id, self.ext.id], self._tz()).items()}
        self.assertLess(days[self.src.id], days[self.ext.id])
        span = accounts_reach([self.acct.id], self._tz())[self.acct.id]
        self.assertEqual((span.first, span.most, span.last), (days[self.ext.id],) * 3)

    def test_a_rules_reach_follows_each_channels_own_sources(self):
        """A rule on a channel whose guide is continued reaches as far as the later source;
        a rule on a channel nothing continues stops with the first source, however far the
        later source runs for other channels."""
        self._channel('Solo', 'd.test')
        self._import(self.src, _run('c.test', *self.FIRST) + _run('d.test', 0, 9, title='Solo'))
        mock.patch('app.routes.recordings.schedule_recording').start()
        self.addCleanup(mock.patch.stopall)
        rules = {}
        for name, q in (('filled', 'Show+12'), ('solo', 'Solo+3')):
            rules[name] = auto_rules.create_rule(auto_rules.read_rule_values(
                {'name': name, 'search': f'grain=airings&q={q}'}))
            auto_rules.evaluate_now(rules[name])
        db.session.expire_all()
        by_rule, members, channel_source = auto_rules._reach_inputs(list(rules.values()))
        reach = auto_rules._guide_reach()

        def reach_of(name):
            return auto_rules._reach_for(auto_rules._rule_sources(
                by_rule[rules[name]], members, channel_source), reach)[0]

        def latest(source):
            return db.session.query(db.func.max(EPGEntry.start_time)).filter(
                EPGEntry.source_id == source.id).scalar()

        self.assertEqual(reach_of('filled'), latest(self.ext))
        self.assertEqual(reach_of('solo'), latest(self.src))
        self.assertLess(latest(self.src), latest(self.ext))

    def test_the_similarity_scan_reads_a_split_schedule_as_one(self):
        from app import epg_similarity
        units, n = epg_similarity._read_units(self.base, epg_similarity._Titles())
        self.assertEqual(n, 110)
        self.assertEqual(len(units[(self.ch.id, self.ext.id)][2]), 100)
        self.assertEqual(len(units[(self.ch.id, self.src.id)][2]), 10)

    def test_the_comparison_reads_each_source_whole(self):
        from app.epg_sources import channel_source_comparisons
        active, (comp,) = channel_source_comparisons(db.session.get(Channel, self.ch.id),
                                                     now=self.base)
        self.assertEqual([x.title for x in active], _titles(0, 9))
        self.assertEqual(comp.source.id, self.ext.id)
        self.assertEqual((comp.comparison.compared, comp.comparison.same), (10, 10))
        self.assertEqual(comp.last_stop, self.base + timedelta(minutes=60 + 100 * 30))

    def test_the_channel_page_names_each_stretch(self):
        from app.epg_sources import channel_guide_view
        view = channel_guide_view(db.session.get(Channel, self.ch.id), False)
        self.assertEqual([c['name'] for c in view['chain']], [self.src.name, self.ext.name])
        self.assertEqual(view['chain'][0]['until'], self.base + timedelta(minutes=60 + 10 * 30))
        row = next(r for r in view['rows'] if r['id'] == self.ext.id)
        self.assertEqual((row['stretch'], row['stretch_from'], row['stretch_note']),
                         (EPG_STRETCH_FILLS, self.base + timedelta(minutes=60 + 10 * 30), None))
        self.assertTrue(view['comparable'])
        html = self.t.app.test_client().get(f'/channels/{self.ch.id}').get_data(as_text=True)
        self.assertIn('and the guide continues from', html)
        self.assertIn('Continues the guide from', html)

    def test_the_channel_page_says_why_a_source_was_not_used(self):
        from app.epg_sources import channel_guide_view
        self._import(self.ext, _run('c.test', 0, 99, title='Other'))
        view = channel_guide_view(db.session.get(Channel, self.ch.id), False)
        self.assertEqual(len(view['chain']), 1)
        row = next(r for r in view['rows'] if r['id'] == self.ext.id)
        self.assertEqual(row['stretch'], EPG_STRETCH_UNRELATED)
        self.assertIn(f"do not match {self.src.name}'s", row['stretch_note'])
        html = self.t.app.test_client().get(f'/channels/{self.ch.id}').get_data(as_text=True)
        self.assertIn('so it is not used to continue the guide', html)

    def test_every_outcome_has_its_sentence(self):
        from app.database import EPG_STRETCH_OUTCOMES
        from app.epg_sources import stretch_note
        notes = {o: stretch_note(o, 'Alpha') for o in EPG_STRETCH_OUTCOMES}
        self.assertIsNone(notes.pop(EPG_STRETCH_FILLS))
        self.assertEqual(len(set(notes.values())), len(notes))
        with self.assertRaises(ValueError):
            stretch_note('something-new', None)

    def test_the_sources_card_counts_the_channels_it_extends(self):
        from app.epg_sources import account_sources_view
        rows = {r['id']: r for r in account_sources_view(self.acct.id)}
        self.assertEqual((rows[self.src.id]['active_here'], rows[self.src.id]['extends_here']),
                         (1, 0))
        self.assertEqual((rows[self.ext.id]['active_here'], rows[self.ext.id]['extends_here']),
                         (0, 1))
        html = self.t.app.test_client().get(f'/accounts/{self.acct.id}').get_data(as_text=True)
        self.assertIn('extends 1 more', html)

    def test_reach_is_no_reason_to_change_source_when_the_guide_already_continues(self):
        from app.epg_scorecard import RULE_REACH, account_findings, account_scorecard
        from app.database import EPG_FINDING_BETTER_SOURCE
        card = account_scorecard(self.acct.id, self._tz(), False, with_channels=True)
        (score,) = card.channels
        self.assertEqual((score.best, score.why), (self.ext.id, RULE_REACH))
        self.assertEqual((score.filled, score.reach_met), ((self.ext.id,), True))
        self.assertEqual(card.guided.day, card.sources[1].reach.day)
        found = account_findings(card, {}, [])
        self.assertFalse([f for f in found if f.kind == EPG_FINDING_BETTER_SOURCE])
        # Refused, the same channel is a finding again.
        self._import(self.ext, _run('c.test', 0, 99, title='Other'))
        card = account_scorecard(self.acct.id, self._tz(), False, with_channels=True)
        self.assertFalse(card.channels[0].reach_met)
        self.assertTrue([f for f in account_findings(card, {}, [])
                         if f.kind == EPG_FINDING_BETTER_SOURCE])


class RanOutTests(_Fill):

    def _retire(self, source, hours):
        """What a refused refresh that kept its listings does, `hours` from now."""
        return retire_expired_listings(source.id, False,
                                       now=datetime.utcnow() + timedelta(hours=hours))

    def test_a_first_source_that_ran_out_hands_the_start_to_the_filler(self):
        # Slot 9 ends 6 hours from now, slot 19 at 11.
        self._retire(self.src, 7)
        self.assertEqual(self._winner(self.ch), self.ext.id)
        self._assert_one_timeline()
        self.assertEqual([s for s, _t in self._guide()], [self.ext.id] * 20)
        self.assertNotIn(EPG_STRETCH_FILLS, self._outcomes().values())

    def test_a_filling_source_that_ran_out_stops_being_a_stretch(self):
        self._retire(self.ext, 12)
        self.assertEqual(self._winner(self.ch), self.src.id)
        self.assertEqual(self._outcomes(), {})
        self.assertEqual(db.session.query(EpgSourceChannel.entry_count).filter_by(
            source_id=self.ext.id).scalar(), 0)


class RuleTests(_Fill):
    """§14.7: a recording made from a filled listing that the first source later replaces
    with its own listing of the same program."""

    def setUp(self):
        super().setUp()
        self.armed = mock.patch('app.routes.recordings.schedule_recording').start()
        self.addCleanup(mock.patch.stopall)
        self.rule_id = auto_rules.create_rule(auto_rules.read_rule_values(
            {'name': 'Fill', 'search': 'grain=airings&q=Show+12'}))
        auto_rules.evaluate_now(self.rule_id)
        db.session.expire_all()

    def _recordings(self):
        db.session.expire_all()
        return Recording.query.filter_by(rule_id=self.rule_id).all()

    def test_the_rule_recorded_the_filled_listing(self):
        (rec,) = self._recordings()
        self.assertEqual(rec.program_title, 'Show 12')

    def test_the_first_source_listing_the_program_itself_makes_no_second_recording(self):
        (before,) = self._recordings()
        self._import(self.src, _run('c.test', 0, 15))
        self.assertEqual(self._guide()[12], (self.src.id, 'Show 12'))
        auto_rules.evaluate_now(self.rule_id)
        (after,) = self._recordings()
        self.assertEqual(after.id, before.id)
        self.assertEqual(after.status, before.status, 'not cancelled as orphaned')

    def test_nor_when_the_first_source_starts_it_two_minutes_off(self):
        (before,) = self._recordings()
        self._import(self.src, _run('c.test', 0, 11)
                     + [('c.test', 60 + 12 * 30 + 2, 28, 'Show 12')]
                     + _run('c.test', 13, 15))
        auto_rules.evaluate_now(self.rule_id)
        (after,) = self._recordings()
        self.assertEqual((after.id, after.status), (before.id, before.status))


class CopyTests(_Fill):

    def test_a_key_write_copies_both_halves_of_a_donor_and_places_them(self):
        """The donor holds the filling source's listings in both tables. A channel given its
        key gets all of them, behind its own first source's end."""
        twin = self._channel('Twin', 'twin.test')
        self._import(self.src, _run('c.test', 0, 9) + _run('twin.test', 0, 5))
        self.assertEqual(self._outcomes(twin), {})
        set_channel_key(db.session.get(Channel, twin.id), db.session.get(EpgSource, self.ext.id),
                        'c.test', case_sensitive=False)
        db.session.commit()
        self.assertEqual(self._guide(twin), [(self.src.id, t) for t in _titles(0, 5)]
                         + [(self.ext.id, t) for t in _titles(6, 19)])
        self._assert_one_timeline(twin)
        self.assertEqual(sorted(t for _s, t in self._alternates(twin)), sorted(_titles(0, 5)))
        self._assert_guide((0, 9), (10, 19))


class TeardownTests(_Fill):

    def test_a_deleted_source_takes_its_stretch_rows(self):
        delete_url_source(self.ext.id, False)
        self.assertEqual(EpgChannelStretch.query.count(), 0)

    def test_a_stopped_subscription_takes_its_stretch_rows(self):
        stop_using_source(self.acct.id, self.ext.id, False)
        self.assertEqual(EpgChannelStretch.query.count(), 0)

    def test_a_hidden_channel_keeps_none(self):
        from app import channel_hiding
        db.session.get(Channel, self.ch.id).hidden = True
        db.session.commit()
        channel_hiding.purge_hidden_epg()
        db.session.commit()
        self.assertEqual(EpgChannelStretch.query.count(), 0)

    def test_a_channel_deleted_through_the_orm_takes_them(self):
        db.session.delete(db.session.get(Channel, self.ch.id))
        db.session.commit()
        self.assertEqual(EpgChannelStretch.query.count(), 0)


class AccountDeleteTests(_Fill):
    """Another account reads Alpha's two sources, its guide continued by the second and a
    third of its own behind that. Deleting Alpha takes both; the third starts the guide."""

    def test_a_deleted_accounts_source_stops_continuing_other_accounts_guides(self):
        from tests.support import seed
        from app.database import Account
        other = seed.make_account(name='Beta')
        own = EpgSource(kind='url', owner_account_id=other.id, name='Own',
                        url='http://own.test/epg.xml')
        db.session.add(own)
        db.session.flush()
        for n, sid in enumerate((self.src.id, self.ext.id, own.id), 1):
            db.session.add(EpgSourceSubscription(source_id=sid, account_id=other.id,
                                                 priority=n))
        ch = seed.make_channel(other, name='Beta chan', epg_channel_id='c.test',
                               stream_id=901)
        db.session.commit()
        self._import(self.src, _run('c.test', *self.FIRST))
        self._import(self.ext, _run('c.test', *self.SECOND))
        self._import(own, _run('c.test', 0, 29))
        self.assertEqual(self._outcomes(ch), {self.ext.id: EPG_STRETCH_FILLS,
                                              own.id: EPG_STRETCH_FILLS})
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        r = self.t.app.test_client().delete(f'/api/accounts/{self.acct.id}')
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIsNone(db.session.get(Account, self.acct.id))
        db.session.expire_all()
        self.assertEqual(self._guide(ch), [(own.id, t) for t in _titles(0, 29)])
        self.assertEqual(self._outcomes(ch), {})
        self._assert_one_timeline(ch)


# ── Static ───────────────────────────────────────────────────────────────────

#: Calls that move or copy listings between or into the two tables. `from_select` is the
#: INSERT ... SELECT every one of them is written as, so a move spelled out in place is seen
#: too. A function passes when a placer is called anywhere in it, not on every path.
_MOVERS = {'_move_rows', '_move', 'demote', 'promote', 'demote_others', '_copy_listings',
           'from_select'}
#: Calls that end in the placement for the channels touched.
_PLACERS = {'place_stretches', 'place_channel_now', 'reresolve_channels', 'reresolve_account'}
#: A function that moves listings and does not place them itself, with the functions that
#: call it and do. Every caller in app/ must be named, and each must reach a placer.
_PLACED_BY_CALLER = {
    # The placement's own machinery, and the two whole-source moves it is built from.
    '_move_rows': {'_move', '_apply_key_now', '_drop_source_now', 'set_source_override',
                   '_apply_placements', 'make_room_for_first'},
    '_move': {'demote', 'promote'},
    'demote': set(),
    'promote': {'reresolve_channels', '_import_source'},
    'demote_others': {'reresolve_channels', '_import_source'},
    '_apply_placements': {'place_stretches', 'place_channel_now'},
    # Makes room before an import writes; the import places at its end.
    'make_room_for_first': {'_import_source'},
    # Copies into the alternates; each caller re-resolves the channels copied.
    'copy_held_listings': {'subscribe_to_source', 'settle_accounts'},
    '_copy_listings': {'copy_held_listings'},
}


def _module_functions(path):
    """{function name: names it calls} for a module's top-level functions, a nested
    closure's calls counted as its outer function's; and the names the module imports from
    epg_sources."""
    with open(path) as f:
        tree = ast.parse(f.read())
    imported = {a.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                and (node.module or '').endswith('epg_sources') for a in node.names}
    out = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            out[node.name] = {n.func.id if isinstance(n.func, ast.Name) else n.func.attr
                              for n in ast.walk(node) if isinstance(n, ast.Call)
                              and isinstance(n.func, (ast.Name, ast.Attribute))}
    return out, imported


class EveryMoverPlacesTests(unittest.TestCase):
    """A path that moved a filling source's rows and skipped the placement would leave its
    whole listing beside the first source's, every program doubled (§14.4)."""

    def setUp(self):
        self.calls = {}
        for root, _dirs, files in os.walk(_APP):
            for name in files:
                if not name.endswith('.py'):
                    continue
                functions, imported = _module_functions(os.path.join(root, name))
                # A module with a `_move` of its own (sports_follow) is not moving listings.
                known = (_MOVERS | _PLACERS | set(_PLACED_BY_CALLER)
                         if name == 'epg_sources.py' else imported | {'from_select'})
                for fn, calls in functions.items():
                    self.calls.setdefault(fn, set()).update(calls & known)

    def test_the_scan_sees_the_movers(self):
        self.assertIn('_move_rows', self.calls['set_source_override'])
        self.assertIn('demote_others', self.calls['_import_source'])

    def test_every_function_that_moves_listings_places_them_or_names_who_does(self):
        missing = [fn for fn, calls in sorted(self.calls.items())
                   if calls & _MOVERS and not calls & _PLACERS and fn not in _PLACED_BY_CALLER]
        self.assertFalse(missing, 'these move or copy listings and neither end in '
                                  'place_stretches() nor name the caller that does: '
                                  f'{missing}')

    def test_every_named_caller_exists_and_places(self):
        for fn, callers in _PLACED_BY_CALLER.items():
            self.assertIn(fn, self.calls, f'{fn} is gone: drop its entry')
            actual = {c for c, calls in self.calls.items() if fn in calls}
            self.assertEqual(actual - {fn}, callers, f'callers of {fn}')
            for caller in callers:
                if caller not in _PLACED_BY_CALLER and caller not in _PLACERS:
                    self.assertTrue(self.calls[caller] & _PLACERS,
                                    f'{caller} calls {fn} and never places')


if __name__ == '__main__':
    unittest.main()
