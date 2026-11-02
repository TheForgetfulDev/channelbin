"""The built-in 4k tag (dev/changelog/1395).

Providers mark UHD and HDR airings with superscript letters (ᵁᴴᴰ, ᴴᴰᴿ) the same way they mark
live and new ones. A new tags table gets a 4k tag for them from create_app()'s seed, an older
install from migration 109, and each runs once, so a 4k tag the user deletes stays deleted.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app import migrations as M  # noqa: E402
from app.database import FOUR_K_TAG_PATTERNS, Tag  # noqa: E402


def _scratch(td):
    conn = sqlite3.connect(os.path.join(td, 'scratch.db'))
    cur = conn.cursor()
    cur.execute('CREATE TABLE tags (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL UNIQUE, '
                'color VARCHAR(32) NOT NULL, created_at DATETIME)')
    cur.execute('CREATE TABLE tag_patterns (id INTEGER PRIMARY KEY, tag_id INTEGER NOT NULL, '
                'pattern VARCHAR(255) NOT NULL)')
    conn.commit()
    return conn, cur


def _tags(cur):
    rows = cur.execute('SELECT t.name, p.pattern FROM tags t '
                       'LEFT JOIN tag_patterns p ON p.tag_id = t.id ORDER BY t.id, p.id')
    out = {}
    for name, pattern in rows:
        out.setdefault(name, []).append(pattern)
    return out


class FreshInstallSeedTests(unittest.TestCase):

    def test_a_new_database_starts_with_the_4k_tag(self):
        t = make_test_app(fresh_schema=True)
        try:
            tag = Tag.query.filter_by(name='4k').one()
            self.assertEqual(sorted(FOUR_K_TAG_PATTERNS),
                             sorted(p.pattern for p in tag.patterns))
            self.assertEqual({'live', 'new', '4k'}, {t.name for t in Tag.query})
        finally:
            t.cleanup()

    def test_a_deleted_4k_tag_does_not_come_back_on_restart(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, 'dvr.db')
            first = make_test_app(extra_overrides={'database': {'path': path}})
            try:
                db.session.delete(Tag.query.filter_by(name='4k').one())
                db.session.commit()
            finally:
                first.cleanup()
            second = make_test_app(extra_overrides={'database': {'path': path}})
            try:
                self.assertIsNone(Tag.query.filter_by(name='4k').first())
            finally:
                second.cleanup()


class M109StepTests(unittest.TestCase):
    """The one-time fact for an install whose tags table predates the 4k tag."""

    def test_adds_the_tag_with_both_markers(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td)
            M._m109_default_4k_tag(conn, cur)
            conn.commit()
            self.assertEqual({'4k': ['ᵁᴴᴰ', 'ᴴᴰᴿ']}, _tags(cur))
            conn.close()

    def test_the_step_writes_the_same_patterns_the_seed_does(self):
        """Two spellings by design - a shipped step freezes its data."""
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td)
            M._m109_default_4k_tag(conn, cur)
            self.assertEqual(list(FOUR_K_TAG_PATTERNS), _tags(cur)['4k'])
            conn.close()

    def test_rerunnable_without_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td)
            M._m109_default_4k_tag(conn, cur)
            M._m109_default_4k_tag(conn, cur)
            conn.commit()
            self.assertEqual(1, cur.execute('SELECT COUNT(*) FROM tags').fetchone()[0])
            self.assertEqual(2, cur.execute('SELECT COUNT(*) FROM tag_patterns').fetchone()[0])
            conn.close()

    def test_a_users_own_4k_tag_is_left_alone_whatever_its_case(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td)
            cur.execute("INSERT INTO tags (name, color) VALUES ('4K', '#000000')")
            M._m109_default_4k_tag(conn, cur)
            conn.commit()
            self.assertEqual({'4K': [None]}, _tags(cur))
            conn.close()

    def test_a_tag_already_matching_a_marker_is_not_doubled(self):
        with tempfile.TemporaryDirectory() as td:
            conn, cur = _scratch(td)
            cur.execute("INSERT INTO tags (name, color) VALUES ('uhd', '#000000')")
            cur.execute("INSERT INTO tag_patterns (tag_id, pattern) VALUES (1, 'ᵁᴴᴰ')")
            M._m109_default_4k_tag(conn, cur)
            conn.commit()
            self.assertEqual({'uhd': ['ᵁᴴᴰ']}, _tags(cur))
            conn.close()

    def test_no_tags_table_is_a_no_op(self):
        """A tags table created after the migrations run gets the seed instead."""
        with tempfile.TemporaryDirectory() as td:
            conn = sqlite3.connect(os.path.join(td, 'empty.db'))
            M._m109_default_4k_tag(conn, conn.cursor())
            conn.close()

    def test_registered_as_migration_109(self):
        self.assertIn((109, M._m109_default_4k_tag),
                      [(v, fn) for v, _d, fn in M.SCHEMA_MIGRATIONS])


if __name__ == '__main__':
    unittest.main()
