"""Tier 0 - the new-recording modal's profile-padding recompute
(static/js/guide.js::applyProfilePadding(), dev/docs/BUGS.md 2026-08-06).

Hand-typing a later Start Time into the guide's new-recording modal,
then touching the Recording Profile dropdown, silently reverted that edit back to the
program's own start time - even for a profile with no start-time padding at all. The cause
is entirely client-side: applyProfilePadding() recomputed BOTH modal-start and modal-stop
from the program's unpadded start/stop on every profile change, with no notion of "the user
already typed something here." Python cannot reach this, so this runs the shipped
static/js/util.js + static/js/guide.js against the markup the real /guide route rendered,
the same arrangement tests/test_logs_page_js.py uses. tests/support/guide_modal.mjs drives
it and reports observations; every assertion lives here.

What it covers: that a hand-edited Start (or Stop) field survives a later profile change,
that an untouched field still gets padded normally, that the info note explains what
happened (not just what padding a profile applied - product principle 1, surfacing what the
app just decided on the user's behalf), and that reopening the modal for a different program
resets the "already edited" tracking.

What it cannot cover: jsdom computes no layout, so this says nothing about how the note
reads visually - only about its text and which field held which value after which event.

Runs against a throwaway temp SQLite DB - never the live one.
  python3 -m unittest tests.test_guide_modal_padding_js
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
from app.database import RecordingProfile  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'guide_modal.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')

_RESULT = None


def _observe():
    """Boot the /guide page once in node and return every scenario's observations."""
    global _RESULT
    if _RESULT is not None:
        return _RESULT
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='guide_modal_js_')
    try:
        with t.app.app_context():
            acc = seed.make_account()
            seed.make_channel(acc, name='Test Channel', in_guide=True)
            # id=1, matching the '1' profile id the harness selects. Zero start padding,
            # real stop padding only - the exact shape of the real repro ("even one
            # that doesn't touch start time, this one only extends the end time").
            db.session.add(RecordingProfile(
                name='Extend End Only', pre_padding_minutes=0, post_padding_minutes=15))
            db.session.commit()
            page = t.client.get('/guide').get_data(as_text=True)
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as f:
            f.write(page)
        proc = subprocess.run(
            ['node', HARNESS, tmp, REPO],
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
class _Base(unittest.TestCase):
    SCENARIO = ''

    @classmethod
    def setUpClass(cls):
        if not cls.SCENARIO:
            raise unittest.SkipTest('base class - carries no cases')
        cls.obs = _observe()[cls.SCENARIO]
        if isinstance(cls.obs, dict) and 'error' in cls.obs:
            raise AssertionError(f'{cls.SCENARIO} threw in the page:\n{cls.obs["error"]}')

    def test_the_page_booted_without_errors(self):
        self.assertEqual(self.obs['errors'], [])


class NoManualEditTests(_Base):
    """The unmodified case: a profile change is still allowed to recompute both fields
    from the program's own start/stop, because the user never touched them."""
    SCENARIO = 'no_manual_edit'

    def test_profile_change_recomputes_untouched_fields(self):
        # pre=0 so start is unchanged from the program's own start; post=15 so stop moves.
        self.assertEqual(self.obs['start'], self.obs['openedStart'])
        self.assertNotEqual(self.obs['stop'], self.obs['openedStop'])

    def test_note_explains_the_padding_only(self):
        self.assertIn('ends 15 min late', self.obs['noteText'])
        self.assertNotIn('left as entered', self.obs['noteText'])
        self.assertTrue(self.obs['noteVisible'])


class HandTypedStartSurvivesProfileChangeTests(_Base):
    """The actual bug: BUGS.md 2026-08-06. A hand-typed Start must survive a later
    profile change even when that profile has zero start-time padding of its own."""
    SCENARIO = 'hand_typed_start_survives_profile_change'

    def test_hand_typed_start_is_not_reverted(self):
        self.assertEqual(self.obs['start'], '2026-08-10T20:30')

    def test_untouched_stop_still_gets_padded(self):
        from datetime import datetime, timedelta
        opened = datetime.strptime(self.obs['openedStop'], '%Y-%m-%dT%H:%M')
        expected = (opened + timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M')
        self.assertEqual(self.obs['stop'], expected)

    def test_note_explains_the_field_was_kept(self):
        self.assertIn('start time', self.obs['noteText'])
        self.assertIn('left as entered', self.obs['noteText'])
        self.assertIn('ends 15 min late', self.obs['noteText'])


class BothHandEditedTests(_Base):
    """Both fields hand-edited: a profile change must leave both exactly alone."""
    SCENARIO = 'both_hand_edited'

    def test_neither_field_is_touched(self):
        self.assertEqual(self.obs['start'], '2026-08-10T20:30')
        self.assertEqual(self.obs['stop'], '2026-08-10T20:45')

    def test_note_names_both_fields(self):
        self.assertIn('start time and stop time', self.obs['noteText'])
        self.assertIn('were left as entered', self.obs['noteText'])


class ReopenClearsEditFlagTests(_Base):
    """Opening the modal for a different program is a fresh start - the "user already
    edited this" tracking must not leak from one program's modal session to the next."""
    SCENARIO = 'reopen_clears_edit_flag'

    def test_first_edit_survived_its_own_profile_change(self):
        self.assertEqual(self.obs['afterFirstEdit'], '2026-08-10T20:30')

    def test_reopened_modal_pads_normally_again(self):
        # pre=0, so the freshly opened program's own UTC start (20:00) is unchanged by
        # profile 1 beyond the display-timezone conversion (America/New_York, UTC-4 in
        # August) - not the stale '20:30' the first program's modal session left behind.
        self.assertEqual(self.obs['reopenedStart'], '2026-08-11T16:00')


if __name__ == '__main__':
    unittest.main()
