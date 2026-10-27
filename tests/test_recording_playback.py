"""Play and Download on a recording's page (app/playback.py, dev/changelog/1312).

Guards dev/docs/BUGS.md 2026-10-27 (copy on a plain-http origin) only indirectly; the rest is
the feature's own contract, each part of which is a promise the page makes to the user:

  * the page decides Play and Download from the row's columns ALONE - a finished recording
    may sit on storage that has spun down, and a page view must not wake it
  * the file is served by recording id, with Range support (the browser's player seeks with
    it), and only from the recording folder or the move destination
  * every way the click can fail shows the errors the OS gave - for the file, and for its
    folder when the file was not found (on this app's own share a stale mount opens the file as
    ENOENT while its folder answers ESTALE) - and never guesses at the cause: a file the user
    deleted and a share that dropped both reach this path (dev/changelog/1313)
  * the phone's bar on a finished recording is Play / Delete / kebab (DESIGN.md 9.5, amended)

No ffmpeg, no network, no /dvr - files are written under the test's own sandboxed recording
folder, which is what the route's runtime load_config() reads.
"""
import builtins
import errno
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, database, playback  # noqa: E402

PAYLOAD = bytes(range(256)) * 64   # 16 KiB with a recognizable byte at every offset


class _Rig(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')

    def tearDown(self):
        self.t.cleanup()

    def _file(self, name, data=PAYLOAD, folder=None):
        path = os.path.join(folder or self.dvr, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as fh:
            fh.write(data)
        return path

    def _rec(self, status='COMPLETED', output_path=None, vcodec='h264', acodec='aac'):
        rec = seed.make_recording(status=status, name='Race day', output_path=output_path,
                                  recorded_video_codec=vcodec, recorded_audio_codec=acodec)
        db.session.commit()
        return rec


class OfferTests(_Rig):
    """What the page offers, from the columns alone."""

    def test_converted_h264_mp4_plays_natively(self):
        o = playback.offer(self._rec(output_path='/nowhere/a.mp4'))
        self.assertTrue(o.play and o.download and o.native)
        self.assertIsNone(o.reason)
        self.assertIsNone(o.caveat)

    def test_hevc_plays_with_a_caveat_naming_the_browsers(self):
        o = playback.offer(self._rec(output_path='/nowhere/a.mp4', vcodec='hevc'))
        self.assertTrue(o.native)
        self.assertIn('HEVC', o.caveat)
        self.assertIn('Firefox', o.caveat)

    def test_non_aac_audio_plays_with_a_caveat(self):
        o = playback.offer(self._rec(output_path='/nowhere/a.mp4', acodec='ac3'))
        self.assertTrue(o.native)
        self.assertIn('ac3', o.caveat)

    def test_unconverted_files_play_through_the_pipeline(self):
        """dev/changelog/1314: a .ts or .mkv is repackaged as it plays, not sent to VLC."""
        for ext, word in (('.ts', 'MPEG-TS'), ('.mkv', 'Matroska')):
            with self.subTest(ext=ext):
                o = playback.offer(self._rec(output_path=f'/nowhere/a{ext}'))
                self.assertTrue(o.play and o.download and o.pipeline)
                self.assertFalse(o.native)
                self.assertIn(word, o.reason)
                self.assertNotIn('VLC', o.reason)

    def test_a_container_the_pipeline_does_not_take_is_download_only(self):
        o = playback.offer(self._rec(output_path='/nowhere/a.avi'))
        self.assertFalse(o.native or o.pipeline)
        self.assertIn('VLC', o.reason)

    def test_video_a_browser_cannot_decode_is_not_sent_through_the_pipeline(self):
        """The pipeline copies video as it is, so it cannot help an mpeg2 picture."""
        for path in ('/nowhere/a.ts', '/nowhere/a.mp4'):
            with self.subTest(path=path):
                o = playback.offer(self._rec(output_path=path, vcodec='mpeg2video'))
                self.assertFalse(o.native or o.pipeline)
                self.assertIn('mpeg2video', o.reason)
                self.assertIn('VLC', o.reason)

    def test_hevc_in_a_ts_carries_the_caveat_and_other_audio_does_not(self):
        """The pipeline re-encodes audio a browser lacks, so only the HEVC caveat applies."""
        self.assertIn('HEVC', playback.offer(self._rec(output_path='/n/a.ts', vcodec='hevc')).caveat)
        self.assertIsNone(playback.offer(self._rec(output_path='/n/a.ts', acodec='ac3')).caveat)

    def test_an_mp4_with_a_video_codec_browsers_lack_is_not_native(self):
        """One flag, one meaning: playable is decided from the codecs, not the extension."""
        o = playback.offer(self._rec(output_path='/nowhere/a.mp4', vcodec='mpeg2video'))
        self.assertFalse(o.native)
        self.assertIn('mpeg2video', o.reason)

    def test_unknown_codec_is_not_refused(self):
        """A row converted before the codec column existed: unknown is not proven-unplayable."""
        o = playback.offer(self._rec(output_path='/nowhere/a.mp4', vcodec=None, acodec=None))
        self.assertTrue(o.native)

    def test_failed_and_aborted_offer_a_kept_source_only(self):
        """A kept joined .ts plays through the pipeline and downloads (dev/changelog/1314)."""
        for status in ('FAILED', 'ABORTED'):
            with self.subTest(status=status):
                rec = self._rec(status=status, output_path='/nowhere/a.ts')
                self.assertEqual(playback.offer(rec, kept_source_on_disk=False), playback.Offer())
                o = playback.offer(rec, kept_source_on_disk=True)
                self.assertTrue(o.download and o.play and o.pipeline)

    def test_no_output_path_offers_nothing(self):
        self.assertEqual(playback.offer(self._rec(output_path=None)), playback.Offer())

    def test_every_recording_status_has_a_branch(self):
        """A new status must be placed on purpose, not fall into the default."""
        statuses = database.status_vocabularies()['REC_STATUS'].values()
        with self.assertNoLogs('app.playback', level='WARNING'):
            for status in statuses:
                playback.offer(self._rec(status=status, output_path='/nowhere/a.mp4'))

    def test_offer_never_touches_the_filesystem(self):
        def refuse(*a, **k):
            raise AssertionError(f'filesystem touched: {a!r}')
        rec = self._rec(output_path='/nowhere/a.mp4')
        with mock.patch('os.stat', refuse), mock.patch('os.lstat', refuse), \
                mock.patch.object(builtins, 'open', refuse):
            playback.offer(rec)


class PageTests(_Rig):
    def _page(self, rec):
        resp = self.t.client.get(f'/recordings/{rec.id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    @staticmethod
    def _bar(html):
        start = html.index('<div class="mobile-actbar">')
        bar = html[start:html.index('<div id="status-strip-slot">', start)]
        menu_at = bar.index('<div class="menu-wrap">')
        return bar[:menu_at], bar[menu_at:]

    def test_page_load_never_touches_a_finished_recordings_file(self):
        """The storage under a finished recording may be asleep; only a click may wake it.
        Scoped to the recording's own file: the page's older probes of files beside it (the
        metadata sidecar, the segment files) are a separate backlog question."""
        out = os.path.join(self.dvr, 'sleeping', 'race.mp4')
        rec = self._rec(output_path=out)
        touched = []
        real_stat, real_lstat, real_open = os.stat, os.lstat, builtins.open

        def watch(real):
            def wrapper(path, *a, **k):
                if isinstance(path, (str, bytes, os.PathLike)) and os.fsdecode(path) == out:
                    touched.append(os.fsdecode(path))
                return real(path, *a, **k)
            return wrapper
        with mock.patch('os.stat', watch(real_stat)), mock.patch('os.lstat', watch(real_lstat)), \
                mock.patch.object(builtins, 'open', watch(real_open)):
            html = self._page(rec)
        self.assertEqual(touched, [])
        self.assertIn('data-act="play"', html)
        self.assertIn('data-act="download"', html)

    def test_phone_bar_on_a_finished_recording_is_play_and_delete(self):
        html = self._page(self._rec(output_path=os.path.join(self.dvr, 'a.mp4')))
        bar, kebab = self._bar(html)
        self.assertEqual(re.findall(r'data-act="([\w-]+)"', bar), ['play', 'delete'])
        self.assertNotIn('Record again', bar)
        self.assertIn('Record again', kebab)
        self.assertIn('data-act="download"', kebab)

    def test_phone_bar_with_nothing_to_play_is_delete_alone(self):
        bar, kebab = self._bar(self._page(self._rec(status='ABORTED', output_path=None)))
        self.assertEqual(re.findall(r'data-act="([\w-]+)"', bar), ['delete'])
        self.assertIn('Record again', kebab)

    def test_phone_bar_with_a_retry_promoted_keeps_retry_and_play(self):
        """The bar holds two of retry, Play, Delete in that order; Delete goes to the kebab."""
        kept = self._file('cancelled.ts')
        bar, kebab = self._bar(self._page(self._rec(status='ABORTED', output_path=kept)))
        self.assertEqual(re.findall(r'data-act="([\w-]+)"', bar), ['retry-convert', 'play'])
        self.assertIn('data-act="download"', kebab)
        self.assertIn('data-act="delete"', kebab)

    def test_failed_recording_with_a_kept_ts_offers_play_and_download(self):
        kept = self._file('failed.ts')
        html = self._page(self._rec(status='FAILED', output_path=kept))
        self.assertIn('data-act="download"', html)
        self.assertIn('data-act="play"', html)
        bar, kebab = self._bar(html)
        # The phone bar keeps its two recovery actions; Play waits in the kebab.
        self.assertNotIn('data-act="play"', bar)
        self.assertIn('data-act="play"', kebab)

    def test_failed_recording_without_a_kept_file_offers_no_play(self):
        html = self._page(self._rec(status='FAILED', output_path=os.path.join(self.dvr, 'gone.ts')))
        self.assertNotIn('data-act="play"', html)

    def test_page_hands_the_player_the_vendored_hls_js(self):
        html = self._page(self._rec(output_path=os.path.join(self.dvr, 'a.ts')))
        self.assertRegex(html, r'js/recording-player\.js[^>]*\s+data-hls-js="[^"]*vendor/hls\.js/hls\.light\.min\.js')

    def test_a_scheduled_recording_offers_neither(self):
        """A recording still capturing offers Play (tests/test_playback_while_capturing.py)."""
        html = self._page(self._rec(status='SCHEDULED', output_path=None))
        self.assertNotIn('data-act="play"', html)
        self.assertNotIn('data-act="download"', html)

    def test_page_loads_the_player_module(self):
        html = self._page(self._rec(output_path=os.path.join(self.dvr, 'a.mp4')))
        self.assertIn('js/recording-player.js', html)


class FileRouteTests(_Rig):
    def test_range_request_gets_206_with_the_requested_bytes(self):
        rec = self._rec(output_path=self._file('a.mp4'))
        resp = self.t.client.get(f'/recordings/{rec.id}/file', headers={'Range': 'bytes=100-199'})
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(resp.headers['Content-Range'], f'bytes 100-199/{len(PAYLOAD)}')
        self.assertEqual(resp.get_data(), PAYLOAD[100:200])
        self.assertEqual(resp.mimetype, 'video/mp4')
        resp.close()

    def test_whole_file_and_download_header(self):
        rec = self._rec(output_path=self._file('Race day.mp4'))
        resp = self.t.client.get(f'/recordings/{rec.id}/file?download=1')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_data(), PAYLOAD)
        self.assertIn('attachment', resp.headers['Content-Disposition'])
        self.assertIn('Race day.mp4', resp.headers['Content-Disposition'])
        resp.close()

    def test_ts_is_served_as_mpegts(self):
        rec = self._rec(output_path=self._file('a.ts'))
        resp = self.t.client.get(f'/recordings/{rec.id}/file?download=1')
        self.assertEqual(resp.mimetype, 'video/mp2t')
        resp.close()

    def test_a_path_outside_the_recording_folders_is_refused(self):
        outside = self._file('elsewhere.mp4', folder=os.path.join(self.t._tmpdir, 'not-dvr'))
        rec = self._rec(output_path=outside)
        for url in (f'/recordings/{rec.id}/file', f'/recordings/{rec.id}/file/check'):
            with self.subTest(url=url):
                resp = self.t.client.get(url)
                self.assertEqual(resp.status_code, 403)
                self.assertIn('outside the recording folder', resp.get_json()['error'])

    def test_a_symlink_out_of_the_recording_folder_is_refused(self):
        outside = self._file('secret.mp4', folder=os.path.join(self.t._tmpdir, 'not-dvr'))
        link = os.path.join(self.dvr, 'link.mp4')
        os.symlink(outside, link)
        rec = self._rec(output_path=link)
        self.assertEqual(self.t.client.get(f'/recordings/{rec.id}/file').status_code, 403)

    def test_the_move_destination_counts_even_with_moving_switched_off(self):
        dest = os.path.join(self.t._tmpdir, 'library')
        moved = self._file('moved.mp4', folder=dest)
        rec = self._rec(output_path=moved)
        cfg = {'recording': {'dvr_output_dir': self.dvr,
                             'move_on_complete': {'enabled': False, 'destination': dest}}}
        self.assertEqual(playback.resolve_file(rec, cfg), moved)

    def test_a_recording_with_no_finished_file_is_refused(self):
        rec = self._rec(status='SCHEDULED', output_path=None)
        resp = self.t.client.get(f'/recordings/{rec.id}/file')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('no finished file', resp.get_json()['error'])

    def test_unknown_recording(self):
        resp = self.t.client.get('/recordings/987654/file')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.get_json())

    def test_missing_file_is_named_on_the_file_route_too(self):
        rec = self._rec(output_path=os.path.join(self.dvr, 'gone.mp4'))
        resp = self.t.client.get(f'/recordings/{rec.id}/file')
        self.assertEqual(resp.status_code, 404)
        self.assertIn('No such file or directory (ENOENT)', resp.get_json()['error'])


class CheckRouteTests(_Rig):
    def _check(self, rec):
        resp = self.t.client.get(f'/recordings/{rec.id}/file/check')
        return resp.status_code, resp.get_json()

    def test_a_playable_file_answers_with_relative_urls_and_its_size(self):
        """Relative URLs work on any origin - bare IP, hostname, http or https."""
        rec = self._rec(output_path=self._file('a.mp4'))
        code, data = self._check(rec)
        self.assertEqual(code, 200)
        self.assertTrue(data['success'] and data['native'])
        self.assertEqual(data['size'], len(PAYLOAD))
        self.assertEqual(data['name'], 'a.mp4')
        self.assertEqual(data['media_url'], f'/recordings/{rec.id}/file')
        self.assertEqual(data['download_url'], f'/recordings/{rec.id}/file?download=1')

    def test_an_unconverted_file_answers_with_the_reason(self):
        rec = self._rec(output_path=self._file('a.ts'))
        code, data = self._check(rec)
        self.assertEqual(code, 200)
        self.assertFalse(data['native'])
        self.assertIn('MPEG-TS', data['reason'])

    def test_the_check_reads_the_file_rather_than_stat_it(self):
        """A network mount can answer a stat from cache while the disk is asleep; a read is
        what wakes it, so the player's own first request does not have to."""
        path = self._file('a.mp4')
        rec = self._rec(output_path=path)
        opened = []
        real_open = builtins.open

        def watch(p, *a, **k):
            if os.fsdecode(p) == path:
                opened.append(a[0] if a else k.get('mode'))
            return real_open(p, *a, **k)
        with mock.patch.object(builtins, 'open', watch):
            self._check(rec)
        self.assertEqual(opened, ['rb'])

    def test_the_time_the_storage_took_is_reported(self):
        rec = self._rec(output_path=self._file('a.mp4'))
        with mock.patch('app.playback.time.monotonic', side_effect=[100.0, 108.4]):
            code, data = self._check(rec)
        self.assertEqual(data['opened_seconds'], 8.4)

    # Words that would be a guess at the cause rather than a report of the error.
    GUESSES = ('moved', 'deleted', 'mounted', 'mount ', 'offline', 'unplugged')

    def assertNoGuess(self, message):
        for word in self.GUESSES:
            self.assertNotIn(word, message.lower())

    def test_file_gone_shows_the_error_without_a_guess(self):
        path = os.path.join(self.dvr, 'gone.mp4')
        code, data = self._check(self._rec(output_path=path))
        self.assertEqual(code, 404)
        self.assertIn(f'Could not open {path}: No such file or directory (ENOENT).',
                      data['error'])
        self.assertIn('is readable, and the file is not in it', data['error'])
        self.assertNoGuess(data['error'])

    def test_folder_gone_shows_both_errors(self):
        folder = os.path.join(self.dvr, 'vanished')
        code, data = self._check(self._rec(output_path=os.path.join(folder, 'a.mp4')))
        self.assertEqual(code, 404)
        self.assertIn('No such file or directory (ENOENT).', data['error'])
        self.assertIn(f'Its folder {folder} could not be read either: '
                      'No such file or directory (ENOENT).', data['error'])
        self.assertNoGuess(data['error'])

    def test_stale_share_shows_both_errors_without_a_guess(self):
        """The shape measured on /dvr-complete: the file opens as ENOENT, the folder ESTALE."""
        folder = os.path.join(self.dvr, 'share')
        path = os.path.join(folder, 'a.mp4')
        rec = self._rec(output_path=path)
        real_stat = os.stat

        def stale(p, *a, **k):
            if os.fsdecode(p) == folder:
                raise OSError(errno.ESTALE, 'Stale file handle')
            return real_stat(p, *a, **k)
        with mock.patch('os.stat', stale):
            code, data = self._check(rec)
        self.assertEqual(code, 503)
        self.assertIn('No such file or directory (ENOENT).', data['error'])
        self.assertIn('could not be read either: Stale file handle (ESTALE).', data['error'])
        self.assertNoGuess(data['error'])

    def test_storage_error_on_open_is_reported_as_given(self):
        path = self._file('a.mp4')
        rec = self._rec(output_path=path)
        real_open = builtins.open

        def stale(p, *a, **k):
            if os.fsdecode(p) == path:
                raise OSError(errno.EIO, 'Input/output error')
            return real_open(p, *a, **k)
        with mock.patch.object(builtins, 'open', stale):
            code, data = self._check(rec)
        self.assertEqual(code, 503)
        self.assertEqual(data['error'], f'Could not open {path}: Input/output error (EIO).')

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root reads anything')
    def test_unreadable_file(self):
        path = self._file('locked.mp4')
        os.chmod(path, 0)
        try:
            code, data = self._check(self._rec(output_path=path))
        finally:
            os.chmod(path, 0o600)
        self.assertEqual(code, 403)
        self.assertIn(f'Could not open {path}: Permission denied (EACCES).', data['error'])

    def test_failed_recording_kept_ts_is_downloadable(self):
        rec = self._rec(status='FAILED', output_path=self._file('kept.ts'))
        code, data = self._check(rec)
        self.assertEqual(code, 200)
        self.assertEqual(data['download_url'], f'/recordings/{rec.id}/file?download=1')


if __name__ == '__main__':
    unittest.main()
