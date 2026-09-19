"""One name per recording status, on every surface that renders one.

dev/changelog/961. A Recording's status is stored under one name and shown to a human under
another - CONCATENATING is shown as JOINING, ABORTED as CANCELLED - because
dev/changelog/867 ruled the stored words are jargon. That rename reached the Recordings list
and the recording detail page and stopped there, so two surfaces kept printing the stored
enum and the same recording read as JOINING on one page and CONCATENATING on another.

Two things are asserted here. The Dashboard's own half is in
tests/test_dashboard_page_conformance.py beside that page's other conformance cases, and its
live SSE relabel in tests/test_dashboard_live_js.py:

  * the label table has exactly one definition (app/fmt_utils.py) and every status the
    database can hold is in it - an unknown status renders visibly rather than landing in a
    real state's branch (CLAUDE.md, "states are enumerated");
  * the channel detail page's recording-observations table renders the label, not the enum.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_recording_status_vocabulary
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
    REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
    REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED,
)
from app.fmt_utils import REC_STATUS_DISPLAY, rec_status_display, rec_status_label  # noqa: E402


ALL_STATUSES = (
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_PAUSED, REC_STATUS_RETRYING,
    REC_STATUS_CONCATENATING, REC_STATUS_ANALYZING, REC_STATUS_CONVERTING,
    REC_STATUS_COMPLETED, REC_STATUS_FAILED, REC_STATUS_ABORTED,
)


class StatusLabelTableTests(unittest.TestCase):
    def test_every_recording_status_has_a_display_row(self):
        for status in ALL_STATUSES:
            self.assertIn(status, REC_STATUS_DISPLAY, f'{status} renders through the default')

    def test_the_two_renamed_statuses_keep_their_human_words(self):
        """The rename is the whole reason this table is shared rather than local to the
        recordings list. If either of these reverts, two pages disagree again."""
        self.assertEqual('JOINING', rec_status_label(REC_STATUS_CONCATENATING))
        self.assertEqual('CANCELLED', rec_status_label(REC_STATUS_ABORTED))

    def test_an_unknown_status_is_shown_rather_than_swallowed(self):
        self.assertEqual('SOMETHING_NEW', rec_status_label('SOMETHING_NEW'))
        section, _st, _badge, label, pulse = rec_status_display('SOMETHING_NEW')
        self.assertEqual('SOMETHING_NEW', label)
        self.assertFalse(pulse)

    def test_waiting_replaces_the_label_only_where_a_row_can_actually_be_parked(self):
        """postprocess_waiting_since is set on ANALYZING and CONVERTING rows only. A join is
        never parked, so a stale column must not turn a live join into WAITING."""
        for status in (REC_STATUS_ANALYZING, REC_STATUS_CONVERTING):
            self.assertEqual('WAITING', rec_status_display(status, waiting=True)[3])
            self.assertFalse(rec_status_display(status, waiting=True)[4])
        for status in (REC_STATUS_CONCATENATING, REC_STATUS_IN_PROGRESS):
            self.assertEqual(REC_STATUS_DISPLAY[status][3],
                             rec_status_display(status, waiting=True)[3])

    def test_waiting_keeps_the_rows_own_section_and_classes(self):
        """Only the label and the pulse move - a parked row stays in the live section and
        keeps its edge color, or it would jump between sections as it parks and resumes."""
        plain = rec_status_display(REC_STATUS_CONVERTING)
        parked = rec_status_display(REC_STATUS_CONVERTING, waiting=True)
        self.assertEqual(plain[:3], parked[:3])

    def test_the_table_has_one_definition(self):
        """A second copy is how the Dashboard drifted in the first place. app/fmt_utils.py
        is the canonical home; nothing else may spell the labels out again."""
        import subprocess
        out = subprocess.run(
            ['grep', '-rn', "'JOINING'", 'app/', 'templates/', 'static/js/'],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))).stdout
        files = {line.split(':')[0] for line in out.splitlines() if line.strip()}
        self.assertEqual({'app/fmt_utils.py'}, files,
                         f'the JOINING label is written in more than one place: {files}')


class ChannelPageStatusTests(unittest.TestCase):
    """The channel detail page's recording-observations table rendered `{{ r.status }}`, so
    it said ABORTED where the Recordings list said CANCELLED for the same recording."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_the_observations_table_renders_the_label(self):
        with self.t.app.app_context():
            acc = seed.make_account()
            ch = seed.make_channel(acc, stream_id=1, name='Test Channel')
            rec = seed.make_recording(status=REC_STATUS_ABORTED, name='Cancelled show',
                                      channel_id=ch.id)
            rec.health_quality_score = 50
            db.session.commit()
            ch_id = ch.id
        html = self.client.get(f'/channels/{ch_id}').get_data(as_text=True)
        m = re.search(r'<td data-label="Status"><span class="badge[^"]*">([^<]*)</span></td>',
                      html)
        self.assertIsNotNone(m, 'no observation status cell rendered')
        self.assertEqual('CANCELLED', m.group(1).strip())


# The terminal-frame guard that lived here (dev/docs/BUGS.md 2026-09-14 @ 10:12:44 AM ET)
# walked dashboard.js's event-name list. That list is gone - the page keys on `status` - so
# the broader rule, every status-moving publish in app/ carries `status`, is now
# tests/test_dashboard_live_js.py::EveryStatusMovingPublishCarriesStatusTests
# (dev/changelog/1023).


if __name__ == '__main__':
    unittest.main()
