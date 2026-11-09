"""A matched recording's file named from TheSportsDB (dev/changelog/1466, dev/docs/DESIGN-sports-data.md 11).

With sports_data.file_names on, the join names a recording linked to a TheSportsDB event
`League YYYY-MM-DD Event`, built from the link's own fields and never TheSportsDB's strFilename. The
date is the event's day in the display timezone, or TheSportsDB's own date for a field event at the
00:00 placeholder. Off, unlinked, or with its match removed, the file keeps the recording's own name.
Decided at the join, so a link that arrived after scheduling counts. Made-up leagues and teams: nothing
received from TheSportsDB ships (design 13). Throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_file_name
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import concatenator, db, sports_data  # noqa: E402
from app.database import (  # noqa: E402
    CONCATENATION_DONE, SPORTS_MATCH_STRONG, SPORTS_MATCH_WEAK, Recording, RecordingEvent, RecordingSegment,
    RecordingSports,
)
from app.sports_view import recording_context  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

NY = ZoneInfo('America/New_York')
# 00:30 UTC on Nov 9 is 7:30 PM on Nov 8 in New York: the UTC date is the wrong day.
KICKOFF = datetime(2099, 11, 9, 0, 30)


def _link(rid, **kw):
    row = RecordingSports(recording_id=rid, tsdb_event_id=kw.pop('tsdb_event_id', 7001),
                          league_name=kw.pop('league_name', 'Pretend Football League'),
                          event_name=kw.pop('event_name', 'Ironvale Bears vs Harborview Gulls'),
                          home_name=kw.pop('home_name', 'Ironvale Bears'),
                          away_name=kw.pop('away_name', 'Harborview Gulls'),
                          event_start=kw.pop('event_start', KICKOFF),
                          local_date=kw.pop('local_date', date(2099, 11, 8)),
                          strength=kw.pop('strength', SPORTS_MATCH_STRONG), airing='first',
                          reason='The league and both teams by name, and the start agrees.', **kw)
    db.session.add(row)
    db.session.commit()
    return row


class FileNameTests(unittest.TestCase):
    """The name itself, from a link's fields."""

    def setUp(self):
        self.t = make_test_app(csrf=False, start_scheduler=False)
        self.addCleanup(self.t.cleanup)

    def _rec(self, name='2099-11-08 - PFL Football - Ironvale'):
        rec = seed.make_recording(name=name)
        db.session.commit()
        return rec

    def test_a_matchup_is_named_league_local_date_event(self):
        rec = self._rec()
        link = _link(rec.id)
        self.assertEqual(sports_data.file_name(link, NY),
                         'Pretend Football League 2099-11-08 Ironvale Bears vs Harborview Gulls')

    def test_the_date_is_the_display_timezones_day_not_utcs(self):
        rec = self._rec()
        link = _link(rec.id, local_date=date(2099, 11, 9))
        self.assertIn(' 2099-11-08 ', sports_data.file_name(link, NY))
        self.assertIn(' 2099-11-09 ', sports_data.file_name(link, ZoneInfo('UTC')))

    def test_a_field_event_at_the_midnight_placeholder_uses_its_own_date(self):
        """A race with no real time: 00:00 UTC moved to New York would be the day before."""
        rec = self._rec()
        link = _link(rec.id, league_name='Pretend Stock Car Series', event_name='Ironvale 400',
                     home_name=None, away_name=None, event_start=datetime(2099, 11, 1, 0, 0),
                     local_date=date(2099, 11, 1))
        self.assertEqual(sports_data.file_name(link, NY), 'Pretend Stock Car Series 2099-11-01 Ironvale 400')

    def test_a_field_event_with_a_real_time_uses_the_display_day(self):
        rec = self._rec()
        link = _link(rec.id, league_name='Pretend Stock Car Series', event_name='Ironvale 200',
                     home_name=None, away_name=None, event_start=datetime(2099, 9, 18, 0, 30),
                     local_date=date(2099, 9, 18))
        self.assertEqual(sports_data.file_name(link, NY), 'Pretend Stock Car Series 2099-09-17 Ironvale 200')

    def test_off_by_default_keeps_the_recordings_own_name_and_says_nothing(self):
        rec = self._rec()
        _link(rec.id)
        self.assertEqual(sports_data.recording_file_name(rec, {}, NY), (rec.name, None))

    def test_on_with_no_link_keeps_the_own_name(self):
        rec = self._rec()
        self.assertEqual(sports_data.recording_file_name(rec, {'sports_data': {'file_names': True}}, NY),
                         (rec.name, None))

    def test_on_with_a_link_names_it_and_says_from_what(self):
        rec = self._rec()
        _link(rec.id, strength=SPORTS_MATCH_WEAK)
        name, note = sports_data.recording_file_name(rec, {'sports_data': {'file_names': True}}, NY)
        self.assertEqual(name, 'Pretend Football League 2099-11-08 Ironvale Bears vs Harborview Gulls')
        self.assertIn('Harborview Gulls at Ironvale Bears', note)
        self.assertIn('weak match', note)

    def test_a_removed_match_keeps_the_own_name_and_says_why(self):
        rec = self._rec()
        _link(rec.id, removed_at=datetime.utcnow())
        name, note = sports_data.recording_file_name(rec, {'sports_data': {'file_names': True}}, NY)
        self.assertEqual(name, rec.name)
        self.assertIn('removed', note)


class JoinNamesTheFileTests(unittest.TestCase):
    """The join writes the file under the sports name, and its event says so."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)

    def _config(self, on):
        self.t.sandbox_config({
            'display': {'timezone': 'America/New_York'},
            'sports_data': {'file_names': on},
            'recording': {
                'dvr_output_dir': self.dvr,
                'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
                'live_thumbnail': {'enabled': False},
                'post_process': {'enabled': False},
            }})

    def _join(self, linked=True, **link_kw):
        rec = seed.make_recording(status='IN_PROGRESS', name='PFL Football')
        db.session.commit()
        rid = rec.id
        if linked:
            _link(rid, **link_kw)
        seg = os.path.join(self.dvr, f'PFL_Football_{rid}_seg_001.ts')
        with open(seg, 'wb') as fh:
            fh.write(b'\x47' * 512)
        now = datetime.utcnow()
        db.session.add(RecordingSegment(recording_id=rid, segment_number=1, file_path=seg,
                                        started_at=now - timedelta(minutes=5), ended_at=now,
                                        exit_reason='STOP_TIME_REACHED', bytes_recorded=512))
        db.session.commit()
        with mock.patch('app.postprocessor.do_postprocess'):
            concatenator.do_concatenation(self.t.app, rid)
        db.session.expire_all()
        done = (RecordingEvent.query.filter_by(recording_id=rid, event_type=CONCATENATION_DONE)
                .order_by(RecordingEvent.id.desc()).first())
        return db.session.get(Recording, rid), done

    def test_on_and_linked_the_file_carries_the_sports_name(self):
        self._config(True)
        rec, done = self._join()
        self.assertEqual(os.path.basename(rec.output_path),
                         'Pretend_Football_League_2099-11-08_Ironvale_Bears_vs_Harborview_Gulls.ts')
        self.assertTrue(os.path.exists(rec.output_path))
        self.assertIn('named from TheSportsDB', done.detail)
        self.assertEqual(rec.name, 'PFL Football', 'the recording keeps its own name in the app')

    def test_off_the_file_keeps_the_recordings_name(self):
        self._config(False)
        rec, done = self._join()
        self.assertEqual(os.path.basename(rec.output_path), 'PFL_Football.ts')
        self.assertNotIn('TheSportsDB', done.detail)

    def test_on_with_the_match_removed_the_file_keeps_its_name_and_says_why(self):
        self._config(True)
        rec, done = self._join(removed_at=datetime.utcnow())
        self.assertEqual(os.path.basename(rec.output_path), 'PFL_Football.ts')
        self.assertIn('its match was removed', done.detail)

    def test_on_with_no_link_the_file_keeps_its_name_quietly(self):
        self._config(True)
        rec, done = self._join(linked=False)
        self.assertEqual(os.path.basename(rec.output_path), 'PFL_Football.ts')
        self.assertNotIn('TheSportsDB', done.detail)


class RecordingPageTests(unittest.TestCase):
    """The Program card says what the file will be called before the join, and nothing after."""

    def setUp(self):
        self.t = make_test_app(csrf=False, start_scheduler=False)
        self.addCleanup(self.t.cleanup)

    def _ctx(self, rec, on=True):
        return recording_context(rec, datetime.utcnow(), NY, False,
                                 {'sports_data': {'file_names': on, 'download_images': False}})

    def test_a_scheduled_linked_recording_shows_the_name_on_disk(self):
        rec = seed.make_recording(status='SCHEDULED', name='PFL Football')
        db.session.commit()
        _link(rec.id)
        self.assertEqual(self._ctx(rec)['file_name'],
                         'Pretend_Football_League_2099-11-08_Ironvale_Bears_vs_Harborview_Gulls')
        self.assertIsNone(self._ctx(rec, on=False)['file_name'])

    def test_once_the_file_is_written_it_says_nothing(self):
        rec = seed.make_recording(status='COMPLETED', name='PFL Football', output_path='/x/PFL_Football.ts')
        db.session.commit()
        _link(rec.id)
        self.assertIsNone(self._ctx(rec)['file_name'])

    def test_the_page_renders_the_file_name_row(self):
        self.t.sandbox_config({'display': {'timezone': 'America/New_York'},
                               'sports_data': {'file_names': True, 'download_images': False}})
        rec = seed.make_recording(status='SCHEDULED', name='PFL Football', program_title='PFL Football')
        db.session.commit()
        _link(rec.id)
        page =self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        self.assertIn('File name', page)
        self.assertIn('Pretend_Football_League_2099-11-08_Ironvale_Bears_vs_Harborview_Gulls', page)

    def test_a_removed_match_says_the_file_keeps_its_name(self):
        rec = seed.make_recording(status='SCHEDULED', name='PFL Football')
        db.session.commit()
        _link(rec.id, removed_at=datetime.utcnow())
        self.assertTrue(self._ctx(rec)['keeps_name'])
        self.assertFalse(self._ctx(rec, on=False)['keeps_name'])


if __name__ == '__main__':
    unittest.main()
