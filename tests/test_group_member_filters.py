"""Tier 0 - the Format, FPS and Tag filter dimensions on the group member list.

`dev/changelog/767` made the member list's filter bar one "+ Filter" chip over the shared
`static/js/filter-bar.js`, whose whole promise is that the next dimension costs one
registry entry. `dev/changelog/768` is the first test of that promise: three dimensions
went in with no second control, no second predicate and no edit to `matches()`.

Two halves, because the work has two:

  * **The server half is the Tag payload.** A tag is a set of literal patterns, not a
    membership table (`app/database.py::Tag`/`TagPattern`), so "does this member carry
    it" is `app/accounts.py::tags_matching()` over the channel's own name - the one
    definition of the question. The trap is where the tags are LOADED: a lazy
    `tag.patterns` reached from inside the row comprehension is a query per member, the
    defect class with a mandatory regression test (`CLAUDE.md` §No hidden I/O in per-row
    loops). `tests/test_scaling_pages.py`'s two group cases are what hold the query count
    flat; these cases hold the meaning.

  * **The client half is the registry**, and it is asserted against the source because
    there is no jsdom harness for `group-detail.js` - it needs the whole rendered page
    plus a stubbed status poll, which `tests/support/filter_bar.mjs` deliberately avoids
    by driving the shared component with synthetic dimensions instead. So the two
    invariants a careless edit would undo are checked as source facts: the FPS rounding
    (without it 59.94 and 60 are two buckets and a group locked to "1920x1080 @ 60" has a
    filter value matching none of its own members), and the tag key being lowercased at
    build AND lookup rather than compared as typed with a fallback.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_group_member_filters
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_group  # noqa: E402
from app import db  # noqa: E402
from app.database import Account, Tag, TagPattern  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def _dims():
    """The FILTER_DIMS array as source text, the way the sibling conformance file reads it."""
    js = _read('static/js/group-detail.js')
    return js[js.index('const FILTER_DIMS = ['):js.index('const filterBar = createFilterBar(')]


class MemberTagPayloadTests(unittest.TestCase):
    """The Tag dimension's values and its predicate both read `row.tags`, so a row that
    carries the wrong set is a filter that hides the wrong members."""

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.client = self.app.test_client()
        with self.app.app_context():
            tag = Tag(name='4k', color='#58a6ff')
            db.session.add(tag)
            db.session.flush()
            # Two patterns on one tag, matching the live "4k" tag: the tag is carried when
            # EITHER appears, which is the OR the filter bar cannot express between values
            # and does not need to.
            db.session.add(TagPattern(tag_id=tag.id, pattern='ᵁᴴᴰ'))
            db.session.add(TagPattern(tag_id=tag.id, pattern='ᴴᴰᴿ'))
            other = Tag(name='Live', color='#3fb950')
            db.session.add(other)
            db.session.flush()
            db.session.add(TagPattern(tag_id=other.id, pattern='ᴸᶦᵛᵉ'))
            acct = make_account()
            self.plain = make_channel(acct, name='FS1')
            self.uhd = make_channel(acct, name='US| FS1 ᵁᴴᴰ (Backup 2)')
            self.hdr = make_channel(acct, name='US| FS1 ᴴᴰᴿ')
            grp = make_group('FS1', members=[self.plain, self.uhd, self.hdr])
            self.gid = grp.id
            self.acct_id = acct.id
            db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _rows(self):
        r = self.client.get(f'/api/channel-groups/{self.gid}/detail-rows')
        self.assertEqual(r.status_code, 200)
        return {row['channel_name']: row for row in r.get_json()['rows']}

    def test_every_row_carries_a_tag_list(self):
        """The predicate reads `r.tags || []`, but a row missing the key entirely would
        also make the whole dimension unavailable - it derives its values from the rows."""
        for row in self._rows().values():
            self.assertIn('tags', row)
            self.assertIsInstance(row['tags'], list)

    def test_a_member_whose_name_carries_a_pattern_carries_the_tag(self):
        rows = self._rows()
        for name in ('US| FS1 ᵁᴴᴰ (Backup 2)', 'US| FS1 ᴴᴰᴿ'):
            self.assertEqual([t['name'] for t in rows[name]['tags']], ['4k'],
                             f'{name} should carry the 4k tag')

    def test_either_pattern_is_enough_and_the_tag_appears_once(self):
        """A tag matches when ANY of its patterns is found, and it is one filter value
        however many patterns sit behind it - two hits must not become two tags."""
        with self.app.app_context():
            acct = db.session.get(Account, self.acct_id)
            both = make_channel(acct, name='FS1 ᵁᴴᴰ ᴴᴰᴿ')
            grp = make_group('Both', members=[both])
            gid = grp.id
            db.session.commit()
        r = self.client.get(f'/api/channel-groups/{gid}/detail-rows')
        self.assertEqual([t['name'] for t in r.get_json()['rows'][0]['tags']], ['4k'])

    def test_a_member_whose_name_carries_nothing_carries_no_tag(self):
        self.assertEqual(self._rows()['FS1']['tags'], [])

    def test_a_tag_that_lives_in_program_titles_offers_nothing_here(self):
        """This is the channel-NAME question, deliberately not channel_search.py's wider
        "airs something matching" one - so the guide's Live/New markers, which appear in
        program titles and never in a channel name, correctly tag no member."""
        for row in self._rows().values():
            self.assertNotIn('Live', [t['name'] for t in row['tags']])

    def test_the_tag_carries_its_color_for_a_row_that_wants_to_show_it(self):
        rows = self._rows()
        tag = rows['US| FS1 ᴴᴰᴿ']['tags'][0]
        self.assertEqual(set(tag), {'id', 'name', 'color'})
        self.assertEqual(tag['color'], '#58a6ff')

    def test_tags_are_loaded_once_for_the_page_not_once_per_member(self):
        """A lazy `tag.patterns` inside the row comprehension is a query per member. The
        pattern-count assertion is what makes this fail on the lazy version: eager loading
        is one SELECT for every tag's patterns regardless of how many members there are."""
        src = _read('app/routes/channel_groups.py')
        body = src[src.index('def group_detail_rows('):src.index('def _banner_facts(')]
        self.assertIn('selectinload(Tag.patterns)', body)
        loop = body[body.index('rows = ['):body.index('counts = _tally(')]
        self.assertNotIn('Tag.query', loop)
        self.assertIn('tags_matching(all_tags, ch.name)', loop)


class FilterRegistryTests(unittest.TestCase):
    """Three dimensions, one registry, no new control - the promise dev/changelog/767 was
    built to make good on."""

    def test_format_fps_and_tag_are_registry_entries(self):
        dims = _dims()
        for key in ("k: 'fmt'", "k: 'fps'", "k: 'tag'"):
            self.assertIn(key, dims)

    def test_no_dimension_is_filtered_outside_the_one_predicate(self):
        """The sibling conformance case guards `matches()`; this guards the three fields
        the new dimensions read, which is where a second consumer would appear."""
        js = _read('static/js/group-detail.js')
        matches = js[js.index('function matches(r)'):js.index('function visibleRows(')]
        for field in ('r.tags', 'resolution', 'fps'):
            self.assertNotIn(field, matches, f'{field} is filtered outside the registry')

    def test_the_bar_gained_no_new_control(self):
        """A dimension that also grows a toolbar control has rebuilt the four standing
        selects dev/changelog/767 deleted. The chip row ships holding the + Filter
        control and nothing else."""
        tpl = _read('templates/channels/group_detail.html')
        block = tpl[tpl.index('id="gd-filter-chips"'):
                    tpl.index('<span class="menu-wrap gd-phone-hide">')]
        self.assertNotIn('<select', block)
        self.assertEqual(block.count('data-menu'), 1)

    def test_the_filter_tooltip_names_what_the_list_can_now_be_narrowed_by(self):
        """UI text describing behavior is part of the change surface - a tooltip listing
        the four dimensions that existed before is a control that lies about itself."""
        tpl = _read('templates/channels/group_detail.html')
        tip = tpl[tpl.index('data-tip="Filter.'):tpl.index('&#43; Filter</button>')]
        for word in ('format', 'FPS', 'tag'):
            self.assertIn(word, tip)

    def test_the_format_bucket_rounds_fps_and_never_reads_the_raw_float(self):
        """`app/channel_groups.py::format_key` rounds to the nearest integer so 59.94 and
        60 are one format. A bucket built on the raw float splits them, and a group locked
        to "1920x1080 @ 60" then offers a filter value none of its members match."""
        js = _read('static/js/group-detail.js')
        fn = js[js.index('const rowFormatKey ='):js.index('const FMT_NONE =')]
        self.assertIn('Math.round(t.fps)', fn)
        self.assertNotIn('${t.fps}', fn)

    def test_the_format_label_is_spelled_the_way_the_server_spells_it(self):
        """`format_label()` renders "1920x1080 @ 60". A chip that spelled it any other way
        would not read as the same thing as the Format column beside it or as the group's
        own reference label in the banner."""
        js = _read('static/js/group-detail.js')
        fn = js[js.index('const rowFormatKey ='):js.index('const FMT_NONE =')]
        self.assertIn('${t.resolution} @ ${Math.round(t.fps)}', fn)

    def test_an_unmeasured_member_is_its_own_bucket(self):
        """Otherwise a member with no measured format is a row no value can select, on the
        one page whose banners are about which formats these members span."""
        dims = _dims()
        js = _read('static/js/group-detail.js')
        self.assertIn("const FMT_NONE = 'none'", js)
        self.assertIn("label: 'Not measured'", js)
        self.assertIn('(rowFormatKey(r) || FMT_NONE) === v', dims)

    def test_the_tag_key_is_lowercased_at_build_and_at_lookup(self):
        """Tag names are user text. An exact-match-then-fallback chain permanently shadows
        the case-variants (CLAUDE.md, keyed lookups), so both ends normalize once."""
        js = _read('static/js/group-detail.js')
        build = js[js.index('const tagValues ='):js.index('const FILTER_DIMS = [')]
        self.assertIn('t.name.toLowerCase()', build)
        dims = _dims()
        tag = dims[dims.index("k: 'tag'"):]
        self.assertIn('t.name.toLowerCase() === v', tag)

    def test_format_is_gated_on_having_more_than_one_bucket(self):
        """One bucket means every member already shares it, so the filter could only ever
        select all of them. Format can ask it this way - and FPS, beside it, cannot - because
        Format carries an explicit "Not measured" bucket that counts toward its own length
        (dev/changelog/770)."""
        self.assertIn('available: () => formatValues().length > 1', _dims())

    def test_fps_is_gated_on_partitioning_rather_than_on_a_second_value(self):
        """dev/docs/BUGS.md 2026-08-20 @ 07:27 - FPS is measured by a health check, so a
        member that was never tested carries no value. On a group whose measured members all
        run at 60, picking 60 still excludes every untested member, so a `length > 1` gate
        hides a dimension that would narrow the list."""
        dims = _dims()
        self.assertIn('available: () => partitions(fpsValues, r => rowFps(r) !== null)', dims)
        self.assertNotIn('available: () => fpsValues().length > 1', dims)

    def test_tag_is_not_gated_that_way_because_tags_do_not_partition_the_list(self):
        """One tag still splits the members into carriers and non-carriers, which is a
        real question - unlike a single format bucket."""
        self.assertIn('available: () => tagValues().length > 0', _dims())

    def test_the_dimensions_derive_their_values_from_every_row(self):
        """Values built from the VISIBLE rows would empty themselves as soon as one was
        chosen, and the bar would then prune the very filter that emptied them."""
        js = _read('static/js/group-detail.js')
        for name in ('const formatValues =', 'const fpsValues =', 'const tagValues ='):
            fn = js[js.index(name):js.index(name) + 600]
            self.assertIn('ROWS.forEach', fn)
            self.assertNotIn('visibleRows(', fn)


if __name__ == '__main__':
    unittest.main()
