"""Tier 0 - audio on the group member list: the column, the drawer, the filters, canExpand.

A health check records five audio facts on every member (`audio_codec`, `audio_channels`,
`audio_sample_rate`, `audio_bitrate_kbps`, `audio_language`) and `_build_test_dict`
serializes all five plus a formatted `audio_summary`. Before `dev/changelog/769` the group
member list showed exactly one of them, in one place: a single "Audio" line inside a row's
expanded Info drawer. There was no audio column, so audio could not be shown in the table,
sorted, or filtered.

The defect underneath the display gap is `canExpand()`, which required
`video_codec || screenshot_filename || error_detail`. A test that probed audio but no video
profile therefore had its audio measured, stored, serialized to the browser and sealed
behind a caret that never rendered. Fixing the predicate alone was not enough: `drawer()`
returns early when `!t.video_codec`, so the drawer it now opens would still have thrown the
audio away. Both halves are asserted here (`dev/docs/BUGS.md` 2026-08-20).

The client half is asserted against the source, for the reason
`tests/test_group_member_filters.py` states: there is no jsdom harness for
`group-detail.js` - it needs the whole rendered page plus a stubbed status poll, which
`tests/support/filter_bar.mjs` deliberately avoids by driving the shared filter component
with synthetic dimensions instead.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_group_member_audio
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def _dims():
    """The FILTER_DIMS array as source text, the way the sibling filter file reads it."""
    js = _read('static/js/group-detail.js')
    return js[js.index('const FILTER_DIMS = ['):js.index('const filterBar = createFilterBar(')]


def _fn(name, end):
    js = _read('static/js/group-detail.js')
    return js[js.index(name):js.index(end)]


class AudioColumnTests(unittest.TestCase):
    """Audio is a column of its own - not a sub-line under Format, which is where it lived
    before the unification commit dropped it (dev/changelog/769)."""

    def test_audio_is_a_column_on_every_facet(self):
        from app.routes.channel_groups import GROUP_DETAIL_COLUMNS
        for facet, cols in GROUP_DETAIL_COLUMNS.items():
            self.assertIn('audio', cols, f'{facet} cannot show audio at all')

    def test_audio_is_off_by_default(self):
        """A column on for everybody would push the video stats right on the many groups
        whose members all carry the same stereo AAC. Available, not shown until asked for -
        the same call FPS and Frames already carry."""
        from app.routes.channel_groups import GROUP_DETAIL_COLUMNS_OFF
        self.assertIn('audio', GROUP_DETAIL_COLUMNS_OFF)

    def test_the_column_is_labelled_and_sortable(self):
        js = _read('static/js/group-detail.js')
        self.assertIn("audio: 'Audio'", js)
        sortable = js[js.index('const COL_SORTABLE ='):js.index('const FIELD_ONLY =')]
        self.assertIn('audio: true', sortable)

    def test_the_column_renders_its_own_td_and_not_a_subtitle_under_format(self):
        """The Format cell keeps FPS as its subtitle and gains nothing else; audio has a
        cell of its own or it cannot be a column."""
        cell = _fn('  function cell(r, k)', '  // duplicated from app/fmt_utils.py')
        self.assertIn("case 'audio':", cell)
        res = cell[cell.index("case 'res':"):cell.index("case 'fps':")]
        self.assertNotIn('audio', res)

    def test_the_sort_key_orders_by_codec_then_channel_count(self):
        """Sorting on the raw summary string would interleave one codec's stereo and 5.1
        feeds with another codec's. Untested rows carry the empty-string sentinel, which a
        column's first click (descending) puts at the bottom the way -1 does for the
        numeric columns."""
        sv = _fn('  function sortValue(r, k)', '  // Every filter dimension reaches')
        audio = sv[sv.index("case 'audio':"):sv.index("case 'framePct':")]
        self.assertIn('audio_codec.toLowerCase()', audio)
        self.assertIn('audio_channels', audio)
        self.assertIn("return '';", audio)

    def test_the_phone_card_can_show_audio_too(self):
        """One toggle serves both widths (dev/changelog/758), so a field the desktop column
        picker offers must be a field the card can draw - otherwise turning it on does
        nothing on a phone."""
        js = _read('static/js/group-detail.js')
        cards = js[js.index('const CARD_FIELDS ='):js.index('const PICKABLE_FIELDS =')]
        self.assertIn("'audio'", cards)
        stat = _fn('  function statLine(r)', '  // The status band down a card')
        self.assertIn("k === 'audio'", stat)


class CanExpandTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-08-20 - an audio-only test's drawer could not be opened, and
    once opened would still have shown nothing."""

    def test_audio_alone_opens_the_drawer(self):
        fn = _fn('  function canExpand(r)', '  function showActionError(')
        self.assertIn('t.audio_codec', fn)

    def test_the_no_video_branch_renders_the_audio_it_has(self):
        """canExpand() alone only opens a drawer that returns before any audio is built.
        Both branches append the same audioItems()."""
        drawer = _fn('  function drawer(r)', '  function renderTable()')
        head = drawer[:drawer.index('let grid =')]
        self.assertIn('function audioItems()', head)
        self.assertIn('${why}</div>${audio}', head)

    def test_a_failed_test_keeps_its_own_error_text(self):
        """Three readings of "no video profile" and they are not interchangeable: a FAIL
        keeps its error whatever else was measured, and a test that measured audio did not
        predate profile capture, so saying it did would be false."""
        drawer = _fn('  function drawer(r)', '  function renderTable()')
        branch = drawer[drawer.index("if (!t || !t.video_codec)"):drawer.index('let grid =')]
        self.assertIn("rowStatus(r) === 'FAIL'", branch)
        self.assertIn('error_detail', branch)
        self.assertIn('predates stream-profile capture', branch)

    def test_the_drawer_shows_the_fields_the_summary_string_drops(self):
        """audio_summary is codec + channels + sample rate. Bitrate and language were
        serialized on every row and rendered nowhere."""
        drawer = _fn('  function drawer(r)', '  function renderTable()')
        self.assertIn('audio_bitrate_kbps', drawer)
        self.assertIn("item('Language'", drawer)


class AudioFilterTests(unittest.TestCase):
    """Three registry entries and nothing else - never a fifth control on the bar
    (dev/changelog/767, 768)."""

    def test_the_three_audio_dimensions_are_registry_entries(self):
        dims = _dims()
        for key in ("k: 'audio'", "k: 'alang'", "k: 'ach'"):
            self.assertIn(key, dims)

    def test_no_audio_filtering_happens_outside_the_one_predicate(self):
        matches = _fn('  function matches(r)', '  function visibleRows(')
        for field in ('audio_codec', 'audio_language', 'audio_channels', 'audio_summary'):
            self.assertNotIn(field, matches, f'{field} is filtered outside the registry')

    def test_the_bar_gained_no_new_control(self):
        """A dimension that also grows a toolbar control has rebuilt the standing selects
        dev/changelog/767 deleted."""
        tpl = _read('templates/channels/group_detail.html')
        block = tpl[tpl.index('id="gd-filter-chips"'):
                    tpl.index('<span class="menu-wrap gd-phone-hide">')]
        self.assertNotIn('<select', block)
        self.assertEqual(block.count('data-menu'), 1)

    def test_the_filter_tooltip_names_audio(self):
        """UI text describing behavior is part of the change surface."""
        tpl = _read('templates/channels/group_detail.html')
        tip = tpl[tpl.index('data-tip="Filter.'):tpl.index('&#43; Filter</button>')]
        self.assertIn('audio', tip)

    def test_the_dimensions_read_the_fields_and_never_the_summary_string(self):
        """audio_summary is three facts glued together for display, so a dimension built on
        it produces one bucket per unique combination and answers no real question."""
        dims = _dims()
        audio = dims[dims.index("k: 'audio'"):dims.index("k: 'account'")]
        self.assertNotIn('audio_summary', audio)
        for field in ('audio_codec', 'audio_language', 'audio_channels'):
            self.assertIn(field, audio)

    def test_the_codec_key_is_lowercased_at_build_and_at_lookup(self):
        """ffprobe's codec strings vary in case across providers. An
        exact-match-then-fallback chain permanently shadows the case-variants (CLAUDE.md,
        keyed lookups), so both ends normalize once - or the menu grows an "AAC" and an
        "aac" that each match half the rows."""
        build = _fn('  const audioCodecValues =', '  const audioLangValues =')
        self.assertIn('c.toLowerCase()', build)
        dims = _dims()
        audio = dims[dims.index("k: 'audio'"):dims.index("k: 'alang'")]
        self.assertIn('c.toLowerCase() === v', audio)

    def test_the_language_key_is_lowercased_at_build_and_at_lookup(self):
        build = _fn('  const audioLangValues =', '  // Mono/Stereo/5.1 rather than the raw')
        self.assertIn('l.toLowerCase()', build)
        dims = _dims()
        lang = dims[dims.index("k: 'alang'"):dims.index("k: 'ach'")]
        self.assertIn('l.toLowerCase() === v', lang)

    def test_channel_counts_are_offered_as_layout_names(self):
        """Nobody picks a feed by "6". An unnamed count still gets a row rather than being
        dropped - a value that exists must be reachable."""
        build = _fn('  const CH_LAYOUT =', '  const FILTER_DIMS = [')
        self.assertIn("2: 'Stereo'", build)
        self.assertIn("6: '5.1'", build)
        self.assertIn('`${n}ch`', build)

    def test_the_three_are_gated_on_partitioning_the_list(self):
        """dev/docs/BUGS.md 2026-08-20 @ 07:27 - a `length > 1` gate hid Audio codec and
        Audio channels on every real group, because a health check is what measures them: 17
        members measured AAC and 7 were never tested, so picking AAC narrows 24 rows to 17
        while the dimension offering it was withheld as degenerate."""
        dims = _dims()
        for values, field in (('audioCodecValues', 'audio_codec'),
                              ('audioLangValues', 'audio_language'),
                              ('audioChValues', 'audio_channels')):
            self.assertIn(
                f'available: () => partitions({values}, '
                f'r => !!(r.last_test && r.last_test.{field}))', dims)
            self.assertNotIn(f'available: () => {values}().length > 1', dims)

    def test_partitioning_counts_a_member_that_carries_no_value_at_all(self):
        """The whole point of the helper: one value plus a member that has none is a real
        question, and two values is a real question. One value and nothing missing is the
        only degenerate case."""
        fn = _fn('  const partitions =', '  const FILTER_DIMS = [')
        self.assertIn('n > 1', fn)
        self.assertIn('ROWS.some(r => !has(r))', fn)

    def test_account_is_not_routed_through_it(self):
        """Every row carries an account whether or not it was ever tested, so nothing is
        missing and `> 1` already says the same thing."""
        dims = _dims()
        account = dims[dims.index("k: 'account'"):dims.index("k: 'tag'")]
        self.assertIn('available: () => accountValues().length > 1', account)
        self.assertNotIn('partitions(', account)

    def test_the_dimensions_derive_their_values_from_every_row(self):
        """Values built from the VISIBLE rows would empty themselves as soon as one was
        chosen, and the bar would then prune the very filter that emptied them."""
        js = _read('static/js/group-detail.js')
        for name in ('const audioCodecValues =', 'const audioLangValues =', 'const audioChValues ='):
            fn = js[js.index(name):js.index(name) + 400]
            self.assertIn('ROWS.forEach', fn)
            self.assertNotIn('visibleRows(', fn)

    def test_an_untested_member_matches_no_audio_value(self):
        """Correct and needing no "Not measured" bucket the way Format has one: this is a
        user-driven filter, not the format lock, so "an untested member is never filtered
        out" does not reach it. Each predicate therefore requires the field to be present
        before it compares - a `|| ''`-style fallback would make every untested member match
        whichever value the fallback happened to spell."""
        dims = _dims()
        audio = dims[dims.index("k: 'audio'"):dims.index("k: 'account'")]
        self.assertIn('return !!c && c.toLowerCase() === v;', audio)
        self.assertIn('return !!l && l.toLowerCase() === v;', audio)
        self.assertIn('return !!n && String(n) === v;', audio)
        self.assertNotIn("|| ''", audio)


class AudioPayloadTests(unittest.TestCase):
    """The five fields the filters read have to arrive on the row, or every dimension is
    empty on a page that shows audio in its column."""

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)

    def test_every_audio_field_is_serialized(self):
        from app.routes.channel_tests import _build_test_dict

        class _T:
            id = 1
            status = 'PASS'
            test_started_at = None
            resolution = '1920x1080'
            fps = 60.0
            frame_count = 100
            frame_pct = 99.0
            bitrate_kbps = 5000.0
            drop_count = 0
            duration_seconds = 10.0
            connect_attempts = 1
            screenshot_path = None
            screenshot_pruned = False
            error_detail = None
            audio_codec = 'aac'
            audio_channels = 2
            audio_sample_rate = 48000
            audio_bitrate_kbps = 128.0
            audio_language = 'eng'
            video_codec = None
            pix_fmt = None
            bit_depth = None
            chroma_subsampling = None
            interlaced = None
            coded_resolution = None
            is_vfr = None
            bits_per_pixel_frame = None
            timeline_gap_count = 0
            timeline_gap_seconds = 0

        with self.t.app.app_context():
            d = _build_test_dict(_T())
        for key in ('audio_codec', 'audio_channels', 'audio_sample_rate',
                    'audio_bitrate_kbps', 'audio_language', 'audio_summary'):
            self.assertIn(key, d)
        self.assertEqual(d['audio_summary'], 'AAC 2ch 48kHz')


if __name__ == '__main__':
    unittest.main()
