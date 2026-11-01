"""The record and edit forms keep which instant a time means across a DST change
(dev/docs/BUGS.md 2026-10-31 @ 11:27:31 PM ET), and an edit made against a window that
moved after its dialog opened is refused (dev/docs/BUGS.md 2026-10-31 @ 11:27:32 PM ET).

The dialog posts bare wall text, and on the night DST ends "01:30" is two instants an hour
apart; the server read every one as the first, so a program in the second 1 AM hour was
recorded an hour early, a recording ending there was cut short, and an edit moved it. The
form now also posts the instant each field was filled from (`start_time_utc`/
`stop_time_utc`), which app/tz_utils.py::parse_form_time prefers while it still shows as the
posted text. Separately, auto-record retrack moves a SCHEDULED recording's times, and a
dialog opened before a move used to write the old window back; the edit now carries the
window it opened on (`opened_start_utc`/`opened_stop_utc`) and gets a 409 naming the current
one when the row no longer holds it.

The US fall-back used here is 2027-11-07 (06:00 UTC), far enough ahead that no create path
moves a past start to "now".

  python3 -m unittest tests.test_dst_record_form
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, tz_utils  # noqa: E402
from app.database import Recording, REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS  # noqa: E402

NY = ZoneInfo('America/New_York')
FIRST_130 = datetime(2027, 11, 7, 5, 30)    # 1:30 AM EDT
SECOND_130 = datetime(2027, 11, 7, 6, 30)   # 1:30 AM EST


def _ny():
    return mock.patch.object(tz_utils, 'get_display_tz', return_value=NY)


class ParseFormTimeTests(unittest.TestCase):
    def test_wall_text_alone_reads_the_first_pass(self):
        with _ny():
            self.assertEqual(tz_utils.parse_form_time('2027-11-07T01:30'), FIRST_130)

    def test_an_instant_showing_as_the_same_text_wins(self):
        with _ny():
            self.assertEqual(
                tz_utils.parse_form_time('2027-11-07T01:30', '2027-11-07T06:30:00.000Z'),
                SECOND_130)
            self.assertEqual(
                tz_utils.parse_form_time('2027-11-07T01:30', '2027-11-07T01:30:00-05:00'),
                SECOND_130)

    def test_an_instant_the_text_no_longer_shows_is_ignored(self):
        with _ny():
            # 07:30Z is 2:30 AM EST: a field typed into after it was filled.
            self.assertEqual(
                tz_utils.parse_form_time('2027-11-07T01:30', '2027-11-07T07:30:00Z'), FIRST_130)

    def test_a_bad_or_offsetless_instant_is_ignored(self):
        with _ny():
            for bad in ('not a time', '2027-11-07T06:30:00'):
                with self.subTest(instant=bad):
                    self.assertEqual(tz_utils.parse_form_time('2027-11-07T01:30', bad),
                                     FIRST_130)

    def test_bad_wall_text_still_raises(self):
        with _ny(), self.assertRaises(ValueError):
            tz_utils.parse_form_time('', '2027-11-07T06:30:00Z')


class _FormCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self._tz = _ny()
        self._tz.start()
        acct = seed.make_account()
        self.ch = seed.make_channel(acct, name='Overnight')
        db.session.commit()

    def tearDown(self):
        self._tz.stop()
        self.t.cleanup()

    def _post(self, path, data):
        with mock.patch('app.routes.recordings.schedule_recording'), \
                mock.patch('app.routes.recordings.unschedule_recording'):
            return self.t.client.post(path, data=data)


class CreateKeepsTheInstantTests(_FormCase):
    def test_a_program_in_the_second_1am_hour_is_recorded_in_it(self):
        resp = self._post('/recordings/new-json', {
            'name': 'Second pass', 'url': 'http://example.test/live/1',
            'channel_id': str(self.ch.id),
            'start_time': '2027-11-07T01:30', 'start_time_utc': '2027-11-07T06:30:00.000Z',
            'stop_time': '2027-11-07T02:30', 'stop_time_utc': '2027-11-07T07:30:00.000Z',
        })
        self.assertEqual(resp.status_code, 200, resp.get_json())
        rec = Recording.query.filter_by(name='Second pass').one()
        self.assertEqual((rec.start_time, rec.stop_time),
                         (SECOND_130, datetime(2027, 11, 7, 7, 30)))

    def test_a_recording_ending_in_the_second_pass_is_not_cut_short(self):
        # 12:30 AM EDT to 1:30 AM EST: three real hours.
        resp = self._post('/recordings/new-json', {
            'name': 'Spans the change', 'url': 'http://example.test/live/1',
            'channel_id': str(self.ch.id),
            'start_time': '2027-11-07T00:30', 'start_time_utc': '2027-11-07T04:30:00.000Z',
            'stop_time': '2027-11-07T01:30', 'stop_time_utc': '2027-11-07T06:30:00.000Z',
        })
        self.assertEqual(resp.status_code, 200, resp.get_json())
        rec = Recording.query.filter_by(name='Spans the change').one()
        self.assertEqual((rec.stop_time - rec.start_time).total_seconds(), 2 * 3600)


class EditKeepsTheInstantTests(_FormCase):
    def setUp(self):
        super().setUp()
        self.rec = seed.make_recording(
            status=REC_STATUS_SCHEDULED, name='Late show', channel_id=self.ch.id,
            start_time=SECOND_130, stop_time=datetime(2027, 11, 7, 7, 30))
        db.session.commit()

    def _edit(self, **extra):
        data = {
            'name': 'Late show renamed', 'url': 'http://example.test/live/1',
            'start_time': '2027-11-07T01:30', 'start_time_utc': '2027-11-07T06:30:00Z',
            'stop_time': '2027-11-07T02:30', 'stop_time_utc': '2027-11-07T07:30:00Z',
            'opened_start_utc': '2027-11-07T06:30:00Z',
            'opened_stop_utc': '2027-11-07T07:30:00Z',
        }
        data.update(extra)
        return self._post(f'/recordings/{self.rec.id}/edit-json', data)

    def test_renaming_does_not_move_it(self):
        resp = self._edit()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        self.assertEqual((rec.name, rec.start_time), ('Late show renamed', SECOND_130))

    def test_an_edit_against_a_moved_window_is_refused_with_the_current_one(self):
        # Retrack moved it an hour later after the dialog opened.
        self.rec.start_time = datetime(2027, 11, 7, 7, 30)
        self.rec.stop_time = datetime(2027, 11, 7, 8, 30)
        db.session.commit()
        resp = self._edit()
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertEqual((body['start_time'], body['stop_time']),
                         ('2027-11-07T07:30:00', '2027-11-07T08:30:00'))
        self.assertIn('moved since this dialog opened', body['error'])
        db.session.expire_all()
        rec = db.session.get(Recording, self.rec.id)
        self.assertEqual((rec.name, rec.start_time), ('Late show', datetime(2027, 11, 7, 7, 30)))

    def test_an_edit_with_no_opened_window_still_saves(self):
        resp = self._edit(opened_start_utc='', opened_stop_utc='')
        self.assertEqual(resp.status_code, 200, resp.get_json())

    def test_a_move_between_the_route_read_and_the_write_is_refused_too(self):
        """The move lands through another connection after this session loaded the row, the
        way retrack's commit lands after the route's first read; the session's copy still
        holds the old window."""
        from app.routes.recordings import _apply_edit_and_reschedule, _WindowMoved
        self.assertEqual(self.rec.start_time, SECOND_130)
        with db.engine.begin() as conn:
            conn.execute(Recording.__table__.update()
                         .where(Recording.__table__.c.id == self.rec.id)
                         .values(start_time=datetime(2027, 11, 7, 7, 30)))
        with self.t.app.test_request_context(), self.assertRaises(_WindowMoved):
            _apply_edit_and_reschedule(
                self.rec.id, 'Overwritten', 'http://example.test/live/1', SECOND_130,
                datetime(2027, 11, 7, 7, 30), opened=(SECOND_130, datetime(2027, 11, 7, 7, 30)))
        db.session.rollback()
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, self.rec.id).name, 'Late show')


class AdjustStopKeepsTheInstantTests(_FormCase):
    def test_an_active_recordings_stop_can_land_in_the_second_pass(self):
        rec = seed.make_recording(
            status=REC_STATUS_IN_PROGRESS, name='Running', channel_id=self.ch.id,
            start_time=datetime(2027, 11, 7, 3, 0), stop_time=datetime(2027, 11, 7, 5, 0))
        db.session.commit()
        with mock.patch('app.routes.recordings.datetime') as fake_dt:
            fake_dt.utcnow.return_value = datetime(2027, 11, 7, 4, 0)
            fake_dt.fromisoformat = datetime.fromisoformat
            with mock.patch('app.routes.recordings.schedule_recording'), \
                    mock.patch('app.routes.recordings.unschedule_recording'):
                resp = self.t.client.post(f'/recordings/{rec.id}/adjust-stop', json={
                    'stop_time': '2027-11-07T01:30', 'stop_time_utc': '2027-11-07T06:30:00Z'})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rec.id).stop_time, SECOND_130)


if __name__ == '__main__':
    unittest.main()
