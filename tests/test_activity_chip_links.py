"""Tier 2 - every row the activity chips list names a page, and the chips themselves
do not navigate (dev/changelog/949).

Three separate claims, and the behavioral half of each is worth asserting because each
one was previously true in the other direction:

1. `_activity_status_dict()` emits an `href` on every background task and on every
   recording it names. A row without one renders a dead `#` link, which is worse than
   the plain text it replaced - the tooltip would look clickable and do nothing.
2. The href points at the thing the row is ABOUT, not at one shared list page. A
   conversion names its recording; a sync names its account.
3. The chips are `<button>`, not `<a>`. A tap is the only gesture a phone has, so a
   chip that navigated on tap could never be read there - the tooltip opened and the
   page left underneath it.

The source-shape cases are what stop 3 being undone: a behavioral assertion cannot see
the difference, since the repo has no jsdom to drive base.html's inline chip code.
"""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import search_index as SI  # noqa: E402
from app.database import SearchIndexState  # noqa: E402

ACTIVITY_URL = '/api/activity/status'
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def _past_window(**kw):
    now = datetime.utcnow()
    return seed.make_recording(start_time=now - timedelta(minutes=20),
                               stop_time=now + timedelta(minutes=40), **kw)


class TaskRowsCarryTheirOwnDestinationTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _body(self):
        return self.t.client.get(ACTIVITY_URL).get_json()

    def test_a_converting_recording_links_to_that_recording(self):
        rec = _past_window(status='CONVERTING', name='Race day')
        db.session.commit()
        tasks = self._body()['background']['tasks']
        self.assertEqual([t['href'] for t in tasks], [f'/recordings/{rec.id}'])

    def test_two_recordings_in_a_phase_link_to_two_different_pages(self):
        """The defect this guards is one generic /jobs link on every row: with two
        tasks up, a tooltip that sends both to the same page has not answered the
        question the user clicked to ask."""
        a = _past_window(status='ANALYZING', name='First')
        b = _past_window(status='ANALYZING', name='Second')
        db.session.commit()
        hrefs = {t['href'] for t in self._body()['background']['tasks']}
        self.assertEqual(hrefs, {f'/recordings/{a.id}', f'/recordings/{b.id}'})

    def test_a_syncing_account_links_to_that_account(self):
        acc = seed.make_account(name='Provider one')
        acc.status = 'SYNCING'
        db.session.commit()
        tasks = self._body()['background']['tasks']
        self.assertEqual([t['href'] for t in tasks], [f'/accounts/{acc.id}'])

    def test_a_rebuilding_index_links_to_the_maintenance_index_card(self):
        db.session.add(SearchIndexState(name=SI.SEARCH_INDEX_PROGRAMS,
                                        status=SI.STATUS_BUILDING))
        db.session.commit()
        task = next(t for t in self._body()['background']['tasks']
                    if t['label'] == 'Search index rebuild')
        self.assertEqual(task['href'], '/maintenance#m-index')

    def test_every_background_task_carries_an_href(self):
        """The tooltip renders every row as a link, so a row with no href is a dead
        one. Asserted over a mixed set rather than per kind, so a NEW kind added to
        _activity_status_dict() without an href fails here."""
        _past_window(status='CONCATENATING', name='Joining')
        _past_window(status='CONVERTING', name='Converting')
        seed.make_account(name='Provider one').status = 'SYNCING'
        db.session.add(SearchIndexState(name=SI.SEARCH_INDEX_CHANNELS,
                                        status=SI.STATUS_BUILDING))
        db.session.commit()
        tasks = self._body()['background']['tasks']
        self.assertEqual(len(tasks), 4)
        for t in tasks:
            self.assertTrue(t.get('href'), f'background task with no href: {t}')

    def test_an_active_recording_row_links_to_its_recording(self):
        rec = _past_window(status='IN_PROGRESS', name='Live one')
        db.session.commit()
        active = self._body()['recording']['active']
        self.assertEqual([r['href'] for r in active], [f'/recordings/{rec.id}'])

    def test_the_next_scheduled_recording_row_links_to_its_recording(self):
        now = datetime.utcnow()
        rec = seed.make_recording(status='SCHEDULED',
                                  start_time=now + timedelta(hours=2),
                                  stop_time=now + timedelta(hours=3))
        db.session.commit()
        payload = self._body()['recording']
        self.assertEqual(payload['state'], 'dim')
        self.assertEqual(payload['next_scheduled']['href'], f'/recordings/{rec.id}')


class ChipsDoNotNavigateTests(unittest.TestCase):
    """Source-shape guards. base.html's chip code is inline and the repo has no jsdom,
    so these read the markup and the script the way the sibling chip tests in
    tests/test_nav_shell.py do."""

    def setUp(self):
        self.base = _read('templates/base.html')
        self.macro = self.base.split('{% macro activity_chips() %}', 1)[1] \
                              .split('{% endmacro %}', 1)[0]

    def test_no_chip_is_a_link(self):
        self.assertNotIn('<a ', self.macro)
        self.assertNotIn('href', self.macro)
        self.assertEqual(self.macro.count('<button class="actchip'), 3)

    def test_every_chip_names_itself_for_a_screen_reader(self):
        """They were links carrying only a number; as buttons the number is all that
        is left unless each says what it counts."""
        self.assertEqual(len(re.findall(r'aria-label="[^"]+"', self.macro)), 3)

    def test_the_click_handler_is_not_gated_on_a_per_chip_flag(self):
        """`clickPins` used to be true for the stats chip alone, because the other
        two navigated on click. Nothing navigates now, so a flag that could turn
        pinning off for a chip would just make that chip unusable on a phone."""
        block = self.base.split('function bindTip(', 1)[1].split('chipRec.forEach', 1)[0]
        self.assertNotIn('clickPins', block)
        self.assertIn("el.addEventListener('click'", block)

    def test_tooltip_rows_are_links_built_by_one_helper(self):
        """Two builders write rows; a second row shape is how one of them ends up
        emitting plain text again."""
        rec = self.base.split('function buildRecTooltip(data) {', 1)[1] \
                       .split('function buildBgTooltip', 1)[0]
        bg = self.base.split('function buildBgTooltip(data) {', 1)[1] \
                      .split('function showTooltip', 1)[0]
        for name, body in (('buildRecTooltip', rec), ('buildBgTooltip', bg)):
            self.assertIn('tipItem(', body, f'{name} no longer builds rows as links')
            self.assertNotIn('<span class="tt-name">', body,
                             f'{name} writes a bare row again instead of a link')

    def test_a_hover_out_arms_a_close_rather_than_closing(self):
        """The tip sits 8px below the chip and its rows are links. Closing on the
        chip's own mouseleave shuts it while the pointer is crossing that band, so
        no row can be reached by hover at all."""
        block = self.base.split('function bindTip(', 1)[1].split('chipRec.forEach', 1)[0]
        leave = block.split("el.addEventListener('mouseleave'", 1)[1]
        self.assertIn('scheduleHide()', leave)
        self.assertNotIn('hideTooltip()', leave)

    def test_an_open_tooltip_is_only_rewritten_when_its_markup_changed(self):
        """applyIndicators runs on every poll. Rewriting innerHTML unconditionally
        replaces the row under the pointer mid-click, which a link cares about and
        plain text did not."""
        block = self.base.split('function applyIndicators(d) {', 1)[1] \
                         .split('\n    }', 1)[0]
        self.assertIn('_shownHtml', block)

    def test_the_row_and_footer_helpers_escape_what_they_interpolate(self):
        """Sibling of TooltipEscapingTests in test_activity_indicator_live_states.py:
        the names reaching these helpers are provider-supplied EPG program titles."""
        for fn in ('function tipItem(', 'function tipMore('):
            body = self.base.split(fn, 1)[1].split('\n    }', 1)[0]
            for raw in re.findall(r"\+ (href|name|label)\b", body):
                self.fail(f'{fn} interpolates {raw} into innerHTML unescaped')


class TooltipRowStylingTests(unittest.TestCase):
    """The visual half of claim 1. Rows used to be a flat run of spans separated by
    one 0.1rem gap identical to the gap inside a row, so two tasks read as one block
    of eight wrapped lines with no boundary anywhere."""

    def setUp(self):
        self.css = _read('static/css/style.css')

    def test_a_row_is_a_padded_block_with_its_own_edge(self):
        rule = self.css.split('\n.tt-item {', 1)[1].split('}', 1)[0]
        self.assertIn('display: block', rule)
        self.assertIn('padding:', rule)
        self.assertIn('border-left:', rule)

    def test_a_row_reacts_to_the_pointer(self):
        """It is a link; a row that looks identical under the cursor does not say so."""
        self.assertIn('.tt-item:hover', self.css)

    def test_the_name_and_detail_stack_inside_a_row(self):
        """They are inline spans by default, so inside one <a> they would run onto a
        single line - the exact complaint that started this change."""
        self.assertIn('.tt-item .tt-name, .tt-item .tt-detail { display: block; }', self.css)


if __name__ == '__main__':
    unittest.main(verbosity=2)
