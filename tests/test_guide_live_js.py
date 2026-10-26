"""Tier 0 - the TV Guide and the sidebar's Recordings count following a recording as it
starts (dev/docs/BUGS.md 2026-10-23 06:36:01 AM and 06:36:02 AM).

In the first-time-user test the guide cell stayed SCHEDULED after the recording began and
the sidebar said 0 recordings while it ran, then 1 after it completed. The guide re-read its
grid only on a five-minute timer, and the count was rendered once and never updated. Both now
follow base.html's /api/nav-status poll: the guide re-reads its grid when the recording
signature moves, and the count is rewritten from the payload's recordings_total. All of it
happens in the browser, so tests/support/guide_live.mjs drives the shipped scripts against
the page and the two payloads the Flask app really answered, and every assertion is here.

What it cannot cover: jsdom computes no layout, and the EPG grid is stubbed empty, so this
says the grid was asked for again, not how the cell then looked.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_guide_live_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import REC_STATUS_IN_PROGRESS  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'guide_live.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='guide_live_js_')
    try:
        with t.app.app_context():
            acc = seed.make_account()
            ch = seed.make_channel(acc, name='Test Channel', in_guide=True)
            db.session.commit()
            ch_id = ch.id
            files = {'page.html': t.client.get('/guide').get_data(as_text=True),
                     'nav-before.json': t.client.get('/api/nav-status').get_data(as_text=True)}
            seed.make_recording(status=REC_STATUS_IN_PROGRESS, channel_id=ch_id)
            db.session.commit()
            db.session.remove()
            files['nav-after.json'] = t.client.get('/api/nav-status').get_data(as_text=True)
        for name, body in files.items():
            with open(os.path.join(tmp, name), 'w', encoding='utf-8') as f:
                f.write(body)
        proc = subprocess.run([shutil.which('node'), HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        _RESULT = {'obs': json.loads(proc.stdout),
                   'nav': {k: json.loads(files[f'nav-{k}.json']) for k in ('before', 'after')}}
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


class NavStatusPayloadTests(unittest.TestCase):
    """The server half: the payload carries the count the sidebar shows."""

    def test_recordings_total_counts_every_recording(self):
        t = make_test_app()
        try:
            with t.app.app_context():
                self.assertEqual(t.client.get('/api/nav-status').get_json()['recordings_total'], 0)
                seed.make_recording()
                seed.make_recording(status=REC_STATUS_IN_PROGRESS)
                db.session.commit()
                db.session.remove()
                self.assertEqual(t.client.get('/api/nav-status').get_json()['recordings_total'], 2)
        finally:
            t.cleanup()


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class _Base:
    SCENARIO = ''

    @classmethod
    def setUpClass(cls):
        res = _observe()
        cls.nav = res['nav']
        cls.obs = res['obs'][cls.SCENARIO]
        if 'error' in cls.obs:
            raise AssertionError(f'{cls.SCENARIO} threw in the page:\n{cls.obs["error"]}')

    def test_the_page_ran_without_errors(self):
        self.assertEqual(self.obs['errors'], [])


class RecordingStartsTests(_Base, unittest.TestCase):
    SCENARIO = 'recording_starts'

    def test_the_fixture_signature_moved(self):
        self.assertNotEqual(self.nav['before']['recording_signature'],
                            self.nav['after']['recording_signature'])

    def test_the_guide_registers_its_hook(self):
        self.assertEqual(self.obs['hook'], 'function')

    def test_the_load_read_the_grid(self):
        """The load's own fetches; the first poll, which only records, adds none - the
        next test counts from here."""
        self.assertGreater(self.obs['atBoot']['gridFetches'], 0)

    def test_a_recording_starting_rereads_the_grid_once(self):
        boot = self.obs['atBoot']['gridFetches']
        self.assertEqual(self.obs['afterChange']['gridFetches'], boot + 1)
        self.assertEqual(self.obs['afterNextPoll']['gridFetches'], boot + 1)

    def test_the_sidebar_count_follows_the_poll(self):
        self.assertEqual(self.obs['atBoot']['recCount'], '0')
        self.assertEqual(self.obs['afterChange']['recCount'], '1')


class RecordDialogOpenTests(_Base, unittest.TestCase):
    """A re-render under the open record dialog would close it mid-choice; the hidden
    copy of the same dialog must not hold the refresh forever either."""
    SCENARIO = 'record_dialog_open'

    def test_an_open_dialog_holds_the_reread_until_it_closes(self):
        boot = self.obs['atBoot']
        self.assertEqual(self.obs['whileOpen'], boot)
        self.assertEqual(self.obs['afterClose'], boot + 1)


if __name__ == '__main__':
    unittest.main()
