"""The `.tbl-cards` phone reflow, across every table that opts into it.

A `.tbl` opting into `.tbl-cards` loses its `<thead>` below 960px and draws each row as a
card of key/value lines, where the key is the cell's own `data-label` (DESIGN.md 3.2,
dev/changelog/1017). So a cell without one renders a bare value with nothing saying what it
measures - invisible to every other test in the suite, because jsdom computes no layout and
the markup is perfectly valid either way.

One module rather than a case in each page's `tests/test_<page>_page_conformance.py`: the
claim is the same sentence on every page, and REFLOWING below is the registry of who makes
it. A page added to that registry is checked for free; a page that opts in without joining
it is caught by test_every_opting_in_table_is_registered, which is the half that actually
stops this going stale.

The registry deliberately names the modal tables that must NOT opt in. A table inside a
sheet is already constrained by it, and reflowing one there was the specific thing 3.2's
opt-in shape was chosen to avoid.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_table_card_reflow
"""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_channel_test  # noqa: E402
from app import db  # noqa: E402
from app.database import Tag, TagPattern, TEST_STATUS_COMPLETED  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Server-rendered pages: endpoint -> how many .tbl-cards tables it must draw.
REFLOWING_PAGES = {
    '/tags': 1,
    '/jobs': 1,
    # The account stats comparison (dev/changelog/1029); drawn only with two or more
    # accounts, so the fixture below seeds a second one.
    '/accounts': 1,
}

# Tables built in JavaScript, so no server render contains them. The source file is what
# gets read instead: (path, the function that builds the <tr>, how many tables opt in).
REFLOWING_JS = [
    ('static/js/hide-rules.js', 'ruleRowHtml', 1),
]

JOBS = [
    {'id': 'sync_1', 'display_name': 'Account sync: provider-one', 'type': 'recurring',
     'next_run_et': 'Sep 04, 2026 03:00 AM', 'next_run_relative': 'in 8 hours',
     'stop_run_et': None, 'schedule_description': 'every 12 hours',
     'edit_url': '/settings?q=sync', 'run_url': '/api/accounts/1/sync',
     'skip_url': '/api/jobs/skip/sync_1', 'overlap': 'yellow'},
    {'id': 'active_9', 'display_name': 'Health check: Sports HD', 'type': 'active',
     'next_run_et': 'running', 'next_run_relative': 'now', 'stop_run_et': None,
     'schedule_description': 'running now', 'edit_url': None, 'overlap': 'green'},
]


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def _card_tables(html):
    """Every <table> carrying .tbl-cards, as its inner markup."""
    return [m.group(1) for m in
            re.finditer(r'<table[^>]*\bclass="[^"]*\btbl-cards\b[^"]*"[^>]*>(.*?)</table>',
                        html, re.S)]


def _rows(table_html):
    body = table_html.split('<tbody>')[1].split('</tbody>')[0]
    return re.findall(r'<tr\b.*?</tr>', body, re.S)


def _cells(row_html):
    return re.findall(r'<td\b[^>]*>', row_html)


class LabelledCellTests(unittest.TestCase):
    """Every cell of a reflowing table says what it is, except a trailing actions cell."""

    @classmethod
    def setUpClass(cls):
        cls.t = make_test_app()
        cls.client = cls.t.app.test_client()
        with cls.t.app.app_context():
            tag = Tag(name='reflow-fixture', color='#22c55e')
            db.session.add(tag)
            db.session.flush()
            db.session.add(TagPattern(tag_id=tag.id, pattern='LIVE'))
            ch = make_channel(make_account(), name='Sports HD')
            make_account(name='Second account')
            make_channel_test(ch, status=TEST_STATUS_COMPLETED, resolution='1920x1080')
            db.session.commit()
            cls.channel_id = ch.id
        cls.patcher = mock.patch('app.routes.jobs._build_job_list', return_value=JOBS)
        cls.patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()
        cls.t.cleanup()

    def _assert_labelled(self, where, table_html):
        rows = _rows(table_html)
        self.assertTrue(rows, f'{where}: a reflowing table rendered no rows to check')
        for row in rows:
            cells = _cells(row)
            for i, cell in enumerate(cells):
                if 'data-label=' in cell:
                    continue
                # A trailing actions cell has no column header, so it correctly has no
                # label. Anywhere else, the value loses its key when the thead goes.
                self.assertEqual(
                    i, len(cells) - 1,
                    f'{where}: cell {i + 1} of {len(cells)} has no data-label, and is not '
                    f'the trailing actions cell, so below 960px it renders a value with '
                    f'nothing saying what it measures (DESIGN.md 3.2): {cell}')

    def test_server_rendered_pages_label_every_cell(self):
        for url, expected in REFLOWING_PAGES.items():
            with self.subTest(url=url):
                html = self.client.get(url).get_data(as_text=True)
                tables = _card_tables(html)
                self.assertEqual(
                    expected, len(tables),
                    f'{url} drew {len(tables)} .tbl-cards table(s), expected {expected} - '
                    'a table that stopped opting in scrolls sideways on a phone again')
                for table in tables:
                    self._assert_labelled(url, table)

    def test_channel_detail_labels_every_cell(self):
        # Its own case rather than a REFLOWING_PAGES entry: the two tables render only
        # when the channel actually has tests and observations, so the URL is seeded.
        html = self.client.get(f'/channels/{self.channel_id}').get_data(as_text=True)
        tables = _card_tables(html)
        self.assertEqual(
            1, len(tables),
            'The seeded channel has one test and no recording observations, so exactly '
            'one of channel detail\'s two tables should render as .tbl-cards')
        for table in tables:
            self._assert_labelled('/channels/<id>', table)

    def test_js_built_rows_label_every_cell(self):
        for path, builder, expected in REFLOWING_JS:
            with self.subTest(path=path):
                src = _read(path)
                self.assertEqual(
                    expected, len(re.findall(r'class="tbl tbl-cards', src)),
                    f'{path}: expected {expected} table(s) opting into the reflow')
                body = src.split(f'function {builder}(')[1].split('\nfunction ')[0]
                cells = _cells(body)
                self.assertTrue(cells, f'{path}::{builder} built no <td> to check')
                for i, cell in enumerate(cells):
                    if 'data-label=' in cell:
                        continue
                    self.assertEqual(
                        i, len(cells) - 1,
                        f'{path}::{builder}: cell {i + 1} of {len(cells)} has no '
                        f'data-label and is not the trailing actions cell: {cell}')


class OptInTests(unittest.TestCase):
    """The reflow is opt-in, and stays that way."""

    def test_the_variant_never_lands_on_bare_tbl(self):
        """`.tbl-cards` selectors must never be written as plain `.tbl` ones. A reflow on
        `.tbl` itself would take the modal tables with it, which is the one thing 3.2's
        opt-in shape exists to prevent."""
        css = _read('static/css/style.css')
        block = css.split('Table as cards on a phone')[1].split('\n/* ──')[0]
        for selector in re.findall(r'^\s*([.\w\[\]="():,\-\s>]+?)\s*\{', block, re.M):
            self.assertIn(
                'tbl-cards', selector,
                'A rule in the .tbl-cards block does not name .tbl-cards, so it applies to '
                f'every .tbl in the app including the ones inside modals: {selector!r}')

    def test_every_opting_in_table_is_registered(self):
        """A page that opts in without joining the registry above is never checked for
        labels, which is the failure mode this whole module exists to close."""
        found = set()
        for root, _dirs, files in os.walk(os.path.join(REPO, 'templates')):
            for name in files:
                if name.endswith('.html'):
                    path = os.path.join(root, name)
                    if 'tbl-cards' in _read(os.path.relpath(path, REPO)):
                        found.add(os.path.relpath(path, REPO))
        for root, _dirs, files in os.walk(os.path.join(REPO, 'static/js')):
            for name in files:
                if name.endswith('.js'):
                    path = os.path.join(root, name)
                    if 'tbl-cards' in _read(os.path.relpath(path, REPO)):
                        found.add(os.path.relpath(path, REPO))
        registered = {'templates/tags.html', 'templates/jobs.html',
                      'templates/channels/detail.html', 'templates/_account_stats.html'} | {p for p, _b, _n in REFLOWING_JS}
        self.assertEqual(
            registered, found,
            'A file opts into .tbl-cards but is not covered above (or is covered and no '
            'longer opts in). Add it to REFLOWING_PAGES / REFLOWING_JS and to this set, '
            'so its cells are checked for data-label.')

    def test_modal_tables_do_not_opt_in(self):
        """The Hide Rules category picker and preview tables live inside a sheet that
        already constrains them; reflowing a table there buys nothing and was the reason
        3.2's variant is opt-in rather than a change to `.tbl`."""
        src = _read('static/js/hide-rules.js')
        for builder in ('cat-list-scroll', 'preview-table-wrap'):
            fragment = src.split(builder)[1][:120]
            self.assertNotIn(
                'tbl-cards', fragment,
                f'The {builder} table opted into the phone reflow; a table inside a modal '
                'is already constrained by the sheet (DESIGN.md 3.2)')


if __name__ == '__main__':
    unittest.main()
