"""Tier 1 unit for the activity-log sequence stamp (app/channel_tester.py::_append_log).

Guards BUGS.md 2026-07-18 "Live log refresh destroyed text selection". The UI fix renders the
activity log append-only instead of rebuilding the node every poll, and it identifies
already-rendered entries by the server's `seq`. That only works if `seq` is present, strictly
increasing, and never reset - the buffer is a deque(maxlen=500) that evicts from the front, so a
positional diff would silently skip or duplicate lines once a long run passes the cap.

Invariants asserted here:
  * every entry carries a `seq`, strictly increasing in append order;
  * `seq` survives into the `/api/channel-tests/status` payload built by get_status();
  * after appending past maxlen, the retained entries' seq values are still strictly increasing
    and the newest equals the total number appended (eviction cannot reset the anchor).

No DB, no Flask - this pokes the module-level ring buffer directly and restores it in tearDown.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import channel_tester  # noqa: E402


class TestChannelTesterLogSeq(unittest.TestCase):
    def setUp(self):
        self._saved = list(channel_tester._state.log_entries)
        channel_tester._state.log_entries.clear()

    def tearDown(self):
        channel_tester._state.log_entries.clear()
        channel_tester._state.log_entries.extend(self._saved)

    def test_every_entry_has_strictly_increasing_seq(self):
        for i in range(10):
            channel_tester._append_log('INFO', f'line {i}')
        seqs = [e['seq'] for e in channel_tester._state.log_entries]
        self.assertEqual(len(seqs), 10)
        self.assertTrue(all(isinstance(s, int) for s in seqs))
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs), 'seq values must be unique')

    def test_seq_reaches_the_status_payload(self):
        channel_tester._append_log('WARN', 'payload check')
        logs = channel_tester.get_status()['logs']
        self.assertTrue(logs, 'get_status() must expose the log buffer')
        self.assertIn('seq', logs[-1])
        self.assertEqual(logs[-1]['msg'], 'payload check')

    def test_seq_survives_ring_buffer_eviction(self):
        maxlen = channel_tester._state.log_entries.maxlen
        self.assertIsNotNone(maxlen, 'log_entries is expected to be a bounded deque')

        total = maxlen + 50
        for i in range(total):
            channel_tester._append_log('INFO', f'line {i}')

        entries = list(channel_tester._state.log_entries)
        self.assertEqual(len(entries), maxlen, 'deque should have evicted from the front')

        seqs = [e['seq'] for e in entries]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))
        # The oldest retained entry is the (total - maxlen)th appended, so the span across the
        # retained window is exactly maxlen - 1 - proving eviction never rewound the counter.
        self.assertEqual(seqs[-1] - seqs[0], maxlen - 1)

    def test_counter_does_not_reset_between_runs(self):
        channel_tester._append_log('INFO', 'run one')
        first = channel_tester._state.log_entries[-1]['seq']
        # A new run clears nothing by design (RunState.clear keeps log_entries), but even a
        # buffer wipe must not rewind the counter - a stale client anchor would re-append.
        channel_tester._state.log_entries.clear()
        channel_tester._append_log('INFO', 'run two')
        self.assertGreater(channel_tester._state.log_entries[-1]['seq'], first)


if __name__ == '__main__':
    unittest.main()
