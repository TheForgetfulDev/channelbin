"""Tier 0 - the Logs PAGE, driven in a real DOM.

Everything this page does happens in the browser: the server renders an empty box
and a badge reading "Connecting", which is also exactly what a page whose script
threw on its first line looks like from Python. That gap has already cost this
project a three-step build where not one listener was registered and nothing went
red (dev/docs/BUGS.md 2026-07-30 05:15 PM), which is why the sibling page got the
same treatment in tests/test_channel_search_page_js.py.

So this runs the shipped static/js/util.js + static/js/logs.js against the markup
the Flask route really rendered, answering /api/logs/history with what the real
endpoint really returned over a seeded log file, and asserts on what the page then
did. tests/support/logs_page.mjs drives it and reports observations; every
assertion lives here, so a failure reads as a sentence about the page.

What it covers that a template scan cannot: that the filter toggles rows already
in the DOM rather than rebuilding them (DESIGN.md 16.6, and the same guarantee that
keeps a live text selection alive - dev/docs/BUGS.md 2026-07-18), that the counts
follow the filter, that severity lands on the row and not on the level, that a
source nobody declared still becomes filterable, that Stop actually closes the
stream instead of only relabelling the button, and that the phone sheet MOVES the
one toolbar rather than rendering a second one.

What it cannot cover: jsdom computes no layout, so whether the box really fills the
viewport, whether the bottom button clears the last row, and the 375px stack are
browser work. The rollout is dev/changelog/447.

Runs against a throwaway temp SQLite DB and a temp log file - never the live ones.
  python3 -m unittest tests.test_logs_page_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'logs_page.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

# One of every level, one noisy source that is off by default, one multi-line entry,
# and exactly one message carrying the word the search case looks for.
LOG_FIXTURE = """2026-08-04 08:00:00,001 [INFO] app.recorder: Recording 12 started
2026-08-04 08:00:01,002 [DEBUG] app.watchdog: poll tick
2026-08-04 08:00:02,003 [WARNING] app.watchdog: Stream stalled for 12s
2026-08-04 08:00:03,004 [ERROR] app.recorder: ffmpeg exited 1
  Traceback line that belongs to the entry above
2026-08-04 08:00:04,005 [INFO] werkzeug: 127.0.0.1 - GET /api/alerts
2026-08-04 08:00:05,006 [CRITICAL] app.concatenator: concat failed
"""

_RESULT = None


def _observe():
    """Boot the page once in node and return every scenario's observations."""
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='logs_page_js_')
    try:
        log_file = os.path.join(tmp, 'channelbin.log')
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write(LOG_FIXTURE)
        client = t.app.test_client()
        with patch('app.routes.logs._log_file_path', return_value=log_file):
            page = client.get('/logs').get_data(as_text=True)
            history = client.get('/api/logs/history?tail=1000').get_data(as_text=True)
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as f:
            f.write(page)
        with open(os.path.join(tmp, 'history.json'), 'w', encoding='utf-8') as f:
            f.write(history)
        proc = subprocess.run(
            ['node', HARNESS, tmp, REPO],
            capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class _Base(unittest.TestCase):
    SCENARIO = ''

    @classmethod
    def setUpClass(cls):
        if not cls.SCENARIO:
            raise unittest.SkipTest('base class - carries the shared cases only')
        cls.obs = _observe()[cls.SCENARIO]
        if isinstance(cls.obs, dict) and 'error' in cls.obs:
            raise AssertionError(f'{cls.SCENARIO} threw in the page:\n{cls.obs["error"]}')

    def test_the_page_booted_without_errors(self):
        self.assertEqual(self.obs['errors'], [])


class PageAsItOpensTests(_Base):
    SCENARIO = 'boot'

    def test_the_history_is_rendered(self):
        self.assertEqual(self.obs['total'], 6)
        self.assertTrue(self.obs['emptyGone'], 'the placeholder outlived the first rows')

    def test_the_stream_is_connected_to_the_real_endpoint(self):
        self.assertIn('/api/logs/stream', self.obs['streamUrl'])
        self.assertEqual(self.obs['status'], {'text': 'Live', 'cls': 'badge badge-in_progress'})

    def test_a_noisy_source_is_off_by_default_but_never_absent(self):
        """Off by default is a filter, not a deletion: the rows and the chip both exist."""
        self.assertEqual(self.obs['noisyRows'], 1)
        self.assertTrue(self.obs['noisyHidden'])
        self.assertTrue(self.obs['noisyChipOffered'])
        self.assertFalse(self.obs['noisyChipActive'])

    def test_the_count_says_what_is_being_withheld(self):
        self.assertEqual(self.obs['visible'], 5)
        self.assertEqual(self.obs['count'], '5 of 6 lines')

    def test_severity_lands_on_the_row_and_only_above_info(self):
        by_level = dict(self.obs['sev'])
        self.assertEqual(by_level['ERROR'], 'log-row sev')
        self.assertEqual(by_level['CRITICAL'], 'log-row sev')
        self.assertEqual(by_level['WARNING'], 'log-row sev warn')
        self.assertEqual(by_level['INFO'], 'log-row')
        self.assertEqual(by_level['DEBUG'], 'log-row')

    def test_each_chip_counts_its_own_lines(self):
        counts = dict(self.obs['chipCounts'])
        self.assertEqual(counts['app.recorder'], '2')
        self.assertEqual(counts['app.watchdog'], '2')
        self.assertEqual(counts['werkzeug'], '1')
        self.assertEqual(counts['app.scheduler'], '0')


class FilterTests(_Base):
    SCENARIO = 'filter'

    def test_turning_a_level_off_hides_its_rows(self):
        self.assertEqual(self.obs['before'], 5)
        self.assertEqual(self.obs['infoRows'], 2)
        # One of the two INFO rows was already hidden as a noisy source.
        self.assertEqual(self.obs['afterInfoOff'], 4)

    def test_the_filter_does_not_rebuild_the_rows(self):
        """A rebuild would take a live text selection with it (BUGS.md 2026-07-18),
        and would mean the stream and the filter both own the region (16.6)."""
        self.assertTrue(self.obs['tagsSurvived'],
                        'the filter replaced the row nodes instead of toggling them')

    def test_rows_are_hidden_by_the_attribute(self):
        self.assertEqual(self.obs['inlineStyles'], 0)

    def test_the_count_follows_the_filter(self):
        self.assertEqual(self.obs['countAfter'], '4 of 6 lines')

    def test_turning_it_back_on_restores_the_rows(self):
        self.assertEqual(self.obs['restored'], 5)


class SearchTests(_Base):
    SCENARIO = 'search'

    def test_the_search_filters_to_matching_lines(self):
        self.assertEqual(self.obs['hits'], 1)
        self.assertEqual(self.obs['countWhileFiltered'], '1 of 6 lines')

    def test_the_clear_button_appears_with_text_and_empties_the_box(self):
        """DESIGN.md 3.11's clear x - this page shipped without one."""
        self.assertTrue(self.obs['wrapLit'])
        self.assertEqual(self.obs['inputAfterClear'], '')
        self.assertFalse(self.obs['wrapLitAfterClear'])
        self.assertEqual(self.obs['afterClear'], 5)


class StreamTests(_Base):
    SCENARIO = 'stream'

    def test_a_new_line_is_appended(self):
        self.assertEqual(self.obs['before'], 6)
        self.assertEqual(self.obs['afterKnown'], 7)
        self.assertEqual(self.obs['newRowClass'], 'log-row sev')

    def test_the_chip_count_follows_the_stream(self):
        self.assertEqual(self.obs['recorderChipCount'], '3')

    def test_the_count_follows_the_stream(self):
        self.assertEqual(self.obs['count'], '7 of 8 lines')

    def test_an_undeclared_source_becomes_filterable(self):
        """A logger nobody listed must not be silently unfilterable, or it can only
        be turned off by turning off something else."""
        self.assertEqual(self.obs['afterUnknown'], 8)
        self.assertTrue(self.obs['unknownChipOffered'])
        self.assertEqual(self.obs['unknownChipLabel'], 'brandnew')
        self.assertTrue(self.obs['unknownRowVisible'])


class StatusTests(_Base):
    SCENARIO = 'status'

    def test_the_server_rendered_badge_is_the_pre_js_state(self):
        self.assertEqual(self.obs['atBoot'], {'text': 'Connecting', 'cls': 'badge badge-scheduled'})

    def test_an_open_stream_reads_live(self):
        self.assertEqual(self.obs['live']['text'], 'Live')
        self.assertTrue(self.obs['pulse'])

    def test_a_dropped_stream_says_reconnecting_not_error(self):
        """connectSSE retries on its own, so the badge must not read as a dead end."""
        self.assertEqual(self.obs['reconnecting']['text'], 'Reconnecting')
        self.assertEqual(self.obs['reconnecting']['cls'], 'badge badge-aborted')

    def test_stop_closes_the_stream_rather_than_relabelling_the_button(self):
        self.assertEqual(self.obs['stopped']['text'], 'Stopped')
        self.assertEqual(self.obs['liveBtn'], 'Start')
        self.assertTrue(self.obs['streamClosed'])

    def test_stopping_says_what_it_did_to_the_lines_already_on_screen(self):
        self.assertTrue(any('stay on screen' in t for t in self.obs['toasts']),
                        f'no toast explained the stop: {self.obs["toasts"]}')


class ClearAndCopyTests(_Base):
    SCENARIO = 'clearAndCopy'

    def test_copy_takes_only_what_is_on_screen(self):
        self.assertEqual(self.obs['visible'], 4)
        self.assertEqual(self.obs['copiedRows'], 4)
        self.assertFalse(self.obs['copiedHasDebug'],
                         'Copy handed back a row the filter is hiding')

    def test_copy_carries_a_multi_line_entry_whole(self):
        """A traceback is one record with newlines in it, not four rows."""
        self.assertEqual(self.obs['copiedLines'], 5)

    def test_clear_empties_the_view_and_every_counter_with_it(self):
        after = self.obs['afterClear']
        self.assertEqual(after['rows'], 0)
        self.assertEqual(after['count'], '')
        self.assertEqual(set(after['chipCounts']), {'0'})
        self.assertTrue(after['empty'])

    def test_clear_says_nothing_on_disk_was_touched(self):
        self.assertTrue(any('Nothing on disk' in t for t in self.obs['toasts']),
                        f'Clear did not say what it did NOT do: {self.obs["toasts"]}')

    def test_the_stream_keeps_working_after_a_clear(self):
        self.assertEqual(self.obs['rowsAfterEmit'], 1)
        self.assertEqual(self.obs['countAfterEmit'], '1 lines')


class PhoneSheetTests(_Base):
    SCENARIO = 'sheet'

    def test_the_bar_says_what_is_currently_filtered(self):
        """Nothing may be hidden by being collapsed (DESIGN.md 16.5 item 4)."""
        self.assertEqual(self.obs['summaryBefore'], 'All levels · default sources')

    def test_the_sheet_is_the_shared_modal_holding_the_one_toolbar(self):
        state = self.obs['openState']
        self.assertEqual(state['modals'], 1)
        self.assertTrue(state['filtersInModal'])
        self.assertEqual(state['chipSets'], 1, 'a second copy of the chips was rendered')
        self.assertEqual(state['title'], 'Filter')

    def test_the_moved_toolbar_still_filters(self):
        self.assertEqual(self.obs['filteredInSheet'], 4)
        self.assertEqual(self.obs['summaryFiltered'], 'DEBUG, WARN, ERROR, CRIT · default sources')
        self.assertTrue(self.obs['barLit'], 'the bar does not show that rows are being withheld')

    def test_reset_returns_the_defaults_without_closing_the_sheet(self):
        self.assertEqual(self.obs['afterReset'], {'visible': 5, 'stillOpen': 1})

    def test_closing_the_sheet_puts_the_toolbar_back_still_wired(self):
        self.assertEqual(self.obs['modalsAfterDone'], 0)
        self.assertEqual(self.obs['homeAfter'], self.obs['homeBefore'])
        self.assertEqual(self.obs['homeAfter'], 'log-filter-slot')
        self.assertTrue(self.obs['worksAfterClose'],
                        'the toolbar came back dead, so the page lost its filter')
        self.assertTrue(self.obs['noteGone'], "the sheet's note was left in the toolbar")


class EmptyHistoryTests(_Base):
    SCENARIO = 'emptyHistory'

    def test_an_empty_history_says_so(self):
        self.assertEqual(self.obs['emptyText'], 'No log history available.')

    def test_the_first_streamed_line_replaces_the_placeholder(self):
        self.assertEqual(self.obs['rowsAfterEmit'], 1)
        self.assertTrue(self.obs['emptyGone'])
        self.assertEqual(self.obs['count'], '1 lines')


if __name__ == '__main__':
    unittest.main()
