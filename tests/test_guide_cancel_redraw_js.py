"""Tier 0 - the TV Guide redraws a cancelled recording's cell (static/js/guide.js,
dev/docs/BUGS.md 2026-11-16 08:44:30 PM, dev/changelog/1609).

A program's cell still read Scheduled after Cancel recording, until the page was reloaded.
Cancel recording deletes the row before it answers and then re-reads the grid; the grid
drew whichever answer came back last, so an older request answering after the cancel's
re-read put the recording back. The plain path is a characterization test; the late answer
is the regression guard.

Runs the shipped util.js + guide.js against the real /guide page in jsdom, with the two
/api/guide/epg answers the Flask app gave with the recording present and after it was
deleted (tests/support/guide_cancel_redraw.mjs). Throwaway temp DB - never the live one.
  python3 -m unittest tests.test_guide_cancel_redraw_js
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'guide_cancel_redraw.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    from app import db
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='guide_cancel_redraw_js_')
    try:
        with t.app.app_context():
            acc = seed.make_account()
            ch = seed.make_channel(acc, name='Documentary HD', in_guide=True)
            entry = seed.make_epg_entry(ch, title='The Long Watch', offset_minutes=120)
            rec = seed.make_recording(
                status='SCHEDULED', name='The Long Watch', channel_id=ch.id,
                start_time=entry.start_time, stop_time=entry.stop_time)
            db.session.commit()
            page = t.client.get('/guide').get_data(as_text=True)
            now = datetime.utcnow()
            q = (f'start={(now - timedelta(hours=1)).isoformat()}'
                 f'&end={(now + timedelta(hours=6)).isoformat()}')
            before = t.client.get(f'/api/guide/epg?{q}').get_json()
            db.session.delete(db.session.get(type(rec), rec.id))
            db.session.commit()
            after = t.client.get(f'/api/guide/epg?{q}').get_json()
        for name, body in (('page.html', page), ('epg-before.json', json.dumps(before)),
                           ('epg-after.json', json.dumps(after))):
            with open(os.path.join(tmp, name), 'w', encoding='utf-8') as f:
                f.write(body)
        proc = subprocess.run(['node', HARNESS, tmp, REPO],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        _RESULT = json.loads(proc.stdout)
        return _RESULT
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class CancelRedrawTests(unittest.TestCase):

    def obs(self, scenario):
        o = _observe()[scenario]
        if 'error' in o:
            self.fail(f'{scenario} threw in the page:\n{o["error"]}')
        self.assertEqual(o['errors'], [], scenario)
        self.assertGreater(o['atBoot'], 0, f'{scenario}: the fixture drew no scheduled cell')
        return o

    def test_the_cell_is_redrawn_after_cancel(self):
        self.assertEqual(self.obs('cancel')['afterCancel'], 0)

    def test_an_older_grid_answer_arriving_late_does_not_redraw_the_recording(self):
        o = self.obs('cancel_with_older_fetch_in_flight')
        self.assertEqual(o['afterCancel'], 0)
        self.assertEqual(o['afterLateAnswer'], 0)


if __name__ == '__main__':
    unittest.main()
