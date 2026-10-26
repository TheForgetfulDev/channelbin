"""The automatic group is named and explained the same way on every page that shows it
(dev/changelog/1289).

Two first-time users met "TV Guide Channels" as a group of identical feeds on Channel
Groups, as a group Readiness said did not exist, as something the dashboard said to start
from Channel Groups, and as the TV Guide menu. Guards dev/docs/BUGS.md 2026-10-26 (the
Channel Groups row described the automatic check as "Duplicate feeds of one channel").
"""
import os
import re
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import migrations as M  # noqa: E402
from app.channel_groups import SYSTEM_CHECK_NAME, SYSTEM_CHECK_SENTENCE  # noqa: E402
from app.database import ChannelGroup, OnDemandTestJob  # noqa: E402


def _system_group():
    return ChannelGroup.query.filter_by(is_system=True).one()


class SeedNameTests(unittest.TestCase):
    def test_a_fresh_install_makes_both_rows_under_the_new_name(self):
        t = make_test_app(fresh_schema=True)
        try:
            self.assertEqual(_system_group().name, SYSTEM_CHECK_NAME)
            self.assertEqual(OnDemandTestJob.query.filter_by(is_system=True).one().name,
                             SYSTEM_CHECK_NAME)
        finally:
            t.cleanup()


class MigrationTests(unittest.TestCase):
    def _db(self, td):
        conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
        cur = conn.cursor()
        for table in ('channel_groups', 'on_demand_test_jobs'):
            cur.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY, name TEXT, '
                        'is_system BOOLEAN)')
        return conn, cur

    def test_m100_renames_the_old_system_rows_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._db(td)
            for table in ('channel_groups', 'on_demand_test_jobs'):
                cur.execute(f"INSERT INTO {table} (name, is_system) VALUES "
                            "('TV Guide Channels', 1), ('TV Guide Channels', 0)")
            conn.commit()
            M._m100_rename_system_check(conn, cur)
            M._m100_rename_system_check(conn, cur)
            for table in ('channel_groups', 'on_demand_test_jobs'):
                rows = cur.execute(f'SELECT name, is_system FROM {table} ORDER BY id').fetchall()
                # A user's own group that happens to carry the old name is not the system one.
                self.assertEqual(rows, [('TV Guide health check', 1), ('TV Guide Channels', 0)])
            conn.close()

    def test_m100_leaves_a_name_someone_chose(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = self._db(td)
            cur.execute("INSERT INTO channel_groups (name, is_system) VALUES ('Nightly', 1)")
            conn.commit()
            M._m100_rename_system_check(conn, cur)
            self.assertEqual(cur.execute('SELECT name FROM channel_groups').fetchone(),
                             ('Nightly',))
            conn.close()

    def test_m100_registered_at_its_own_version(self):
        registered = {v: fn for v, _d, fn in M.SCHEMA_MIGRATIONS}
        self.assertIs(registered.get(100), M._m100_rename_system_check)


class GroupsListTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def _body(self):
        resp = self.t.client.get('/channel-groups')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _system_row(self, body):
        gid = _system_group().id
        start = body.index(f'class="grp-item" data-group="{gid}"')
        return body[start:body.index('class="grp-detail"', start)]

    def test_the_automatic_check_has_its_own_section_and_the_one_sentence(self):
        body = self._body()
        auto = body.index('data-section="auto"')
        mine = body.index('class="sec-head" data-section="groups"')
        self.assertLess(auto, body.index(f'data-group="{_system_group().id}"'))
        self.assertLess(body.index(f'data-group="{_system_group().id}"'), mine)
        self.assertIn(f'data-section-note="auto">{SYSTEM_CHECK_SENTENCE}</p>', body)

    def test_the_automatic_row_is_described_as_a_health_check_not_a_group(self):
        """dev/docs/BUGS.md 2026-10-26: its kind glyph said "Channel group. Duplicate feeds of
        one channel" over a list of unrelated guide channels."""
        row = self._system_row(self._body())
        self.assertNotIn('Duplicate feeds', row)
        self.assertNotIn('&#9939;', row)
        self.assertIn('data-tip="Health check.&#10;' + SYSTEM_CHECK_SENTENCE, row)

    def test_an_empty_automatic_check_says_it_fills_itself(self):
        row = self._system_row(self._body())
        self.assertIn('Nothing to test yet', row)
        self.assertNotIn('0 channels', row)

    def test_clone_is_offered_only_where_the_server_allows_it(self):
        row = self._system_row(self._body())
        self.assertRegex(row, r'data-act="clone"[^>]*disabled')

    def test_no_groups_of_your_own_shows_the_empty_state_and_counts_zero(self):
        body = self._body()
        self.assertIn('Your groups <span class="cnt">0</span>', body)
        self.assertRegex(body, r'id="grp-empty">')

    def test_one_group_of_your_own_hides_the_empty_state(self):
        ch = seed.make_channel(seed.make_account(), name='Fox One')
        seed.make_group(name='Fox', members=[ch])
        db.session.commit()
        body = self._body()
        self.assertIn('Your groups <span class="cnt">1</span>', body)
        self.assertRegex(body, r'id="grp-empty" style="display:none"')


class OtherSurfacesTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_group_page_explains_it_and_does_not_claim_a_user_created_it(self):
        body = self.t.client.get(f'/channel-groups/{_system_group().id}').get_data(as_text=True)
        lead = body[body.index('class="gd-sub-lead"'):]
        lead = lead[:lead.index('</p>')]
        self.assertIn(SYSTEM_CHECK_SENTENCE, lead)
        dates = body[body.index('class="gd-dates"'):]
        dates = dates[:dates.index('</div>')]
        self.assertIn('Made by ChannelBin', dates)
        self.assertNotIn('Created', dates)

    def test_dashboard_names_the_guide_check_instead_of_sending_the_user_to_start_one(self):
        body = self.t.client.get('/').get_data(as_text=True)
        self.assertNotIn('Start one from Channel Groups', body)
        self.assertRegex(body, r'&#34;TV Guide health check&#34; (tests the channels in your '
                               r'TV Guide|has no schedule)')

    def test_readiness_does_not_deny_the_group_the_user_can_see(self):
        from app.readiness import _check_guide_groups

        class Ctx:
            guide_groups = 0
        result = _check_guide_groups(Ctx())
        found = str(result)
        self.assertNotIn('None of your channel groups', found)
        self.assertIn('No channel group of yours is a guide row yet', found)

    def test_zero_profiles_says_that_is_normal(self):
        body = self.t.client.get('/health-check-profiles').get_data(as_text=True)
        self.assertIn('Having none is normal', body)
        self.assertIn('TV Guide health check', body)


class NoOldNameInShippedCopyTests(unittest.TestCase):
    """The old name survives only in the migration ladder, which describes what was done to
    a database of its vintage."""

    def test_no_template_script_or_route_spells_the_old_name(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        hits = []
        for top in ('app', 'templates', 'static/js'):
            for dirpath, _dirs, files in os.walk(os.path.join(root, top)):
                for f in files:
                    if not f.endswith(('.py', '.html', '.js')):
                        continue
                    path = os.path.join(dirpath, f)
                    rel = os.path.relpath(path, root)
                    if rel in ('app/migrations.py', 'app/channel_groups.py'):
                        continue
                    with open(path, encoding='utf-8') as fh:
                        for n, line in enumerate(fh, 1):
                            # Half the name catches it split across two string lines.
                            if re.search(r'Guide Channels', line):
                                hits.append(f'{rel}:{n}')
        self.assertEqual(hits, [])


if __name__ == '__main__':
    unittest.main()
