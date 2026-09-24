"""Tier 2 - one failure produces one typed alert, and a failure that recovers clears it.

Guards dev/changelog/930 and the two dev/docs/BUGS.md entries it carries (2026-09-12).

app/__init__.py's _AlertHandler turns every ERROR-level log record into a LOG_ERROR alert.
That had two consequences:

  * A site that had already raised its own typed alert produced a SECOND row for the same
    event - a conversion give-up raised CONVERSION_FAILED and then logged the same message
    ten milliseconds later. The fix is a record marker, extra={'already_alerted': True},
    that the handler skips. The log line stays at ERROR: the level describes the log, the
    marker describes the alert.
  * Four failures had no type of their own, so nothing could ever clear them - "Sync failed
    for account 2" stood open through eight days of successful syncs.

**Why these tests attach a real _AlertHandler.** tests/support/app.py::_strip_alert_handlers
removes it on every make_test_app(), so in an ordinary test no LOG_ERROR row is ever written
and an assertion like "exactly one alert row" passes whether or not the marker works. Every
case below therefore re-attaches a live handler and asserts against it; without that the
double-up half of this file would be vacuous.

No network, no real ffmpeg: run_conversion_supervised is stubbed and the provider fetch is a
patched requests (CLAUDE.md §Testing).

Run standalone:
  python3 -m unittest tests.test_typed_failure_alerts
"""
import logging
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_pkg  # noqa: E402
import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
from app import db  # noqa: E402
from app import accounts as accounts_mod  # noqa: E402
from app.accounts import _do_sync  # noqa: E402
from app.database import Account, Alert  # noqa: E402
from app.postprocessor import ConversionResult, do_postprocess  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402

M3U_URL = 'http://provider.test/playlist.m3u'


class _LiveAlertHandlerCase(unittest.TestCase):
    """Re-attaches the real log->alert handler that make_test_app strips.

    _AlertHandler is a closure-local class inside _setup_logging, so it cannot be imported;
    calling _setup_logging is how a test gets the genuine article rather than a stand-in
    that could drift from it. A cfg with no logging.file keeps it to a StreamHandler, and
    logging.basicConfig is a no-op here because the root logger already has handlers.
    """

    def setUp(self):
        self.t = make_test_app()
        self._attach_alert_handler()

    def tearDown(self):
        self._detach_alert_handler()
        self.t.cleanup()

    def _attach_alert_handler(self):
        app_pkg._setup_logging({'logging': {'level': 'INFO'}})
        root = logging.getLogger()
        self._handlers = [h for h in root.handlers if type(h).__name__ == '_AlertHandler']
        self.assertTrue(self._handlers,
                        '_setup_logging did not attach an _AlertHandler - these tests would '
                        'silently pass without one')

    def _detach_alert_handler(self):
        root = logging.getLogger()
        for h in list(root.handlers):
            if type(h).__name__ == '_AlertHandler':
                root.removeHandler(h)

    def _alerts_for(self, recording_id):
        return Alert.query.filter_by(recording_id=recording_id).all()


class AlreadyAlertedMarkerTests(_LiveAlertHandlerCase):
    """The marker itself, at the handler."""

    def test_an_unmarked_error_still_becomes_a_log_alert(self):
        """Sanity, and the control for every other case here: the handler is live, so a
        LOG_ERROR row really would be written if the marker were not honored."""
        logging.getLogger('app.test_marker').error('something nobody expected')
        rows = Alert.query.filter_by(alert_type='LOG_ERROR').all()
        self.assertEqual(len(rows), 1)
        self.assertIn('nobody expected', rows[0].title)

    def test_a_marked_error_writes_no_alert_row(self):
        logging.getLogger('app.test_marker').error(
            'already surfaced by a typed alert', extra={'already_alerted': True})
        self.assertEqual(Alert.query.filter_by(alert_type='LOG_ERROR').all(), [])

    def test_a_marked_error_is_still_logged_at_error(self):
        """CLAUDE.md "failure paths must be observable": the marker suppresses the second
        alert, never the log line. Downgrading the level to dodge the alert is the mistake
        this replaces (app/scheduler.py's old WARNING dodge)."""
        marker_log = logging.getLogger('app.test_marker')
        with self.assertLogs(marker_log, level='ERROR') as captured:
            marker_log.error('still in dvr.log', extra={'already_alerted': True})
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelno, logging.ERROR)
        self.assertTrue(getattr(captured.records[0], 'already_alerted', False))

    def test_the_marker_does_not_leak_to_the_next_record(self):
        """A logging `extra` is per-record, but an implementation that stashed the flag
        anywhere else would silence real errors - the failure mode worth naming."""
        marker_log = logging.getLogger('app.test_marker')
        marker_log.error('marked', extra={'already_alerted': True})
        marker_log.error('unmarked')
        rows = Alert.query.filter_by(alert_type='LOG_ERROR').all()
        self.assertEqual([r.title for r in rows], ['unmarked'])


def _pp_config(pp_overrides=None, move=None):
    pp = dict({'enabled': True, 'format': 'mp4', 'delete_source': False,
               'reencode_mode': 'never', 'pre_output_timeout_seconds': 60,
               'auto_restart': True, 'max_restart_attempts': 1,
               'stall_seconds': 0, 'progress_interval_seconds': 5}, **(pp_overrides or {}))
    return cfgmod._deep_merge(cfgmod.load_config(), {'recording': {
        'gather_health_data': False,
        'move_on_complete': move or {'enabled': False},
        'post_script': {'enabled': False},
        'post_process': pp,
    }})


class OneAlertPerFailureTests(_LiveAlertHandlerCase):
    """Each failure reaches the Alerts page exactly once, as itself."""

    def _recording_with_source(self, status='CONCATENATING'):
        rec = seed.make_recording(status=status, name='typed-failure')
        ts = os.path.join(self.t._tmpdir, f'rec_{rec.id}.ts')
        with open(ts, 'wb') as fh:
            fh.write(b'x' * 2048)
        rec.output_path = ts
        db.session.commit()
        return rec.id, ts

    def tearDown(self):
        with ppmod._active_lock:
            ppmod._active_conversions.clear()
            ppmod._cancel_requested.clear()
        super().tearDown()

    def test_a_conversion_give_up_raises_one_alert_not_two(self):
        """The reported double-up: CONVERSION_FAILED plus a LOG_ERROR ten milliseconds
        later, both describing the same dead conversion."""
        rid, ts = self._recording_with_source()
        cfg = _pp_config()
        stub = mock.Mock(side_effect=lambda *a, **k: ConversionResult(
            False, 'died', 'Conversion failed!', out_time=12.0))
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', stub):
            do_postprocess(self.t.app, rid, ts)

        db.session.expire_all()
        rows = self._alerts_for(rid)
        self.assertEqual([r.alert_type for r in rows], ['CONVERSION_FAILED'],
                         f'one failure, one alert - got {[r.alert_type for r in rows]}')

    def test_a_move_failure_raises_its_own_type(self):
        rid, ts = self._recording_with_source()
        dest = os.path.join(self.t._tmpdir, 'destination')
        cfg = _pp_config(move={'enabled': True, 'destination': dest})
        ok = mock.Mock(side_effect=lambda *a, **k: ConversionResult(True, 'success'))
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', ok), \
             mock.patch.object(ppmod, 'ensure_dir',
                               side_effect=OSError('destination is not writable')):
            do_postprocess(self.t.app, rid, ts)

        db.session.expire_all()
        rows = self._alerts_for(rid)
        self.assertEqual([r.alert_type for r in rows], ['RECORDING_MOVE_FAILED'],
                         f'the move failure must be typed, got {[r.alert_type for r in rows]}')
        self.assertIn('not writable', rows[0].body)

    def test_a_concat_with_no_valid_segments_raises_its_own_type(self):
        import app.concatenator as concatenator

        rec = seed.make_recording(status='IN_PROGRESS', name='dead concat')
        db.session.commit()
        rid = rec.id
        with mock.patch('app.recorder.persist_final_thumbnail'):
            concatenator.do_concatenation(self.t.app, rid)

        db.session.expire_all()
        rows = self._alerts_for(rid)
        self.assertEqual([r.alert_type for r in rows], ['CONCATENATION_FAILED'],
                         f'a lost capture must not read as "Application Error (log)" - '
                         f'got {[r.alert_type for r in rows]}')


class FailureAlertsClearThemselvesTests(_LiveAlertHandlerCase):
    """A failure that has since recovered must stop standing."""

    def tearDown(self):
        with ppmod._active_lock:
            ppmod._active_conversions.clear()
            ppmod._cancel_requested.clear()
        super().tearDown()

    def _open_alert(self, alert_type, recording_id, source='postprocessor'):
        row = Alert(alert_type=alert_type, severity='ERROR', title=f'{alert_type} stood',
                    source=source, recording_id=recording_id)
        db.session.add(row)
        db.session.commit()
        return row.id

    def _recording_with_source(self):
        rec = seed.make_recording(status='CONCATENATING', name='clears-itself')
        ts = os.path.join(self.t._tmpdir, f'rec_{rec.id}.ts')
        with open(ts, 'wb') as fh:
            fh.write(b'x' * 2048)
        rec.output_path = ts
        db.session.commit()
        return rec.id, ts

    def test_a_completed_conversion_clears_an_earlier_conversion_failure(self):
        """Keyed on the recording, not (type, source): CONVERSION_FAILED is raised with
        source='postprocessor' here and source='scheduler' from startup recovery, so only
        the id reaches both."""
        rid, ts = self._recording_with_source()
        stale = self._open_alert('CONVERSION_FAILED', rid, source='scheduler')
        cfg = _pp_config()
        ok = mock.Mock(side_effect=lambda *a, **k: ConversionResult(True, 'success'))
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', ok):
            do_postprocess(self.t.app, rid, ts)

        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, stale).dismissed_at,
                             'a conversion that completed must clear its own failure alert')

    def test_a_successful_move_clears_an_earlier_move_failure(self):
        rid, ts = self._recording_with_source()
        stale = self._open_alert('RECORDING_MOVE_FAILED', rid)
        dest = os.path.join(self.t._tmpdir, 'destination')
        cfg = _pp_config(move={'enabled': True, 'destination': dest})
        ok = mock.Mock(side_effect=lambda *a, **k: ConversionResult(True, 'success'))
        # The stubbed conversion reports success without writing its .mp4, so the real
        # shutil.move would raise FileNotFoundError and send this down the failure branch -
        # the opposite of the path under test. Standing in for the move itself is what
        # makes this a successful-move case rather than a second failure case.
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', ok), \
             mock.patch.object(ppmod.shutil, 'move'):
            do_postprocess(self.t.app, rid, ts)

        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Alert, stale).dismissed_at,
                             'the file reached its destination, so the move failure is over')

    def test_a_concatenation_failure_is_not_self_clearing(self):
        """CONCATENATION_FAILED is deliberately NOT in the self-clearing set: nothing
        re-runs a concatenation that found nothing to concatenate, so it is a record of a
        loss and only deleting the recording clears it."""
        rid, ts = self._recording_with_source()
        stale = self._open_alert('CONCATENATION_FAILED', rid, source='concatenator')
        cfg = _pp_config()
        ok = mock.Mock(side_effect=lambda *a, **k: ConversionResult(True, 'success'))
        with mock.patch.object(cfgmod, 'load_config', return_value=cfg), \
             mock.patch.object(ppmod, 'run_conversion_supervised', ok):
            do_postprocess(self.t.app, rid, ts)

        db.session.expire_all()
        self.assertIsNone(db.session.get(Alert, stale).dismissed_at,
                          'a lost capture stays on the record until the recording is deleted')


class SyncFailureAlertTests(_LiveAlertHandlerCase):
    """SYNC_FAILED: standing while the account is failing, resolved by the next sync that
    finishes. The eight-day-old "Sync failed for account 2" is what this closes."""

    def setUp(self):
        super().setUp()
        self.account = Account(name='M3U Provider', account_type='m3u',
                               m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()
        self.account_id = self.account.id

    def _playlist(self):
        lines = ['#EXTM3U']
        for sid in range(1, 4):
            lines.append(f'#EXTINF:-1 tvg-id="ch{sid}.test" tvg-name="Channel {sid}",Channel {sid}')
            lines.append(f'http://provider.test/live/u/p/{sid}.ts')
        return ('\n'.join(lines) + '\n').encode()

    def _sync_failing(self):
        with mock.patch.object(accounts_mod, 'requests') as req:
            req.get = mock.Mock(side_effect=Exception('provider refused the connection'))
            _do_sync(self.account_id, threading.Event())
        db.session.expire_all()

    def _sync_succeeding(self):
        body = self._playlist()

        def fake_get(url, **_kwargs):
            resp = mock.Mock()
            resp.content = body
            resp.raise_for_status.return_value = None
            resp.status_code = 200
            return resp

        with mock.patch.object(accounts_mod, 'requests') as req:
            req.get = fake_get
            _do_sync(self.account_id, threading.Event())
        db.session.expire_all()

    def _sync_failed_alerts(self, include_dismissed=False):
        q = Alert.query.filter_by(alert_type='SYNC_FAILED')
        if not include_dismissed:
            q = q.filter(Alert.dismissed_at.is_(None))
        return q.all()

    def test_a_failed_sync_raises_a_typed_alert(self):
        self._sync_failing()
        rows = self._sync_failed_alerts()
        self.assertEqual(len(rows), 1,
                         'a failed sync must raise SYNC_FAILED, not only a LOG_ERROR row')
        self.assertEqual(rows[0].source, f'account:{self.account_id}:sync-failed')
        self.assertIn('refused the connection', rows[0].body)

    def test_a_failed_sync_does_not_also_raise_a_log_alert(self):
        self._sync_failing()
        self.assertEqual(Alert.query.filter_by(alert_type='LOG_ERROR').all(), [],
                         'the sync failure already has a typed alert')

    def test_a_later_successful_sync_clears_it(self):
        self._sync_failing()
        self.assertEqual(len(self._sync_failed_alerts()), 1)

        self._sync_succeeding()
        self.assertEqual(self._sync_failed_alerts(), [],
                         'a sync that finished means the failure is over - this is the '
                         '"Sync failed for account 2" that stood through eight days of '
                         'successful syncs')
        self.assertEqual(len(self._sync_failed_alerts(include_dismissed=True)), 1,
                         'cleared by dismissing the standing row, not by deleting history')

    def test_repeated_failures_refresh_one_row_rather_than_stacking(self):
        self._sync_failing()
        self._sync_failing()
        self.assertEqual(len(self._sync_failed_alerts()), 1,
                         'the standing-alert shape exists so a nightly retry loop cannot '
                         'bury the Alerts page')


if __name__ == '__main__':
    unittest.main(verbosity=2)
