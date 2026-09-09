"""Tier 2 - /api/guide/epg correctness (dev/changelog/268, chunk 5).

Pins three shipped guide-API defects, each of which rendered a real recording
invisible or mis-attributed in the TV Guide grid:

  * Two recordings on one channel: the row must report BOTH, and each program's
    has_recording/recording_id must point at the recording that actually overlaps
    that program's window - not whichever was scheduled last (the dict-keyed-by-URL
    overwrite bug, BUGS.md 2026-06-29 11:01 PM).
  * A recording whose URL ends in `.m3u8` must still match its channel - the loose
    normalization strips `.ts`/`.m3u8` on both sides (BUGS.md 2026-07-16 02:42 PM).
  * Group rows expose a numeric group_id, and every program carries a numeric
    channel_id (the active member), never a synthetic `g<id>` string
    (BUGS.md 2026-07-17 07:02 AM).

DB-backed against a make_test_app() temp DB seeded with guide channels/EPG.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import EPGEntry  # noqa: E402


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


class GuideEpgApiTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # A fixed anchor so program/recording windows are deterministic; all rows
        # live inside [anchor, anchor+3h] which is the query window.
        self.anchor = datetime(2026, 7, 17, 18, 0, 0)
        self.acc = seed.make_account()
        # stream_id=1 → stream_url http://example.test/live/1
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Guide Ch', in_guide=True)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _epg(self, offset_h, dur_h=1, title='Prog'):
        start = self.anchor + timedelta(hours=offset_h)
        e = EPGEntry(channel_id=self.ch.id, title=title,
                     start_time=start, stop_time=start + timedelta(hours=dur_h))
        db.session.add(e)
        db.session.flush()
        return e

    def _get_window(self):
        resp = self.t.client.get(
            f'/api/guide/epg?start={_iso(self.anchor)}&end={_iso(self.anchor + timedelta(hours=3))}')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        return resp.get_json()

    def _row_for(self, data, row_id):
        for row in data['channels']:
            if row['id'] == row_id:
                return row
        self.fail(f'row {row_id!r} not in response: {[r["id"] for r in data["channels"]]}')

    def test_two_recordings_on_one_channel_both_reported(self):
        """BUGS.md 2026-06-29 11:01 PM - the second recording must not overwrite the first."""
        a = self._epg(0, title='First')       # 18:00–19:00
        b = self._epg(1, title='Second')      # 19:00–20:00
        rec1 = seed.make_recording(status='SCHEDULED', name='rec1', channel_id=self.ch.id,
                                   url='http://example.test/live/1',
                                   start_time=a.start_time, stop_time=a.stop_time)
        rec2 = seed.make_recording(status='SCHEDULED', name='rec2', channel_id=self.ch.id,
                                   url='http://example.test/live/1',
                                   start_time=b.start_time, stop_time=b.stop_time)
        db.session.commit()

        row = self._row_for(self._get_window(), self.ch.id)
        rec_ids = {r['id'] for r in row['recordings']}
        self.assertEqual(rec_ids, {rec1.id, rec2.id},
                         'both recordings on the channel must be reported')

        by_title = {p['title']: p for p in row['programs']}
        self.assertTrue(by_title['First']['has_recording'])
        self.assertEqual(by_title['First']['recording_id'], rec1.id)
        self.assertTrue(by_title['Second']['has_recording'])
        self.assertEqual(by_title['Second']['recording_id'], rec2.id,
                         'each program must match the recording overlapping ITS window')

    def test_m3u8_recording_url_matches_channel(self):
        """BUGS.md 2026-07-16 02:42 PM - a .m3u8 recording URL must still match."""
        a = self._epg(0, title='OnAir')
        rec = seed.make_recording(status='SCHEDULED', name='m3u8rec', channel_id=self.ch.id,
                                  url='http://example.test/live/1.m3u8',
                                  start_time=a.start_time, stop_time=a.stop_time)
        db.session.commit()

        row = self._row_for(self._get_window(), self.ch.id)
        self.assertIn(rec.id, {r['id'] for r in row['recordings']})
        prog = next(p for p in row['programs'] if p['title'] == 'OnAir')
        self.assertTrue(prog['has_recording'], '.m3u8 recording URL failed to match channel')
        self.assertEqual(prog['recording_id'], rec.id)

    def test_missing_channel_reports_lifecycle(self):
        """dev/changelog/626 - the guide's channel column must surface a channel the
        provider stopped sending, the same 'missing' state /channels already carries
        (app/accounts.py::channel_lifecycle_state). A healthy channel and a group row
        must not pick up a stale/spurious flag."""
        since = self.anchor - timedelta(days=30)
        missing_ch = seed.make_channel(self.acc, stream_id=2, name='Gone Ch', in_guide=True,
                                       last_seen_at=since)
        self.acc.last_sync_at = self.anchor
        m1 = seed.make_channel(self.acc, name='Member A')
        group = seed.make_group(name='Grp', members=[m1])
        db.session.commit()

        data = self._get_window()
        missing_row = self._row_for(data, missing_ch.id)
        self.assertEqual(missing_row['lifecycle'], 'missing')
        self.assertEqual(missing_row['lifecycle_date'], since.strftime('%Y-%m-%d'))
        self.assertFalse(missing_row['lifecycle_repoint_available'],
                         'no duplicate exists for this channel, so no repoint target either')

        healthy_row = self._row_for(data, self.ch.id)
        self.assertIsNone(healthy_row['lifecycle'])
        self.assertEqual(healthy_row['lifecycle_date'], '')
        self.assertFalse(healthy_row['lifecycle_repoint_available'])

        group_row = self._row_for(data, f'g{group.id}')
        self.assertIsNone(group_row['lifecycle'],
                          "a group's lifecycle isn't a single member channel's to report")

    def test_missing_duplicate_channel_reports_repoint_availability(self):
        """dev/changelog/627 - the guide's Missing pill only offers the click-to-re-point
        hint when a real recovery target exists (accounts.repoint_candidates_for_channels,
        the same lookup the channel detail page's Re-point action uses), not guessed from a
        cheaper guide-scoped signal. The plain-missing case in the test above (no duplicate)
        already proves the False side."""
        since = self.anchor - timedelta(days=30)
        missing_ch = seed.make_channel(self.acc, stream_id=2, name='Gone Ch', in_guide=True,
                                       last_seen_at=since, is_duplicate_stream_url=True)
        survivor = seed.make_channel(self.acc, stream_id=3, name='Survivor Ch', in_guide=False,
                                     is_duplicate_stream_url=True)
        missing_ch.stream_url = 'http://example.test/live/shared-repoint'
        survivor.stream_url = 'http://example.test/live/shared-repoint'
        self.acc.last_sync_at = self.anchor
        db.session.commit()

        missing_row = self._row_for(self._get_window(), missing_ch.id)
        self.assertEqual(missing_row['lifecycle'], 'missing')
        self.assertTrue(missing_row['lifecycle_repoint_available'])

    def test_group_row_ids_are_numeric(self):
        """BUGS.md 2026-07-17 07:02 AM - programs carry a numeric channel_id (active
        member), and the row a numeric group_id; the synthetic 'g<id>' key never leaks
        into channel_id."""
        m1 = seed.make_channel(self.acc, name='Member A')
        m2 = seed.make_channel(self.acc, name='Member B')
        group = seed.make_group(name='Grp', members=[m1, m2])
        self._epg(0, title='GroupProg')  # EPG lives on self.ch, not the group; group
        # members have their own - add one to the member the group renders from.
        db.session.add(EPGEntry(channel_id=m1.id, title='GroupProg',
                                start_time=self.anchor,
                                stop_time=self.anchor + timedelta(hours=1)))
        db.session.add(EPGEntry(channel_id=m2.id, title='GroupProg',
                                start_time=self.anchor,
                                stop_time=self.anchor + timedelta(hours=1)))
        db.session.commit()

        row = self._row_for(self._get_window(), f'g{group.id}')
        self.assertTrue(row['is_group'])
        self.assertEqual(row['group_id'], group.id)
        self.assertIsInstance(row['group_id'], int)
        member_ids = {m1.id, m2.id}
        for prog in row['programs']:
            self.assertIsInstance(prog['channel_id'], int,
                                  'program channel_id must be a numeric member id')
            self.assertIn(prog['channel_id'], member_ids)
            self.assertEqual(prog['group_id'], group.id)


if __name__ == '__main__':
    unittest.main(verbosity=2)
