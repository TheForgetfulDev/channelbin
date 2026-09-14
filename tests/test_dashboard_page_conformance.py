"""The Live Dashboard against the design that produced it.

DESIGN.md section 16 (approved 2026-08-03) settles this page; rollout is
dev/changelog/445. Every case here is a decision from that round, or a defect the
round caught, that a careless edit would quietly undo:

  * 16.2 permits exactly ONE control in a section head and it is a forward jump-off
    naming a page. There are no create actions on this page at all - they were asked for
    and rejected in the same breath ("it should be a jumping off
    point to get to those pages").
  * The `->` is the deliverable, not decoration: without it a pill reading
    "Recordings" wears the same styling as an in-row action button and reads as "do
    something to this section". 16.6 requires it be its OWN element, because a glyph
    rendered into generated text is erased by the next regeneration and is not
    addressable to a test - this file is that test.
  * 16.3's scale has ONE definition in the tree. The TV Guide and this timeline draw
    the same density, and a second hand-typed copy diverges the first time either is
    tuned - invisibly, because neither page alone shows the disagreement.
  * 16.3 also rules what an estimate-less job renders: an outline with no length,
    never a number invented to make the bar legible. The app has no runtime estimates
    yet, so this is the state EVERY job takes today.
  * 16.4's thresholds come from app/config.py, not from a typed number, because the
    warning has to describe what scheduler.py will actually do.
  * 16.6: `[hidden]` must win outright, one composer per region, and a section's head
    count and the rows under it come from one computation.

Painted geometry - the tiles' and bars' measured widths, whether the NOW label is
clipped by the scroller, the 375px stack - needs real layout and is Tier 4/browser,
not anything this file can assert.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_dashboard_page_conformance
"""
import json
import re
import unittest
from datetime import datetime, timedelta

from app import db
from app.routes.dashboard import (
    DASHBOARD_SECTIONS, DASHBOARD_SECTIONS_PREF, DASHBOARD_SECTION_ORDER, _section_state,
)
from tests.support.app import make_test_app
from tests.support.seed import make_account, make_channel, make_recording


class DashboardPageConformanceTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        with self.t.app.app_context():
            acc = make_account(name='Acct One')
            ch = make_channel(acc, name='Channel One')
            make_recording(status='SCHEDULED', name='Booked show', channel_id=ch.id)
            make_recording(status='IN_PROGRESS', name='Capturing now', channel_id=ch.id)
            db.session.commit()
        self.html = self.client.get('/').get_data(as_text=True)

    def tearDown(self):
        self.t.cleanup()

    def test_the_page_renders(self):
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_the_page_is_converted(self):
        """DESIGN.md 3.10's page header, and none of the pre-revamp chrome it replaced."""
        self.assertIn('class="page-head"', self.html)
        self.assertNotIn('class="page-header"', self.html)
        self.assertNotIn('dash-card', self.html)
        self.assertNotIn('dashboard-grid', self.html)

    def test_every_section_head_carries_exactly_one_action(self):
        """16.2. Not zero (two sections have no natural "add" and would otherwise be
        the only ones with no control at all) and not two."""
        heads = self._section_heads()
        self.assertTrue(heads, 'no section heads rendered')
        for head in heads:
            self.assertEqual(len(re.findall(r'<a class="btn btn-sm"', head)), 1,
                             f'a section head does not carry exactly one control: {head!r}')

    def test_no_section_head_offers_a_create_action(self):
        """16.2: "There are no create actions on the Dashboard at all." Adding an
        account or scheduling a recording happens on the page that owns that work."""
        for head in self._section_heads():
            self.assertNotIn('+ Add', head)
            self.assertNotIn('+ Schedule', head)
            self.assertNotIn('+ New', head)

    def test_the_forward_arrow_is_its_own_element(self):
        """16.6: carried as its own element so the destination name stays addressable.

        The assertion is deliberately about the STRUCTURE, not the glyph: a test that
        only searched the page for U+2192 would keep passing if the arrow were folded
        back into the label, which is the state round 7 had to fix.
        """
        actions = [m for head in self._section_heads()
                   for m in re.findall(r'<a class="btn btn-sm" href="[^"]*">(.*?)</a>', head, re.S)]
        self.assertTrue(actions)
        for label in actions:
            self.assertRegex(label, r'^\s*\S.*<span class="arw">\s*(&#8594;|→)\s*</span>\s*$',
                             f'the jump-off arrow is not its own element: {label!r}')

    def test_each_section_action_names_its_own_destination(self):
        """A section with no registry entry renders no control, so a section added
        later cannot silently inherit somebody else's page."""
        heads = '\n'.join(self._section_heads())
        for key, spec in DASHBOARD_SECTIONS.items():
            self.assertIn(spec['to'], heads,
                          f'section {key} does not name its destination in its own head')

    def test_section_order_and_visibility_come_from_the_server(self):
        """16.1's user-controlled sections, and the frontend rule that server-rendered
        initial state must equal the settled state: JS may upgrade this markup, it must
        never be required to calm it down. A section the user turned off arrives
        already hidden rather than flashing on and being removed."""
        rendered = re.findall(r'class="dash-sec" data-sec="(\w+)"( hidden)?', self.html)
        self.assertEqual([k for k, _ in rendered], DASHBOARD_SECTION_ORDER)
        hidden = {k for k, h in rendered if h}
        # `upcoming` starts off: the timeline already answers "what is booked and when".
        self.assertEqual(hidden, {'upcoming'})

    def test_a_hidden_section_is_hidden_by_the_attribute_not_by_a_style(self):
        """16.6's `[hidden]` reset is what makes this work, so the markup is entitled
        to rely on the attribute alone."""
        self.assertRegex(self.html, r'data-sec="upcoming" hidden')
        for tag in re.findall(r'<div class="dash-sec"[^>]*>', self.html):
            self.assertNotIn('display', tag,
                             f'a section is hidden by an inline style, not the attribute: {tag}')

    def test_a_section_head_count_and_its_rows_are_one_computation(self):
        """16.6. The two record sections are cut from the ONE query the view runs; a
        head that counts a second query is how a page ends up saying 3 above 2 rows."""
        block = re.search(r'data-sec="live".*?(?=data-sec="upcoming")', self.html, re.S).group(0)
        count = int(re.search(r'<span class="cnt">(\d+)</span>', block).group(1))
        self.assertEqual(count, len(re.findall(r'class="drow', block)))

    def test_the_timeline_reads_its_own_data(self):
        """16.6: hiding the Upcoming ROWS must not empty the axis, so the timeline is
        handed every recording rather than the enabled set. `upcoming` is off by
        default above, so a payload carrying only the live rows would pass every other
        test in this file and still be wrong."""
        payload = self._timeline()
        names = {r['name'] for r in payload['recordings']}
        self.assertIn('Booked show', names, 'a hidden section emptied the axis')
        self.assertIn('Capturing now', names)

    def test_the_timeline_carries_no_invented_runtime_estimate(self):
        """16.3: a job with no run history draws as an outline with NO LENGTH. The app
        has no estimates at all yet, so every job must arrive with a null one - a
        default of 0-plus-a-minimum, or any placeholder, puts a number on the axis
        that nothing backs."""
        for job in self._timeline()['jobs']:
            self.assertIsNone(job['est_seconds'],
                              f"job {job['id']} arrived with an invented runtime")

    def test_the_timeline_does_not_draw_recordings_twice(self):
        """_build_job_list() models a scheduled recording as a job (`start_<id>`), and
        those are the same recordings the lanes already draw. Keeping them would put
        every scheduled capture on the page twice - once as a bar and once as a pip on
        the rail beneath it."""
        for job in self._timeline()['jobs']:
            self.assertFalse(job['id'].startswith(('start_', 'active_')),
                             f"{job['id']} is a recording drawn on the jobs rail")

    def test_the_sync_guard_thresholds_come_from_config(self):
        """16.4: the marks describe what scheduler.py will actually do, so they are
        read from app/config.py rather than typed into the page."""
        from app.config import load_config
        cfg = load_config()['sync']
        guards = self._timeline()['syncGuards']
        self.assertEqual(guards['skipWithinMinutes'], cfg['skip_sync_if_recording_within_minutes'])
        self.assertEqual(guards['skipIfRecordingActive'], cfg['skip_sync_if_recording_active'])

    def test_the_row_grid_uses_no_content_dependent_track(self):
        """CLAUDE.md's per-row-grid rule. The head and each row are separate grid
        containers, so a content-dependent track sizes itself per row and slides every
        header label off its column. Invisible to this suite otherwise - jsdom computes
        no layout - which is exactly why the declaration is asserted instead.
        """
        with open('static/css/style.css') as fh:
            css = fh.read()
        rule = re.search(r'\.dash-head, \.dash-rows \.drow \{(.*?)\}', css, re.S)
        self.assertIsNotNone(rule, 'the shared dashboard row grid is gone')
        tracks = re.search(r'grid-template-columns:([^;]+);', rule.group(1)).group(1)
        for banned in ('auto', 'max-content', 'min-content', 'fit-content'):
            self.assertNotIn(banned, tracks,
                             f'{banned} sizes per row and desyncs the header')

    def _section_heads(self):
        """Each `<div class="sec-head">...</div>` block, non-greedy to its own close."""
        return re.findall(r'<div class="sec-head">(.*?)\n\s*</div>', self.html, re.S)

    def _timeline(self):
        blob = re.search(r'id="dash-timeline">(.*?)</script>', self.html, re.S)
        self.assertIsNotNone(blob, 'the timeline payload is gone')
        return json.loads(blob.group(1))


class WindowOpenRowTests(unittest.TestCase):
    """DESIGN.md 16.1 as amended: "Recordings in progress" is every recording whose window
    is open, not only the capturing ones.

    dev/docs/BUGS.md 2026-08-15: the page's one query listed IN_PROGRESS / CONCATENATING /
    CONVERTING / SCHEDULED, so a PAUSED or RETRYING recording appeared in neither section
    and vanished from the page entirely - while the recordings list rendered it as live.
    """

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        with self.t.app.app_context():
            acc = make_account(name='Acct One')
            ch = make_channel(acc, name='Channel One')
            now = datetime.utcnow()
            for status, name in (('PAUSED', 'Paused show'), ('RETRYING', 'Dead stream show')):
                make_recording(status=status, name=name, channel_id=ch.id,
                               start_time=now - timedelta(minutes=20),
                               stop_time=now + timedelta(minutes=40),
                               started_at=now - timedelta(minutes=20), with_segment=True)
            db.session.commit()
        self.html = self.client.get('/').get_data(as_text=True)

    def tearDown(self):
        self.t.cleanup()

    def _live_block(self):
        return re.search(r'data-sec="live".*?(?=data-sec="upcoming")', self.html, re.S).group(0)

    def test_paused_and_retrying_rows_render_in_the_live_section(self):
        block = self._live_block()
        self.assertIn('Paused show', block)
        self.assertIn('Dead stream show', block)
        self.assertEqual(int(re.search(r'<span class="cnt">(\d+)</span>', block).group(1)), 2)

    def test_each_row_is_badged_as_its_own_status(self):
        block = self._live_block()
        self.assertIn('badge-paused', block)
        self.assertIn('badge-retrying', block)

    def test_the_stat_cells_are_server_rendered_not_left_as_dashes(self):
        """Only a capturing recording emits STATS_SNAPSHOT, so these rows never get an SSE
        patch - a '-' here is permanent, and CLAUDE.md requires the server-rendered state
        already be right rather than needing JS to calm it down."""
        block = self._live_block()
        for kind in ('elapsed', 'remaining', 'bytes'):
            for cell in re.findall(rf'id="{kind}-\d+">([^<]*)<', block):
                self.assertNotEqual(cell, '-', f'{kind} cell was left for JS to fill')

    def test_the_badge_and_bar_classes_these_rows_use_are_defined(self):
        """A class emitted with no rule renders unstyled and is silently wrong - and
        `badge-` is allowlisted in CssDeadClassTests, so nothing else catches this one."""
        with open('static/css/style.css') as fh:
            css = fh.read()
        for cls in ('.badge-retrying', '.tl-bar.stalled', '.tl-bar.unknown'):
            self.assertIn(cls, css, f'{cls} is emitted but defined in no stylesheet')

    def test_the_timeline_bar_class_names_every_status_explicitly(self):
        """CLAUDE.md's states-are-enumerated rule. tlBar's class used to be a ternary chain
        ending in a bare `: 'converting'`, so any status not IN_PROGRESS or SCHEDULED drew
        as a conversion bar - which is what PAUSED and RETRYING would have done the moment
        they reached the axis."""
        with open('static/js/dashboard.js') as fh:
            src = fh.read()
        table = re.search(r'const TL_BAR_CLASS = \{(.*?)\};', src, re.S)
        self.assertIsNotNone(table, 'tlBar went back to deriving its class inline')
        from app.database import (
            REC_STATUS_SCHEDULED, REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING,
            REC_STATUS_CONVERTING, WINDOW_OPEN_STATUSES,
        )
        for status in (*WINDOW_OPEN_STATUSES, REC_STATUS_SCHEDULED,
                       REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING,
                       REC_STATUS_CONVERTING):
            self.assertIn(f'{status}:', table.group(1),
                          f'{status} would fall through to another state\'s bar')

    def test_the_timeline_draws_these_rows_too(self):
        payload = json.loads(
            re.search(r'id="dash-timeline">(.*?)</script>', self.html, re.S).group(1))
        self.assertEqual({r['status'] for r in payload['recordings']}, {'PAUSED', 'RETRYING'})


class TimeAxisScaleTests(unittest.TestCase):
    """DESIGN.md 16.3: the scale is READ, not copied.

    "a second hand-typed copy diverges the first time either is tuned, and the
    divergence is invisible on either page alone." The constants moved out of
    guide.js into util.js, the shared-JS canonical home, when this page shipped.
    """

    SCALE_CONSTANTS = ('PX_PER_MIN_DESKTOP', 'PX_PER_MIN_MOBILE', 'NOW_SCROLL_DIVISOR')

    def test_each_scale_constant_is_declared_exactly_once_in_the_tree(self):
        import glob
        for name in self.SCALE_CONSTANTS:
            sites = []
            for path in glob.glob('static/js/*.js'):
                with open(path) as fh:
                    if re.search(rf'^\s*(const|let|var)\s+{name}\s*=', fh.read(), re.M):
                        sites.append(path)
            self.assertEqual(sites, ['static/js/util.js'],
                             f'{name} is declared in {sites}, not once in util.js')

    def test_both_surfaces_read_the_shared_constants(self):
        """A definition nothing reads is not shared, it is just moved."""
        for path in ('static/js/guide.js', 'static/js/dashboard.js'):
            with open(path) as fh:
                src = fh.read()
            self.assertIn('PX_PER_MIN_DESKTOP', src, f'{path} stopped reading the scale')
            self.assertIn('PX_PER_MIN_MOBILE', src, f'{path} stopped reading the scale')


class HiddenAttributeResetTests(unittest.TestCase):
    """DESIGN.md 16.6, first bullet.

    `hidden` is a user-agent rule, so ANY author rule setting `display` on the same
    element beats it outright, whatever its specificity. That cost this app two
    shipped defects during the design round - a permanently visible button and a
    page rendered stacked underneath another one. The fix is the global reset, and
    it must carry !important: rule ordering works until the next `display` rule
    anyone writes, and then it silently stops.
    """

    def test_the_global_reset_ships_and_carries_important(self):
        with open('static/css/style.css') as fh:
            css = fh.read()
        self.assertRegex(css, r'\[hidden\]\s*\{\s*display:\s*none\s*!important\s*;?\s*\}')

    def test_no_page_carries_its_own_hidden_workaround(self):
        """Four local `.x[hidden] { display: none }` rules existed in guide.css purely
        to work around the missing reset. Leaving them behind is unmarked duplication
        of a rule that now applies globally."""
        import glob
        for path in glob.glob('static/css/*.css'):
            if path.endswith('style.css'):
                continue
            with open(path) as fh:
                self.assertNotIn('[hidden]', fh.read(),
                                 f'{path} still carries a local [hidden] workaround')


class SectionPrefsTests(unittest.TestCase):
    """The stored order is user-written JSON in a /api/user-prefs row, so it is
    defended rather than trusted: it arranges a page, and must never be able to 500 it.
    """

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _store(self, value):
        from app.database import UserPref
        with self.t.app.app_context():
            pref = UserPref(key=DASHBOARD_SECTIONS_PREF, value=json.dumps(value))
            db.session.add(pref)
            db.session.commit()

    def test_a_stored_order_is_honored(self):
        self._store({'order': ['accounts', 'timeline'], 'on': {'timeline': False}})
        html = self.client.get('/').get_data(as_text=True)
        rendered = [k for k, _ in re.findall(r'data-sec="(\w+)"( hidden)?', html)]
        self.assertEqual(rendered[:2], ['accounts', 'timeline'])
        self.assertIn('data-sec="timeline" hidden', html)

    def test_a_section_missing_from_a_stored_order_still_appears(self):
        """Adding a section later must not strand a user on an order that predates it."""
        self._store({'order': ['accounts'], 'on': {}})
        with self.t.app.app_context():
            order, _on = _section_state()
        self.assertEqual(sorted(order), sorted(DASHBOARD_SECTIONS))
        self.assertEqual(order[0], 'accounts')

    def test_an_unknown_section_in_a_stored_order_is_dropped(self):
        self._store({'order': ['not_a_section', 'health'], 'on': {}})
        with self.t.app.app_context():
            order, _on = _section_state()
        self.assertNotIn('not_a_section', order)
        self.assertEqual(order[0], 'health')

    def test_a_malformed_pref_falls_back_instead_of_500ing(self):
        for value in ('not json at all', '[]', '"a string"', 'null'):
            from app.database import UserPref
            with self.t.app.app_context():
                pref = db.session.get(UserPref, DASHBOARD_SECTIONS_PREF)
                if pref is None:
                    pref = UserPref(key=DASHBOARD_SECTIONS_PREF)
                    db.session.add(pref)
                pref.value = value
                db.session.commit()
            self.assertEqual(self.client.get('/').status_code, 200,
                             f'a stored pref of {value!r} broke the page')


class MetricStripTests(unittest.TestCase):
    """16.1's strip is why this page is not just a list, and every tile is DERIVED.

    That is load-bearing rather than tidy: the question of WHAT the
    tiles show was deferred ("that will be an easy change once I use it"), and it stays an easy
    change only while no tile is hardcoded.
    """

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _tiles(self, html):
        return re.findall(
            r'<div class="m-k">(.*?)</div>\s*<div class="m-v">(.*?)</div>\s*<div class="m-s">(.*?)</div>',
            html, re.S)

    def test_the_strip_renders_six_tiles(self):
        tiles = self._tiles(self.client.get('/').get_data(as_text=True))
        self.assertEqual(len(tiles), 6)

    def test_the_capturing_tile_counts_what_is_actually_capturing(self):
        """Derived, not decorative: seed a capture and the number moves."""
        before = dict((k.strip(), v.strip()) for k, v, _ in
                      self._tiles(self.client.get('/').get_data(as_text=True)))
        self.assertEqual(before['Capturing now'], '0')

        with self.t.app.app_context():
            acc = make_account()
            ch = make_channel(acc)
            make_recording(status='IN_PROGRESS', name='Live one', channel_id=ch.id)
            make_recording(status='IN_PROGRESS', name='Live two', channel_id=ch.id)
            db.session.commit()

        after = dict((k.strip(), v.strip()) for k, v, _ in
                     self._tiles(self.client.get('/').get_data(as_text=True)))
        self.assertEqual(after['Capturing now'], '2')

    def test_the_accounts_tile_counts_errored_accounts_out(self):
        with self.t.app.app_context():
            make_account(name='Healthy')
            make_account(name='Broken').status = 'ERROR'
            db.session.commit()
        tiles = dict((k.strip(), v.strip()) for k, v, _ in
                     self._tiles(self.client.get('/').get_data(as_text=True)))
        self.assertEqual(tiles['Accounts'], '1 of 2')

    def test_a_missing_log_file_is_not_reported_as_a_quiet_hour(self):
        """The one wrong answer for this tile. A log the page cannot read says so;
        printing 0 would claim the last hour produced no errors."""
        from unittest.mock import patch
        with patch('app.routes.logs._log_file_path', return_value='/nonexistent/dvr.log'):
            tiles = dict((k.strip(), (v.strip(), s.strip())) for k, v, s in
                         self._tiles(self.client.get('/').get_data(as_text=True)))
        value, sub = tiles['Errors, last hour']
        self.assertNotEqual(value, '0')
        self.assertIn('no readable log', sub)


class RowClickThroughTests(unittest.TestCase):
    """dev/changelog/666: getting from the dashboard to the thing it is showing used to
    require finding the one small Details/Manage button in the corner of a row - and for
    accounts, that button did not even land on the specific account (it went to the
    generic accounts list). Every row-shaped section now carries a `data-href` to its own
    detail page, dashboard.js navigates on any click outside `.c-actions`/`[data-tip]`."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_a_live_recording_row_carries_its_own_detail_href(self):
        with self.t.app.app_context():
            acc = make_account()
            ch = make_channel(acc)
            rec = make_recording(status='IN_PROGRESS', name='Live one', channel_id=ch.id)
            db.session.commit()
            rec_id = rec.id
        html = self.client.get('/').get_data(as_text=True)
        m = re.search(rf'<div class="drow[^>]*\bid="card-{rec_id}"[^>]*>', html)
        self.assertIsNotNone(m)
        self.assertIn(f'data-href="/recordings/{rec_id}"', m.group(0))

    def test_an_account_row_links_to_its_own_account_not_the_list(self):
        """The bug this closes: the row's Manage link used to point at
        accounts.accounts_list regardless of which account the row was for."""
        with self.t.app.app_context():
            acc = make_account(name='Acct One')
            db.session.commit()
            acc_id = acc.id
        html = self.client.get('/').get_data(as_text=True)
        own_url = f'/accounts/{acc_id}'
        row = re.search(rf'<div class="drow[^>]*\bdata-acct="{acc_id}"[^>]*>', html)
        self.assertIsNotNone(row)
        self.assertIn(f'data-href="{own_url}"', row.group(0))
        self.assertIn(f'href="{own_url}">Manage</a>', html)
        self.assertNotIn('href="/accounts">Manage</a>', html)

    def test_the_next_recording_tile_links_to_that_recording(self):
        with self.t.app.app_context():
            acc = make_account()
            ch = make_channel(acc)
            now = datetime.utcnow()
            rec = make_recording(status='SCHEDULED', name='Later show', channel_id=ch.id,
                                  start_time=now + timedelta(hours=1),
                                  stop_time=now + timedelta(hours=2))
            db.session.commit()
            rec_id = rec.id
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn(f'data-href="/recordings/{rec_id}"', html)

    def test_the_next_recording_tile_carries_no_href_when_nothing_is_scheduled(self):
        html = self.client.get('/').get_data(as_text=True)
        m = re.search(r'<div class="mtile[^"]*"[^>]*>\s*<div class="m-k">Next recording</div>',
                       html, re.S)
        self.assertIsNotNone(m)
        self.assertNotIn('data-href', m.group(0))


class RowHealthEdgeAndSortTests(unittest.TestCase):
    """dev/changelog/670: the Recording and Account sync status cards should carry the
    same left-edge health-status color other cards use, and their column headers should
    sort on click. Both reuse existing mappings rather than inventing new ones - the
    the one `fmt_utils.REC_STATUS_DISPLAY` table for recording rows, the Accounts list's edge classes
    (now `m.account_edge_class`) for account rows - so a status can never be colored two
    different ways on two pages."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_every_reachable_recording_status_carries_its_own_edge_class(self):
        """Only these seven statuses can ever reach the dashboard's query (dashboard()'s
        filter excludes COMPLETED/FAILED/ABORTED entirely) - each must render the exact
        class the recordings list uses for the same status, not the st-abort fallback."""
        now = datetime.utcnow()
        expect = {
            'SCHEDULED': 'st-sched', 'IN_PROGRESS': 'st-live', 'PAUSED': 'st-paused',
            'RETRYING': 'st-retry', 'CONCATENATING': 'st-concat', 'ANALYZING': 'st-concat',
            'CONVERTING': 'st-concat',
        }
        with self.t.app.app_context():
            acc = make_account()
            ch = make_channel(acc)
            ids = {}
            for status in expect:
                rec = make_recording(
                    status=status, name=f'{status} show', channel_id=ch.id,
                    start_time=now - timedelta(minutes=10), stop_time=now + timedelta(minutes=50),
                    started_at=None if status == 'SCHEDULED' else now - timedelta(minutes=10))
                ids[status] = rec.id
            db.session.commit()
        html = self.client.get('/').get_data(as_text=True)
        for status, cls in expect.items():
            m = re.search(rf'<div class="drow[^>]*\bid="card-{ids[status]}"[^>]*>', html)
            self.assertIsNotNone(m, f'no row found for {status}')
            self.assertIn(cls, m.group(0).split('"')[1].split(),
                          f'{status} row is missing {cls}')

    def test_every_reachable_recording_status_renders_its_label_not_the_raw_enum(self):
        """dev/changelog/961: the badge text is the word every other surface uses for that
        status, not Recording.status. This page printed the stored enum, so it said
        CONCATENATING where the Recordings list said JOINING and IN_PROGRESS where the list
        said RECORDING - the same state under two names on two pages, which is exactly what
        dev/changelog/867's rename was for."""
        now = datetime.utcnow()
        expect = {
            'SCHEDULED': 'SCHEDULED', 'IN_PROGRESS': 'RECORDING', 'PAUSED': 'PAUSED',
            'RETRYING': 'RETRYING', 'CONCATENATING': 'JOINING', 'ANALYZING': 'ANALYZING',
            'CONVERTING': 'CONVERTING',
        }
        with self.t.app.app_context():
            acc = make_account()
            ch = make_channel(acc)
            ids = {}
            for status in expect:
                rec = make_recording(
                    status=status, name=f'{status} show', channel_id=ch.id,
                    start_time=now - timedelta(minutes=10), stop_time=now + timedelta(minutes=50),
                    started_at=None if status == 'SCHEDULED' else now - timedelta(minutes=10))
                ids[status] = rec.id
            db.session.commit()
        html = self.client.get('/').get_data(as_text=True)
        for status, label in expect.items():
            m = re.search(rf'<span class="badge[^"]*" id="badge-{ids[status]}">([^<]*)</span>',
                          html)
            self.assertIsNotNone(m, f'no badge found for {status}')
            self.assertEqual(label, m.group(1).strip(),
                             f'{status} badge should read {label}')

    def test_a_parked_post_processing_row_badges_as_waiting(self):
        """dev/changelog/961: the background-task chip already called a parked row
        "Waiting to convert" (dev/changelog/954) while the row badge two inches away still
        said ANALYZING. One derivation, in fmt_utils.rec_status_display, so the two cannot
        disagree. A CONCATENATING row is deliberately NOT waitable - a join is never
        parked - so it must keep reading JOINING even with the column set."""
        now = datetime.utcnow()
        with self.t.app.app_context():
            acc = make_account()
            ch = make_channel(acc)
            parked = make_recording(
                status='ANALYZING', name='Parked', channel_id=ch.id,
                start_time=now - timedelta(minutes=10), stop_time=now + timedelta(minutes=50),
                started_at=now - timedelta(minutes=10))
            joining = make_recording(
                status='CONCATENATING', name='Joining', channel_id=ch.id,
                start_time=now - timedelta(minutes=10), stop_time=now + timedelta(minutes=50),
                started_at=now - timedelta(minutes=10))
            parked.postprocess_waiting_since = now
            joining.postprocess_waiting_since = now
            db.session.commit()
            parked_id, joining_id = parked.id, joining.id
        html = self.client.get('/').get_data(as_text=True)
        m = re.search(rf'<span class="badge[^"]*" id="badge-{parked_id}">([^<]*)</span>', html)
        self.assertIsNotNone(m)
        self.assertEqual('WAITING', m.group(1).strip())
        m = re.search(rf'<span class="badge[^"]*" id="badge-{joining_id}">([^<]*)</span>', html)
        self.assertIsNotNone(m)
        self.assertEqual('JOINING', m.group(1).strip())

    def test_the_page_hands_its_javascript_the_same_label_table_it_rendered(self):
        """dashboard.js relabels a badge from the status on an SSE frame. It reads the
        server's own table out of #dash-status-labels rather than carrying a second,
        hand-written copy that can drift from app/fmt_utils.py (dev/changelog/961)."""
        from app.fmt_utils import REC_STATUS_DISPLAY
        html = self.client.get('/').get_data(as_text=True)
        m = re.search(r'<script type="application/json" id="dash-status-labels">(.*?)</script>',
                      html, re.S)
        self.assertIsNotNone(m, 'the page renders no status-label table for its JS')
        labels = json.loads(m.group(1))
        self.assertEqual({s: row[3] for s, row in REC_STATUS_DISPLAY.items()}, labels)

    def test_every_account_status_carries_its_own_edge_class(self):
        expect = {'OK': 'st-ok', 'SYNCING': 'st-sync', 'ERROR': 'st-bad', 'UNSYNCED': 'st-none'}
        with self.t.app.app_context():
            ids = {}
            for status in expect:
                acc = make_account(name=f'Acct {status}')
                acc.status = status
                db.session.flush()
                ids[status] = acc.id
            db.session.commit()
        html = self.client.get('/').get_data(as_text=True)
        for status, cls in expect.items():
            m = re.search(rf'<div class="drow[^>]*\bdata-acct="{ids[status]}"[^>]*>', html)
            self.assertIsNotNone(m, f'no row found for {status}')
            self.assertIn(cls, m.group(0).split('"')[1].split(),
                          f'{status} account row is missing {cls}')

    def test_every_edge_class_the_page_can_emit_is_defined_in_css(self):
        with open('static/css/style.css') as fh:
            css = fh.read()
        for cls in ('.drow.st-sched', '.drow.st-live', '.drow.st-paused', '.drow.st-retry',
                    '.drow.st-concat', '.drow.st-done', '.drow.st-fail', '.drow.st-abort',
                    '.drow.st-ok', '.drow.st-sync', '.drow.st-bad', '.drow.st-none'):
            self.assertIn(cls, css, f'{cls} is emitted but defined in no stylesheet')

    def test_live_and_accounts_headers_are_sortable_on_every_visible_column(self):
        with self.t.app.app_context():
            acc = make_account()
            ch = make_channel(acc)
            make_recording(status='IN_PROGRESS', name='Live one', channel_id=ch.id)
            db.session.commit()
        html = self.client.get('/').get_data(as_text=True)
        live_block = re.search(r'data-sec="live".*?(?=data-sec="upcoming")', html, re.S).group(0)
        head = re.search(r'<div class="dash-head">(.*?)</div>', live_block, re.S).group(1)
        for key in ('name', 'status', 'elapsed', 'remaining', 'recorded'):
            self.assertIn(f'data-sort="{key}"', head, f'{key} column is not sortable')

        acct_head = re.search(r'data-sec="accounts".*?<div class="dash-head">(.*?)</div>',
                              html, re.S).group(1)
        for key in ('name', 'status', 'channels', 'lastsync', 'nextsync'):
            self.assertIn(f'data-sort="{key}"', acct_head, f'{key} column is not sortable')

    def test_sort_click_handler_is_wired_from_a_shared_helper(self):
        """CLAUDE.md's DRY rule: reuse util.js's sortChildren rather than a hand-rolled
        reorder loop, the same helper index.html's recordings list and group-detail.js's
        member table already use."""
        with open('static/js/dashboard.js') as fh:
            src = fh.read()
        self.assertIn('sortChildren(', src)
        self.assertIn("querySelectorAll('.dash-head .sortable')", src)

    def test_the_account_card_names_how_many_channels_are_hidden(self):
        """dev/docs/DESIGN-channel-hiding.md §11 "Counts": hidden_channel_count is rendered
        beside channel_count everywhere the latter already is, including this card."""
        with self.t.app.app_context():
            acc = make_account(name='SomeHidden', channel_count=100, hidden_channel_count=40)
            db.session.commit()
            acct_id = acc.id
        html = self.client.get('/').get_data(as_text=True)
        m = re.search(rf'<div class="drow[^>]*data-acct="{acct_id}"[^>]*>(.*?)(?=<div class="drow|\Z)',
                      html, re.S)
        self.assertIsNotNone(m)
        self.assertIn('40', m.group(1))
        self.assertIn('hidden', m.group(1))


if __name__ == '__main__':
    unittest.main()
