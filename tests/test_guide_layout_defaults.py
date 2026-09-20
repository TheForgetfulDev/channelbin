"""Tier 2 - the TV Guide Layout popover's defaults, its stored pref, and the JS mirror
(DESIGN.md 12.3, shipped in dev/changelog/344).

One fact - "is this Layout field on?" - is declared in three places that must agree:
app/routes/guide.py::GUIDE_LAYOUT_FIELD_DEFAULTS (the authority), the rendered `checked`
attribute of each popover checkbox, and static/js/guide.js's LAYOUT_FALLBACK literal. Two
independent declarations of one fact always eventually drift, and here the drift is silent:
a checkbox rendered unchecked while the JS state says on renders a guide that does not match
its own controls, with nothing raising.

Also guards read_guide_layout()'s two merge rules (a stored pref wins per key; an unknown
key from a stale pref never reaches the template), and the channel detail page's "What's On"
card, which embeds the same grid and reads the same machinery under its own pref keys - its
subset of the popover, its independence from the guide's stored Layout, and the
collapse-gaps default it still takes from display.guide_collapse_gaps (BUGS.md 2026-07-26
11:04 PM; the card grew a real Layout surface in dev/changelog/349).
"""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Channel, UserPref  # noqa: E402
from app.routes.guide import (GUIDE_LAYOUT_FIELD_DEFAULTS, GUIDE_LAYOUT_PREF_KEY,  # noqa: E402
                              GUIDE_LAYOUT_EXTRA_DEFAULTS, GUIDE_LAYOUT_MOBILE_OVERRIDES,
                              GUIDE_LAYOUT_MOBILE_PREF_KEY, GUIDE_CELL_FIELDS,
                              GUIDE_SORT_KEYS, read_guide_layout)
from app.routes.channels import (WHATSON_LAYOUT_PREF_KEY,  # noqa: E402
                                 WHATSON_LAYOUT_MOBILE_PREF_KEY)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE_JS = os.path.join(REPO, 'static', 'js', 'guide.js')

# `<input type="checkbox" data-layout="rec_status" checked>` - the attribute order the
# template renders. `checked` is Jinja-conditional, so its presence is the assertion.
_CHECKBOX_RE = re.compile(
    r'<input type="checkbox" data-layout="(?P<field>\w+)"(?P<attrs>[^>]*)>')


def _rendered_layout_checkboxes(html):
    """{field: bool} for every Layout checkbox in the rendered page."""
    return {m.group('field'): 'checked' in m.group('attrs')
            for m in _CHECKBOX_RE.finditer(html)}


def _js_layout_fallback():
    """{field: bool} parsed out of guide.js's LAYOUT_FALLBACK literal.

    Parsed rather than evaluated in node: guide.js reads GUIDE_CONFIG at module top level,
    so the file cannot be loaded outside a page. The literal is plain `key: true|false`
    pairs precisely so this stays a regex and not a JS engine.
    """
    with open(GUIDE_JS, encoding='utf-8') as fh:
        src = fh.read()
    body = re.search(r'const LAYOUT_FALLBACK = \{(.*?)\};', src, re.S)
    if body is None:
        raise AssertionError('LAYOUT_FALLBACK literal not found in static/js/guide.js - it '
                             'is the JS mirror of GUIDE_LAYOUT_FIELD_DEFAULTS and this test '
                             'is what keeps the two in step.')
    return {k: v == 'true'
            for k, v in re.findall(r'(\w+):\s*(true|false)', body.group(1))}


class GuideLayoutDefaultsTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        acc = seed.make_account()
        ch = seed.make_channel(acc, name='Layout Test Channel', in_guide=True)
        seed.make_epg_entry(ch, title='Layout Test Program')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _guide_html(self):
        resp = self.t.client.get('/guide')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_rendered_checkboxes_match_the_server_defaults(self):
        """Every popover checkbox renders `checked` iff GUIDE_LAYOUT_FIELD_DEFAULTS says so."""
        rendered = _rendered_layout_checkboxes(self._guide_html())
        for field, default in GUIDE_LAYOUT_FIELD_DEFAULTS.items():
            self.assertIn(field, rendered,
                          f'Layout field {field!r} has a default but no rendered checkbox')
            self.assertEqual(
                default, rendered[field],
                f'Layout checkbox {field!r} renders checked={rendered[field]} but '
                f'GUIDE_LAYOUT_FIELD_DEFAULTS says {default} - the popover and the guide '
                f'it controls now disagree.')

    def test_every_rendered_checkbox_has_a_declared_default(self):
        """No checkbox may exist that the defaults dict does not know about - it would
        persist a key read_guide_layout() drops on the next load, so the control would
        silently forget its own state. collapse_gaps is the one field seeded from config
        instead of the dict (see _guide_layout_defaults)."""
        rendered = _rendered_layout_checkboxes(self._guide_html())
        known = set(GUIDE_LAYOUT_FIELD_DEFAULTS) | {'collapse_gaps'}
        self.assertEqual(set(), set(rendered) - known)

    def test_js_fallback_matches_the_server_defaults(self):
        """guide.js's LAYOUT_FALLBACK mirrors the Python authority, key for key."""
        js = _js_layout_fallback()
        for field, default in GUIDE_LAYOUT_FIELD_DEFAULTS.items():
            self.assertIn(field, js,
                          f'{field!r} is a server Layout default with no LAYOUT_FALLBACK '
                          f'entry - pages that embed the grid without the popover '
                          f'(channel detail) would render it as undefined/off.')
            self.assertEqual(default, js[field],
                             f'LAYOUT_FALLBACK.{field} is {js[field]} but the server '
                             f'default is {default}.')

    def test_stored_pref_round_trips_into_the_markup(self):
        """A saved Layout pref is what the page renders, per field."""
        flipped = {k: (not v) for k, v in GUIDE_LAYOUT_FIELD_DEFAULTS.items()}
        db.session.add(UserPref(key=GUIDE_LAYOUT_PREF_KEY, value=json.dumps(flipped)))
        db.session.commit()

        rendered = _rendered_layout_checkboxes(self._guide_html())
        for field, want in flipped.items():
            self.assertEqual(want, rendered[field],
                             f'stored pref {field}={want} did not reach the markup')

    def test_stale_pref_keys_are_ignored_and_missing_keys_keep_their_default(self):
        """A pref written by an older build must not add unknown fields or blank out
        fields it predates."""
        db.session.add(UserPref(
            key=GUIDE_LAYOUT_PREF_KEY,
            value=json.dumps({'subtitle': False, 'a_retired_field': True})))
        db.session.commit()

        layout = read_guide_layout({}, GUIDE_LAYOUT_PREF_KEY)
        self.assertNotIn('a_retired_field', layout)
        self.assertFalse(layout['subtitle'])
        self.assertEqual(GUIDE_LAYOUT_FIELD_DEFAULTS['description'], layout['description'])

    def test_corrupt_pref_value_falls_back_to_defaults(self):
        """Unparseable JSON must render the defaults, not 500 the page."""
        db.session.add(UserPref(key=GUIDE_LAYOUT_PREF_KEY, value='{not json'))
        db.session.commit()

        rendered = _rendered_layout_checkboxes(self._guide_html())
        for field, default in GUIDE_LAYOUT_FIELD_DEFAULTS.items():
            self.assertEqual(default, rendered[field])

    def test_collapse_gaps_default_comes_from_config(self):
        """collapse_gaps is the one field whose default is the user's configured
        display.guide_collapse_gaps - it predates Layout and is still channel detail's
        live setting."""
        self.assertFalse(read_guide_layout(
            {'display': {'guide_collapse_gaps': False}}, GUIDE_LAYOUT_PREF_KEY)['collapse_gaps'])
        self.assertTrue(read_guide_layout(
            {'display': {'guide_collapse_gaps': True}}, GUIDE_LAYOUT_PREF_KEY)['collapse_gaps'])

    # ── Mobile Layout (DESIGN.md 13.6, shipped in dev/changelog/345) ──────────

    def test_mobile_and_desktop_layouts_are_stored_under_separate_keys(self):
        """12.3 as amended: a phone's field set must not reshape the desktop grid. The two
        reads must be independent, or the whole point of the second key is lost."""
        db.session.add(UserPref(key=GUIDE_LAYOUT_PREF_KEY,
                                value=json.dumps({'subtitle': False})))
        db.session.add(UserPref(key=GUIDE_LAYOUT_MOBILE_PREF_KEY,
                                value=json.dumps({'tag_dots': False})))
        db.session.commit()

        desktop = read_guide_layout({}, GUIDE_LAYOUT_PREF_KEY)
        mobile = read_guide_layout({}, GUIDE_LAYOUT_MOBILE_PREF_KEY, mobile=True)
        self.assertFalse(desktop['subtitle'])
        self.assertTrue(desktop['tag_dots'], 'mobile pref leaked into the desktop read')
        self.assertFalse(mobile['tag_dots'])
        self.assertTrue(mobile['subtitle'], 'desktop pref leaked into the mobile read')

    def test_both_layouts_reach_the_page(self):
        """guide.js picks the breakpoint's dict at load, so both must be rendered - the
        server cannot see a media query."""
        html = self._guide_html()
        self.assertIn('layoutMobile:', html)
        self.assertIn(GUIDE_LAYOUT_MOBILE_PREF_KEY, html)
        self.assertIn(GUIDE_LAYOUT_PREF_KEY, html)

    def test_mobile_never_renders_a_description_in_a_cell(self):
        """13.9 is a hard rule, not a default: a description lives in the record modal the
        cell opens at phone widths and there is no checkbox to turn it on. A pref that says
        otherwise -
        one written before the rule, or hand-edited - must still not produce one."""
        db.session.add(UserPref(key=GUIDE_LAYOUT_MOBILE_PREF_KEY,
                                value=json.dumps({'description': True})))
        db.session.commit()
        self.assertFalse(
            read_guide_layout({}, GUIDE_LAYOUT_MOBILE_PREF_KEY, mobile=True)['description'])

    def test_mobile_default_preset_is_detailed(self):
        """13.9's default preset turns the time line on, which desktop leaves off."""
        mobile = read_guide_layout({}, GUIDE_LAYOUT_MOBILE_PREF_KEY, mobile=True)
        for field, want in GUIDE_LAYOUT_MOBILE_OVERRIDES.items():
            self.assertEqual(want, mobile[field])
        self.assertFalse(read_guide_layout({}, GUIDE_LAYOUT_PREF_KEY)['start_time'],
                         'the mobile override leaked into the desktop defaults')

    def test_non_boolean_layout_values_survive_the_merge(self):
        """Every Layout value used to be coerced with bool(), which was right while they
        were all checkboxes. field_order is a list and sort_key a string, and a blind bool()
        turns both into True - silently, since nothing raises on a truthy value."""
        order = ['tag_dots', 'title', 'subtitle', 'time', 'rec_status']
        db.session.add(UserPref(
            key=GUIDE_LAYOUT_MOBILE_PREF_KEY,
            value=json.dumps({'field_order': order, 'sort_key': 'health',
                              'sort_reversed': True})))
        db.session.commit()

        layout = read_guide_layout({}, GUIDE_LAYOUT_MOBILE_PREF_KEY, mobile=True)
        self.assertEqual(order, layout['field_order'])
        self.assertEqual('health', layout['sort_key'])
        self.assertTrue(layout['sort_reversed'])

    def test_stored_field_order_is_repaired_not_trusted(self):
        """A retired field name must be dropped and a newly-added one appended, or a stale
        stored order hides a field from the sheet with nothing raising."""
        db.session.add(UserPref(
            key=GUIDE_LAYOUT_MOBILE_PREF_KEY,
            value=json.dumps({'field_order': ['tag_dots', 'a_retired_field', 'tag_dots']})))
        db.session.commit()

        order = read_guide_layout({}, GUIDE_LAYOUT_MOBILE_PREF_KEY, mobile=True)['field_order']
        self.assertNotIn('a_retired_field', order)
        self.assertEqual('tag_dots', order[0], 'the stored order was not honoured')
        self.assertEqual(sorted(GUIDE_CELL_FIELDS), sorted(order),
                         'every cell field must appear exactly once')

    def test_bad_field_order_and_sort_key_fall_back_to_defaults(self):
        """A non-list order or a sort key that no longer exists is rejected outright - a
        pref naming a retired key would otherwise sort by nothing."""
        db.session.add(UserPref(
            key=GUIDE_LAYOUT_MOBILE_PREF_KEY,
            value=json.dumps({'field_order': 'not-a-list', 'sort_key': 'bitrate'})))
        db.session.commit()

        layout = read_guide_layout({}, GUIDE_LAYOUT_MOBILE_PREF_KEY, mobile=True)
        self.assertEqual(GUIDE_CELL_FIELDS, layout['field_order'])
        self.assertEqual(GUIDE_LAYOUT_EXTRA_DEFAULTS['sort_key'], layout['sort_key'])
        self.assertIn(layout['sort_key'], GUIDE_SORT_KEYS)

    def test_js_mirrors_the_cell_fields_and_extra_defaults(self):
        """The mobile field list and the extra (non-checkbox) defaults are declared in both
        Python and guide.js. Same drift, same silence as the checkbox mirror above: a JS
        list missing a field renders a cell the server's stored order still names."""
        with open(GUIDE_JS, encoding='utf-8') as fh:
            src = fh.read()
        js_fields = re.search(r'const CELL_FIELDS = \[(.*?)\];', src)
        self.assertIsNotNone(js_fields, 'CELL_FIELDS literal not found in static/js/guide.js')
        self.assertEqual(GUIDE_CELL_FIELDS, re.findall(r"'(\w+)'", js_fields.group(1)))

        fallback = re.search(r'const LAYOUT_FALLBACK = \{(.*?)\};', src, re.S).group(1)
        for key in GUIDE_LAYOUT_EXTRA_DEFAULTS:
            self.assertIn(f'{key}:', fallback,
                          f'{key!r} is a server Layout default with no LAYOUT_FALLBACK entry')

    def test_channel_column_has_exactly_one_click_listener(self):
        """The channel column must have ONE click handler branching on the breakpoint, not
        one per behaviour. Two listeners on the same element both fire on the same click, in
        registration order - the mobile channel sheet opened and the desktop navigation then
        immediately replaced the page, so the sheet was unreachable with nothing raising
        (BUGS.md 2026-07-26 07:14 PM). Gating the second one would leave the same trap set
        for the next behaviour added here, so the invariant is that there is only one.
        """
        with open(GUIDE_JS, encoding='utf-8') as fh:
            src = fh.read()
        # A binding looks up the element and attaches within a few lines of doing so; the
        # window is what keeps this from matching every later click listener in the file.
        bindings = [m for m in re.finditer(r"getElementById\('guide-channel-col'\)", src)
                    if "addEventListener('click'" in src[m.end():m.end() + 400]]
        self.assertEqual(1, len(bindings),
                         f'{len(bindings)} click listeners are bound to #guide-channel-col; '
                         f'they all fire on one click and the last navigation wins.')

    def test_js_breakpoint_matches_the_stylesheet(self):
        """guide.js decides what the grid is BUILT from (time scale, which stored Layout,
        the field order) and guide.css decides what it LOOKS like. The two reading different
        breakpoints gives a mobile-styled grid drawn at the desktop scale, with nothing
        raising - so the query is asserted equal rather than left to a comment."""
        with open(GUIDE_JS, encoding='utf-8') as fh:
            js = fh.read()
        with open(os.path.join(REPO, 'static', 'css', 'guide.css'), encoding='utf-8') as fh:
            css = fh.read()
        self.assertIn("matchMedia('(max-width: 768px)')", js)
        self.assertIn('@media (max-width: 768px)', css)

    # ── The channel detail page's "What's On" card (dev/changelog/349) ────────
    # It embeds the same grid, so it reads the same Layout machinery under its own keys.
    # Before that it had no Layout surface at all and took collapse_gaps through a separate
    # GUIDE_CONFIG.collapseGapsDefault channel; losing that read left its behaviour stuck on
    # guide.js's fallback literal regardless of the configured value (BUGS.md 2026-07-26
    # 11:04 PM). The defect is the same one either way - the configured value not reaching
    # the card - so these assert it through the path that exists now.

    def _detail_html(self):
        ch = Channel.query.filter_by(name='Layout Test Channel').one()
        resp = self.t.client.get(f'/channels/{ch.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_whatson_collapse_gaps_default_comes_from_config(self):
        """The card's collapse_gaps still starts from display.guide_collapse_gaps."""
        for configured in (True, False):
            self.assertEqual(configured, read_guide_layout(
                {'display': {'guide_collapse_gaps': configured}},
                WHATSON_LAYOUT_PREF_KEY)['collapse_gaps'])

    def test_whatson_layout_is_stored_under_its_own_keys(self):
        """A control inside one channel's page must not reshape the whole TV Guide, and the
        guide's stored Layout must not silently reshape the card - so the two pages' keys
        are independent in both directions."""
        db.session.add(UserPref(key=GUIDE_LAYOUT_PREF_KEY,
                                value=json.dumps({'subtitle': False})))
        db.session.add(UserPref(key=WHATSON_LAYOUT_PREF_KEY,
                                value=json.dumps({'description': False})))
        db.session.commit()

        guide = read_guide_layout({}, GUIDE_LAYOUT_PREF_KEY)
        card = read_guide_layout({}, WHATSON_LAYOUT_PREF_KEY)
        self.assertFalse(guide['subtitle'])
        self.assertTrue(card['subtitle'], "the guide's pref leaked into the What's On card")
        self.assertFalse(card['description'])
        self.assertTrue(guide['description'], "the card's pref leaked into the guide")

    def test_whatson_renders_both_breakpoints_layout_and_opts_in_to_mobile(self):
        """guide.js picks the breakpoint's dict at load, and `layoutMobile` is also the flag
        isMobileGuide() gates on - the card opted OUT of every mobile-guide behaviour until
        its revamp approved a mobile design for it, so its presence is the opt-in."""
        html = self._detail_html()
        self.assertIn('layoutMobile:', html)
        self.assertIn(WHATSON_LAYOUT_PREF_KEY, html)
        self.assertIn(WHATSON_LAYOUT_MOBILE_PREF_KEY, html)
        self.assertIn('singleChannel: true', html)

    def test_whatson_popover_renders_only_settings_it_can_act_on(self):
        """The card draws no channel column and has no channel list, so the channel-column
        fields and "Include failed channels" must not be offered - a rendered control that
        changes nothing is worse than an absent one."""
        rendered = _rendered_layout_checkboxes(self._detail_html())
        self.assertIn('collapse_gaps', rendered)
        self.assertIn('subtitle', rendered)
        for field in ('show_failed', 'ch_resolution', 'ch_fps', 'ch_bitrate', 'ch_audio'):
            self.assertNotIn(field, rendered,
                             f'{field!r} is offered on the What\'s On card but has nothing '
                             f'to act on there')

    def test_single_channel_embed_is_never_hidden_by_the_health_filter(self):
        """"Include failed channels" is off by default and the card offers no way to turn it
        on, so on a single-channel embed it must not act at all - otherwise the detail page
        of every channel whose last check FAILED renders with its own What's On row blanked,
        which is the page you most need it on.

        Two independent mechanisms hide a row - the `hide-failed-channels` CSS class that
        applyShowFailed() puts on #guide-wrap, and the isChannelHealthHidden() predicate the
        renderer and the counts go through - and neither consults the other, so BOTH carry
        the guard. Asserted against the source because the outcome is a rendered CSS rule,
        which only a browser could observe; this at least cannot go one-sided silently.
        (The card gained #guide-wrap when it adopted the shipped grid markup in
        dev/changelog/349 - before that the class landed on nothing.)"""
        with open(GUIDE_JS, encoding='utf-8') as fh:
            src = fh.read()
        for fn in ('applyShowFailed', 'isChannelHealthHidden'):
            body = re.search(r'function %s\(\w*\) \{(.*?)\n\}' % fn, src, re.S)
            self.assertIsNotNone(body, f'{fn}() not found in guide.js')
            self.assertIn('SINGLE_CHANNEL', body.group(1),
                          f'{fn}() no longer exempts a single-channel embed from the '
                          f'"Include failed channels" filter - a failed channel\'s own '
                          f'detail page will render an empty What\'s On row.')

    def test_whatson_stored_pref_round_trips_into_the_popover(self):
        """A saved card Layout is what the popover renders, per field."""
        db.session.add(UserPref(key=WHATSON_LAYOUT_PREF_KEY,
                                value=json.dumps({'description': False, 'duration': True})))
        db.session.commit()

        rendered = _rendered_layout_checkboxes(self._detail_html())
        self.assertFalse(rendered['description'])
        self.assertTrue(rendered['duration'])


if __name__ == '__main__':
    unittest.main()
