"""Every CANCELLED recording names why, and the detail page says what the cancel left behind.

Guards dev/docs/BUGS.md 2026-09-20 @ 10:08:41 AM ET. The detail page rendered every ABORTED
recording as the one sentence "Cancelled manually ... captured files were discarded". Three of
the five cancel paths make that false: a cancel during post-capture analysis and a cancel
during conversion both keep the joined .ts on purpose - the same page's kebab offers Retry
conversion over it - and a group demotion, dissolve or delete cancels SCHEDULED recordings that
nobody touched manually and that had captured nothing to discard.

Covers, in order:
  - WriterTests: each ABORTED writer stores its own reason.
  - WriterCoverageTests: no writer of ABORTED anywhere in app/ can omit the reason.
  - RetryClearsReasonTests: a Retry that takes a row out of ABORTED clears the column.
  - StripTests: each reason renders its own sentence and the right next step.
  - TemplateCoverageTests: every value in CANCEL_REASONS has a branch in the strip.

No real ffmpeg, no network, nothing under the real /dvr.
  python3 -m unittest tests.test_cancel_reason_disclosure
"""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, recorder  # noqa: E402
from app import channel_groups as cgmod  # noqa: E402
from app import database as dbmod  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingSegment, CANCEL_REASONS,
    REC_STATUS_SCHEDULED, REC_STATUS_IN_PROGRESS, REC_STATUS_ANALYZING,
    REC_STATUS_CONVERTING, REC_STATUS_ABORTED,
    CANCEL_BEFORE_START, CANCEL_GROUP_CHANGE, CANCEL_DURING_CAPTURE,
    CANCEL_DURING_ANALYSIS, CANCEL_DURING_CONVERSION,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, 'templates', 'recording_detail.html')
APP_DIR = os.path.join(ROOT, 'app')


class WriterTests(unittest.TestCase):
    """Each path that marks a recording ABORTED stamps its own reason in the same commit."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=False)
        self.t.app.config['WTF_CSRF_ENABLED'] = False

    def tearDown(self):
        self.t.cleanup()

    def _reason(self, rid):
        db.session.expire_all()
        rec = db.session.get(Recording, rid)
        self.assertEqual(REC_STATUS_ABORTED, rec.status, 'setup: expected the ABORTED path')
        return rec.cancel_reason

    def test_cancel_while_scheduled(self):
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='not yet')
        db.session.commit()
        resp = self.t.client.post(f'/recordings/{rec.id}/cancel')
        self.assertEqual(302, resp.status_code)
        self.assertEqual(CANCEL_BEFORE_START, self._reason(rec.id))

    def test_a_group_change_cancels_its_schedule(self):
        """The one cancel with no person behind it, and the reason the column exists."""
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name='group row')
        db.session.commit()
        ids = cgmod.cancel_scheduled_recordings([rec], 'Channel group "Sports" was deleted.')
        db.session.commit()
        self.assertEqual([rec.id], ids)
        self.assertEqual(CANCEL_GROUP_CHANGE, self._reason(rec.id))

    def test_abort_during_capture(self):
        rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, name='live one')
        db.session.commit()
        recorder.abort_recording(self.t.app, rec.id)
        self.assertEqual(CANCEL_DURING_CAPTURE, self._reason(rec.id))

    def test_cancel_during_analysis(self):
        rec = seed.make_recording(status=REC_STATUS_ANALYZING, name='scanning')
        db.session.commit()
        resp = self.t.client.post(f'/recordings/{rec.id}/cancel')
        self.assertEqual(302, resp.status_code)
        self.assertEqual(CANCEL_DURING_ANALYSIS, self._reason(rec.id))

    def test_cancel_a_stranded_conversion(self):
        """The branch with no live ffmpeg behind it, so this route does the write itself."""
        rec = seed.make_recording(status=REC_STATUS_CONVERTING, name='stranded')
        db.session.commit()
        with mock.patch('app.postprocessor.request_cancel_conversion', return_value=False):
            resp = self.t.client.post(f'/recordings/{rec.id}/cancel-convert')
        self.assertEqual(302, resp.status_code)
        self.assertEqual(CANCEL_DURING_CONVERSION, self._reason(rec.id))


class WriterCoverageTests(unittest.TestCase):
    """No writer of ABORTED anywhere in app/ may leave the column unset.

    The live-conversion cancel in postprocessor.do_postprocess is behind a whole capture
    chain, so a source check is what covers it rather than a tower of mocks - and unlike a
    mock it also covers the next writer somebody adds.
    """

    def test_every_aborted_writer_sets_a_cancel_reason(self):
        offenders = []
        for dirpath, _dirs, files in os.walk(APP_DIR):
            for fn in sorted(files):
                if not fn.endswith('.py'):
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, encoding='utf-8') as fh:
                    lines = fh.read().split('\n')
                for i, line in enumerate(lines):
                    m = re.match(r'\s*(\w+)\.status\s*=\s*REC_STATUS_ABORTED\s*$', line)
                    if not m:
                        continue
                    var = m.group(1)
                    window = '\n'.join(lines[i:i + 4])
                    if f'{var}.cancel_reason =' not in window:
                        rel = os.path.relpath(path, ROOT)
                        offenders.append(f'{rel}:{i + 1}')
        self.assertEqual([], offenders,
                         'these writers mark a recording CANCELLED without naming why')


class RetryClearsReasonTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(start_scheduler=False)
        self.t.app.config['WTF_CSRF_ENABLED'] = False

    def tearDown(self):
        self.t.cleanup()

    def test_retry_conversion_on_a_cancelled_row_clears_it(self):
        """The column holds a value only while the row is ABORTED; a Retry takes it out."""
        out = os.path.join(self.t._tmpdir, 'kept.ts')
        with open(out, 'wb') as fh:
            fh.write(b'x' * 64)
        rec = seed.make_recording(status=REC_STATUS_ABORTED, name='retry me',
                                  cancel_reason=CANCEL_DURING_CONVERSION, output_path=out)
        db.session.commit()
        rid = rec.id
        with mock.patch('app.concatenator.run_postprocess_claimed'), \
             mock.patch('app.postprocessor.is_conversion_active', return_value=False), \
             mock.patch('app.concatenator.is_concat_active', return_value=False):
            resp = self.t.client.post(f'/recordings/{rid}/retry-convert')
        self.assertEqual(302, resp.status_code)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Recording, rid).cancel_reason)


class StripTests(unittest.TestCase):
    """What the CANCELLED strip says for each reason."""

    def setUp(self):
        self.t = make_test_app(start_scheduler=False)

    def tearDown(self):
        self.t.cleanup()

    def _page(self, reason, *, ts=False, segment_row=False, segment_file=False):
        rec = seed.make_recording(status=REC_STATUS_ABORTED, name=f'r {reason}',
                                  cancel_reason=reason,
                                  completed_at=datetime.utcnow() - timedelta(minutes=5))
        if ts:
            out = os.path.join(self.t._tmpdir, f'{rec.id}.ts')
            with open(out, 'wb') as fh:
                fh.write(b'x' * 64)
            rec.output_path = out
        if segment_row or segment_file:
            seg_path = os.path.join(self.t._tmpdir, f'{rec.id}_seg_001.ts')
            if segment_file:
                with open(seg_path, 'wb') as fh:
                    fh.write(b'x' * 64)
            db.session.add(RecordingSegment(recording_id=rec.id, segment_number=1,
                                            file_path=seg_path, started_at=rec.start_time,
                                            bytes_recorded=64))
        db.session.commit()
        resp = self.t.client.get(f'/recordings/{rec.id}')
        self.assertEqual(200, resp.status_code)
        html = resp.get_data(as_text=True)
        strip = re.search(r'<div class="live-strip abort-strip">(.*?)</div>', html, re.S)
        self.assertIsNotNone(strip, 'no CANCELLED strip rendered')
        return html, re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', strip.group(1))).strip()

    def _assert_names(self, reason, phrase, cta, **kw):
        html, strip = self._page(reason, **kw)
        self.assertIn(phrase, strip)
        self.assertIn(cta, strip)
        self.assertNotIn('Cancelled manually', strip)
        return html, strip

    def test_before_start(self):
        self._assert_names(CANCEL_BEFORE_START, 'before it started',
                           'use Record again to reschedule it')

    def test_group_change_does_not_blame_the_user(self):
        _html, strip = self._assert_names(CANCEL_GROUP_CHANGE, 'channel group',
                                          'use Record again to reschedule it')
        self.assertNotIn('you ', strip)

    def test_during_capture_is_the_one_real_discard(self):
        self._assert_names(CANCEL_DURING_CAPTURE, 'discarded',
                           'nothing recoverable is left on disk', segment_row=True)

    def test_during_analysis_keeps_the_ts_and_offers_retry(self):
        html, strip = self._assert_names(CANCEL_DURING_ANALYSIS, '.ts file was kept',
                                         'use Retry conversion to finish it', ts=True)
        self.assertNotIn('discarded', strip)
        self.assertIn('data-act="retry-convert"', html)

    def test_during_conversion_keeps_the_ts_and_offers_retry(self):
        html, strip = self._assert_names(CANCEL_DURING_CONVERSION, '.ts file was kept',
                                         'use Retry conversion to finish it', ts=True)
        self.assertNotIn('discarded', strip)
        self.assertIn('data-act="retry-convert"', html)

    def test_a_cancelled_row_is_never_offered_retry_join(self):
        """retry_concat refuses ABORTED, so naming Retry join here would bounce the click."""
        html, strip = self._page(CANCEL_DURING_CAPTURE, segment_file=True)
        self.assertIn('cannot be joined', strip)
        self.assertNotIn('Retry join', strip)
        self.assertNotIn('data-act="retry-concat"', html)

    def test_a_row_from_before_the_vocabulary_guesses_at_nothing(self):
        """NULL claims nothing about who cancelled it or why, and still says what is on
        disk - once, from the recovery half rather than twice from both."""
        _html, strip = self._page(None, ts=True)
        self.assertIn('the reason was not recorded', strip)
        self.assertIn('use Retry conversion to finish it', strip)
        self.assertNotIn('Cancelled manually', strip)
        self.assertNotIn('discarded', strip)
        self.assertEqual(1, strip.count('is still on disk'),
                         'the disk fact belongs to the recovery half and is said once')

    def test_a_row_from_before_the_vocabulary_with_nothing_left(self):
        _html, strip = self._page(None, segment_row=True)
        self.assertIn('the reason was not recorded', strip)
        self.assertIn('nothing recoverable is left on disk', strip)


class TemplateCoverageTests(unittest.TestCase):
    def test_every_reason_has_a_branch_in_the_cancelled_strip(self):
        """A reason with no branch would render as a row from before the vocabulary, which
        says less than the app knows - the defect this vocabulary exists to end."""
        with open(TEMPLATE, encoding='utf-8') as fh:
            src = fh.read()
        missing = [r for r in CANCEL_REASONS if f"rec.cancel_reason == '{r}'" not in src]
        self.assertEqual([], missing)

    def test_the_vocabulary_names_every_constant(self):
        declared = {v for k, v in vars(dbmod).items()
                    if k.startswith('CANCEL_') and isinstance(v, str)}
        self.assertEqual(declared, set(CANCEL_REASONS))


if __name__ == '__main__':
    unittest.main()
