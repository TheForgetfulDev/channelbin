"""Tier 0 - the block-account hint has one copy, read by both dialogs (dev/changelog/1370).

The hint under "Block account use during this recording" is drawn by the record modal's
`block_picker` macro (templates/_record_modal.html), and the running recording's Block
accounts dialog in static/js/recording-switch.js reads the rendered macro's text rather than
keeping a copy of its own. This drives the shipped script against the picker a real /guide
render drew and checks the dialog shows that same sentence. tests/support/recording_block_hint.mjs
reports; every assertion lives here.

Runs against a throwaway temp SQLite DB - never the live one.
  python3 -m unittest tests.test_recording_block_hint_js
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'recording_block_hint.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

HINT = ('This recording will prevent using the account(s) you block here, even if the top '
        'channel in the group is on that account. If there are no other accounts available, '
        'the recording waits for the block to end.')


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class BlockHintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t = make_test_app()
        tmp = tempfile.mkdtemp(prefix='recording_block_hint_js_')
        try:
            with t.app.app_context():
                seed.make_channel(seed.make_account(), name='Documentary HD', in_guide=True)
                page = t.client.get('/guide').get_data(as_text=True)
            m = re.search(r'<details[^>]*id="active-rec-blocks".*?</details>', page, re.S)
            assert m, 'the /guide page no longer draws the active recording block picker'
            with open(os.path.join(tmp, 'picker.html'), 'w', encoding='utf-8') as f:
                f.write(m.group(0))
            proc = subprocess.run(['node', HARNESS, tmp, REPO], capture_output=True,
                                  text=True, timeout=120, cwd=REPO)
            if proc.returncode != 0:
                raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
            cls.obs = json.loads(proc.stdout)
        finally:
            t.cleanup()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_script_error(self):
        self.assertEqual(self.obs['errors'], [])

    def test_the_record_modal_says_the_hint(self):
        self.assertEqual(' '.join(self.obs['pickerHint'].split()), HINT)

    def test_the_running_recordings_dialog_says_the_same_hint(self):
        self.assertEqual(self.obs['dialogHint'], HINT)
