"""An alert that clears itself stays on the Alerts page for a day (dev/changelog/1293).

The second first-time-user test (finding 30) watched the Alerts badge come and go during a
stall, then opened the page to "No alerts": the app had taken the alert down by stamping
`dismissed_at`, the same column a person's Dismiss writes, and the default view hides
every dismissed row. `Alert.cleared_by_app` is the recorded difference between "it went
away" and "someone hid it", and the page lists the last 24 hours of the first kind under
"Cleared recently".

What a careless edit would undo:
  * every place the app takes an alert down goes through `alerts.mark_cleared()` - a new
    writer of `dismissed_at` in app/ fails the scan below until it picks a side;
  * a deleted recording's alerts are taken down, never "cleared": nothing recovered;
  * a person's Dismiss or Ignore never marks a row cleared, and never restamps one that
    already cleared itself.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_alerts_cleared_recently
"""
import os
import re
import unittest
from datetime import datetime, timedelta

from tests.support.app import make_test_app
from tests.support import seed
from app import db
from app import alerts as alerts_mod
from app.database import Alert

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Files that may assign dismissed_at directly: the one app-side writer and the person's
#: routes. migrations.py rewrites history and is exempt as everywhere else.
_DISMISS_WRITERS = {'app/alerts.py', 'app/routes/alerts.py', 'app/migrations.py'}


def _alert(**kw):
    kw.setdefault('alert_type', 'RECORDING_NOT_CAPTURING')
    kw.setdefault('severity', 'ERROR')
    kw.setdefault('title', 'Recording not capturing')
    kw.setdefault('source', 'src-1')
    kw.setdefault('created_at', datetime.utcnow() - timedelta(minutes=5))
    a = Alert(**kw)
    db.session.add(a)
    db.session.commit()
    return a


class ClearPathTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)

    def test_a_standing_alert_cleared_by_type_and_source_is_marked(self):
        a = _alert()
        alerts_mod.dismiss_open_alerts(a.alert_type, a.source)
        db.session.expire_all()
        row = db.session.get(Alert, a.id)
        self.assertIsNotNone(row.dismissed_at)
        self.assertTrue(row.cleared_by_app)

    def test_a_recording_alert_cleared_by_id_is_marked(self):
        rec = seed.make_recording(status='IN_PROGRESS')
        db.session.commit()
        a = _alert(recording_id=rec.id)
        alerts_mod.dismiss_open_alerts_for_recording(rec.id)
        db.session.expire_all()
        self.assertTrue(db.session.get(Alert, a.id).cleared_by_app)

    def test_a_deleted_recordings_alerts_are_not_cleared(self):
        """The person deleted the recording; nothing about it recovered."""
        rec = seed.make_recording(status='IN_PROGRESS')
        db.session.commit()
        a = _alert(recording_id=rec.id)
        alerts_mod.detach_recording_alerts(rec.id)
        db.session.commit()
        db.session.expire_all()
        row = db.session.get(Alert, a.id)
        self.assertIsNotNone(row.dismissed_at)
        self.assertIsNone(row.recording_id)
        self.assertFalse(row.cleared_by_app)

    def test_an_already_dismissed_row_keeps_its_owner_and_time(self):
        then = datetime.utcnow() - timedelta(hours=2)
        a = _alert(dismissed_at=then)
        self.assertFalse(alerts_mod.mark_cleared([a]))
        self.assertEqual(a.dismissed_at, then)
        self.assertFalse(a.cleared_by_app)

    def test_a_persons_dismiss_is_not_a_clear(self):
        a = _alert(alert_type='TEST', read_at=datetime.utcnow())
        resp = self.t.app.test_client().post(f'/api/alerts/{a.id}/dismiss')
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        row = db.session.get(Alert, a.id)
        self.assertIsNotNone(row.dismissed_at)
        self.assertFalse(row.cleared_by_app)

    def test_ignoring_a_cleared_alert_keeps_when_it_cleared(self):
        cleared_at = datetime.utcnow() - timedelta(hours=3)
        a = _alert(dismissed_at=cleared_at, cleared_by_app=True)
        resp = self.t.app.test_client().post(f'/api/alerts/{a.id}/ignore')
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        row = db.session.get(Alert, a.id)
        self.assertEqual(row.dismissed_at, cleared_at)
        self.assertTrue(row.cleared_by_app)


class DismissWriterScanTests(unittest.TestCase):

    def test_only_the_named_writers_assign_dismissed_at(self):
        offenders = []
        pat = re.compile(r"\.dismissed_at\s*=(?!=)|['\"]dismissed_at['\"]\s*:")
        for dirpath, _dirs, files in os.walk(os.path.join(REPO, 'app')):
            for f in files:
                if not f.endswith('.py'):
                    continue
                path = os.path.join(dirpath, f)
                rel = os.path.relpath(path, REPO)
                if rel in _DISMISS_WRITERS:
                    continue
                with open(path) as fh:
                    for n, line in enumerate(fh, 1):
                        if pat.search(line):
                            offenders.append(f'{rel}:{n}')
        self.assertEqual(offenders, [], 'take an alert down through alerts.mark_cleared(), '
                                        'or say why a person did it')


class AlertsPageTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        self.client = self.t.app.test_client()

    def _page(self, query=''):
        resp = self.client.get('/alerts' + query)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_an_alert_that_cleared_itself_is_still_on_the_page(self):
        _alert(title='Came and went', dismissed_at=datetime.utcnow() - timedelta(minutes=1),
               cleared_by_app=True)
        html = self._page()
        self.assertIn('id="al-card-cleared"', html)
        self.assertIn('Came and went', html)
        self.assertIn('Cleared itself', html)
        self.assertNotIn('No alerts', html)

    def test_it_says_how_long_the_alert_stood(self):
        now = datetime.utcnow()
        _alert(created_at=now - timedelta(minutes=4), dismissed_at=now, cleared_by_app=True)
        self.assertRegex(self._page(), r'4m after it was raised')

    def test_a_cleared_row_draws_no_unread_edge(self):
        _alert(dismissed_at=datetime.utcnow(), cleared_by_app=True)
        html = self._page()
        card = html[html.index('id="al-card-cleared"'):]
        self.assertNotRegex(card.split('id="al-card-past"')[0], r'class="al-row unread')

    def test_it_leaves_after_a_day(self):
        _alert(title='Long gone', dismissed_at=datetime.utcnow() - timedelta(hours=25),
               cleared_by_app=True)
        html = self._page()
        self.assertNotIn('id="al-card-cleared"', html)
        self.assertNotIn('Long gone', html)

    def test_a_persons_dismissal_is_not_listed(self):
        _alert(title='Hidden by hand', dismissed_at=datetime.utcnow())
        html = self._page()
        self.assertNotIn('id="al-card-cleared"', html)
        self.assertNotIn('Hidden by hand', html)

    def test_with_dismissed_shown_it_is_listed_once(self):
        _alert(title='Only once please', dismissed_at=datetime.utcnow(), cleared_by_app=True)
        html = self._page('?include_dismissed=1')
        self.assertEqual(html.count('Only once please'), 1)
        self.assertIn('id="al-card-cleared"', html)

    def test_the_intro_says_cleared_alerts_stay_a_day(self):
        self.assertIn('Cleared recently', self._page())


if __name__ == '__main__':
    unittest.main()
