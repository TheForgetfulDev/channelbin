"""What a queued sync's search index rebuild can and cannot run into, over real HTTP.

A sync ends by rebuilding the search index, and the sync queue starts the next account after a
fixed pause. dev/changelog/1443 asks whether the two can meet. They cannot: the rebuild is the
tail of `_do_sync`, so the sync's admission ticket covers it and the pause is not armed until it
has finished, however long it takes.

Invariants, on ChannelBin's unmodified `_do_sync` against the simulator:
  (a) While a queued sync's rebuild runs, the sync still holds its ticket, so a second sync is
      refused.
  (b) During that rebuild the queue is armed only for its crash resume. The pause is armed after
      the rebuild ends.
  (c) The pause is longer than the delay on the rule check a guide import asks for, so that
      check runs between accounts rather than being refused all the way down the queue.

  python3 -m unittest tests.test_sync_queue_rebuild
"""
import inspect
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import admission, auto_rules, db, search_index, sync_queue  # noqa: E402
from tests.support.sim_sync import SimSyncCase  # noqa: E402


class RebuildInsideTheSyncTests(SimSyncCase):

    def setUp(self):
        super().setUp()
        self.events = []
        self.second_sync = None
        self.addCleanup(admission.reset_for_tests)

    def _rebuild_probe(self):
        real = search_index.rebuild_search_indexes

        def probe(*args, **kwargs):
            self.events.append('rebuild starts')
            self.second_sync = admission.try_start(admission.KIND_SYNC, 'another account')
            admission.release(self.second_sync)
            result = real(*args, **kwargs)
            self.events.append('rebuild ends')
            return result
        return probe

    def _run_step(self):
        first = self.add_xtream('north-tv')
        second = self.add_m3u()
        for account_id in (first, second):
            self.account(account_id).sync_enabled = False
        db.session.commit()

        def arm(pending, total, queued_at, override, delay_seconds):
            self.events.append(('arm', delay_seconds, list(pending)))

        with mock.patch('app.scheduler._app', self.t.app), \
                mock.patch.object(sync_queue, '_arm', side_effect=arm), \
                mock.patch('app.search_index.rebuild_search_indexes', self._rebuild_probe()):
            sync_queue.run_step([first, second], 2, datetime.utcnow())
        return second

    def test_a_second_sync_is_refused_while_the_rebuild_runs(self):
        self._run_step()
        self.assertIn('rebuild starts', self.events)
        self.assertFalse(self.second_sync.granted)
        self.assertEqual(self.second_sync.blocked_by, admission.KIND_SYNC)

    def test_the_pause_is_armed_only_after_the_rebuild_ends(self):
        second = self._run_step()
        self.assertEqual(self.events, [
            ('arm', sync_queue.RESUME_AFTER_SECONDS, [second]),
            'rebuild starts',
            'rebuild ends',
            ('arm', sync_queue.PAUSE_SECONDS, [second]),
        ])


class RuleCheckTests(unittest.TestCase):

    def test_the_pause_outlasts_the_delay_on_a_guide_imports_rule_check(self):
        delay = inspect.signature(auto_rules.queue_evaluation).parameters['delay_seconds'].default
        self.assertGreater(sync_queue.PAUSE_SECONDS, delay)


if __name__ == '__main__':
    unittest.main()
