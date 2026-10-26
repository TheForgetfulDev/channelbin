"""The default hide rules for a provider's divider rows (dev/changelog/1276).

A provider puts rows like "##### NEWS #####" between the sections of its lineup. They are not
channels, and a first-time user saw them listed as channels with a checkbox each. Every install
starts with global name rules that hide them - ordinary rules once written, which the user may
edit or delete, and which are never written a second time: a new database gets them from
create_app()'s seed, an older one from migration 98, and each of those runs once.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import migrations as M  # noqa: E402
from app import channel_hiding  # noqa: E402
from app.database import Channel, ChannelHideRule, HIDE_TARGET_NAME_GLOB  # noqa: E402

#: Divider rows as the four real accounts and the simulator send them.
DIVIDERS = ('##### NEWS #####', '### MOVIE CHANNELS ###', '## MAX ESPN ᴴᴰ/ᴿᴬᵂ ⁶⁰ᶠᵖˢ ##',
            '♬♬♬ MUSIC RADIO ♬♬♬', '---- US CLASSIC ----', '=== US CLASSIC ===',
            '*** US CLASSIC ***')
#: Real channel names that sit close to a divider's shape and must stay listed.
CHANNELS = ('#0 Movistar', 'News -- 24', 'A**B', '== x', '-- x', 'US| ESPN ᴴᴰ', '♬ Radio')


def _default_rules():
    return sorted((r.account_id, r.target, r.pattern, r.enabled) for r in
                  ChannelHideRule.query.filter_by(target=HIDE_TARGET_NAME_GLOB))


def _expected():
    return sorted((None, HIDE_TARGET_NAME_GLOB, p, True)
                  for p in channel_hiding.DEFAULT_NAME_GLOBS)


class PatternTests(unittest.TestCase):
    """The patterns, against SQLite's own GLOB."""

    def setUp(self):
        self.conn = sqlite3.connect(':memory:')

    def tearDown(self):
        self.conn.close()

    def _hidden(self, name):
        return any(self.conn.execute('SELECT ? GLOB ?', (name, p)).fetchone()[0]
                   for p in channel_hiding.DEFAULT_NAME_GLOBS)

    def test_every_divider_shape_is_matched(self):
        for name in DIVIDERS:
            self.assertTrue(self._hidden(name), name)

    def test_real_channel_names_are_not(self):
        for name in CHANNELS:
            self.assertFalse(self._hidden(name), name)

    def test_the_step_writes_the_same_list_the_seed_does(self):
        """Two spellings by design - a shipped step freezes its data - so they are held
        equal here while they are meant to be."""
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td)
            M._m098_default_hide_rules(conn, cur)
            got = {r[0] for r in cur.execute('SELECT pattern FROM channel_hide_rules')}
            conn.close()
        self.assertEqual(set(channel_hiding.DEFAULT_NAME_GLOBS), got)


class FreshInstallSeedTests(unittest.TestCase):

    def test_a_new_database_starts_with_the_rules(self):
        t = make_test_app(fresh_schema=True)
        try:
            self.assertEqual(_expected(), _default_rules())
        finally:
            t.cleanup()

    def test_a_deleted_rule_does_not_come_back_on_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'dvr.db')
            first = make_test_app(extra_overrides={'database': {'path': path}})
            try:
                self.assertEqual(_expected(), _default_rules(), 'the first startup seeds')
                ChannelHideRule.query.filter_by(pattern='##*##').delete()
                db.session.commit()
            finally:
                first.cleanup()

            second = make_test_app(extra_overrides={'database': {'path': path}})
            try:
                patterns = {r[2] for r in _default_rules()}
                self.assertNotIn('##*##', patterns, 'a deleted default rule came back')
                self.assertEqual(len(channel_hiding.DEFAULT_NAME_GLOBS) - 1, len(patterns),
                                 'a restart wrote the defaults again')
            finally:
                second.cleanup()


def _scratch(td):
    conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
    cur = conn.cursor()
    cur.execute('CREATE TABLE channel_hide_rules (id INTEGER PRIMARY KEY, account_id INTEGER, '
                'target VARCHAR(32) NOT NULL, pattern VARCHAR(512) NOT NULL, '
                'enabled BOOLEAN NOT NULL DEFAULT 1, created_at DATETIME NOT NULL, '
                'updated_at DATETIME, match_count INTEGER, deferred_count INTEGER, '
                'counted_at DATETIME)')
    conn.commit()
    return conn, cur


class M098StepTests(unittest.TestCase):
    """The one-time fact for an install that predates the rules."""

    def _rules(self, cur):
        return sorted(cur.execute(
            'SELECT account_id, target, pattern, enabled FROM channel_hide_rules').fetchall(),
            key=lambda r: (r[0] is not None, r[0] or 0, r[2]))

    def test_writes_the_rules_and_owes_the_recompute(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td)
            M._m098_default_hide_rules(conn, cur)
            conn.commit()
            self.assertEqual(sorted((None, 'name_glob', p, 1) for p in
                                    channel_hiding.DEFAULT_NAME_GLOBS), self._rules(cur))
            self.assertTrue(M._backfill_pending(cur, M._BF_DEFAULT_HIDE_RULES),
                            'a rule hides nothing until the recompute runs')
            conn.close()

    def test_rerunnable_without_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td)
            M._m098_default_hide_rules(conn, cur)
            M._m098_default_hide_rules(conn, cur)
            conn.commit()
            self.assertEqual(len(channel_hiding.DEFAULT_NAME_GLOBS), len(self._rules(cur)))
            conn.close()

    def test_a_users_own_global_rule_is_not_doubled_but_a_scoped_one_is_not_enough(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td)
            cur.execute("INSERT INTO channel_hide_rules (account_id, target, pattern, enabled, "
                        "created_at) VALUES (NULL, 'name_glob', '##*##', 0, 'x')")
            cur.execute("INSERT INTO channel_hide_rules (account_id, target, pattern, enabled, "
                        "created_at) VALUES (3, 'name_glob', '==*==', 1, 'x')")
            M._m098_default_hide_rules(conn, cur)
            conn.commit()
            rules = self._rules(cur)
            self.assertIn((None, 'name_glob', '##*##', 0), rules,
                          'a rule the user already had, switched off, is left as they set it')
            self.assertEqual(1, sum(1 for r in rules if r[0] is None and r[2] == '##*##'))
            self.assertIn((None, 'name_glob', '==*==', 1), rules,
                          'one account\'s rule does not stand in for a global one')
            conn.close()

    def test_registered_as_migration_98(self):
        self.assertIn((98, M._m098_default_hide_rules),
                      [(v, fn) for v, _d, fn in M.SCHEMA_MIGRATIONS])


class M098DischargeTests(unittest.TestCase):
    """channel_hiding.apply_default_rules() - the half of migration 98 that needs the ORM."""

    def setUp(self):
        self.t = make_test_app()
        acc = seed.make_account(name='A')
        for name in DIVIDERS + CHANNELS:
            seed.make_channel(acc, name=name)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _register(self):
        db.session.execute(db.text(M._BACKFILL_LEDGER_DDL))
        db.session.execute(db.text(
            "INSERT OR REPLACE INTO migration_backfills (name, registered_at) VALUES (:n, 'x')"),
            {'n': M._BF_DEFAULT_HIDE_RULES})
        db.session.commit()

    def _hidden(self):
        db.session.expire_all()
        return {c.name for c in Channel.query.filter_by(hidden=True)}

    def test_nothing_is_hidden_until_the_obligation_is_discharged(self):
        self.assertEqual(set(), self._hidden(),
                         'the rules exist from the template, but no recompute has run')

    def test_discharging_hides_the_dividers_and_nothing_else(self):
        self._register()
        channel_hiding.apply_default_rules()
        self.assertEqual(set(DIVIDERS), self._hidden())
        self.assertFalse(M.obligation_pending(M._BF_DEFAULT_HIDE_RULES))
        counts = {r.pattern: r.match_count for r in ChannelHideRule.query}
        self.assertEqual(3, counts['##*##'], 'the rule list shows what each rule matches')

    def test_with_no_obligation_it_does_nothing(self):
        channel_hiding.apply_default_rules()
        self.assertEqual(set(), self._hidden())


if __name__ == '__main__':
    unittest.main()
