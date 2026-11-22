"""An EPG import deletes its source's window a bounded number of rows per transaction
(dev/docs/BUGS.md 2026-11-20 @ 06:44 PM).

The import cleared a source's listings in the window with one DELETE per table in one
transaction. Once one provider guide reached about a million rows, that held the write lock
for 75-80 s on prod, every other writer's retries ran out behind it, and a scheduler pass
failed to record its jobs' next run times. The delete now walks the ids
EPG_IMPORT_DELETE_ID_SPAN at a time, one commit each.

  - WindowDeleteChunkTests: no DELETE of a listing table removes more than one span, each
    span commits before the next, and each is read through the id range rather than the
    source's index.
  - WindowDeleteScopeTests: only this source's rows that end inside the window go; its
    ended rows and another source's rows interleaved by id survive, and the feed's rows
    land.

No network: runs import_source against a throwaway temp SQLite DB.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event  # noqa: E402

from app import accounts, db  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import Channel, EPGEntry, EpgAlternateEntry  # noqa: E402
from tests.support import make_test_app, seed  # noqa: E402
from tests.support.seed import make_epg_source  # noqa: E402

_CFG = {'sync': {'epg_collapse_threshold_percent': 0}}


def _xmltv(epg_channel_id, titles):
    now = datetime.utcnow()
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for i, title in enumerate(titles):
        start = now + timedelta(hours=3 + i)
        stop = start + timedelta(minutes=30)
        parts.append(
            f'<programme start="{start.strftime("%Y%m%d%H%M%S")} +0000" '
            f'stop="{stop.strftime("%Y%m%d%H%M%S")} +0000" channel="{epg_channel_id}">'
            f'<title>{title}</title></programme>')
    parts.append('</tv>')
    return ''.join(parts).encode('utf-8')


class _Case(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.acct = seed.make_account(name='Alpha')
        self.src = make_epg_source(self.acct)
        self.ch = seed.make_channel(self.acct, name='One')
        other = seed.make_account(name='Beta')
        self.other_src = make_epg_source(other)
        self.other_ch = seed.make_channel(other, name='Two')
        db.session.flush()
        # Each channel already reads its own source, so the import moves no rows between
        # tables and every DELETE it runs is the window delete.
        db.session.get(Channel, self.ch.id).epg_source_id = self.src.id
        db.session.get(Channel, self.other_ch.id).epg_source_id = self.other_src.id
        # Ids interleaved: an id range this source walks also holds the other source's rows.
        for i in range(7):
            self._listing(self.ch, self.src, f'Old {i}', hours=2 + i)
            self._listing(self.other_ch, self.other_src, f'Other {i}', hours=2 + i)
        for i in range(2):
            self._listing(self.ch, self.src, f'Ended {i}', hours=-72 - i)
        for i in range(5):
            self._listing(self.ch, self.src, f'Alt {i}', hours=2 + i, model=EpgAlternateEntry)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _listing(self, ch, src, title, hours, model=EPGEntry):
        start = datetime.utcnow() + timedelta(hours=hours)
        db.session.add(model(channel_id=ch.id, source_id=src.id, title=title,
                             start_time=start, stop_time=start + timedelta(minutes=30)))
        db.session.flush()

    def _import(self, span):
        """Import a two-program feed for the first channel, returning the rows each DELETE of
        a listing table removed, and how many commits ran between the first DELETE and the
        last."""
        removed, commits = [], []

        self.statements = []

        def on_execute(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('DELETE FROM epg_entries') or \
                    statement.startswith('DELETE FROM epg_alternate_entries'):
                removed.append((statement.split()[2], cursor.rowcount))
                self.statements.append((statement, parameters))

        def on_commit(conn):
            if removed:
                commits.append(len(removed))

        engines = list(db.engines.values())
        for engine in engines:
            event.listen(engine, 'after_cursor_execute', on_execute)
            event.listen(engine, 'commit', on_commit)
        try:
            with mock.patch.object(accounts, 'EPG_IMPORT_DELETE_ID_SPAN', span):
                synced, reason = import_source(
                    db.session.get(type(self.src), self.src.id),
                    _xmltv(self.ch.epg_channel_id, ['New A', 'New B']), epg_days=3, cfg=_CFG)
        finally:
            for engine in engines:
                event.remove(engine, 'after_cursor_execute', on_execute)
                event.remove(engine, 'commit', on_commit)
        db.session.expire_all()
        self.assertIsNone(reason)
        self.assertEqual(synced, 2)
        return removed, commits


class WindowDeleteChunkTests(_Case):

    def test_no_delete_removes_more_than_one_span(self):
        removed, _ = self._import(span=2)
        entries = [n for table, n in removed if table == 'epg_entries']
        alternates = [n for table, n in removed if table == 'epg_alternate_entries']
        self.assertEqual(sum(entries), 7)
        self.assertEqual(sum(alternates), 5)
        self.assertLessEqual(max(entries), 2, entries)
        self.assertLessEqual(max(alternates), 2, alternates)

    def test_each_span_commits_before_the_next(self):
        removed, commits = self._import(span=2)
        deletes = len(removed)
        self.assertGreater(deletes, 2)
        # A commit lands after every DELETE: the running count of DELETEs at each commit
        # covers 1..deletes with no gap.
        self.assertTrue(set(range(1, deletes + 1)) <= set(commits), (deletes, commits))

    def test_each_span_is_read_by_id_range_not_the_source_index(self):
        """Through the source's index every span walks every row the source holds."""
        self._import(span=2)
        self.assertTrue(self.statements)
        for statement, parameters in self.statements:
            plan = ' '.join(str(row[-1]) for row in db.session.connection().exec_driver_sql(
                'EXPLAIN QUERY PLAN ' + statement, parameters))
            self.assertIn('INTEGER PRIMARY KEY', plan, (statement, plan))


class WindowDeleteScopeTests(_Case):

    def test_only_this_sources_window_goes(self):
        self._import(span=2)
        mine = sorted(e.title for e in EPGEntry.query.filter_by(source_id=self.src.id))
        self.assertEqual(mine, ['Ended 0', 'Ended 1', 'New A', 'New B'])
        theirs = sorted(e.title for e in EPGEntry.query.filter_by(source_id=self.other_src.id))
        self.assertEqual(theirs, [f'Other {i}' for i in range(7)])
        self.assertEqual(EpgAlternateEntry.query.filter_by(source_id=self.src.id).count(), 0)

    def test_a_span_larger_than_the_source_is_one_delete_per_table(self):
        removed, _ = self._import(span=10_000)
        self.assertEqual(removed, [('epg_entries', 7), ('epg_alternate_entries', 5)])


if __name__ == '__main__':
    unittest.main()
