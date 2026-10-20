"""One validation behind every Remove Duplicate Channels surface (dev/changelog/1244).

The TV Guide's dedup route and a health check's each spelled the keeper check themselves,
verbatim comment included, and the group page's had a third copy with a cluster-aware
same-stream rule. accounts.transfer_keeper_problem() is the one spelling: the three routes
differ only in the set a keeper must belong to and the sentence's tail.

  python3 -m unittest tests.test_transfer_keeper_problem
"""
import os
import unittest
from types import SimpleNamespace

from app.accounts import transfer_keeper_problem

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ch(id, url='http://example.test/live/1', cluster=None):
    return SimpleNamespace(id=id, stream_url=url, duplicate_cluster_id=cluster)


class KeeperProblemTests(unittest.TestCase):
    def test_a_valid_keeper_has_no_problem(self):
        self.assertIsNone(transfer_keeper_problem(_ch(1), _ch(2)))

    def test_missing_self_and_outsider_are_the_same_sentence_with_the_surfaces_tail(self):
        self.assertEqual(transfer_keeper_problem(_ch(1), None), 'No valid channel to keep')
        self.assertEqual(transfer_keeper_problem(_ch(1), _ch(1)), 'No valid channel to keep')
        self.assertEqual(transfer_keeper_problem(_ch(1), _ch(2), member_ids={1, 3}, where=' in this test'),
                         'No valid channel to keep in this test')
        self.assertIsNone(transfer_keeper_problem(_ch(1), _ch(2), member_ids={1, 2}))

    def test_a_different_stream_is_refused(self):
        self.assertEqual(transfer_keeper_problem(_ch(1), _ch(2, url='http://example.test/live/9')),
                         'Kept channel is no longer the same stream')

    def test_a_caller_may_define_same_stream_itself(self):
        """The group page also accepts one stored duplicate cluster (dev/changelog/1175)."""
        src, keeper = _ch(1, cluster=7), _ch(2, url='http://other.test/live/1', cluster=7)
        self.assertEqual(transfer_keeper_problem(src, keeper), 'Kept channel is no longer the same stream')
        same_cluster = lambda a, b: a.duplicate_cluster_id == b.duplicate_cluster_id  # noqa: E731
        self.assertIsNone(transfer_keeper_problem(src, keeper, same_stream=same_cluster))

    def test_the_three_routes_call_it_and_none_keeps_its_own_copy(self):
        for rel in ('app/routes/guide.py', 'app/routes/channel_tests.py', 'app/routes/channel_groups.py'):
            with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
                src = fh.read()
            self.assertIn('transfer_keeper_problem(', src, rel)
            self.assertNotIn("'No valid channel to keep", src, rel)
            self.assertNotIn('Server re-validates the keeper', src, rel)


if __name__ == '__main__':
    unittest.main(verbosity=2)
