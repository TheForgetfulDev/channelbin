"""create_alert() tells the truth about app contexts (dev/docs/BUGS.md 2026-10-20).

Its docstring promised "safe to call from any thread (uses Flask app context internally)",
and nothing in it pushed one: every caller works because it already holds a context, and a
background thread that took the sentence at its word got RuntimeError from the first query,
which _write_alert_row()'s broad except turned into a logged "Failed to write alert row" -
the alert for a real problem quietly never landed. Now the docstring says callers must hold
a context, and an off-context call is refused up front with a WARNING that names the alert.

  python3 -m unittest tests.test_alert_off_context
"""
import threading
import unittest

from app import alerts
from app.alerts import STORAGE_PATH_UNUSABLE, create_alert
from app.database import Alert
from tests.support.app import make_test_app


class OffContextTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_a_bare_thread_gets_a_warning_naming_the_alert_and_no_row(self):
        raised = []

        def worker():
            try:
                create_alert(STORAGE_PATH_UNUSABLE, 'DVR output directory is unusable',
                             body='from a thread with no context', source='/nowhere')
            except Exception as exc:  # the refusal must be a log line, not an exception
                raised.append(exc)

        with self.assertLogs('app.alerts', level='WARNING') as logs:
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(10)
        self.assertEqual(raised, [])
        self.assertTrue(any('no app context' in line and STORAGE_PATH_UNUSABLE in line
                            for line in logs.output), logs.output)
        with self.t.app.app_context():
            self.assertEqual(Alert.query.count(), 0)

    def test_the_same_call_under_a_context_writes_the_row(self):
        with self.t.app.app_context():
            create_alert(STORAGE_PATH_UNUSABLE, 'DVR output directory is unusable',
                         body='under a context', source='/nowhere')
            self.assertEqual(Alert.query.filter_by(alert_type=STORAGE_PATH_UNUSABLE).count(), 1)

    def test_the_docstring_no_longer_promises_a_context(self):
        doc = alerts.create_alert.__doc__
        self.assertNotIn('Safe to call from any thread', doc)
        self.assertIn('must hold an app context', doc)


if __name__ == '__main__':
    unittest.main(verbosity=2)
