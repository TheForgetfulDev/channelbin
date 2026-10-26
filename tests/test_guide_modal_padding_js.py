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
class _Base:
    SCENARIO = ''

    @classmethod
    def setUpClass(cls):
        cls.obs = _observe()[cls.SCENARIO]
        if isinstance(cls.obs, dict) and 'error' in cls.obs:
            raise AssertionError(f'{cls.SCENARIO} threw in the page:\n{cls.obs["error"]}')

    def test_the_page_booted_without_errors(self):
        self.assertEqual(self.obs['errors'], [])


class NoManualEditTests(_Base, unittest.TestCase):
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


class HandTypedStartSurvivesProfileChangeTests(_Base, unittest.TestCase):
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


class BothHandEditedTests(_Base, unittest.TestCase):
    """Both fields hand-edited: a profile change must leave both exactly alone."""
    SCENARIO = 'both_hand_edited'

    def test_neither_field_is_touched(self):
        self.assertEqual(self.obs['start'], '2026-08-10T20:30')
        self.assertEqual(self.obs['stop'], '2026-08-10T20:45')

    def test_note_names_both_fields(self):
        self.assertIn('start time and stop time', self.obs['noteText'])
        self.assertIn('were left as entered', self.obs['noteText'])


class ReopenClearsEditFlagTests(_Base, unittest.TestCase):
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



class RecordNowForAProgramAlreadyOnTests(_Base, unittest.TestCase):
    """"Schedule recording" for a show already on made a first-time tester stop and wonder
    whether it would record now or later (dev/changelog/1290). While the warning applies, the
    dialog and its button say Record now, and the warning names that button."""
    SCENARIO = 'record_now_labels'

    def test_a_program_already_on_reads_record_now(self):
        self.assertEqual(self.obs['late'], {'title': 'Record now', 'submit': 'Record now'})

    def test_a_start_typed_into_the_future_goes_back_to_schedule(self):
        self.assertEqual(self.obs['typedFuture'],
                         {'title': 'Schedule recording', 'submit': 'Schedule recording'})

    def test_a_program_not_yet_on_reads_schedule(self):
        self.assertEqual(self.obs['future'],
                         {'title': 'Schedule recording', 'submit': 'Schedule recording'})

    def test_the_warning_names_the_button_it_is_about(self):
        self.assertIn('the moment you press Record now', self.obs['lateText'])


class ReplaceKeepsItsOwnWordsTests(_Base, unittest.TestCase):
    """A replace also deletes the recording it replaces, so its button keeps saying so."""
    SCENARIO = 'replace_keeps_its_words'

    def test_replace_is_not_relabelled(self):
        self.assertEqual(self.obs['labels'],
                         {'title': 'Replace recording', 'submit': 'Replace recording'})

    def test_the_warning_names_the_replace_button(self):
        self.assertIn('the moment you press Replace recording', self.obs['lateText'])


class RecordModalLayoutTests(_Base, unittest.TestCase):
    """On a phone the aired-part warning sat below the fold behind the sheet's buttons, and
    both testers stopped on a Stream URL field they could not act on (dev/changelog/1290)."""
    SCENARIO = 'modal_layout'

    def test_the_warning_comes_before_the_form_fields(self):
        self.assertEqual(self.obs['order'], ['modal-late-note', 'modal-name', 'modal-start'])

    def test_the_stream_url_is_inside_a_disclosure(self):
        self.assertEqual(self.obs['isDetails'], 'DETAILS')
        self.assertTrue(self.obs['inPick'])

    def test_the_disclosure_is_closed_again_on_the_next_open(self):
        self.assertTrue(self.obs['openedOnce'])
        self.assertFalse(self.obs['openAfterReopen'])

    def test_the_url_still_goes_with_the_form(self):
        self.assertEqual(self.obs['urlValue'], 'http://example.test/live/1')


class StartFollowUpTests(_Base, unittest.TestCase):
    """The grid redrew the moment the save answered, before the recorder had started, and
    nothing asked again until the next nav poll, so the guide cell said SCHEDULED for 8 to
    10 s and the red pill came late (dev/changelog/1290)."""
    SCENARIO = 'start_followup'

    def test_the_page_booted_without_errors(self):
        self.assertEqual(self.obs['now']['errors'], [])
        self.assertEqual(self.obs['later']['errors'], [])

    def test_a_recording_that_starts_now_is_followed_up_soon(self):
        self.assertGreaterEqual(self.obs['now']['followups'], 1)
        self.assertLessEqual(min(self.obs['now']['delays']), 2000)

    def test_a_recording_for_later_is_not(self):
        self.assertEqual(self.obs['later']['followups'], 0)


if __name__ == '__main__':
    unittest.main()


class ProfileChangeRenamesTests(_Base, unittest.TestCase):
    """A profile carries its own filename template and tag cleanup, so picking one in a
    new-recording modal re-asks the server for the name, as it re-pads the times
    (dev/changelog/1161)."""
    SCENARIO = 'profile_change_renames'

    def test_opens_with_the_server_rendered_name_and_asks_nothing(self):
        self.assertEqual(self.obs['opened'], 'Test Program')

    def test_picking_a_profile_renames_with_it(self):
        self.assertEqual(self.obs['afterPick'], 'Renamed for 1')

    def test_no_profile_asks_for_the_global_naming(self):
        self.assertEqual(self.obs['afterNone'], 'Renamed for null')

    def test_the_request_carries_the_showing(self):
        first = self.obs['requests'][0]
        self.assertEqual(first['profile_id'], '1')
        self.assertEqual(first['title'], 'Test Program')
        self.assertEqual(first['sub_title'], 'Pilot')
        self.assertEqual(first['category'], 'Drama')
        self.assertEqual(first['channel_name'], 'Chan')
        self.assertEqual(first['start_time'], '2026-08-10T20:00:00')
        self.assertIsNone(self.obs['requests'][1]['profile_id'])


class TypedNameSurvivesTests(_Base, unittest.TestCase):
    SCENARIO = 'typed_name_survives_profile_change'

    def test_a_typed_name_is_kept_and_not_even_asked_about(self):
        self.assertEqual(self.obs['name'], 'My own name')
        self.assertEqual(self.obs['requests'], [])


class EditDoesNotRenameTests(_Base, unittest.TestCase):
    SCENARIO = 'edit_does_not_rename'

    def test_a_scheduled_recordings_name_is_left_alone(self):
        self.assertEqual(self.obs['name'], 'Stored name')
        self.assertEqual(self.obs['requests'], [])


class ProgramAlreadyOnSaysWhatIsMissedTests(_Base, unittest.TestCase):
    """A program that started 20 minutes ago opened "Schedule recording" with the past start
    time and nothing else; the user learned the first 20 minutes were lost from the
    RECORDING_CREATED_AFTER_EVENT_START event (dev/changelog/1275). The server moves a past
    start to now, so the modal says so before the user confirms."""
    SCENARIO = 'late_program'

    def test_the_note_shows(self):
        self.assertTrue(self.obs['visible'])

    def test_it_says_how_much_has_aired_and_that_it_will_be_missing(self):
        self.assertIn('started 20m ago', self.obs['text'])
        self.assertIn('the first 20m will not be in the file', self.obs['text'])


class TypedFutureStartClearsTheLateNoteTests(_Base, unittest.TestCase):
    SCENARIO = 'late_program_start_typed_future'

    def test_a_start_typed_into_the_future_takes_the_note_away(self):
        self.assertTrue(self.obs['before'])
        self.assertFalse(self.obs['visible'])


class ProfileChangeKeepsTheLateNoteTests(_Base, unittest.TestCase):
    SCENARIO = 'late_program_profile_change_keeps_it'

    def test_the_note_survives_a_profile_pick_that_does_not_move_the_start(self):
        self.assertTrue(self.obs['visible'])
        self.assertIn('started 20m ago', self.obs['text'])


class FutureProgramHasNoLateNoteTests(_Base, unittest.TestCase):
    SCENARIO = 'future_program'

    def test_nothing_is_said_about_a_program_not_yet_on(self):
        self.assertFalse(self.obs['visible'])


class EditOfAScheduledRecordingHasNoLateNoteTests(_Base, unittest.TestCase):
    SCENARIO = 'late_then_edit_hides_it'

    def test_an_edit_opened_after_a_late_schedule_does_not_carry_the_note_over(self):
        self.assertTrue(self.obs['before'])
        self.assertFalse(self.obs['visible'])
