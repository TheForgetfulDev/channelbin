"""The recording signature the Dashboard's recording regions re-render on (dev/changelog/1143).

routes/dashboard.py::recording_signature() rides on /api/nav-status, and the Dashboard
renders the one its rows were read at; the page swaps its tiles, header count, timeline
blob and record sections when the two differ. Guards dev/docs/BUGS.md 2026-09-26 @
08:50:16 PM: a recording that started while / was open stayed under Upcoming as Scheduled.

So the signature has to move for every change to what those regions show - a part that
misses one is a change the open page never shows - and stay still under everything SSE
already keeps current, or every open Dashboard re-renders on every poll while something
captures. The held-start and delete cases drive the real writers; the others set the
column every writer of that change sets.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_recording_signature
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_recording  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Recording, REC_STATUS_COMPLETED, REC_STATUS_CONVERTING, REC_STATUS_FAILED,
    REC_STATUS_IN_PROGRESS, REC_STATUS_SCHEDULED,
)
from app.routes.dashboard import recording_signature  # noqa: E402


class RecordingSignatureTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        now = datetime.utcnow()
        self.sched = make_recording(status=REC_STATUS_SCHEDULED, name='Later',
                                    start_time=now + timedelta(minutes=5),
                                    stop_time=now + timedelta(minutes=65)).id
        self.live = make_recording(status=REC_STATUS_IN_PROGRESS, name='Now',
                                   started_at=now - timedelta(minutes=10)).id
        self.done = make_recording(status=REC_STATUS_COMPLETED, name='Earlier').id
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        self.ctx.pop()
        self.t.cleanup()

    def _sig(self):
        db.session.expire_all()
        return recording_signature()

    def _set(self, rid, **cols):
        rec = db.session.get(Recording, rid)
        for k, v in cols.items():
            setattr(rec, k, v)
        db.session.commit()

    def _moves(self, change):
        before = self._sig()
        change()
        self.assertNotEqual(before, self._sig())

    def _still(self, change):
        before = self._sig()
        change()
        self.assertEqual(before, self._sig())

    # ── what the regions show ────────────────────────────────────────────

    def test_a_recording_starting_moves_it(self):
        """The defect itself: SCHEDULED to IN_PROGRESS changes which section the row is in."""
        self._moves(lambda: self._set(self.sched, status=REC_STATUS_IN_PROGRESS,
                                      started_at=datetime.utcnow()))

    def test_a_recording_finishing_moves_it(self):
        self._moves(lambda: self._set(self.live, status=REC_STATUS_COMPLETED))

    def test_a_recording_scheduled_elsewhere_moves_it(self):
        def schedule():
            make_recording(status=REC_STATUS_SCHEDULED, name='New one',
                           start_time=datetime.utcnow() + timedelta(hours=2),
                           stop_time=datetime.utcnow() + timedelta(hours=3))
            db.session.commit()
        self._moves(schedule)

    def test_a_scheduled_recording_edited_elsewhere_moves_it(self):
        self._moves(lambda: self._set(self.sched, name='Later, renamed'))
        self._moves(lambda: self._set(
            self.sched, start_time=datetime.utcnow() + timedelta(minutes=30)))

    def test_a_live_recording_extended_moves_it(self):
        self._moves(lambda: self._set(
            self.live, stop_time=datetime.utcnow() + timedelta(hours=4)))

    def test_a_held_start_moves_it_and_so_does_its_end(self):
        """The Upcoming row's middle cell becomes "Waiting for" with no status change and no
        SSE frame, so only the signature can tell the page (dev/changelog/1141)."""
        from app.recorder import _note_start_deferred, end_capture_wait
        self._moves(lambda: _note_start_deferred(self.sched, 'a free connection slot'))
        self._moves(lambda: end_capture_wait(self.sched))

    def test_a_conversion_parking_moves_it(self):
        self._set(self.live, status=REC_STATUS_CONVERTING)
        self._moves(lambda: self._set(self.live, postprocess_waiting_since=datetime.utcnow(),
                                      postprocess_waiting_on_name='Other'))

    def test_deleting_a_finished_recording_moves_it(self):
        """The one change that touches no unfinished row - only the table's count sees it."""
        def delete():
            resp = self.client.post(f'/recordings/{self.done}/delete-json', json={})
            self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self._moves(delete)

    def test_a_retry_out_of_failed_moves_it(self):
        self._set(self.live, status=REC_STATUS_FAILED)
        self._moves(lambda: self._set(self.live, status=REC_STATUS_SCHEDULED))

    # ── what it must ignore ──────────────────────────────────────────────

    def test_nothing_changing_leaves_it_still(self):
        self.assertEqual(self._sig(), self._sig())

    def test_what_sse_keeps_current_leaves_it_still(self):
        """Bytes, stalls and progress move every second while something captures or
        converts. A signature that moved with them would re-render every open Dashboard
        on every nav poll."""
        self._still(lambda: self._set(self.live, total_stall_count=7,
                                      total_downtime_seconds=42.0,
                                      updated_at=datetime.utcnow() + timedelta(seconds=5)))
        self._set(self.sched, status=REC_STATUS_CONVERTING)
        self._still(lambda: self._set(self.sched, conversion_progress_pct=55.0,
                                      conversion_eta_seconds=120))

    def test_a_finished_recording_edited_leaves_it_still(self):
        """The Dashboard shows no finished recording, so renaming one changes nothing it
        draws."""
        self._still(lambda: self._set(self.done, name='Earlier, renamed'))

    # ── the wire ─────────────────────────────────────────────────────────

    def test_nav_status_carries_it_and_the_dashboard_renders_the_same_string(self):
        sig = self._sig()
        nav = self.client.get('/api/nav-status').get_json()
        self.assertEqual(nav['recording_signature'], sig)
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn(f'data-rec-sig="{sig}"', page)


if __name__ == '__main__':
    unittest.main()
