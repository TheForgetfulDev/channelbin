"""Tier 0 - the recording page's Play and Download (static/js/recording-player.js) and
util.js's copyText(), driven in a real DOM on a plain-http bare-IP origin.

dev/changelog/1312. What the browser half promises:

  * the <video> gets the file's URL only after the click's check has answered, so a missing
    file or a dead share is named in words instead of showing as a black box
  * a slow answer explains itself, counting up, because storage that has spun down can take
    ten seconds or more to wake - and adds that the storage may be offline after a minute
  * every failure (a missing file, an unreachable server, a codec the browser lacks) lands in the
    status line, and Download stays off until there is a file to download
  * copying works where navigator.clipboard does not exist - any plain-http origin that is
    not localhost (dev/docs/BUGS.md 2026-10-27)
  * a .ts or .mkv plays through the preview pipeline (dev/changelog/1314): the modal's own
    controls seek by starting a new session, quick skips add up into one, the position is
    where the session landed plus the player's clock, a long pause lets the session go, and
    every stop on the server is shown in its words
  * a recording still capturing plays live (dev/changelog/1316): the player starts at the live
    edge, follows the recorder across a segment change saying why it moved, skips back into
    what was captured and returns with Live, carries on across the end of a finished segment,
    asks again while the capture is between segments, and says what became of a capture that
    stopped under it

jsdom plays no media and computes no layout; the browser verification in the changelog
covers what it cannot.

  python3 -m unittest tests.test_recording_player_js
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'recording_player.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is None:
        proc = subprocess.run([shutil.which('node'), HARNESS, REPO],
                              capture_output=True, text=True, cwd=REPO, timeout=120)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stdout}\n{proc.stderr}')
        _RESULT = json.loads(proc.stdout)
    return _RESULT


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class RecordingPlayerTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_no_script_error(self):
        self.assertEqual(self.obs['errors'], [])

    def test_both_dialogs_are_registered(self):
        self.assertEqual(self.obs['dialogsRegistered'], ['download', 'play'])

    # ── copy on a non-secure origin ──────────────────────────────────────────
    def test_copy_works_without_navigator_clipboard(self):
        self.assertEqual(self.obs['secureContext']['clipboard'], 'absent')
        self.assertEqual(self.obs['copyOk'], 'resolved')
        self.assertEqual(self.obs['copiedValue'], '/dvr/Race day.mp4')
        self.assertEqual(self.obs['textareaLeftBehind'], 0)

    def test_copy_refused_says_so(self):
        self.assertTrue(self.obs['copyRefused'].startswith('rejected: Copy failed'))

    # ── the slow storage ─────────────────────────────────────────────────────
    def test_nothing_is_handed_to_the_video_until_the_check_answers(self):
        slow = self.obs['slow']
        for moment in ('start', 'at1s', 'at9s', 'at61s'):
            self.assertIsNone(slow[moment]['src'], moment)
            self.assertTrue(slow[moment]['downloadDisabled'], moment)
        self.assertEqual(slow['fetched'], '/recordings/7/file/check')

    def test_the_wait_explains_itself_and_counts(self):
        slow = self.obs['slow']
        self.assertEqual(slow['start']['status'], 'Opening the file…')
        self.assertIn('(9s)', slow['at9s']['status'])
        self.assertIn('spun down', slow['at9s']['status'])
        self.assertNotIn('offline', slow['at9s']['status'])
        self.assertIn('(61s)', slow['at61s']['status'])
        self.assertIn('may be offline', slow['at61s']['status'])

    def test_answered_check_starts_the_file_and_stops_the_counter(self):
        slow = self.obs['slow']
        self.assertEqual(slow['answered']['src'], '/recordings/7/file')
        self.assertFalse(slow['answered']['downloadDisabled'])
        self.assertEqual(slow['intervalsLeft'], 0)

    def test_playing_says_how_long_the_storage_took(self):
        playing = self.obs['slow']['playing']
        self.assertEqual(playing['kind'], 'ok')
        self.assertIn('Playing Race day.mp4', playing['status'])
        self.assertIn('61s to answer', playing['status'])

    def test_modal_uses_the_shared_player(self):
        start = self.obs['slow']['start']
        self.assertEqual(start['title'], 'Play - Race day')
        self.assertIn('player-modal', start['panelClass'])

    def test_footer_download_hands_over_the_file_and_keeps_the_player_open(self):
        slow = self.obs['slow']
        self.assertEqual(slow['footerDownload'],
                         {'href': '/recordings/7/file?download=1', 'download': 'Race day.mp4'})
        self.assertTrue(slow['stillOpen'])
        self.assertEqual(slow['afterClose']['modals'], 0)

    # ── every failure is named ───────────────────────────────────────────────
    def test_missing_file_is_named_and_nothing_is_loaded(self):
        m = self.obs['missing']
        self.assertEqual(m['kind'], 'error')
        self.assertIn('Could not open /dvr/Race day.mp4: No such file or directory (ENOENT)', m['status'])
        self.assertIsNone(m['src'])
        self.assertTrue(m['downloadDisabled'])

    def test_unreachable_server_is_said_in_words(self):
        u = self.obs['unreachable']
        self.assertEqual(u['kind'], 'error')
        self.assertIn('Lost contact with ChannelBin', u['status'])
        self.assertNotIn('Failed to fetch', u['status'])

    def test_unconverted_file_gives_the_reason_and_offers_download(self):
        n = self.obs['notNative']
        self.assertEqual(n['kind'], 'warn')
        self.assertIn('MPEG-TS', n['status'])
        self.assertIsNone(n['src'])
        self.assertFalse(n['downloadDisabled'])

    def test_caveat_shown_while_loading_and_codec_named_on_error(self):
        self.assertIn('HEVC', self.obs['hevcLoading']['status'])
        err = self.obs['hevcError']
        self.assertEqual(err['kind'], 'error')
        self.assertIn('video hevc, audio aac', err['status'])
        self.assertIn('DEMUXER_ERROR_NO_SUPPORTED_STREAMS', err['status'])
        self.assertIn('VLC', err['status'])

    def test_closing_before_the_answer_leaves_nothing_running(self):
        self.assertEqual(self.obs['closedEarly'], {'modals': 0, 'intervalsLeft': 0})

    # ── a .ts through the preview pipeline (dev/changelog/1314) ─────────────
    def test_pipeline_starts_a_session_from_the_beginning_with_the_seek_controls(self):
        p = self.obs['pipe']['started']
        self.assertEqual(p['posted'], [['/api/recordings/7/playback', {'offset': 0}]])
        self.assertEqual(p['skips'], ['-10 min', '-1 min', '-10 s', '+10 s', '+1 min', '+10 min'])
        self.assertEqual(p['barMax'], '7200')
        self.assertEqual(p['pos'], '0s / 2h 0m 0s')
        self.assertIsNone(p['src'])                       # nothing until the server is READY
        self.assertFalse(p['downloadDisabled'])

    def test_pipeline_plays_from_the_first_segment_not_a_live_edge(self):
        h = self.obs['pipe']['hls']
        self.assertEqual(h['count'], 1)
        self.assertEqual(h['url'], '/api/preview/s1/index.m3u8')
        self.assertEqual(h['config'].get('startPosition'), 0)
        self.assertNotIn('liveMaxLatencyDurationCount', h['config'])   # a pause must not jump ahead

    def test_position_is_where_the_session_landed_plus_the_players_clock(self):
        # Asked for 0 s, landed at 2.6 s (a keyframe), 10 s of playing since.
        self.assertEqual(self.obs['pipe']['playing']['pos'], '12s / 2h 0m 0s')

    def test_playing_says_how_it_plays(self):
        p = self.obs['pipe']['playing']
        self.assertEqual(p['kind'], 'ok')
        self.assertIn('MPEG-TS', p['status'])
        self.assertIn('without re-encoding the video', p['status'])

    def test_quick_skips_are_one_seek_from_the_real_position(self):
        pending = self.obs['pipe']['seekPending']
        self.assertEqual(pending['posted'], 0)
        self.assertEqual(pending['pos'], '30m 12s / 2h 0m 0s')
        seeked = self.obs['pipe']['seeked']
        self.assertEqual(seeked['posted'], [['/api/recordings/7/playback', {'offset': 1812.6}]])
        self.assertTrue(seeked['oldDestroyed'])

    def test_a_landing_far_from_the_request_is_not_believed(self):
        self.assertEqual(self.obs['pipe']['untrustedOrigin'], '30m 12s / 2h 0m 0s')

    def test_a_long_pause_lets_the_session_go_and_play_resumes_there(self):
        short = self.obs['pipe']['shortPause']
        self.assertEqual(short, {'posted': 0, 'destroyed': False})
        held = self.obs['pipe']['held']
        self.assertEqual(held['posted'], [['/api/preview/s2/stop', None]])
        self.assertTrue(held['destroyed'])
        self.assertEqual(held['pauseLabel'], 'Play')
        self.assertIn('Paused at 30m 17s', held['status'])
        resumed = self.obs['pipe']['resumed']
        self.assertEqual(resumed['posted'], [['/api/recordings/7/playback', {'offset': 1817.6}]])
        self.assertEqual(resumed['pauseLabel'], 'Pause')

    def test_the_bar_seeks_on_release_only(self):
        self.assertEqual(self.obs['pipe']['dragging'], {'pos': '1h 0m 0s / 2h 0m 0s', 'posted': 0})
        self.assertEqual(self.obs['pipe']['barSeek'], [['/api/recordings/7/playback', {'offset': 3600}]])

    def test_a_stop_on_the_server_is_shown_in_its_words(self):
        r = self.obs['pipe']['readFailed']
        self.assertEqual(r['kind'], 'error')
        self.assertIn("stopped reading the recording's file", r['status'])
        self.assertIn('Input/output error', r['status'])
        self.assertEqual(r['pauseLabel'], 'Play')

    def test_the_end_of_the_file_plays_out_and_says_so(self):
        done = self.obs['pipe']['serverDone']
        self.assertFalse(done['destroyed'])               # the buffer still plays
        self.assertNotEqual(done['kind'], 'error')
        end = self.obs['pipe']['atEnd']
        self.assertIn('Reached the end of the recording', end['status'])
        self.assertEqual(end['pauseLabel'], 'Play')
        # The label stays at the end; Play starts the recording again from the beginning.
        self.assertEqual(end['pos'], '2h 0m 0s / 2h 0m 0s')
        self.assertEqual(self.obs['pipe']['replay'], [['/api/recordings/7/playback', {'offset': 0}]])

    def test_closing_stops_the_session_and_leaves_no_wait(self):
        self.assertEqual(self.obs['pipe']['closed'],
                         {'posted': [['/api/preview/s7/stop', None]], 'modals': 0, 'timeoutsLeft': 0})

    def test_no_measured_duration_hides_the_bar_only(self):
        n = self.obs['pipe']['noDuration']
        self.assertTrue(n['barHidden'])
        self.assertEqual(n['pos'], '0s')
        self.assertEqual(len(n['skips']), 6)

    # ── a recording still capturing (dev/changelog/1316) ─────────────────────
    def test_capture_starts_live_with_nothing_to_download(self):
        c = self.obs['cap']['started']
        self.assertEqual(c['posted'], [['/api/recordings/7/playback', {'offset': 0, 'live': True}]])
        self.assertTrue(c['downloadDisabled'])
        self.assertFalse(c['liveHidden'])
        self.assertEqual(c['status'], 'Starting live…')

    def test_live_says_so_and_how_far_behind(self):
        c = self.obs['cap']['live']
        self.assertEqual(c['pos'], 'Live - 2m 40s captured so far')
        self.assertTrue(c['livePrimary'])
        self.assertEqual(c['barMax'], '160')
        self.assertIn('about ten seconds behind', c['status'])
        self.assertIn('without re-encoding the video', c['status'])

    def test_a_segment_change_is_followed_and_explained(self):
        self.assertEqual(self.obs['cap']['moved']['posted'],
                         [['/api/recordings/7/playback', {'offset': 163.5, 'live': True, 'moved_from': 1}]])
        c = self.obs['cap']['movedPlaying']
        self.assertTrue(c['livePrimary'])
        self.assertIn('moved to a new segment file: File stalled at 6316621824 bytes', c['status'])

    def test_skipping_back_leaves_live_and_live_returns(self):
        self.assertEqual(self.obs['cap']['back']['posted'],
                         [['/api/recordings/7/playback', {'offset': 110}]])
        behind = self.obs['cap']['behind']
        self.assertFalse(behind['livePrimary'])
        self.assertEqual(behind['pos'], '1m 50s / 2m 40s captured so far')
        self.assertEqual(self.obs['cap']['liveClick'],
                         [['/api/recordings/7/playback', {'offset': 160.5, 'live': True}]])

    def test_the_end_of_a_finished_segment_carries_on_into_the_next(self):
        self.assertEqual(self.obs['cap']['chained']['posted'][-1],
                         ['/api/recordings/7/playback', {'offset': 160}])

    def test_between_segments_it_says_so_and_asks_again(self):
        b = self.obs['cap']['betweenSegments']
        self.assertIn('starting segment 3', b['status'])
        self.assertIn('Trying again', b['status'])
        self.assertNotEqual(b['kind'], 'error')
        self.assertEqual(b['posted'], 1)
        self.assertEqual(self.obs['cap']['retried']['posted'], 2)

    def test_a_capture_that_stopped_says_what_it_is_now_and_stops_asking(self):
        s = self.obs['cap']['stopped']
        self.assertEqual(s['kind'], 'warn')
        self.assertIn('The recording stopped capturing.', s['status'])
        self.assertIn('final file is being made', s['status'])
        self.assertEqual(s['posted'], 1)
        self.assertTrue(s['liveHidden'])
        self.assertEqual(s['pauseLabel'], 'Play')
        # The position never reads past what was captured.
        self.assertEqual(s['pos'], '2m 40s / 2m 40s captured so far')

    def test_paused_plays_what_was_captured_and_says_why(self):
        p = self.obs['cap']['paused']
        self.assertEqual(p['posted'], [['/api/recordings/7/playback', {'offset': 0}]])
        self.assertTrue(p['liveHidden'])
        self.assertIn('The recording is paused', p['status'])
        self.assertEqual(p['pos'], '0s / 2m 10s captured so far')

    # ── Download from the kebab ──────────────────────────────────────────────
    def test_kebab_download_warns_of_the_wait_then_hands_over_the_file(self):
        self.assertIn('spun down', self.obs['kebabWaitingToast'])
        dl = self.obs['kebabDownload']
        self.assertEqual(dl['click'], [{'href': '/recordings/7/file?download=1',
                                        'download': 'Race day.mp4'}])
        self.assertIn('3.0 GB', dl['toast'])

    def test_kebab_download_on_a_dead_share_says_why(self):
        stale = self.obs['kebabStale']
        self.assertIn('Stale file handle', stale['toast'])
        self.assertIn('toast-error', stale['cls'])


if __name__ == '__main__':
    unittest.main()
