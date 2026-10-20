"""The app's live-update streams answer at once, and let go when the client leaves.

dev/docs/BUGS.md 2026-06-30 @ 09:00:55 AM: both dashboard SSE generators wrote nothing until
their first 15-second queue timeout produced a keepalive, and EventSource.onopen fires only on
the first byte of the body - so the status badge sat on "Connecting..." for 15 seconds over a
connection that was healthy the whole time. dev/docs/BUGS.md 2026-10-19 @ 04:39:29 PM: the Logs
page's stream had the same defect, and the dashboard fix had put its first yield outside the
try whose finally unsubscribes, so a client that left within 15 seconds leaked its queue.

The route smoke sweep skips these endpoints because they never end, so nothing else reads
their first chunk or closes one.
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app import events as ev  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402


def _subscriber_count():
    return sum(len(v) for v in ev._subscribers.values())


class SseFirstByteTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        rec = seed.make_recording(status='IN_PROGRESS')
        db.session.commit()
        self.recording_id = rec.id

    def tearDown(self):
        self.t.cleanup()

    def _first_chunk(self, url):
        """The first body chunk and how long it took, closing the stream right after - the
        same thing a browser does when the user navigates away from a page within seconds."""
        # Timed from before the request: the test client pulls the first chunk itself
        # while it waits for the response headers, exactly as a WSGI server does.
        started = time.monotonic()
        resp = self.t.client.get(url, buffered=False)
        try:
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.mimetype, 'text/event-stream')
            chunk = next(iter(resp.response))
            elapsed = time.monotonic() - started
        finally:
            resp.close()
        if isinstance(chunk, bytes):
            chunk = chunk.decode()
        return chunk, elapsed

    def _assert_answers_at_once(self, url):
        chunk, elapsed = self._first_chunk(url)
        self.assertTrue(chunk.startswith(':'), f'first chunk was not an SSE comment: {chunk!r}')
        self.assertLess(elapsed, 1.0)

    def test_the_all_recordings_stream_writes_before_any_event_or_keepalive(self):
        self._assert_answers_at_once('/api/stream')

    def test_the_single_recording_stream_writes_before_any_event_or_keepalive(self):
        self._assert_answers_at_once(f'/api/stream/{self.recording_id}')

    def test_the_logs_stream_writes_before_any_log_line_or_keepalive(self):
        """Over a log file that exists and stays quiet - a missing one answers at once on
        a branch of its own and proves nothing."""
        quiet = os.path.join(self.t._tmpdir, 'quiet.log')
        with open(quiet, 'w') as f:
            f.write('')
        with patch('app.routes.logs._log_file_path', return_value=quiet):
            self._assert_answers_at_once('/api/logs/stream')

    def test_leaving_before_the_first_event_unsubscribes_the_stream(self):
        """A queue left subscribed is one publish() fills to its cap and then walks past on
        every event for the life of the process."""
        for url in ('/api/stream', f'/api/stream/{self.recording_id}'):
            with self.subTest(url=url):
                before = _subscriber_count()
                self._first_chunk(url)
                self.assertEqual(_subscriber_count(), before)


if __name__ == '__main__':
    unittest.main()
