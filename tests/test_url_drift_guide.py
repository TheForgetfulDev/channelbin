"""Tier 2 - guide recording<->channel matching survives provider URL drift (URL drift 2/3).

Guards DESIGN-url-drift.md §3: `Recording.url` is frozen at creation, so once a provider
rewrites its stream URLs (new domain and/or rotated embedded credentials) sync repoints the
Channel row while the recording keeps the dead old URL. The guide used to match recordings to
programs by URL alone, so every scheduled recording silently lost its guide indicator at the
exact moment the user most needed to see it.

Matching is now identity-first (group_id > channel_id > URL); URL matching survives only for
manual URL-only recordings, which have no channel identity to match on.

Covers both consumers of the index - the guide grid (/api/guide/epg) and the airing
search (/api/channels/search?grain=airings), which replaced /api/guide/search when the
Extended Search modal was retired (dev/changelog/416).

DB-backed against a make_test_app() temp DB. Run standalone:
  python3 -m unittest tests.test_url_drift_guide
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

DRIFTED_URL = 'http://newstream.test/live/newuser/newpass/1'


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S')


class GuideUrlDriftTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # Future-anchored: the airing search hides showings that have ended by default
        # (the `past` standing option), so a fixed past date would make it vacuous.
        self.anchor = (datetime.utcnow() + timedelta(hours=2)).replace(
            minute=0, second=0, microsecond=0)
        self.acc = seed.make_account()
        self.ch = seed.make_channel(self.acc, stream_id=1, name='Drift Ch', in_guide=True)
        self.entry = EPGEntry(channel_id=self.ch.id, title='DriftProg',
                              start_time=self.anchor,
                              stop_time=self.anchor + timedelta(hours=1))
        db.session.add(self.entry)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _drift_the_channel(self):
        """What a sync does when the provider moves its stream domain + creds: the Channel
        row is repointed in place (identity is stream_id-keyed) while recordings keep the
        URL they froze at creation."""
        self.ch.stream_url = DRIFTED_URL
        self.ch.raw_stream_url = DRIFTED_URL + '.ts'
        db.session.commit()

    def _grid_program(self):
        resp = self.t.client.get(
            f'/api/guide/epg?start={_iso(self.anchor)}'
            f'&end={_iso(self.anchor + timedelta(hours=2))}')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        for row in resp.get_json()['channels']:
            if row['id'] == self.ch.id:
                return next(p for p in row['programs'] if p['title'] == 'DriftProg'), row
        self.fail('channel row missing from guide response')

    def _search_program(self):
        resp = self.t.client.get(
            '/api/channels/search?grain=airings&facets=&in=epg-title&q=DriftProg')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        rows = resp.get_json()['rows']
        self.assertTrue(rows, 'search returned no results for the seeded program')
        return rows[0]

    def test_grid_keeps_indicator_after_url_drift(self):
        """The core invariant: a SCHEDULED channel-backed recording keeps has_recording
        when rec.url no longer equals the channel's current stream_url."""
        rec = seed.make_recording(status='SCHEDULED', name='drifted', channel_id=self.ch.id,
                                  url='http://example.test/live/1',
                                  start_time=self.entry.start_time,
                                  stop_time=self.entry.stop_time)
        db.session.commit()
        self._drift_the_channel()

        prog, row = self._grid_program()
        self.assertTrue(prog['has_recording'],
                        'guide lost the scheduled indicator after the provider rewrote the URL')
        self.assertEqual(prog['recording_id'], rec.id)
        self.assertIn(rec.id, {r['id'] for r in row['recordings']})

    def test_search_keeps_indicator_after_url_drift(self):
        """Same invariant on the other consumer of the index (the airing search)."""
        rec = seed.make_recording(status='SCHEDULED', name='drifted', channel_id=self.ch.id,
                                  url='http://example.test/live/1',
                                  start_time=self.entry.start_time,
                                  stop_time=self.entry.stop_time)
        db.session.commit()
        self._drift_the_channel()

        result = self._search_program()
        self.assertIsNotNone(result['recording'],
                             'search lost the scheduled indicator after the URL drifted')
        self.assertEqual(result['recording']['id'], rec.id)
        self.assertEqual(result['record_state'], 'scheduled')

    def test_manual_url_only_recording_still_matches_by_url(self):
        """CONTROL (passed before the fix - it guards the URL path against regressing).

        channel_id is NULL for a manually-entered URL recording, so there is no identity
        to match on and the URL fallback must still work."""
        rec = seed.make_recording(status='SCHEDULED', name='manual', channel_id=None,
                                  url=self.ch.stream_url,
                                  start_time=self.entry.start_time,
                                  stop_time=self.entry.stop_time)
        db.session.commit()

        prog, _row = self._grid_program()
        self.assertTrue(prog['has_recording'],
                        'URL fallback dropped for manual URL-only recordings')
        self.assertEqual(prog['recording_id'], rec.id)

    def test_channel_recording_still_reported_once(self):
        """CONTROL (passed before the fix).

        Identity and URL both point at this row (undrifted channel) - it must be matched,
        and the row must not report the same recording twice."""
        rec = seed.make_recording(status='SCHEDULED', name='both', channel_id=self.ch.id,
                                  url=self.ch.stream_url,
                                  start_time=self.entry.start_time,
                                  stop_time=self.entry.stop_time)
        db.session.commit()

        prog, row = self._grid_program()
        self.assertTrue(prog['has_recording'])
        self.assertEqual(prog['recording_id'], rec.id)
        ids = [r['id'] for r in row['recordings']]
        self.assertEqual(ids.count(rec.id), 1, f'recording listed more than once: {ids}')

    def test_recording_on_another_channel_does_not_match(self):
        """CONTROL (passed before the fix).

        Identity-first matching must not turn into match-everything: a recording on a
        different channel stays off this row."""
        other = seed.make_channel(self.acc, stream_id=2, name='Other Ch', in_guide=True)
        seed.make_recording(status='SCHEDULED', name='other', channel_id=other.id,
                            url=other.stream_url,
                            start_time=self.entry.start_time,
                            stop_time=self.entry.stop_time)
        db.session.commit()

        prog, row = self._grid_program()
        self.assertFalse(prog['has_recording'],
                         "another channel's recording leaked onto this row")
        self.assertEqual(row['recordings'], [])

    def test_group_backed_recording_still_matches_its_group_row(self):
        """CONTROL (passed before the fix).

        Group rows matched by group_id beforehand and must keep doing so - the
        group index is checked first."""
        m1 = seed.make_channel(self.acc, stream_id=10, name='Member A')
        m2 = seed.make_channel(self.acc, stream_id=11, name='Member B')
        group = seed.make_group(name='Drift Grp', members=[m1, m2])
        for m in (m1, m2):
            db.session.add(EPGEntry(channel_id=m.id, title='GrpProg',
                                    start_time=self.anchor,
                                    stop_time=self.anchor + timedelta(hours=1)))
        rec = seed.make_recording(status='SCHEDULED', name='grouprec', group_id=group.id,
                                  channel_id=m1.id, url='http://stale.test/live/999',
                                  start_time=self.anchor,
                                  stop_time=self.anchor + timedelta(hours=1))
        db.session.commit()

        resp = self.t.client.get(
            f'/api/guide/epg?start={_iso(self.anchor)}'
            f'&end={_iso(self.anchor + timedelta(hours=2))}')
        row = next(r for r in resp.get_json()['channels'] if r['id'] == f'g{group.id}')
        prog = next(p for p in row['programs'] if p['title'] == 'GrpProg')
        self.assertTrue(prog['has_recording'])
        self.assertEqual(prog['recording_id'], rec.id)


if __name__ == '__main__':
    unittest.main(verbosity=2)
