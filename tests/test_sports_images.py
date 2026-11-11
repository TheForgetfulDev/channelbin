"""TheSportsDB's poster and background for a recording's event (app/sports_images.py, the sidecar's
poster chain in app/metadata_sidecar.py, the Program card's Artwork row), dev/changelog/1465.

Downloads run over real HTTP against tests/support/fake_sportsdb.py's image host, serving images
made up here (tests/support/images.py): nothing received from TheSportsDB ships (design 13). The
fake is on loopback http, so the downloading tests accept its address; the address check itself
is tested on its own. Throwaway temp SQLite DB and images folder - never the live dvr.db.
  python3 -m unittest tests.test_sports_images
"""
import json
import os
import shutil
import sys
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, sports_data, sports_images  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (  # noqa: E402
    METADATA_SIDECAR_WRITTEN, REC_STATUS_COMPLETED, SPORTS_AIRING_FIRST, SPORTS_FETCH_OK,
    SPORTS_FETCH_RATE_LIMITED, SPORTS_FETCH_REFUSED, SPORTS_MATCH_KIND_GUIDE, SPORTS_MATCH_STRONG,
    Recording, RecordingEvent, RecordingProfile, RecordingSports, SportsEvent, SportsImage, SportsLeague,
    SportsMatch, SportsState, detach_recording_references,
)
from app.metadata_sidecar import (  # noqa: E402
    POSTER_FROM_PROFILE, POSTER_FROM_SPORTS, POSTER_FROM_SPORTS_THUMB, write_sidecar,
)
from app.profile_posters import MAX_BYTES, store_poster, sniff_image  # noqa: E402
from app.recorder import recording_image_paths  # noqa: E402
from app.storage_dirs import SPORTS_IMAGES, image_dir  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.fake_sportsdb import FakeSportsDB  # noqa: E402
from tests.support.images import jpeg_bytes, png_bytes  # noqa: E402

KICKOFF = datetime(2099, 11, 8, 18, 0)
NOW = datetime(2099, 11, 7, 12, 0)
POSTER = png_bytes(68, 100)
THUMB = jpeg_bytes(1280, 720)


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.fake = FakeSportsDB()
        self.addCleanup(self.fake.stop)
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.configure()
        # The fake is plain http on loopback; the real check is AddressTests' subject.
        p = patch.object(sports_images, 'address_refused', lambda url: '')
        p.start()
        self.addCleanup(p.stop)
        self.account = seed.make_account()
        self.ch = seed.make_channel(self.account)
        self.league = SportsLeague(tsdb_id=9001, name='Pretend Football League',
                                   match_terms=json.dumps(['Pretend Football League']))
        db.session.add(self.league)
        db.session.commit()
        self.poster_url = self.fake.image('poster/aaa.png', POSTER, 'image/png')
        self.thumb_url = self.fake.image('thumb/bbb.jpg', THUMB)
        self.event = self.make_event(7001, self.poster_url, self.thumb_url)

    def configure(self, sidecar=True, download=True):
        """The runtime config.yaml: images are only downloaded for a recording that writes a sidecar."""
        self.t.sandbox_config({'recording': {'dvr_output_dir': self.dvr, 'metadata_sidecar': {'enabled': sidecar}},
                               'sports_data': {'download_images': download}})

    def make_event(self, tsdb_id, poster_url, thumb_url, start=KICKOFF):
        e = SportsEvent(tsdb_id=tsdb_id, league_id=self.league.id, name='Ironvale Bears vs Harborview Gulls',
                        home_team_id=2, away_team_id=1, home_name='Ironvale Bears', away_name='Harborview Gulls',
                        season='2099', round=10, start_at=start, local_date=start.date(),
                        poster_url=poster_url, thumb_url=thumb_url)
        db.session.add(e)
        db.session.flush()
        db.session.add(SportsMatch(event_id=e.id, channel_id=self.ch.id, kind=SPORTS_MATCH_KIND_GUIDE,
                                   program_start_time=start, program_stop_time=start + timedelta(hours=3),
                                   title_key='pfl football', program_title='PFL Football',
                                   strength=SPORTS_MATCH_STRONG, airing=SPORTS_AIRING_FIRST,
                                   reason='The league and both teams by name, and the start agrees.'))
        db.session.commit()
        return e

    def recording(self, start=KICKOFF, link=True, **kw):
        rec = seed.make_recording(channel_id=self.ch.id, program_start_time=start,
                                  program_stop_time=start + timedelta(hours=3), program_title='PFL Football',
                                  start_time=start, stop_time=start + timedelta(hours=3), **kw)
        db.session.commit()
        if link:
            self.assertEqual(sports_data.link_recording(rec.id), sports_data.LINK_LINKED)
        db.session.expire_all()
        return rec

    def tick(self, now):
        db.session.expire_all()
        out = sports_data.tick(now=now)
        db.session.expire_all()
        return out

    def drain(self, now=NOW, limit=10):
        for _ in range(limit):
            if self.tick(now) == 'idle':
                return now
            now += timedelta(minutes=5)
        self.fail('the worker never ran out of images')

    def image(self, url):
        db.session.expire_all()
        return SportsImage.query.filter_by(url=url).first()

    def image_calls(self):
        return self.fake.calls_to('image')

    def folder(self):
        return image_dir(load_config(), SPORTS_IMAGES)


class DownloadTests(_Case):
    """One download per image, at the worker's pace, for unfinished recordings only."""

    def test_the_poster_and_thumb_are_downloaded_once_each_at_the_pace(self):
        self.recording()
        self.assertEqual(self.tick(NOW), SPORTS_FETCH_OK)
        # The pace is shared with the schedule calls: the next turn waits.
        self.assertEqual(self.tick(NOW + timedelta(seconds=5)), 'waiting')
        self.assertEqual(self.tick(NOW + timedelta(minutes=5)), SPORTS_FETCH_OK)
        self.assertEqual(self.tick(NOW + timedelta(minutes=10)), 'idle')
        self.assertEqual(len(self.image_calls()), 2)
        poster, thumb = self.image(self.poster_url), self.image(self.thumb_url)
        self.assertTrue(poster.file_name.endswith('.png') and thumb.file_name.endswith('.jpg'))
        with open(os.path.join(self.folder(), poster.file_name), 'rb') as fh:
            self.assertEqual(fh.read(), POSTER)
        self.assertIsNotNone(db.session.get(SportsState, 1).last_call_at)

    def test_two_recordings_of_one_event_share_one_download(self):
        self.recording()
        rec2 = seed.make_recording(channel_id=self.ch.id, program_start_time=KICKOFF,
                                   program_stop_time=KICKOFF + timedelta(hours=3), program_title='PFL Football',
                                   start_time=KICKOFF, stop_time=KICKOFF + timedelta(hours=3))
        db.session.commit()
        sports_data.link_recording(rec2.id)
        self.drain()
        self.assertEqual(len(self.image_calls()), 2)
        self.assertEqual(SportsImage.query.count(), 2)

    def test_a_finished_recording_downloads_nothing(self):
        self.recording(status=REC_STATUS_COMPLETED)
        self.assertEqual(self.tick(NOW), 'idle')
        self.assertEqual(self.image_calls(), [])

    def test_a_removed_link_downloads_nothing(self):
        rec = self.recording()
        sports_data.set_link_removed(rec.id, True)
        self.assertEqual(self.tick(NOW), 'idle')
        self.assertEqual(self.image_calls(), [])

    def test_switched_off_downloads_nothing(self):
        self.configure(download=False)
        self.recording()
        self.assertEqual(self.tick(NOW), 'idle')
        self.assertEqual(self.image_calls(), [])

    def test_a_page_that_is_not_an_image_is_not_kept_and_is_tried_again_later(self):
        self.fake.image('poster/aaa.png', '<html>Oops</html>', 'text/html')
        self.recording()
        self.assertEqual(self.tick(NOW), SPORTS_FETCH_REFUSED)
        img = self.image(self.poster_url)
        self.assertIsNone(img.file_name)
        self.assertEqual(img.reason, 'What came back was not a JPEG or PNG image.')
        self.assertEqual(img.next_try_at, NOW + timedelta(hours=sports_data.REFUSED_RETRY_HOURS))
        self.assertEqual(os.listdir(self.folder()) if os.path.isdir(self.folder()) else [], [])

    def test_an_image_over_the_cap_is_not_kept(self):
        self.fake.image('poster/aaa.png', POSTER + b'\x00' * MAX_BYTES, 'image/png')
        self.recording()
        self.tick(NOW)
        img = self.image(self.poster_url)
        self.assertIsNone(img.file_name)
        self.assertIn('larger than 10 MB', img.reason)

    def test_an_image_over_the_cap_with_no_length_header_is_cut_off(self):
        self.fake.image('poster/aaa.png', POSTER + b'\x00' * MAX_BYTES, 'image/png', length=False)
        self.recording()
        self.tick(NOW)
        img = self.image(self.poster_url)
        self.assertIsNone(img.file_name)
        self.assertIn('larger than 10 MB', img.reason)

    def test_a_download_past_its_deadline_is_not_kept(self):
        with patch.object(sports_images, 'DOWNLOAD_DEADLINE_SECONDS', -1):
            self.recording()
            self.tick(NOW)
        img = self.image(self.poster_url)
        self.assertIsNone(img.file_name)
        self.assertIn('took more than', img.reason)

    def test_a_recording_that_writes_no_metadata_file_downloads_nothing(self):
        self.configure(sidecar=False)
        self.recording()
        self.assertEqual(self.tick(NOW), 'idle')
        self.assertEqual(self.image_calls(), [])

    def test_a_copy_that_went_missing_is_downloaded_again(self):
        self.recording()
        self.drain()
        os.remove(os.path.join(self.folder(), self.image(self.poster_url).file_name))
        self.assertEqual(self.tick(NOW + timedelta(hours=1)), SPORTS_FETCH_OK)
        self.assertEqual(len(self.image_calls()), 3)
        self.assertTrue(os.path.exists(os.path.join(self.folder(), self.image(self.poster_url).file_name)))

    def test_the_worker_stays_while_an_unfinished_recording_is_owed_artwork(self):
        rec = self.recording()
        SportsMatch.query.delete()
        SportsEvent.query.delete()
        db.session.commit()
        self.assertTrue(sports_data.worker_needed())
        r = db.session.get(Recording, rec.id)
        r.status = REC_STATUS_COMPLETED
        db.session.commit()
        self.assertFalse(sports_data.worker_needed())

    def test_a_redirect_is_not_followed(self):
        self.fake.image('poster/aaa.png', '', 'text/html', status=302,
                        headers={'Location': self.fake.image('thumb/elsewhere.png', POSTER, 'image/png')})
        self.recording()
        self.tick(NOW)
        self.assertIn('a redirect, which is not followed', self.image(self.poster_url).reason)
        self.assertNotIn('/images/media/event/thumb/elsewhere.png', [c[2] for c in self.image_calls()])

    def test_a_missing_image_is_tried_again_after_the_wait_and_then_kept(self):
        del self.fake.images['/images/media/event/poster/aaa.png']
        self.recording()
        self.tick(NOW)
        self.assertEqual(self.image(self.poster_url).reason, 'HTTP 404.')
        self.fake.image('poster/aaa.png', POSTER, 'image/png')
        self.tick(NOW + timedelta(minutes=5))   # the thumb's turn
        self.assertEqual(self.tick(NOW + timedelta(minutes=10)), 'idle')   # the poster waits
        self.assertEqual(self.tick(NOW + timedelta(hours=7)), SPORTS_FETCH_OK)
        img = self.image(self.poster_url)
        self.assertIsNotNone(img.file_name)
        self.assertIsNone(img.reason)
        self.assertEqual(img.tries, 2)

    def test_a_429_backs_the_pace_off_and_keeps_the_image_due(self):
        self.recording()
        self.fake.fault = 'rate_limited'
        self.assertEqual(self.tick(NOW), SPORTS_FETCH_RATE_LIMITED)
        self.assertGreater(db.session.get(SportsState, 1).backoff_seconds, 0)
        self.assertEqual(self.image(self.poster_url).next_try_at, NOW)

    def test_a_refused_address_is_never_called_and_never_tried_again(self):
        with patch.object(sports_images, 'address_refused', lambda url: 'Its address is on evil.test.'):
            self.recording()
            self.assertEqual(self.tick(NOW), SPORTS_FETCH_REFUSED)
            self.assertEqual(self.image_calls(), [])
            self.assertIsNone(self.image(self.poster_url).next_try_at)
            self.assertIsNone(db.session.get(SportsState, 1))   # no call, so the pace never moved
            self.tick(NOW + timedelta(days=1))
            self.assertEqual(self.image(self.poster_url).tries, 1)


class AddressTests(unittest.TestCase):
    """Only a plain https address on thesportsdb.com is fetched: it comes from their answer."""

    def test_their_image_host_is_fetched(self):
        self.assertEqual(sports_images.address_refused('https://r2.thesportsdb.com/images/media/event/poster/x.jpg'), '')
        self.assertEqual(sports_images.address_refused('https://www.thesportsdb.com/images/x.jpg'), '')

    def test_anything_else_is_refused(self):
        for url in ('http://r2.thesportsdb.com/x.jpg', 'https://evil.test/x.jpg',
                    'https://thesportsdb.com.evil.test/x.jpg', 'https://someone@r2.thesportsdb.com/x.jpg',
                    'https://r2.thesportsdb.com:8443/x.jpg', 'https://127.0.0.1/x.jpg', 'file:///etc/passwd',
                    'https://[::1/x.jpg',
                    # Parsed as a thesportsdb.com host by urlsplit, fetched from 192.168.1.1 by requests.
                    'https://192.168.1.1\\.thesportsdb.com/a.jpg', 'https://r2.thesportsdb.com /x.jpg'):
            self.assertNotEqual(sports_images.address_refused(url), '', url)


class SidecarTests(_Case):
    """The poster chain: pinned profile image, TheSportsDB's poster, its thumb, then the frames; and
    the thumb as the background whatever the poster is."""

    def setUp(self):
        super().setUp()
        self.out = os.path.join(self.dvr, 'game.mp4')
        with open(self.out, 'wb') as fh:
            fh.write(b'm' * 64)

    def write(self, rec):
        rid = rec.id
        db.session.expire_all()
        self.assertTrue(write_sidecar(rid, self.out, load_config()))
        db.session.expire_all()
        ev = (RecordingEvent.query.filter_by(recording_id=rid, event_type=METADATA_SIDECAR_WRITTEN)
              .order_by(RecordingEvent.id.desc()).first())
        return ET.parse(os.path.join(self.dvr, 'game.nfo')).getroot(), ev

    def test_their_poster_and_background_are_written_beside_the_video(self):
        rec = self.recording()
        self.drain()
        root, ev = self.write(rec)
        with open(os.path.join(self.dvr, 'game-poster.png'), 'rb') as fh:
            self.assertEqual(fh.read(), POSTER)
        with open(os.path.join(self.dvr, 'game-fanart.jpg'), 'rb') as fh:
            self.assertEqual(fh.read(), THUMB)
        self.assertEqual(root.find('thumb').text, 'game-poster.png')
        self.assertEqual(root.find('thumb').get('aspect'), 'poster')
        self.assertEqual(root.find('fanart/thumb').text, 'game-fanart.jpg')
        self.assertEqual(json.loads(ev.extra_data)['poster_source'], POSTER_FROM_SPORTS)
        self.assertIn("game-fanart.jpg as its background (TheSportsDB's for its event)", ev.detail)

    def test_with_no_poster_the_thumb_is_the_poster_too(self):
        e = db.session.get(SportsEvent, self.event.id)
        e.poster_url = None
        db.session.commit()
        rec = self.recording()
        self.drain()
        root, ev = self.write(rec)
        self.assertEqual(root.find('thumb').text, 'game-poster.jpg')
        self.assertEqual(root.find('fanart/thumb').text, 'game-fanart.jpg')
        self.assertEqual(json.loads(ev.extra_data)['poster_source'], POSTER_FROM_SPORTS_THUMB)

    def test_a_pinned_profile_image_still_wins_and_the_background_is_still_theirs(self):
        cfg = load_config()
        profile = RecordingProfile(name='Football')
        db.session.add(profile)
        db.session.commit()
        info = sniff_image(png_bytes(1000, 1500))
        profile.poster_file = store_poster(cfg, profile.id, png_bytes(1000, 1500), info)
        db.session.commit()
        rec = self.recording(profile_id=profile.id)
        self.drain()
        root, ev = self.write(rec)
        self.assertEqual(json.loads(ev.extra_data)['poster_source'], POSTER_FROM_PROFILE)
        self.assertEqual(root.find('fanart/thumb').text, 'game-fanart.jpg')

    def pinned_profile(self, prefer):
        cfg = load_config()
        profile = RecordingProfile(name='Football', prefer_sports_poster=prefer)
        db.session.add(profile)
        db.session.commit()
        info = sniff_image(png_bytes(1000, 1500))
        profile.poster_file = store_poster(cfg, profile.id, png_bytes(1000, 1500), info)
        db.session.commit()
        return profile

    def test_a_profile_can_put_their_poster_ahead_of_its_pinned_one(self):
        """dev/changelog/1505: prefer_sports_poster moves TheSportsDB's own poster above the pin,
        and the event says the pinned one was passed over."""
        profile = self.pinned_profile(prefer=True)
        rec = self.recording(profile_id=profile.id)
        self.drain()
        root, ev = self.write(rec)
        with open(os.path.join(self.dvr, 'game-poster.png'), 'rb') as fh:
            self.assertEqual(fh.read(), POSTER)
        self.assertEqual(json.loads(ev.extra_data)['poster_source'], POSTER_FROM_SPORTS)
        self.assertIn('Profile "Football" puts TheSportsDB\'s poster ahead of its pinned one', ev.detail)
        self.assertEqual(root.find('fanart/thumb').text, 'game-fanart.jpg')

    def test_their_thumb_never_replaces_a_pinned_poster(self):
        """dev/changelog/1505: an event with no poster keeps the pinned one, and the event says why."""
        e = db.session.get(SportsEvent, self.event.id)
        e.poster_url = None
        db.session.commit()
        profile = self.pinned_profile(prefer=True)
        rec = self.recording(profile_id=profile.id)
        self.drain()
        root, ev = self.write(rec)
        self.assertEqual(json.loads(ev.extra_data)['poster_source'], POSTER_FROM_PROFILE)
        self.assertIn('TheSportsDB has no poster for this event, so the one pinned to profile "Football" '
                      'was used', ev.detail)
        self.assertEqual(root.find('fanart/thumb').text, 'game-fanart.jpg')

    def test_a_preferred_poster_not_downloaded_keeps_the_pin_and_says_why(self):
        """dev/changelog/1505: the reason TheSportsDB's poster was missing is said, not swallowed
        as it is when the pin is simply wanted first."""
        del self.fake.images['/images/media/event/poster/aaa.png']
        profile = self.pinned_profile(prefer=True)
        rec = self.recording(profile_id=profile.id)
        self.drain()
        _, ev = self.write(rec)
        self.assertEqual(json.loads(ev.extra_data)['poster_source'], POSTER_FROM_PROFILE)
        self.assertIn("TheSportsDB's poster for its event had not been downloaded (HTTP 404)", ev.detail)

    def test_a_preferred_poster_that_cannot_be_copied_falls_back_to_the_pin(self):
        profile = self.pinned_profile(prefer=True)
        rec = self.recording(profile_id=profile.id)
        self.drain()
        real = shutil.copyfile

        def copy(src, dst):
            if SPORTS_IMAGES in src and dst.endswith('-poster.png'):
                raise PermissionError(13, 'Permission denied')
            return real(src, dst)
        with patch('app.metadata_sidecar.shutil.copyfile', side_effect=copy):
            _, ev = self.write(rec)
        self.assertEqual(json.loads(ev.extra_data)['poster_source'], POSTER_FROM_PROFILE)
        self.assertIn("TheSportsDB's image could not be copied (Permission denied)", ev.detail)

    def test_with_it_off_the_pin_still_wins_and_nothing_is_said_about_their_poster(self):
        profile = self.pinned_profile(prefer=False)
        rec = self.recording(profile_id=profile.id)
        self.drain()
        _, ev = self.write(rec)
        self.assertEqual(json.loads(ev.extra_data)['poster_source'], POSTER_FROM_PROFILE)
        self.assertNotIn('ahead of', ev.detail)
        self.assertNotIn('no poster for this event', ev.detail)

    def test_not_downloaded_yet_falls_back_and_says_why(self):
        del self.fake.images['/images/media/event/poster/aaa.png']
        rec = self.recording()
        self.drain()
        root, ev = self.write(rec)
        self.assertEqual(root.find('fanart/thumb').text, 'game-fanart.jpg')
        self.assertNotEqual(json.loads(ev.extra_data)['poster_source'], POSTER_FROM_SPORTS)
        self.assertIn("TheSportsDB's poster for its event had not been downloaded (HTTP 404)", ev.detail)

    def test_switched_off_uses_none_already_downloaded_and_removes_an_old_background(self):
        rec = self.recording()
        self.drain()
        self.write(rec)
        self.configure(download=False)
        root, ev = self.write(rec)
        self.assertIsNone(root.find('fanart'))
        self.assertFalse(os.path.exists(os.path.join(self.dvr, 'game-fanart.jpg')))
        self.assertIn('off in Settings', ev.detail)

    def test_a_copy_that_fails_falls_back_and_still_writes_the_file(self):
        rec = self.recording()
        self.drain()
        real = shutil.copyfile

        def copy(src, dst):
            if SPORTS_IMAGES in src:
                raise PermissionError(13, 'Permission denied')
            return real(src, dst)
        with patch('app.metadata_sidecar.shutil.copyfile', side_effect=copy):
            root, ev = self.write(rec)
        self.assertIsNone(root.find('fanart'))
        self.assertNotEqual(json.loads(ev.extra_data)['poster_source'], POSTER_FROM_SPORTS)
        self.assertIn("TheSportsDB's background could not be copied (Permission denied)", ev.detail)

    def test_a_lookup_that_fails_still_writes_the_file(self):
        rec = self.recording()
        self.drain()
        with patch.object(sports_images, 'artwork', side_effect=OperationalError('x', {}, Exception('locked'))):
            root, ev = self.write(rec)
        self.assertIsNone(root.find('fanart'))
        self.assertIn("TheSportsDB's images could not be looked up", ev.detail)

    def test_a_removed_link_carries_no_artwork_of_theirs(self):
        rec = self.recording()
        self.drain()
        sports_data.set_link_removed(rec.id, True)
        root, ev = self.write(rec)
        self.assertIsNone(root.find('fanart'))
        self.assertNotIn('TheSportsDB', ev.detail)


class TeardownTests(_Case):
    """A file shared by every recording naming its address goes only with the last of them."""

    def downloaded_pair(self):
        a = self.recording()
        b = seed.make_recording(channel_id=self.ch.id, program_start_time=KICKOFF,
                                program_stop_time=KICKOFF + timedelta(hours=3), program_title='PFL Football',
                                start_time=KICKOFF, stop_time=KICKOFF + timedelta(hours=3))
        db.session.commit()
        sports_data.link_recording(b.id)
        self.drain()
        names = sorted(i.file_name for i in SportsImage.query.all())
        return a.id, b.id, [os.path.join(self.folder(), n) for n in names]

    def delete(self, rid):
        detach_recording_references(rid)
        db.session.delete(db.session.get(Recording, rid))
        db.session.commit()

    def test_a_shared_image_is_kept_until_the_last_recording_goes(self):
        a, b, files = self.downloaded_pair()
        self.assertFalse(set(files) & set(recording_image_paths(a)))
        self.delete(a)
        self.assertEqual(SportsImage.query.count(), 2)
        self.assertEqual(sorted(set(files) & set(recording_image_paths(b))), files)
        self.delete(b)
        self.assertEqual(SportsImage.query.count(), 0)

    def test_the_prune_removes_what_no_recording_names_and_stray_files(self):
        a, b, files = self.downloaded_pair()
        stray = os.path.join(self.folder(), '0123456789abcdef0123.jpg')
        tmp = os.path.join(self.folder(), 'x.jpg.tmp')
        unrelated = os.path.join(self.folder(), 'keep-me.txt')
        for path in (stray, tmp, unrelated):
            with open(path, 'wb') as fh:
                fh.write(b'x')
        sports_images.prune(load_config())
        self.assertTrue(all(os.path.exists(f) for f in files))
        self.assertFalse(os.path.exists(stray) or os.path.exists(tmp))
        self.assertTrue(os.path.exists(unrelated))
        # The link moves to an event with other images: the old ones are no longer named.
        RecordingSports.query.update({'poster_url': None, 'thumb_url': None})
        db.session.commit()
        sports_images.prune(load_config())
        self.assertEqual(SportsImage.query.count(), 0)
        self.assertFalse(any(os.path.exists(f) for f in files))


class PageTests(_Case):
    """The Program card's Artwork row, and the route its View links open."""

    def page(self, rid):
        db.session.remove()
        return self.t.client.get(f'/recordings/{rid}').get_data(as_text=True)

    def test_downloaded_images_are_named_and_viewable(self):
        rec = self.recording()
        self.drain()
        html = self.page(rec.id)
        self.assertIn('Poster and background from TheSportsDB.', html)
        self.assertIn(f'/recordings/{rec.id}/sports-image/poster', html)
        resp = self.t.client.get(f'/recordings/{rec.id}/sports-image/background')
        self.assertEqual((resp.status_code, resp.mimetype, resp.data), (200, 'image/jpeg', THUMB))
        resp.close()

    def test_one_not_downloaded_says_why_and_when_it_tries_again(self):
        del self.fake.images['/images/media/event/poster/aaa.png']
        rec = self.recording()
        self.drain()
        html = self.page(rec.id)
        self.assertIn('Background from TheSportsDB.', html)
        self.assertIn('Poster not downloaded yet. HTTP 404. Tries again', html)
        self.assertEqual(self.t.client.get(f'/recordings/{rec.id}/sports-image/poster').status_code, 404)

    def pinned(self, prefer):
        profile = RecordingProfile(name='Football', prefer_sports_poster=prefer)
        db.session.add(profile)
        db.session.commit()
        profile.poster_file = store_poster(load_config(), profile.id, png_bytes(1000, 1500),
                                           sniff_image(png_bytes(1000, 1500)))
        db.session.commit()
        return profile

    def test_the_row_names_which_poster_a_pinned_profile_uses(self):
        """dev/changelog/1505: the Artwork row follows the profile's choice."""
        off = self.pinned(prefer=False)
        rec = self.recording(profile_id=off.id)
        self.drain()
        self.assertIn('The image pinned to profile &#34;Football&#34; is the poster.', self.page(rec.id))
        off = db.session.get(RecordingProfile, off.id)
        off.prefer_sports_poster = True
        db.session.commit()
        self.assertIn('Profile &#34;Football&#34; puts this poster ahead of its pinned one.', self.page(rec.id))

    def test_a_preferred_poster_not_downloaded_is_not_claimed(self):
        """dev/changelog/1505: the row says the pinned image is the poster while TheSportsDB's is not
        downloaded, as the sidecar would write it, never that TheSportsDB's goes ahead."""
        del self.fake.images['/images/media/event/poster/aaa.png']
        profile = self.pinned(prefer=True)
        rec = self.recording(profile_id=profile.id)
        self.drain()
        html = self.page(rec.id)
        self.assertNotIn('ahead of its pinned one', html)
        self.assertIn('The image pinned to profile &#34;Football&#34; is the poster until TheSportsDB&#39;s '
                      'is downloaded.', html)

    def test_a_pinned_poster_is_never_called_replaced_by_their_thumb(self):
        """dev/docs/BUGS.md 2026-11-11: with an event that has no poster, the row said the thumb was
        also the poster and, in the next sentence, that the pinned image was. Either way the switch is."""
        e = db.session.get(SportsEvent, self.event.id)
        e.poster_url = None
        db.session.commit()
        profile = self.pinned(prefer=False)
        rec = self.recording(profile_id=profile.id)
        self.drain()
        html = self.page(rec.id)
        self.assertNotIn('also used as the poster', html)
        self.assertIn('Background from TheSportsDB.', html)
        self.assertIn('The image pinned to profile &#34;Football&#34; is the poster.', html)
        profile = db.session.get(RecordingProfile, profile.id)
        profile.prefer_sports_poster = True
        db.session.commit()
        html = self.page(rec.id)
        self.assertNotIn('also used as the poster', html)
        self.assertIn('is the poster, since the event has none.', html)

    def test_an_event_with_no_artwork_says_so(self):
        e = db.session.get(SportsEvent, self.event.id)
        e.poster_url = e.thumb_url = None
        db.session.commit()
        rec = self.recording()
        self.assertIn('TheSportsDB has no poster or background for this event.', self.page(rec.id))

    def test_switched_off_says_so_and_serves_nothing(self):
        rec = self.recording()
        self.drain()
        self.configure(download=False)
        self.assertIn('Downloading posters and backgrounds is off in Settings.', self.page(rec.id))
        self.assertEqual(self.t.client.get(f'/recordings/{rec.id}/sports-image/poster').status_code, 404)

    def test_with_no_metadata_file_it_says_why_nothing_was_downloaded(self):
        self.configure(sidecar=False)
        rec = self.recording()
        self.drain()
        html = self.page(rec.id)
        self.assertIn('Not downloaded: this recording writes no metadata file, the one place they are used.', html)
        self.assertEqual(html.count('writes no metadata file'), 1)

    def test_the_image_is_served_as_nothing_but_an_image(self):
        rec = self.recording()
        self.drain()
        resp = self.t.client.get(f'/recordings/{rec.id}/sports-image/poster')
        self.assertEqual((resp.mimetype, resp.headers.get('X-Content-Type-Options')), ('image/png', 'nosniff'))
        resp.close()

    def test_an_unknown_role_is_a_404(self):
        rec = self.recording()
        self.assertEqual(self.t.client.get(f'/recordings/{rec.id}/sports-image/banner').status_code, 404)


if __name__ == '__main__':
    unittest.main()
