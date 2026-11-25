"""Tier 2 - comparing what two EPG sources say about one channel (DESIGN-epg-sources.md §4,
§12; dev/changelog/1108).

  - CompareListingsTests: the pure comparator. The same program is a matching title starting
    within five minutes; a same-titled listing up to six hours away has moved; each verdict
    (identical / descriptions / mostly / shifted / unrelated / nothing to compare) from the
    counts; only the stretch both sources list is judged; a side listing the channel twice is
    shown and left out of the verdict; the title match drops providers' superscript tags and
    a leading `Live:`, and takes a title that is the other plus more words only when the two
    start together.
  - OtherNameTests: one program named two ways (dev/changelog/1725) - accents and
    punctuation folded, one side's episode title as the other's title, every word of a
    title of two words or more inside the other, a word or two spelled differently - each
    from prod's own pairs - and the pairs that must stay apart: a 60% word overlap, a
    one-word title inside a longer one, a word four letters apart, a different number, a
    kana voicing mark, symbol-only titles, and anything but equal titles across a move.
  - PlaceholderTests: a listing titled with its channel's own name is a placeholder, left out
    of the verdict, on either side; a title match still pairs one.
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
                             ROW_ONLY_ACTIVE, ROW_ONLY_OTHER, ROW_PLACEHOLDER_ACTIVE,
                             ROW_PLACEHOLDER_OTHER, ROW_SAME, Listing,
                             channel_source_comparisons, compare_listings, comparison_title,
                             placeholder_names)
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
        c = compare_listings(_day(SHOWS), _day(SHOWS), placeholders=frozenset())
        self.assertEqual(c.verdict, CMP_IDENTICAL)
        self.assertEqual((c.compared, c.same), (6, 6))
        self.assertEqual({r.kind for r in c.rows}, {ROW_SAME})

    def test_a_start_within_five_minutes_is_the_same_program(self):
        other = _day(SHOWS)
        other[3] = other[3]._replace(start=other[3].start + timedelta(minutes=5))
        self.assertEqual(compare_listings(_day(SHOWS), other, placeholders=frozenset()).verdict, CMP_IDENTICAL)

    def test_a_rain_delay_is_a_move_not_a_different_program(self):
        """The NASCAR question: one source moved the race 90 minutes, the other did not."""
        other = _day(SHOWS)
        other[1] = other[1]._replace(start=other[1].start + timedelta(minutes=90),
                                     stop=other[1].stop + timedelta(minutes=90))
        c = compare_listings(_day(SHOWS), other, placeholders=frozenset())
        self.assertEqual(c.verdict, CMP_SHIFTED)
        self.assertEqual((c.same, c.moved), (5, 1))
        self.assertEqual(c.largest_move, timedelta(minutes=90))
        moved = next(r for r in c.rows if r.kind == ROW_MOVED)
        self.assertEqual(moved.active.title, 'NASCAR Cup Series')

    def test_the_nearest_airing_of_a_repeating_title_is_the_one_paired(self):
        a = _day(['SportsCenter'] * 4)
        b = _day(['SportsCenter'] * 4, start=T0 + timedelta(minutes=20))
        c = compare_listings(a, b, placeholders=frozenset())
        self.assertEqual(c.moved, 4)
        self.assertEqual({r.other.start - r.active.start for r in c.rows
                          if r.kind == ROW_MOVED}, {timedelta(minutes=20)})

    def test_the_closest_pair_is_taken_first(self):
        """Two airings could take one listing; the nearer one does, not the earlier."""
        a = [Listing('S', None, None, T0, T0 + timedelta(minutes=30)),
             Listing('S', None, None, T0 + timedelta(minutes=60), T0 + timedelta(minutes=90))]
        b = [Listing('S', None, None, T0 + timedelta(minutes=50), T0 + timedelta(minutes=80))]
        c = compare_listings(a, b, placeholders=frozenset())
        moved = next(r for r in c.rows if r.kind == ROW_MOVED)
        self.assertEqual(moved.active.start, T0 + timedelta(minutes=60))

    def test_a_tag_on_one_side_does_not_stop_a_move(self):
        a = _day(['Race ᴸᶦᵛᵉ', 'News', 'Movie'])
        b = _day(['Other', 'News', 'Movie'])
        b.append(Listing('Race', None, None, T0 + timedelta(minutes=90),
                         T0 + timedelta(minutes=150)))
        self.assertEqual(compare_listings(a, b, placeholders=frozenset()).moved, 1)

    def test_more_than_six_hours_apart_is_another_airing(self):
        a = _day(['Special'] + SHOWS[1:])
        b = _day(['Other'] + SHOWS[1:]) + _day(['Special'], start=T0 + timedelta(hours=7))
        c = compare_listings(a, b, placeholders=frozenset())
        self.assertEqual(c.moved, 0)
        self.assertEqual(c.different, 1)

    def test_descriptions_on_one_side_only(self):
        c = compare_listings(_day(SHOWS, described=True), _day(SHOWS), placeholders=frozenset())
        self.assertEqual(c.verdict, CMP_DESCRIPTIONS)
        self.assertEqual((c.described_active, c.described_other, c.description_gaps),
                         (6, 0, 6))

    def test_a_few_unmatched_listings_are_mostly_the_same(self):
        other = _day(SHOWS)
        other[4] = other[4]._replace(title='Infomercial')
        c = compare_listings(_day(SHOWS), other, placeholders=frozenset())
        self.assertEqual(c.verdict, CMP_MOSTLY)
        self.assertEqual((c.same, c.different), (5, 1))

    def test_fewer_than_half_matching_is_unrelated(self):
        c = compare_listings(_day(SHOWS), _day(SHOWS[:2] + ['A', 'B', 'C', 'D']), placeholders=frozenset())
        self.assertEqual(c.verdict, CMP_UNRELATED)
        c = compare_listings(_day(SHOWS), _day(SHOWS[:3] + ['A', 'B', 'C']), placeholders=frozenset())
        self.assertEqual(c.verdict, CMP_MOSTLY, 'exactly half is not fewer than half')

    def test_only_the_stretch_both_list_is_judged(self):
        """A source listing 3 hours is not unrelated to one listing 6: the rest is shown and
        not counted."""
        c = compare_listings(_day(SHOWS), _day(SHOWS[:3]), placeholders=frozenset())
        self.assertEqual(c.verdict, CMP_IDENTICAL)
        self.assertEqual(c.until, T0 + timedelta(hours=3))
        self.assertEqual(c.compared, 3)
        outside = [r for r in c.rows if not r.counted]
        self.assertEqual([r.kind for r in outside], [ROW_ONLY_ACTIVE] * 3)

    def test_nothing_in_common_is_nothing_to_compare(self):
        self.assertEqual(compare_listings(_day(SHOWS), [], placeholders=frozenset()).verdict, CMP_NO_OVERLAP)
        later = _day(SHOWS, start=T0 + timedelta(days=1))
        self.assertEqual(compare_listings(_day(SHOWS), later, placeholders=frozenset()).verdict, CMP_NO_OVERLAP)

    def test_a_channel_listed_twice_is_shown_and_left_out_of_the_verdict(self):
        """The live shape: a file naming one channel under `ESPN.us` and `espn.us` imports
        both into it when ids match case-insensitively. Counted, 6 of 12 listings had no
        partner and two agreeing guides read as unrelated."""
        doubled = sorted(_day(SHOWS) + _day([t + ' Extra' for t in SHOWS]),
                         key=lambda x: x.start)
        c = compare_listings(doubled, _day(SHOWS), placeholders=frozenset())
        self.assertEqual(c.verdict, CMP_IDENTICAL)
        self.assertEqual((c.compared, c.doubled_active), (6, 6))
        self.assertEqual({r.kind for r in c.rows if not r.counted}, {ROW_DOUBLED_ACTIVE})

    def test_superscript_tags_and_a_leading_live_do_not_make_a_different_program(self):
        c = compare_listings(_day(['U.S. Senate ᴸᶦᵛᵉ', 'The Good Side ᴺᵉʷ', 'News']),
                             _day(['Live: U.S. Senate', 'The Good Side', 'News']), placeholders=frozenset())
        self.assertEqual(c.verdict, CMP_IDENTICAL)

    def test_a_title_with_the_episode_folded_in_is_the_same_program(self):
        c = compare_listings(_day(['Hot Bench', 'Flip Side', 'News']),
                             _day(['Hot Bench - Timesharing Is Caring', 'Flip Side', 'News']), placeholders=frozenset())
        self.assertEqual(c.verdict, CMP_IDENTICAL)

    def test_a_longer_title_is_never_a_move(self):
        """`BBC News` and `BBC News America` two hours apart are two programs."""
        a = _day(['BBC News', 'X', 'Y', 'Z'])
        b = _day(['Q', 'X', 'BBC News America', 'Z'])
        c = compare_listings(a, b, placeholders=frozenset())
        self.assertEqual(c.moved, 0)
        self.assertEqual(sum(1 for r in c.rows if r.kind == ROW_DIFFERENT), 2)

    def test_every_listing_lands_in_exactly_one_row(self):
        a = _day(SHOWS) + _day(['Tail'], start=T0 + timedelta(hours=6))
        b = _day(['Pre-race', 'Other', 'Post-race'], start=T0 + timedelta(minutes=2))
        c = compare_listings(a, b, placeholders=frozenset())
        self.assertEqual(sorted(r.active for r in c.rows if r.active), sorted(a))
        self.assertEqual(sorted(r.other for r in c.rows if r.other), sorted(b))
        self.assertIn(ROW_ONLY_ACTIVE, {r.kind for r in c.rows})
        self.assertEqual([r.start for r in c.rows], sorted(r.start for r in c.rows))

    def test_a_listing_only_the_other_side_has(self):
        b = _day(SHOWS[:3]) + _day(['Bonus'], start=T0 + timedelta(minutes=30))
        c = compare_listings(_day(SHOWS[:3]), b, placeholders=frozenset())
        self.assertEqual(c.only_other, 1)
        self.assertEqual(c.verdict, CMP_MOSTLY)
        self.assertIn(ROW_ONLY_OTHER, {r.kind for r in c.rows})


def _with_sub(listings, i, sub_title):
    """`listings` with the i-th one carrying an episode title."""
    out = list(listings)
    out[i] = out[i]._replace(sub_title=sub_title)
    return out


NONE = frozenset()


class OtherNameTests(unittest.TestCase):
    """Every pair named here is one prod's 2026-11-25 copy held (dev/changelog/1725)."""

    def _one(self, a, b, a_sub=None, b_sub=None):
        """The row two titles in one slot, between two agreeing listings, end up in."""
        x = _with_sub(_day(['Before', a, 'After']), 1, a_sub)
        y = _with_sub(_day(['Before', b, 'After']), 1, b_sub)
        c = compare_listings(x, y, placeholders=NONE)
        (row,) = [r for r in c.rows if r.start == T0 + timedelta(hours=1)]
        return c, row

    def test_accents_and_punctuation_are_folded_into_the_title_match(self):
        for a, b in (('Rozpal to ve Španělsku, šéfe!', 'Rozpal to ve Španělsku, šéfe'),
                     ('Segeln: Dubai SailGP ţ', 'Segeln: Dubai SailGP ț'),
                     ('Расследования Мердока', 'Расследования Мёрдока')):
            with self.subTest(a=a):
                c, row = self._one(a, b)
                self.assertEqual((row.kind, row.renamed), (ROW_SAME, False))
                self.assertEqual(c.verdict, CMP_IDENTICAL)

    def test_one_sides_episode_title_as_the_others_title(self):
        for kw in ({'b_sub': 'Gli eterni bonsai'}, {'a_sub': 'Gli eterni bonsai'}):
            with self.subTest(**kw):
                a, b = (('Gli eterni bonsai', 'Scugnizzi per sempre') if 'b_sub' in kw
                        else ('Scugnizzi per sempre', 'Gli eterni bonsai'))
                c, row = self._one(a, b, **kw)
                self.assertEqual((row.kind, row.renamed), (ROW_SAME, True))
                self.assertEqual(c.renamed, 1)

    def test_every_word_of_one_title_in_the_other(self):
        for a, b in (('Nové Město', 'Epreuve de Nove Mesto'),
                     ('PARTIZAN - DUBAI', 'Košarka – Aba Liga: Partizan – Dubai'),
                     ('The Christmas Heiress', 'The Heiress of Christmas')):
            with self.subTest(a=a):
                _c, row = self._one(a, b)
                self.assertEqual((row.kind, row.renamed), (ROW_SAME, True))

    def test_one_title_spelled_two_ways(self):
        for a, b in (('Родовые проклятья', 'Родовые проклятия'),
                     ('Pinkove Zvezdice', 'Pinkove zvjezdice')):
            with self.subTest(a=a):
                _c, row = self._one(a, b)
                self.assertEqual((row.kind, row.renamed), (ROW_SAME, True))

    def test_pairs_that_stay_different_programs(self):
        """A 60% word overlap would have joined the first (16 of 20,006 unrelated pairs);
        a one-word title never stands for a longer one; a word four letters apart, or a
        one-word title a letter apart, is another word; a number is never a misspelling; a
        kana voicing mark is part of the letter."""
        for a, b in (('FOX SPORTS News', 'FOX 10 News at 4PM'),
                     ('News', 'BBC World News'),
                     ('Football', 'Monday Night Football'),
                     ('Weltcup', 'Ski alpin: Weltcup Kitzbühel Abfahrt'),
                     ('The Bachelor', 'The Bachelorette'),
                     ('Friends', 'Fiends'),
                     ('Charmed', 'Charted'),
                     ('Formula 1', 'Formula 2'),
                     ('Episode 3', 'Episode 4'),
                     ('ガンダム', 'カンタム'),
                     ('⚽', '🏀'),
                     ('Family Guy', "Bob's Burgers")):
            with self.subTest(a=a):
                c, row = self._one(a, b)
                self.assertEqual(row.kind, ROW_DIFFERENT)
                self.assertEqual(c.renamed, 0)

    def test_a_one_word_episode_title_is_not_the_others_title(self):
        _c, row = self._one('News', 'Evening Bulletin', b_sub='News')
        self.assertEqual(row.kind, ROW_DIFFERENT)

    def test_a_title_with_more_words_is_named_differently(self):
        """The older pass (dev/changelog/1108) is another name too, and says so."""
        for a, b in (('Hot Bench', 'Hot Bench - Timesharing Is Caring'),
                     ('ITV Evening News', 'ITV Evening News; Weather')):
            with self.subTest(a=a):
                _c, row = self._one(a, b)
                self.assertEqual((row.kind, row.renamed), (ROW_SAME, True))

    def test_a_title_of_symbols_alone_keeps_its_symbols(self):
        self.assertEqual(comparison_title('⚽'), '⚽')
        self.assertEqual(comparison_title('Ποδόσφαιρο'), 'ποδοσφαιρο')

    def test_another_name_is_never_a_move(self):
        """Only equal titles pair across a move; `Nové Město` two hours from the longer
        title is two listings."""
        a = _day(['Nové Město', 'X', 'Y', 'Z'])
        b = _day(['Q', 'X', 'Epreuve de Nove Mesto', 'Z'])
        c = compare_listings(a, b, placeholders=NONE)
        self.assertEqual((c.moved, c.renamed), (0, 0))

    def test_a_move_matches_on_the_folded_title(self):
        a = _day(['Rozpal to ve Španělsku, šéfe!', 'X', 'Y', 'Z'])
        b = _day(['Q', 'X', 'Rozpal to ve Spanelsku sefe', 'Z'])
        self.assertEqual(compare_listings(a, b, placeholders=NONE).moved, 1)

    def test_a_guide_named_differently_throughout_agrees(self):
        """The shape that kept a guide from filling: every slot the same program, most of
        them named another way, used to read as unrelated."""
        a = _day(['PARTIZAN - DUBAI', 'Родовые проклятья', 'Nové Město', 'News'])
        b = _day(['Košarka – Aba Liga: Partizan – Dubai', 'Родовые проклятия',
                  'Epreuve de Nove Mesto', 'News'])
        c = compare_listings(a, b, placeholders=NONE)
        self.assertIn(c.verdict, (CMP_IDENTICAL, CMP_DESCRIPTIONS))
        self.assertEqual((c.same, c.renamed, c.different), (4, 3, 0))


class PlaceholderTests(unittest.TestCase):
    NAMES = placeholder_names(['UK: TALKSPORT ᴴᴰ'])

    def test_the_channels_name_on_the_other_side_is_no_program(self):
        c = compare_listings(_day(SHOWS[:3]), _day([SHOWS[0], 'talkSPORT', SHOWS[2]]),
                             placeholders=self.NAMES)
        kinds = [r.kind for r in c.rows if r.kind != ROW_SAME]
        self.assertEqual(sorted(kinds), [ROW_ONLY_ACTIVE, ROW_PLACEHOLDER_OTHER])
        self.assertEqual((c.different, c.only_active, c.placeholder_other), (0, 1, 1))
        self.assertEqual(c.compared, 3)
        self.assertFalse(next(r for r in c.rows if r.kind == ROW_PLACEHOLDER_OTHER).counted)

    def test_the_channels_name_on_the_active_side_is_no_program(self):
        c = compare_listings(_day([SHOWS[0], 'UK: TALKSPORT', SHOWS[2]]), _day(SHOWS[:3]),
                             placeholders=self.NAMES)
        self.assertEqual((c.different, c.only_other, c.placeholder_active), (0, 1, 1))
        self.assertEqual(c.compared, 2, 'a placeholder is not an active listing judged')
        self.assertIn(ROW_PLACEHOLDER_ACTIVE, {r.kind for r in c.rows})

    def test_without_the_channels_name_it_is_a_different_program(self):
        c = compare_listings(_day(SHOWS[:3]), _day([SHOWS[0], 'talkSPORT', SHOWS[2]]),
                             placeholders=NONE)
        self.assertEqual((c.different, c.placeholder_other), (1, 0))

    def test_the_active_sides_placeholders_are_not_held_against_it(self):
        """Judged on what it does list: one real program, and it matches. The other side's
        placeholders count as that source having nothing there, which the verdict already
        weighs as listings with no partner."""
        active = _day(['talkSPORT', 'talkSPORT', 'talkSPORT', SHOWS[3]])
        self.assertEqual(compare_listings(active, _day(SHOWS[:4]), placeholders=NONE).verdict,
                         CMP_UNRELATED)
        c = compare_listings(active, _day(SHOWS[:4]), placeholders=self.NAMES)
        self.assertEqual((c.verdict, c.compared, c.placeholder_active), (CMP_MOSTLY, 1, 3))

    def test_the_channels_name_on_both_sides_is_the_same_program(self):
        """A block named for the channel (`Sky News` on Sky News) pairs by title first."""
        c = compare_listings(_day(['News', 'talkSPORT']), _day(['News', 'talkSPORT']),
                             placeholders=self.NAMES)
        self.assertEqual((c.same, c.placeholder_active, c.placeholder_other), (2, 0, 0))

    def test_every_listing_lands_in_exactly_one_row(self):
        a = _day([SHOWS[0], 'UK: TALKSPORT', SHOWS[2], 'talkSPORT'])
        b = _day(['talkSPORT', SHOWS[1], 'Other', 'Late'])
        c = compare_listings(a, b, placeholders=self.NAMES)
        self.assertEqual(sorted(r.active for r in c.rows if r.active), sorted(a))
        self.assertEqual(sorted(r.other for r in c.rows if r.other), sorted(b))

    def test_short_names_and_none_are_not_placeholders(self):
        self.assertEqual(placeholder_names(['UK: TALKSPORT', None, 'TV', '']),
                         frozenset({'talksport'}))


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

    def test_a_program_named_differently_and_a_placeholder_say_so(self):
        """dev/changelog/1725: both are shown under Differences only, with their counts."""
        talk = self._channel('UK: TALKSPORT', 'talk.test')
        import_source(self.src, _xmltv(_schedule('both.test', REAL) + _schedule('solo.test', REAL)
                                       + _schedule('talk.test', ['Nové Město', 'Film', 'Sport',
                                                                 'News'])),
                      epg_days=3, cfg=CFG)
        import_source(self.ext, _xmltv(_schedule('both.test', OTHER)
                                       + _schedule('talk.test', ['Epreuve de Nove Mesto',
                                                                 'talkSPORT', 'Sport',
                                                                 'News'])),
                      epg_days=3, cfg=CFG)
        html = self._page(talk, diff='1')
        self.assertIn('Same, named differently', html)
        self.assertIn(f"Only the channel&#39;s name in {self.ext.name}", html)
        self.assertIn('1 program of those matches under a different name', html)
        self.assertIn(f"{self.ext.name} lists only the channel&#39;s name at 1 time", html)

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
