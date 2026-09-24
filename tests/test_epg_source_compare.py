"""Tier 2 - comparing what two EPG sources say about one channel (DESIGN-epg-sources.md §4,
§12; dev/changelog/1108).

  - CompareListingsTests: the pure comparator. The same program is a matching title starting
    within five minutes; a same-titled listing up to six hours away has moved; each verdict
    (identical / descriptions / mostly / shifted / unrelated / nothing to compare) from the
    counts; only the stretch both sources list is judged; a side listing the channel twice is
    shown and left out of the verdict; the title match drops providers' superscript tags and
    a leading `Live:`, and takes a title that is the other plus more words only when the two
    start together.
  - ComparePageTests: the page over two real sources, its link on the channel page, the
    source picker and Differences only, and the empty states. Nothing is written.

No network: every feed is a local byte string (CLAUDE.md §Testing).
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import EPGEntry, EpgAlternateEntry, EpgSource  # noqa: E402
from app.epg_sources import (CMP_DESCRIPTIONS, CMP_IDENTICAL, CMP_MOSTLY,  # noqa: E402
                             CMP_NO_OVERLAP, CMP_SHIFTED, CMP_UNRELATED,
                             ROW_DIFFERENT, ROW_DOUBLED_ACTIVE, ROW_MOVED,
                             ROW_ONLY_ACTIVE, ROW_ONLY_OTHER, ROW_SAME, Listing,
                             channel_source_comparisons, compare_listings)
from tests.test_epg_sources import CFG, REAL, _Fixture, _schedule, _xmltv  # noqa: E402

T0 = datetime(2026, 9, 27, 17, 0)


def _day(titles, start=T0, minutes=60, described=False):
    """Back-to-back hour-long listings, one per title."""
    return [Listing(t, None, 'About it.' if described else None,
                    start + timedelta(minutes=i * minutes),
                    start + timedelta(minutes=(i + 1) * minutes))
            for i, t in enumerate(titles)]


SHOWS = ['Pre-race', 'NASCAR Cup Series', 'Post-race', 'News', 'Late Show', 'Movie']


class CompareListingsTests(unittest.TestCase):

    def test_the_same_listings_are_identical(self):
        c = compare_listings(_day(SHOWS), _day(SHOWS))
        self.assertEqual(c.verdict, CMP_IDENTICAL)
        self.assertEqual((c.compared, c.same), (6, 6))
        self.assertEqual({r.kind for r in c.rows}, {ROW_SAME})

    def test_a_start_within_five_minutes_is_the_same_program(self):
        other = _day(SHOWS)
        other[3] = other[3]._replace(start=other[3].start + timedelta(minutes=5))
        self.assertEqual(compare_listings(_day(SHOWS), other).verdict, CMP_IDENTICAL)

    def test_a_rain_delay_is_a_move_not_a_different_program(self):
        """The NASCAR question: one source moved the race 90 minutes, the other did not."""
        other = _day(SHOWS)
        other[1] = other[1]._replace(start=other[1].start + timedelta(minutes=90),
                                     stop=other[1].stop + timedelta(minutes=90))
        c = compare_listings(_day(SHOWS), other)
        self.assertEqual(c.verdict, CMP_SHIFTED)
        self.assertEqual((c.same, c.moved), (5, 1))
        self.assertEqual(c.largest_move, timedelta(minutes=90))
        moved = next(r for r in c.rows if r.kind == ROW_MOVED)
        self.assertEqual(moved.active.title, 'NASCAR Cup Series')

    def test_the_nearest_airing_of_a_repeating_title_is_the_one_paired(self):
        a = _day(['SportsCenter'] * 4)
        b = _day(['SportsCenter'] * 4, start=T0 + timedelta(minutes=20))
        c = compare_listings(a, b)
        self.assertEqual(c.moved, 4)
        self.assertEqual({r.other.start - r.active.start for r in c.rows
                          if r.kind == ROW_MOVED}, {timedelta(minutes=20)})

    def test_the_closest_pair_is_taken_first(self):
        """Two airings could take one listing; the nearer one does, not the earlier."""
        a = [Listing('S', None, None, T0, T0 + timedelta(minutes=30)),
             Listing('S', None, None, T0 + timedelta(minutes=60), T0 + timedelta(minutes=90))]
        b = [Listing('S', None, None, T0 + timedelta(minutes=50), T0 + timedelta(minutes=80))]
        c = compare_listings(a, b)
        moved = next(r for r in c.rows if r.kind == ROW_MOVED)
        self.assertEqual(moved.active.start, T0 + timedelta(minutes=60))

    def test_a_tag_on_one_side_does_not_stop_a_move(self):
        a = _day(['Race ᴸᶦᵛᵉ', 'News', 'Movie'])
        b = _day(['Other', 'News', 'Movie'])
        b.append(Listing('Race', None, None, T0 + timedelta(minutes=90),
                         T0 + timedelta(minutes=150)))
        self.assertEqual(compare_listings(a, b).moved, 1)

    def test_more_than_six_hours_apart_is_another_airing(self):
        a = _day(['Special'] + SHOWS[1:])
        b = _day(['Other'] + SHOWS[1:]) + _day(['Special'], start=T0 + timedelta(hours=7))
        c = compare_listings(a, b)
        self.assertEqual(c.moved, 0)
        self.assertEqual(c.different, 1)

    def test_descriptions_on_one_side_only(self):
        c = compare_listings(_day(SHOWS, described=True), _day(SHOWS))
        self.assertEqual(c.verdict, CMP_DESCRIPTIONS)
        self.assertEqual((c.described_active, c.described_other, c.description_gaps),
                         (6, 0, 6))

    def test_a_few_unmatched_listings_are_mostly_the_same(self):
        other = _day(SHOWS)
        other[4] = other[4]._replace(title='Infomercial')
        c = compare_listings(_day(SHOWS), other)
        self.assertEqual(c.verdict, CMP_MOSTLY)
        self.assertEqual((c.same, c.different), (5, 1))

    def test_fewer_than_half_matching_is_unrelated(self):
        c = compare_listings(_day(SHOWS), _day(SHOWS[:2] + ['A', 'B', 'C', 'D']))
        self.assertEqual(c.verdict, CMP_UNRELATED)
        c = compare_listings(_day(SHOWS), _day(SHOWS[:3] + ['A', 'B', 'C']))
        self.assertEqual(c.verdict, CMP_MOSTLY, 'exactly half is not fewer than half')

    def test_only_the_stretch_both_list_is_judged(self):
        """A source listing 3 hours is not unrelated to one listing 6: the rest is shown and
        not counted."""
        c = compare_listings(_day(SHOWS), _day(SHOWS[:3]))
        self.assertEqual(c.verdict, CMP_IDENTICAL)
        self.assertEqual(c.until, T0 + timedelta(hours=3))
        self.assertEqual(c.compared, 3)
        outside = [r for r in c.rows if not r.counted]
        self.assertEqual([r.kind for r in outside], [ROW_ONLY_ACTIVE] * 3)

    def test_nothing_in_common_is_nothing_to_compare(self):
        self.assertEqual(compare_listings(_day(SHOWS), []).verdict, CMP_NO_OVERLAP)
        later = _day(SHOWS, start=T0 + timedelta(days=1))
        self.assertEqual(compare_listings(_day(SHOWS), later).verdict, CMP_NO_OVERLAP)

    def test_a_channel_listed_twice_is_shown_and_left_out_of_the_verdict(self):
        """The live shape: a file naming one channel under `ESPN.us` and `espn.us` imports
        both into it when ids match case-insensitively. Counted, 6 of 12 listings had no
        partner and two agreeing guides read as unrelated."""
        doubled = sorted(_day(SHOWS) + _day([t + ' Extra' for t in SHOWS]),
                         key=lambda x: x.start)
        c = compare_listings(doubled, _day(SHOWS))
        self.assertEqual(c.verdict, CMP_IDENTICAL)
        self.assertEqual((c.compared, c.doubled_active), (6, 6))
        self.assertEqual({r.kind for r in c.rows if not r.counted}, {ROW_DOUBLED_ACTIVE})

    def test_superscript_tags_and_a_leading_live_do_not_make_a_different_program(self):
        c = compare_listings(_day(['U.S. Senate ᴸᶦᵛᵉ', 'The Good Side ᴺᵉʷ', 'News']),
                             _day(['Live: U.S. Senate', 'The Good Side', 'News']))
        self.assertEqual(c.verdict, CMP_IDENTICAL)

    def test_a_title_with_the_episode_folded_in_is_the_same_program(self):
        c = compare_listings(_day(['Hot Bench', 'Flip Side', 'News']),
                             _day(['Hot Bench - Timesharing Is Caring', 'Flip Side', 'News']))
        self.assertEqual(c.verdict, CMP_IDENTICAL)

    def test_a_longer_title_is_never_a_move(self):
        """`BBC News` and `BBC News America` two hours apart are two programs."""
        a = _day(['BBC News', 'X', 'Y', 'Z'])
        b = _day(['Q', 'X', 'BBC News America', 'Z'])
        c = compare_listings(a, b)
        self.assertEqual(c.moved, 0)
        self.assertEqual(sum(1 for r in c.rows if r.kind == ROW_DIFFERENT), 2)

    def test_every_listing_lands_in_exactly_one_row(self):
        a = _day(SHOWS) + _day(['Tail'], start=T0 + timedelta(hours=6))
        b = _day(['Pre-race', 'Other', 'Post-race'], start=T0 + timedelta(minutes=2))
        c = compare_listings(a, b)
        self.assertEqual(sorted(r.active for r in c.rows if r.active), sorted(a))
        self.assertEqual(sorted(r.other for r in c.rows if r.other), sorted(b))
        self.assertIn(ROW_ONLY_ACTIVE, {r.kind for r in c.rows})
        self.assertEqual([r.start for r in c.rows], sorted(r.start for r in c.rows))

    def test_a_listing_only_the_other_side_has(self):
        b = _day(SHOWS[:3]) + _day(['Bonus'], start=T0 + timedelta(minutes=30))
        c = compare_listings(_day(SHOWS[:3]), b)
        self.assertEqual(c.only_other, 1)
        self.assertEqual(c.verdict, CMP_MOSTLY)
        self.assertIn(ROW_ONLY_OTHER, {r.kind for r in c.rows})


OTHER = ['News', 'Film', 'Something Else']


class ComparePageTests(_Fixture):
    """Alpha reads self.src first and self.ext second; `both` is listed by each."""

    def setUp(self):
        super().setUp()
        self.client = self.t.app.test_client()
        self.ext = self._second_source()
        self.both = self._channel('Both', 'both.test')
        self.solo = self._channel('Solo', 'solo.test')
        import_source(self.src, _xmltv(_schedule('both.test', REAL)
                                       + _schedule('solo.test', REAL)), epg_days=3, cfg=CFG)
        import_source(self.ext, _xmltv(_schedule('both.test', OTHER)), epg_days=3, cfg=CFG)

    def _page(self, ch, **args):
        r = self.client.get(f'/channels/{ch.id}/guide-compare', query_string=args)
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_the_loader_compares_the_active_guide_with_the_other_source(self):
        active, comps = channel_source_comparisons(self.both)
        self.assertEqual([x.title for x in active], REAL)
        self.assertEqual([c.source.id for c in comps], [self.ext.id])
        self.assertEqual(comps[0].comparison.verdict, CMP_MOSTLY)
        self.assertEqual(comps[0].comparison.same, 2)

    def test_the_page_shows_the_verdict_the_counts_and_the_rows(self):
        before = (EPGEntry.query.count(), EpgAlternateEntry.query.count())
        html = self._page(self.both)
        self.assertIn('Mostly the same', html)
        self.assertIn('2 of 3 programs match.', html)
        self.assertIn('Something Else', html)
        self.assertIn('Different program', html)
        self.assertIn(f'<th>{self.src.name}</th>', html)
        self.assertEqual((EPGEntry.query.count(), EpgAlternateEntry.query.count()), before,
                         'the page writes nothing')

    def test_differences_only_hides_the_matching_rows(self):
        html = self._page(self.both, diff='1')
        self.assertIn('Different program', html)
        self.assertNotIn('>Same<', html)
        self.assertIn('>Same<', self._page(self.both).replace('<td data-label="Difference">', '>'))

    def test_differences_only_leaves_out_a_channel_listed_twice(self):
        now = datetime.utcnow().replace(second=0, microsecond=0)
        first = EPGEntry.query.filter_by(channel_id=self.both.id).order_by(
            EPGEntry.start_time).first()
        db.session.add(EPGEntry(channel_id=self.both.id, source_id=self.src.id,
                                title='Doubled Copy', start_time=first.start_time,
                                stop_time=first.stop_time))
        db.session.commit()
        self.assertGreater(first.stop_time, now)
        self.assertIn('Listed twice in', self._page(self.both))
        self.assertNotIn('Listed twice in', self._page(self.both, diff='1'))

    def test_the_channel_page_links_to_it_only_when_there_is_something_to_compare(self):
        link = f'/channels/{self.both.id}/guide-compare'
        self.assertIn(link, self.client.get(f'/channels/{self.both.id}').get_data(as_text=True))
        solo = self.client.get(f'/channels/{self.solo.id}').get_data(as_text=True)
        self.assertNotIn(f'/channels/{self.solo.id}/guide-compare', solo)

    def test_a_channel_one_source_lists_says_so(self):
        self.assertIn(f'Only {self.src.name} has listings for this channel', self._page(self.solo))

    def test_a_channel_with_no_guide_says_so(self):
        bare = self._channel('Bare', 'nowhere.test')
        self.assertIn('has no guide right now', self._page(bare))

    def test_an_unknown_source_falls_back_to_the_first(self):
        self.assertIn('Something Else', self._page(self.both, source='9999'))

    def test_an_unknown_channel_is_404(self):
        self.assertEqual(self.client.get('/channels/999999/guide-compare').status_code, 404)

    def test_only_sources_the_account_reads_are_compared(self):
        stranger = EpgSource(kind='url', owner_account_id=self.acct.id, name='Unread',
                             url='http://unread.test/x.xml')
        db.session.add(stranger)
        db.session.flush()
        now = datetime.utcnow()
        db.session.add(EpgAlternateEntry(source_id=stranger.id, channel_id=self.both.id,
                                         title='Stray', start_time=now,
                                         stop_time=now + timedelta(hours=1)))
        db.session.commit()
        _active, comps = channel_source_comparisons(self.both)
        self.assertEqual([c.source.id for c in comps], [self.ext.id])


if __name__ == '__main__':
    unittest.main()
