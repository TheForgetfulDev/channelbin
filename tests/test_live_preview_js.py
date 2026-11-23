"""Tier 0 - the live preview dialog (static/js/live-preview.js) when the browser cannot decode
the stream, driven in a real DOM (dev/changelog/1461, dev/docs/BUGS.md 2026-11-06 06:39).

What the browser half promises:

  * a decode failure reported only by the <video> element - hls.js raising nothing, which is
    what channel 44107's stream did in Chrome 155 - is acted on: one recovery, then the failure
  * the failure is the browser's own words, never a guess at the codec
  * the copy's session is let go at once, and a GPU re-encode is offered; pressing it starts a
    new session that asks for the re-encode, and playing says the video is re-encoded
  * a re-encode restarted on the CPU after its GPU decode died says so (dev/changelog/1688)
  * a re-encode that fails too says so and offers nothing more
  * with no GPU, the server's reason stands where the button would be

  python3 -m unittest tests.test_live_preview_js
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'live_preview.mjs')
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
class LivePreviewDecodeFailureTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_no_script_errors(self):
        self.assertEqual(self.obs['errors'], [])

    def test_the_elements_own_error_is_recovered_once_then_reported(self):
        self.assertEqual(self.obs['firstError']['recovered'], 1)
        self.assertNotEqual(self.obs['firstError']['kind'], 'error')
        f = self.obs['failed']
        self.assertEqual(f['kind'], 'error')
        self.assertEqual(f['recovered'], 1)

    def test_the_failure_is_the_browsers_words_and_no_guess(self):
        st = self.obs['failed']['status']
        self.assertEqual(st, 'This browser could not play this stream: PipelineStatus::'
                             'PIPELINE_ERROR_DECODE: Failed to send video packet for decoding.')
        for guess in ('HEVC', 'usual reason', 'audio codec'):
            self.assertNotIn(guess, st)

    def test_the_copys_session_is_let_go_at_once(self):
        self.assertEqual(self.obs['failed']['stops'], ['/api/preview/s1/stop'])
        self.assertTrue(self.obs['failed']['destroyed'])

    def test_the_reencode_is_offered_and_asked_for(self):
        self.assertEqual(self.obs['failed']['button'], 'Try re-encoded')
        self.assertEqual(self.obs['clicked']['posted'],
                         [['/api/channels/9/preview', {'reencode': True}]])
        self.assertIsNone(self.obs['clicked']['offer'])
        p = self.obs['reencodePlaying']
        self.assertEqual(p['kind'], 'ok')
        self.assertIn('re-encodes the video on the GPU', p['status'])

    def test_a_restart_on_the_cpu_is_said_when_it_happens_and_while_playing(self):
        """A re-encode whose GPU decode died is restarted on the CPU by the server, and the
        dialog says so - at the poll that first carries the note, and in the playing status after
        it (dev/changelog/1688)."""
        n = self.obs['restartNoted']
        self.assertEqual(n['kind'], 'warn')
        self.assertTrue(n['status'].startswith('The GPU could not decode this channel'), n['status'])
        p = self.obs['restartPlaying']
        self.assertEqual(p['kind'], 'warn')
        self.assertIn('re-encodes the video on the GPU', p['status'])
        self.assertIn('restarted it decoding on the CPU', p['status'])

    def test_a_failed_reencode_says_so_and_offers_nothing_more(self):
        f = self.obs['reencodeFailed']
        self.assertTrue(f['status'].startswith('This browser could not play the re-encoded video: '
                                               'append failed'), f['status'])
        self.assertIsNone(f['offer'])

    def test_without_a_gpu_the_reason_stands_where_the_button_would(self):
        n = self.obs['noGpu']
        self.assertIn("is unsupported", n['status'])
        self.assertIsNone(n['button'])
        self.assertIn('there is no GPU device', n['offer'])


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class FeedPreviewRoundTests(unittest.TestCase):
    """A feed preview (dev/changelog/1645): the player attaches to one round at a time, lets go of
    it when the output moves to another source, attaches to the next with its own decoder, and says
    what is on."""

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()

    def test_it_starts_on_the_feed_route_and_plays_round_one(self):
        r1 = self.obs['feedRound1']
        self.assertEqual(r1['posted'], ['/api/feed/1000000001/preview'])
        self.assertEqual(r1['url'], '/api/preview/f1/index.m3u8?round=1')
        self.assertEqual(r1['made'], 1)
        self.assertEqual(r1['status'], 'Playing what an IPTV player gets: the waiting screen. Its sound is '
                                       're-encoded to AAC so this browser can play it.')

    def test_the_first_connect_says_connecting_not_moved(self):
        self.assertIn('Connecting to the stream', self.obs['feedConnecting']['status'])

    def test_a_new_round_lets_go_of_the_old_one_and_attaches_again(self):
        switching = self.obs['feedSwitching']
        self.assertTrue(switching['r1Destroyed'])
        self.assertEqual(switching['made'], 1, 'nothing is attached until the next round is ready')
        self.assertIn('moved to another source', switching['status'])
        r2 = self.obs['feedRound2']
        self.assertEqual(r2['made'], 2)
        self.assertEqual(r2['url'], '/api/preview/f1/index.m3u8?round=2')
        self.assertTrue(r2['status'].startswith('Playing what an IPTV player gets: "Local game".'), r2['status'])

    def test_a_round_restarted_on_the_cpu_is_named_as_that(self):
        """A feed round that died decoding on the GPU comes back as a new round; the player says
        it restarted on the CPU and why, never that the output moved (dev/changelog/1688)."""
        r = self.obs['feedRestarting']
        self.assertEqual(r['kind'], 'warn')
        self.assertIn('restarted it decoding on the CPU', r['status'])
        self.assertNotIn('moved to another source', r['status'])
        done = self.obs['feedRestarted']
        self.assertEqual(done['status'], 'Restarted decoding on the CPU - starting playback…')
        self.assertEqual(done['url'], '/api/preview/f1/index.m3u8?round=3')
