"""Tier 2 - a parked post-processing chain says it is waiting, and says what for
(dev/changelog/954).

Recording 17, 2026-09-13: the row sat at ANALYZING for hours while its detail page led with
"the recorded file (8.1 GB) is being checked for damage before conversion. This reads the
whole file, so it takes longer on a large recording." The damage check had finished; the row
was parked, and the app knew it - the accurate sentence was already in the event log
(CONVERSION_YIELDED). The second yield site has the same defect one status along: a paused
conversion published CONVERTING, so the page read "Conversion in progress" with an ETA that
had stopped moving. dev/changelog/952 made the pause a real SIGSTOP, which turned that ETA
from unhelpful into wrong.

The invariants, in the order the work happens:

  (a) The park is ONE fact in three columns and set_postprocess_wait() is its only writer in
      app/. A stamp with no blocker renders a wait naming nobody; a blocker with no stamp is
      a finished recording still claiming to be waiting.
  (b) Both yield sites record WHO they are waiting on, not just that they are waiting.
  (c) Every path that clears the park clears all three columns: the resume, a run that ends
      while suspended, a cancelled pre-start wait, the cancel-conversion route, and the
      startup sweep.
  (d) The recording detail page leads with the wait at BOTH statuses, names the blocker, and
      drops the frozen ETA - while an unparked row still renders its phase exactly as before.
  (e) The recordings list badges a parked row WAITING with no pulse and names the blocker on
      the relative line, without Recording.status moving. The status must not move: the
      startup sweep, the collision query and the cancel route all branch on it.
  (f) The Dashboard's background-task row says paused rather than naming the phase.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_postprocess_wait_surface
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Recording, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING, REC_STATUS_IN_PROGRESS,
    REC_STATUS_ABORTED, REC_STATUS_CONCATENATING, REC_STATUS_COMPLETED,
)
import app.postprocessor as ppmod  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import make_account, make_channel, make_recording  # noqa: E402

BLOCKER = 'Supercars: Race 29'


# ── (a) one fact, one writer ──────────────────────────────────────────────────
class WriterMovesAllThreeColumnsTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_parking_records_the_blocker_and_what_it_is_doing(self):
        with self.t.app.app_context():
            rec = make_recording(status=REC_STATUS_ANALYZING, name='parked')
            blocker = make_recording(status=REC_STATUS_IN_PROGRESS, name=BLOCKER)
            db.session.commit()
            ppmod.set_postprocess_wait(rec, blocker)
            db.session.commit()
            self.assertIsNotNone(rec.postprocess_waiting_since)
            self.assertEqual(BLOCKER, rec.postprocess_waiting_on_name)
            self.assertEqual('is in progress', rec.postprocess_waiting_on_state)

    def test_the_state_phrase_is_the_one_the_event_log_already_uses(self):
        # _conflict_phrase is the single vocabulary; a second hand-typed copy would let the
        # page and the event disagree about the same recording.
        with self.t.app.app_context():
            rec = make_recording(status=REC_STATUS_CONVERTING, name='parked')
            blocker = make_recording(status=REC_STATUS_CONCATENATING, name=BLOCKER)
            db.session.commit()
            ppmod.set_postprocess_wait(rec, blocker)
            self.assertEqual(ppmod._conflict_phrase(blocker), rec.postprocess_waiting_on_state)

    def test_clearing_clears_all_three(self):
        with self.t.app.app_context():
            rec = make_recording(status=REC_STATUS_CONVERTING, name='parked')
            blocker = make_recording(status=REC_STATUS_IN_PROGRESS, name=BLOCKER)
            db.session.commit()
            ppmod.set_postprocess_wait(rec, blocker)
            ppmod.set_postprocess_wait(rec, None)
            db.session.commit()
            self.assertIsNone(rec.postprocess_waiting_since)
            self.assertIsNone(rec.postprocess_waiting_on_name)
            self.assertIsNone(rec.postprocess_waiting_on_state)

    def test_a_stamp_never_exists_without_a_blocker(self):
        with self.t.app.app_context():
            rec = make_recording(status=REC_STATUS_ANALYZING, name='parked')
            blocker = make_recording(status=REC_STATUS_IN_PROGRESS, name=BLOCKER)
            db.session.commit()
            for conflict in (blocker, None, blocker, None):
                ppmod.set_postprocess_wait(rec, conflict)
                self.assertEqual(rec.postprocess_waiting_since is None,
                                 rec.postprocess_waiting_on_name is None,
                                 'the stamp and the blocker disagreed')


# ── (b)/(c) the yield sites and every way out ─────────────────────────────────
class PersistAndClearTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_persist_writes_the_blocker_through_its_own_commit(self):
        with self.t.app.app_context():
            rec = make_recording(status=REC_STATUS_ANALYZING, name='parked')
            blocker = make_recording(status=REC_STATUS_IN_PROGRESS, name=BLOCKER)
            db.session.commit()
            rid = rec.id
            ppmod._persist_postprocess_waiting(rid, blocker)
            db.session.expire_all()
            self.assertEqual(BLOCKER, db.session.get(Recording, rid).postprocess_waiting_on_name)

    def test_persist_with_none_clears_the_blocker_too(self):
        with self.t.app.app_context():
            rec = make_recording(status=REC_STATUS_ANALYZING, name='parked')
            blocker = make_recording(status=REC_STATUS_IN_PROGRESS, name=BLOCKER)
            db.session.commit()
            rid = rec.id
            ppmod._persist_postprocess_waiting(rid, blocker)
            ppmod._persist_postprocess_waiting(rid, None)
            db.session.expire_all()
            row = db.session.get(Recording, rid)
            self.assertIsNone(row.postprocess_waiting_since)
            self.assertIsNone(row.postprocess_waiting_on_name)

    def test_cancel_conversion_clears_the_whole_park(self):
        # The stranded-row branch: no live chain exists to clear it, so a finished recording
        # would keep claiming to be waiting forever.
        from app.routes.recordings import _cancel_conversion
        with self.t.app.app_context():
            rec = make_recording(status=REC_STATUS_CONVERTING, name='parked')
            blocker = make_recording(status=REC_STATUS_IN_PROGRESS, name=BLOCKER)
            db.session.commit()
            ppmod.set_postprocess_wait(rec, blocker)
            db.session.commit()
            rid = rec.id
            with mock.patch('app.postprocessor.request_cancel_conversion', return_value=False):
                _cancel_conversion(rid, rec)
            db.session.expire_all()
            row = db.session.get(Recording, rid)
            self.assertEqual(REC_STATUS_ABORTED, row.status)
            self.assertIsNone(row.postprocess_waiting_since)
            self.assertIsNone(row.postprocess_waiting_on_name)
            self.assertIsNone(row.postprocess_waiting_on_state)


class StartupSweepTests(unittest.TestCase):
    """No parked chain survives a restart, so every park present at startup is orphaned -
    and one left half-cleared still renders a wait on the page."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=True)

    def tearDown(self):
        self.t.cleanup()

    def test_startup_clears_the_blocker_not_just_the_stamp(self):
        from app.scheduler import resume_in_progress_recordings
        with self.t.app.app_context():
            rec = make_recording(status=REC_STATUS_ANALYZING, name='parked')
            blocker = make_recording(status=REC_STATUS_IN_PROGRESS, name=BLOCKER)
            db.session.commit()
            ppmod.set_postprocess_wait(rec, blocker)
            rec.postprocess_waiting_since = datetime.utcnow() - timedelta(hours=3)
            rec.output_path = os.path.join(self.t._tmpdir, 'gone.ts')
            # The blocker finished while the service was down. It is left out of the sweep's
            # own resume path deliberately: what is under test is the park being cleared,
            # not a capture being relaunched.
            blocker.status = REC_STATUS_COMPLETED
            db.session.commit()
            rid = rec.id
            with mock.patch('app.concatenator.do_concatenation'):
                resume_in_progress_recordings(self.t.app)
            db.session.expire_all()
            row = db.session.get(Recording, rid)
            self.assertIsNone(row.postprocess_waiting_since)
            self.assertIsNone(row.postprocess_waiting_on_name)
            self.assertIsNone(row.postprocess_waiting_on_state)


# ── (d) the recording detail page ─────────────────────────────────────────────
class DetailPageStripTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _page(self, status, *, parked=True, pct=None, out_size=None, eta=None):
        with self.t.app.app_context():
            acc = make_account(name='Acct One')
            ch = make_channel(acc, name='Channel One')
            rec = make_recording(status=status, name='parked show', channel_id=ch.id)
            rec.final_file_size = 8_100_000_000
            rec.conversion_progress_pct = pct
            rec.conversion_out_size = out_size
            rec.conversion_eta_seconds = eta
            if parked:
                blocker = make_recording(status=REC_STATUS_IN_PROGRESS, name=BLOCKER)
                db.session.commit()
                ppmod.set_postprocess_wait(rec, blocker)
            db.session.commit()
            rid = rec.id
        return self.client.get(f'/recordings/{rid}').get_data(as_text=True)

    def _list_page(self):
        return self.client.get('/recordings').get_data(as_text=True)

    def test_parked_before_conversion_leads_with_the_wait(self):
        html = self._page(REC_STATUS_ANALYZING)
        self.assertIn('The conversion will start automatically when no recordings are active.',
                      html)
        self.assertIn(BLOCKER, html)

    def test_parked_before_conversion_does_not_claim_the_damage_scan_is_running(self):
        html = self._page(REC_STATUS_ANALYZING)
        self.assertNotIn('is being checked for damage before conversion', html)

    def test_an_unparked_analyzing_row_still_describes_the_damage_scan(self):
        html = self._page(REC_STATUS_ANALYZING, parked=False)
        self.assertIn('is being checked for damage before conversion', html)
        self.assertNotIn('will start automatically when no recordings are active', html)

    def test_parked_mid_conversion_says_paused_and_where(self):
        html = self._page(REC_STATUS_CONVERTING, pct=66.4, out_size=4_600_000_000, eta=7200)
        self.assertIn('Paused at 66%', html)
        self.assertIn('Conversion will resume automatically when no recordings are active.',
                      html)
        self.assertIn(BLOCKER, html)

    def test_parked_mid_conversion_drops_the_frozen_eta(self):
        # The ffmpeg is SIGSTOPped, so the persisted ETA cannot advance. A stopped clock
        # shown as a countdown is the thing that misleads.
        html = self._page(REC_STATUS_CONVERTING, pct=66.4, out_size=4_600_000_000, eta=7200)
        self.assertNotIn('Conversion in progress', html)
        self.assertNotIn('120 min left', html)

    def test_parked_mid_conversion_says_the_encoded_part_is_kept(self):
        html = self._page(REC_STATUS_CONVERTING, pct=66.4, out_size=4_600_000_000)
        self.assertIn('written so far and kept', html)

    def test_an_unparked_converting_row_still_shows_progress_and_eta(self):
        html = self._page(REC_STATUS_CONVERTING, parked=False, pct=66.4, eta=7200)
        self.assertIn('Conversion in progress', html)
        self.assertIn('120 min left', html)


# ── (e) the recordings list ───────────────────────────────────────────────────
class ListRowTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _row(self, status, *, parked=True, pct=None):
        from app.routes.recordings import _index_row
        from app.tz_utils import get_display_tz
        # A request context, not a bare app context: _index_row builds url_for() links.
        with self.t.app.test_request_context('/recordings'):
            acc = make_account(name='Acct One')
            ch = make_channel(acc, name='Channel One')
            rec = make_recording(status=status, name='parked show', channel_id=ch.id)
            rec.conversion_progress_pct = pct
            if parked:
                blocker = make_recording(status=REC_STATUS_IN_PROGRESS, name=BLOCKER)
                db.session.commit()
                ppmod.set_postprocess_wait(rec, blocker)
            db.session.commit()
            return _index_row(rec, datetime.utcnow(), get_display_tz(), set()), rec

    def test_a_parked_row_badges_waiting_and_stops_pulsing(self):
        row, _ = self._row(REC_STATUS_ANALYZING)
        self.assertEqual('WAITING', row['badge_label'])
        self.assertFalse(row['badge_pulse'])

    def test_a_parked_row_names_the_blocker(self):
        row, _ = self._row(REC_STATUS_ANALYZING)
        self.assertIn(BLOCKER, row['rel'])

    def test_a_parked_conversion_reports_the_percentage_it_paused_at(self):
        row, _ = self._row(REC_STATUS_CONVERTING, pct=66.4)
        self.assertIn('paused at 66%', row['rel'])
        self.assertIn(BLOCKER, row['rel'])

    def test_the_stored_status_does_not_move(self):
        # The badge is a display derivation from two stored facts. Were it a real status,
        # scheduler.py's CONCATENATING/ANALYZING resume sweep would no longer match the row
        # and a restart would strand the chain instead of resuming it.
        row, rec = self._row(REC_STATUS_ANALYZING)
        self.assertEqual(REC_STATUS_ANALYZING, rec.status)
        self.assertEqual(REC_STATUS_ANALYZING, row['status'])

    def test_an_unparked_converting_row_still_reads_as_converting(self):
        row, _ = self._row(REC_STATUS_CONVERTING, parked=False, pct=66.4)
        self.assertEqual('CONVERTING', row['badge_label'])
        self.assertTrue(row['badge_pulse'])
        self.assertIn('converting', row['rel'])

    def test_an_unparked_analyzing_row_still_reads_as_ended(self):
        row, _ = self._row(REC_STATUS_ANALYZING, parked=False)
        self.assertEqual('ANALYZING', row['badge_label'])
        self.assertIn('ago', row['rel'])


# ── (f) the Dashboard background-task row ─────────────────────────────────────
class DashboardBackgroundTaskTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _tasks(self, status, *, parked=True, extra_name=None):
        with self.t.app.app_context():
            acc = make_account(name='Acct One')
            ch = make_channel(acc, name='Channel One')
            rec = make_recording(status=status, name='parked show', channel_id=ch.id)
            if extra_name:
                make_recording(status=status, name=extra_name, channel_id=ch.id)
            if parked:
                blocker = make_recording(status=REC_STATUS_IN_PROGRESS, name=BLOCKER)
                db.session.commit()
                ppmod.set_postprocess_wait(rec, blocker)
            db.session.commit()
        payload = self.client.get('/api/activity/status').get_json()
        return payload['background']['tasks']

    def test_a_parked_conversion_reads_as_paused(self):
        tasks = self._tasks(REC_STATUS_CONVERTING)
        parked = [t for t in tasks if t['label'] == 'Conversion paused']
        self.assertEqual(1, len(parked), f'no paused row in {tasks}')
        self.assertIn(BLOCKER, parked[0]['detail'])

    def test_a_parked_pre_conversion_row_reads_as_waiting(self):
        tasks = self._tasks(REC_STATUS_ANALYZING)
        self.assertIn('Waiting to convert', [t['label'] for t in tasks])

    def test_an_unparked_row_in_the_same_status_keeps_its_phase_label(self):
        # The parked wording is per row. Rebinding the loop's own label would give every
        # later recording in the same status the first one's wording.
        tasks = self._tasks(REC_STATUS_CONVERTING, extra_name='busy show')
        labels = sorted(t['label'] for t in tasks)
        self.assertEqual(['Conversion paused', 'Converting'], labels)

    def test_an_unparked_conversion_keeps_its_phase_label(self):
        tasks = self._tasks(REC_STATUS_CONVERTING, parked=False)
        self.assertEqual(['Converting'], [t['label'] for t in tasks])


if __name__ == '__main__':
    unittest.main()
