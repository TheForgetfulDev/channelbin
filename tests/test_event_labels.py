"""Every event type has a plain name, and the event log leads with it (dev/changelog/1293).

The second first-time-user test (finding 8) read the recording's event log as code:
RECORDING_CREATED_AFTER_EVENT_START, "start_time set to now", "ffmpeg exited 255". The
type is still what Logs and support bundles spell, so it stays on the row in small print -
a label, never a rename. One table, `database.EVENT_LABELS`, names recording, channel and
group events alike, and a type added without an entry fails here rather than reaching a
page as its constant.

The tag dialog half guards dev/docs/BUGS.md 2026-10-26 11:30 AM: the Name field still
promised a `{!tag:name}` placeholder that dev/changelog/123 retired.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_event_labels
"""
import os
import re
import unittest

from tests.support.app import make_test_app
from tests.support import seed
from app import db, recorder
from app import database as dbmod
from app.database import (
    ChannelEvent, EVENT_LABELS, RecordingEvent, RecordingSegment, add_recording_event,
    event_label, CHANNEL_URL_CHANGED, DIAGNOSTICS, GROUP_MEMBER_PARTICIPATION,
    RECORDING_CREATED_AFTER_EVENT_START,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _event_type_constants():
    """Every module-level constant in app/database.py that spells its own name - the same
    scan tests/test_vocabulary_constants.py uses for the event-type families."""
    return {name for name, value in vars(dbmod).items()
            if isinstance(value, str) and name.isupper() and value == name}


class EventLabelTableTests(unittest.TestCase):

    def test_every_event_type_has_a_label(self):
        types = _event_type_constants()
        self.assertGreater(len(types), 100, 'the event-type scan found almost nothing')
        missing = sorted(types - set(EVENT_LABELS))
        self.assertEqual(missing, [], f'event types with no plain name: {missing}')

    def test_the_table_names_nothing_that_is_not_an_event_type(self):
        self.assertEqual(sorted(set(EVENT_LABELS) - _event_type_constants()), [])

    def test_labels_are_words_in_sentence_case(self):
        for t, label in EVENT_LABELS.items():
            with self.subTest(t):
                self.assertNotIn('_', label)
                self.assertNotEqual(label, label.upper())
                self.assertTrue(label[0].isupper())
                self.assertNotIn('—', label)

    def test_a_retired_type_still_reads_as_words(self):
        self.assertEqual(event_label('RECORDING_COMPLETE'), 'Recording complete')
        self.assertEqual(event_label(None), '')


class RecordingEventLogTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.t = make_test_app()
        rec = seed.make_recording(status='COMPLETED', name='labels-page')
        add_recording_event(rec.id, RECORDING_CREATED_AFTER_EVENT_START, detail='d1')
        db.session.commit()
        cls.html = cls.t.app.test_client().get(f'/recordings/{rec.id}').get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def test_the_row_leads_with_the_plain_name(self):
        self.assertIn('<span class="ev-type">Created after the program started</span>', self.html)

    def test_the_type_itself_stays_on_the_row(self):
        """Logs and support bundles spell the type; the row keeps it to match on."""
        self.assertIn(f'<span class="ev-code">{RECORDING_CREATED_AFTER_EVENT_START}</span>',
                      self.html)
        self.assertNotIn(f'<span class="ev-type">{RECORDING_CREATED_AFTER_EVENT_START}', self.html)


class TimelineLabelTests(unittest.TestCase):
    """The channel and group Activity timelines title-cased the constant ("Channel Url
    Changed"); they read the same table as the event log now."""

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)

    def test_a_channel_event_reads_as_its_label(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc, stream_id='7', name='Label Channel')
        db.session.add(ChannelEvent(channel_id=ch.id, event_type=CHANNEL_URL_CHANGED,
                                    detail='moved'))
        db.session.commit()
        html = self.t.app.test_client().get(f'/channels/{ch.id}').get_data(as_text=True)
        self.assertIn(EVENT_LABELS[CHANNEL_URL_CHANGED], html)
        self.assertNotIn('Channel Url Changed', html)

    def test_the_template_keeps_no_label_table_of_its_own(self):
        with open(os.path.join(REPO, 'templates', 'channels', '_timeline.html')) as f:
            src = f.read()
        self.assertNotIn('group_ev_label', src)
        self.assertNotRegex(src, r"event_type \| replace\('_', ' '\) \| title")
        self.assertEqual(EVENT_LABELS[GROUP_MEMBER_PARTICIPATION], 'Participation changed')


class EventDetailWordingTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        self.rec = seed.make_recording(status='IN_PROGRESS', with_segment=True)
        db.session.commit()
        self.seg = RecordingSegment.query.filter_by(recording_id=self.rec.id).first()

    def _detail(self, exit_reason, code=255):
        self.seg.exit_reason = exit_reason
        recorder.record_segment_diagnostics(self.rec.id, self.seg, code, 'tail line')
        db.session.commit()
        ev = RecordingEvent.query.filter_by(recording_id=self.rec.id,
                                            event_type=DIAGNOSTICS).one()
        return ev.detail

    def test_255_after_channelbins_own_stop_says_so(self):
        """ffmpeg 7.1 exits 255 on the SIGTERM ChannelBin stops it with
        (dev/changelog/1287); "ffmpeg exited 255" beside a stall kill read as a crash."""
        detail = self._detail('STALL_KILLED')
        self.assertIn('ffmpeg stopped when ChannelBin told it to (exit code 255)', detail)
        self.assertNotIn('ffmpeg exited 255', detail)

    def test_255_on_its_own_is_still_ffmpeg_giving_up(self):
        self.assertIn('ffmpeg exited 255', self._detail('PROCESS_EXITED'))

    def test_a_late_created_recording_does_not_name_a_column(self):
        from app.routes import recordings as rroutes
        with open(rroutes.__file__) as f:
            src = f.read()
        self.assertNotIn('start_time set to now', src)


class TagDialogWordingTests(unittest.TestCase):
    """dev/docs/BUGS.md 2026-10-26 11:30 AM: the retired `{!tag:name}` placeholder."""

    def test_the_tag_dialog_names_only_the_placeholder_that_exists(self):
        from app.accounts import _TAG_TOKEN_RE
        for rel in (('static', 'js', 'tag-modal.js'), ('templates', 'tags.html')):
            with open(os.path.join(REPO, *rel)) as f:
                src = f.read()
            with self.subTest(rel[-1]):
                # Only literal examples are checked: the dialog's rename warning builds
                # `{tag:<the tag's own name>}` in script, which is not an example.
                tokens = re.findall(r'\{!?tag:[\w!-]*\}', src)
                self.assertTrue(tokens, 'the page no longer shows a placeholder at all')
                for token in tokens:
                    self.assertTrue(_TAG_TOKEN_RE.fullmatch(token),
                                    f'{token} is not a placeholder file names understand')


if __name__ == '__main__':
    unittest.main()
