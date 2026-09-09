"""Cancel sync is honored inside the channel-upsert and EPG-import phases, not only between them.

`_do_sync` used to poll its stop event at three points only - after the mark-syncing commit
and after each channel-upsert commit. Nothing looked at it inside `_upsert_channels` or
anywhere in the EPG fetch/import, which are the two phases that actually take minutes on a
real account. So a cancel issued during either was silently swallowed: the sync ran to
completion, `_mark_success_and_commit` stamped SUCCESS/OK, and `sync_account`'s `finally`
popped the unused cancel reason - while the route had already told the user "Cancel signal
sent". UI text describing backend behavior that did not happen (dev/changelog/720).

The fix threads a stop event into both loops and raises `SyncCancelled` from wherever it is
observed, which `_do_sync` catches ahead of its broad handler and routes to
`_mark_sync_cancelled`. Because what a cancel leaves behind differs by phase, the exception
carries the phase's own sentence and it is appended to the stored reason.

Covers:
  - UpsertCancelTests: `_upsert_channels` stops mid-loop and commits nothing.
  - ImportCancelTests: `_import_xmltv` stops before its delete (previous EPG kept) and
    inside its batch loop (buffered rows salvaged, count honest, reason names the loss).
  - CountPassCancelTests: the collapse guard's count pass is cancellable too.
  - CancelReasonTests: `_mark_sync_cancelled` appends the phase detail and keeps the
    cancelling caller's own reason.
  - DoSyncCancelTests: end-to-end through the real `_do_sync` - a cancel fired from inside
    each phase produces CANCELLED, not SUCCESS. These are the cases that fail without the
    fix on behavior rather than on a missing keyword argument.

No network: `requests.get` is patched at `app.accounts.requests.get`. Runs against a
throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sync_cancel_midphase
"""
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import (  # noqa: E402
    SyncCancelled, _count_projected_epg_entries, _do_sync, _import_xmltv,
    _mark_sync_cancelled, _upsert_channels, _sync_cancel_reasons,
)
from app.database import Account, AccountSyncLog, Channel, EPGEntry, M3uAccount  # noqa: E402
from tests.support import make_test_app  # noqa: E402

M3U_URL = 'http://provider.test/playlist.m3u8?user=realuser&pass=realpass'
EPG_URL = 'http://provider.test/xmltv.php?username=realuser&password=realpass'


def _playlist(n):
    """An M3U carrying n channels, all mapped to EPG ids the fixture XMLTV uses."""
    parts = ['#EXTM3U']
    for i in range(1, n + 1):
        parts.append(f'#EXTINF:-1 tvg-id="ch{i}.test",Channel {i}')
        parts.append(f'http://provider.test/stream{i}.ts')
    return ('\n'.join(parts) + '\n').encode('utf-8')


def _xmltv(entries):
    """entries: iterable of (epg_channel_id, offset_minutes, duration_minutes)."""
    now = datetime.utcnow()
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for chid, offset_min, dur_min in entries:
        start = now + timedelta(minutes=offset_min)
        stop = start + timedelta(minutes=dur_min)
        parts.append(
            f'<programme start="{start.strftime("%Y%m%d%H%M%S")} +0000" '
            f'stop="{stop.strftime("%Y%m%d%H%M%S")} +0000" channel="{chid}">'
            f'<title>Show</title></programme>'
        )
    parts.append('</tv>')
    return ''.join(parts).encode('utf-8')


def _many_programs(n_channels, per_channel):
    """Enough <programme> elements to cross the import loop's 500-entry checkpoint.

    Start offsets wrap inside 3 days on purpose: the import window is `epg_days` wide and
    `_match_program` drops anything past it, so a straight ascending offset would silently
    leave most of these unmatched and never reach the checkpoint at all.
    """
    return _xmltv([
        (f'ch{c}.test', 60 + (i % 130) * 30, 25)
        for c in range(1, n_channels + 1)
        for i in range(per_channel)
    ])


def _stream(i):
    return {'stream_id': i, 'name': f'Channel {i}',
            '_stream_url': f'http://provider.test/stream{i}.ts',
            'epg_channel_id': f'ch{i}.test'}


class UpsertCancelTests(unittest.TestCase):
    """`_upsert_channels` polls the stop event on the checkpoint it already runs, and
    aborting mid-loop leaves nothing behind: the whole upsert is one transaction its
    caller commits only on return."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Cancel Test', m3u_url=M3U_URL, status='OK')
        db.session.add(self.account)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def test_stop_event_already_set_aborts_before_any_channel_is_written(self):
        ev = threading.Event()
        ev.set()

        with self.assertRaises(SyncCancelled):
            _upsert_channels(self.account, [_stream(i) for i in range(1, 600)],
                             stop_event=ev)

        db.session.rollback()
        self.assertEqual(Channel.query.filter_by(account_id=self.account.id).count(), 0)

    def test_cancel_raised_partway_through_commits_no_partial_channel_list(self):
        # Fired from the progress checkpoint so the abort lands mid-loop rather than at
        # entry - the case the old code could not answer at all.
        ev = threading.Event()
        real_set_progress = None

        def _cancel_at_second_checkpoint(account_id, phase, done, total):
            real_set_progress(account_id, phase, done, total)
            if done > 1:
                ev.set()

        import app.accounts as accounts_mod
        real_set_progress = accounts_mod._set_sync_progress
        with mock.patch.object(accounts_mod, '_set_sync_progress',
                               side_effect=_cancel_at_second_checkpoint):
            with self.assertRaises(SyncCancelled):
                _upsert_channels(self.account, [_stream(i) for i in range(1, 1200)],
                                 stop_event=ev)

        db.session.rollback()
        self.assertEqual(
            Channel.query.filter_by(account_id=self.account.id).count(), 0,
            'an aborted upsert must commit nothing - the caller owns the commit')

    def test_detail_says_nothing_was_saved(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(SyncCancelled) as caught:
            _upsert_channels(self.account, [_stream(1)], stop_event=ev)
        self.assertIn('no changes were saved', str(caught.exception))

    def test_no_stop_event_still_upserts_normally(self):
        synced, _skipped, _dup, _drifted, _new = _upsert_channels(
            self.account, [_stream(i) for i in range(1, 6)])
        db.session.commit()
        self.assertEqual(synced, 5)
        self.assertEqual(Channel.query.filter_by(account_id=self.account.id).count(), 5)


class _EpgFixture(unittest.TestCase):
    """An account with one EPG-mapped channel and a guide already populated."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Cancel Test', m3u_url=M3U_URL, epg_url=EPG_URL,
                                  status='OK')
        db.session.add(self.account)
        db.session.flush()
        self.channel = Channel(
            account_id=self.account.id, stream_id=1, name='Ch1',
            stream_url='http://provider.test/stream1.ts', epg_channel_id='ch1.test',
        )
        db.session.add(self.channel)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _seed_old_epg(self, n=3):
        now = datetime.utcnow()
        for i in range(n):
            db.session.add(EPGEntry(
                channel_id=self.channel.id, title=f'Old {i}',
                start_time=now + timedelta(hours=i),
                stop_time=now + timedelta(hours=i, minutes=30)))
        db.session.commit()

    def _epg_titles(self):
        return {e.title for e in EPGEntry.query.filter_by(channel_id=self.channel.id).all()}


class ImportCancelTests(_EpgFixture):
    """The delete inside `_import_xmltv` is the line the cancel story turns on."""

    def test_cancel_before_the_delete_keeps_the_previous_epg(self):
        self._seed_old_epg()
        ev = threading.Event()
        ev.set()

        with self.assertRaises(SyncCancelled) as caught:
            _import_xmltv(self.account, _xmltv([('ch1.test', 60, 30)]), epg_days=3,
                          cfg={'sync': {'epg_collapse_threshold_percent': 0}},
                          stop_event=ev)

        db.session.rollback()
        self.assertEqual(len(self._epg_titles()), 3,
                          'stopping before the delete must leave the old guide intact')
        self.assertIn('previous guide data was kept', str(caught.exception))

    def test_cancel_inside_the_batch_loop_salvages_what_was_parsed(self):
        # Fired from the progress checkpoint, which the import loop reaches every 500
        # entries - so the cancel lands after the delete has already committed.
        ev = threading.Event()
        import app.accounts as accounts_mod
        real_set_progress = accounts_mod._set_sync_progress

        def _cancel_at_first_epg_checkpoint(account_id, phase, done, total):
            real_set_progress(account_id, phase, done, total)
            if phase == 'epg':
                ev.set()

        xml = _many_programs(1, 1400)
        with mock.patch.object(accounts_mod, '_set_sync_progress',
                               side_effect=_cancel_at_first_epg_checkpoint):
            with self.assertRaises(SyncCancelled) as caught:
                _import_xmltv(self.account, xml, epg_days=3,
                              cfg={'sync': {'epg_collapse_threshold_percent': 0}},
                              stop_event=ev)

        db.session.expire_all()
        stored = EPGEntry.query.filter_by(channel_id=self.channel.id).count()
        self.assertGreater(stored, 0, 'entries parsed before the cancel must be kept')
        self.assertLess(stored, 1400, 'the import must actually have stopped early')
        # The reason names the count, and the count must match what is really on disk -
        # `synced` counts rows appended to the pending batch, so a cancel that did not
        # salvage the buffer would report entries that were never inserted.
        self.assertIn(f'only the {stored} entries', str(caught.exception))
        self.assertIn('cleared', str(caught.exception))

    def test_unset_stop_event_imports_normally(self):
        self._seed_old_epg()
        synced, reason = _import_xmltv(
            self.account, _xmltv([('ch1.test', 60, 30)]), epg_days=3,
            cfg={'sync': {'epg_collapse_threshold_percent': 0}},
            stop_event=threading.Event())
        self.assertIsNone(reason)
        self.assertEqual(synced, 1)


class CountPassCancelTests(_EpgFixture):
    """The collapse guard's count pass is a second full walk of the payload, so it polls
    too - and stopping there is free, since it only counts."""

    def test_count_pass_stops_on_a_set_event(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(SyncCancelled):
            _count_projected_epg_entries(
                _many_programs(1, 900), {'ch1.test': [self.channel.id]}, False,
                datetime.utcnow() - timedelta(hours=1),
                datetime.utcnow() + timedelta(days=3),
                stop_event=ev)

    def test_count_pass_without_an_event_is_unchanged(self):
        total, err = _count_projected_epg_entries(
            _xmltv([('ch1.test', 60, 30)]), {'ch1.test': [self.channel.id]}, False,
            datetime.utcnow() - timedelta(hours=1),
            datetime.utcnow() + timedelta(days=3))
        self.assertIsNone(err)
        self.assertEqual(total, 1)

    def test_armed_guard_cancels_before_the_delete_and_keeps_the_old_epg(self):
        # baseline > 0 and a non-zero threshold is what arms the count pass at all.
        self._seed_old_epg()
        self.account.epg_entry_count = 3
        db.session.commit()
        ev = threading.Event()
        ev.set()

        with self.assertRaises(SyncCancelled):
            _import_xmltv(self.account, _many_programs(1, 900), epg_days=3,
                          cfg={'sync': {'epg_collapse_threshold_percent': 20}},
                          stop_event=ev)

        db.session.rollback()
        self.assertEqual(len(self._epg_titles()), 3)


class CancelReasonTests(unittest.TestCase):
    """`_mark_sync_cancelled` appends the phase's account of what it left behind, without
    losing the cancelling caller's own reason."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='Reason Test', m3u_url=M3U_URL, status='SYNCING')
        db.session.add(self.account)
        db.session.flush()
        self.sync_log = AccountSyncLog(account_id=self.account.id,
                                       started_at=datetime.utcnow(), status='IN_PROGRESS')
        db.session.add(self.sync_log)
        db.session.commit()

    def tearDown(self):
        _sync_cancel_reasons.pop(self.account.id, None)
        self.t.cleanup()

    def test_detail_is_appended_to_the_stored_reason(self):
        _mark_sync_cancelled(self.account.id, self.sync_log, detail='nothing was saved.')
        db.session.expire_all()
        log_row = db.session.get(AccountSyncLog, self.sync_log.id)
        self.assertEqual(log_row.status, 'CANCELLED')
        self.assertEqual(log_row.error_message, 'Cancelled by user - nothing was saved.')
        self.assertEqual(db.session.get(Account, self.account.id).last_error,
                          'Sync cancelled by user - nothing was saved.')

    def test_callers_own_reason_survives_the_detail(self):
        _sync_cancel_reasons[self.account.id] = 'Cancelled by account delete'
        _mark_sync_cancelled(self.account.id, self.sync_log, detail='nothing was saved.')
        db.session.expire_all()
        self.assertEqual(db.session.get(AccountSyncLog, self.sync_log.id).error_message,
                          'Cancelled by account delete - nothing was saved.')

    def test_no_detail_leaves_the_reason_untouched(self):
        _mark_sync_cancelled(self.account.id, self.sync_log)
        db.session.expire_all()
        self.assertEqual(db.session.get(AccountSyncLog, self.sync_log.id).error_message,
                          'Cancelled by user')


def _fake_get_factory(playlist_bytes, xml_bytes):
    def _fake_get(url, **kwargs):
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        if url == M3U_URL:
            resp.content = playlist_bytes
            return resp
        if url == EPG_URL:
            resp.content = xml_bytes
            return resp
        raise AssertionError(f'unexpected requests.get call: {url}')
    return _fake_get


class DoSyncCancelTests(unittest.TestCase):
    """End-to-end through the real `_do_sync`. These drive the cancel from inside a phase
    without passing any new argument, so they fail on the old code by reporting SUCCESS -
    which is the behavior this change exists to fix."""

    def setUp(self):
        self.t = make_test_app()
        self.account = M3uAccount(name='E2E Cancel', m3u_url=M3U_URL, epg_url=EPG_URL,
                                  status='OK')
        db.session.add(self.account)
        db.session.commit()
        self.account_id = self.account.id

    def tearDown(self):
        _sync_cancel_reasons.pop(self.account_id, None)
        self.t.cleanup()

    def _latest_log(self):
        return (AccountSyncLog.query.filter_by(account_id=self.account_id)
                .order_by(AccountSyncLog.id.desc()).first())

    def _sync_cancelling_in(self, phase, playlist, xml):
        """Run a real sync, setting its stop event from the first checkpoint of `phase`."""
        ev = threading.Event()
        import app.accounts as accounts_mod
        real_set_progress = accounts_mod._set_sync_progress

        def _cancel_in_phase(account_id, phase_name, done, total):
            real_set_progress(account_id, phase_name, done, total)
            if phase_name == phase and done > 1:
                ev.set()

        with mock.patch('app.accounts.requests.get',
                        side_effect=_fake_get_factory(playlist, xml)), \
             mock.patch.object(accounts_mod, '_set_sync_progress',
                               side_effect=_cancel_in_phase):
            _do_sync(self.account_id, ev)
        db.session.expire_all()

    def test_cancel_during_channel_upsert_finishes_cancelled_not_success(self):
        self._sync_cancelling_in('channels', _playlist(1200), _xmltv([('ch1.test', 60, 30)]))

        log_row = self._latest_log()
        self.assertEqual(log_row.status, 'CANCELLED',
                          'a cancel inside the channel phase must not report SUCCESS')
        self.assertIn('no changes were saved', log_row.error_message)
        account = db.session.get(Account, self.account_id)
        self.assertEqual(account.status, 'UNSYNCED')
        self.assertEqual(Channel.query.filter_by(account_id=self.account_id).count(), 0)

    def test_cancel_during_epg_import_finishes_cancelled_and_names_the_partial_guide(self):
        self._sync_cancelling_in('epg', _playlist(1), _many_programs(1, 1400))

        log_row = self._latest_log()
        self.assertEqual(log_row.status, 'CANCELLED',
                          'a cancel inside the EPG phase must not report SUCCESS')
        self.assertIn('entries imported before the cancel', log_row.error_message)
        # The channel phase committed before the EPG phase started, so those rows stay.
        self.assertEqual(Channel.query.filter_by(account_id=self.account_id).count(), 1)

    def test_cancel_skips_the_search_index_rebuild(self):
        # A rebuild would answer a cancellation with seconds of held write lock; the
        # staleness is safe because search_index_readiness() falls back to LIKE.
        with mock.patch('app.search_index.rebuild_search_indexes') as rebuild:
            self._sync_cancelling_in('channels', _playlist(1200),
                                     _xmltv([('ch1.test', 60, 30)]))
        rebuild.assert_not_called()

    def test_uncancelled_sync_still_finishes_success(self):
        with mock.patch('app.accounts.requests.get',
                        side_effect=_fake_get_factory(_playlist(3),
                                                      _xmltv([('ch1.test', 60, 30)]))):
            _do_sync(self.account_id, threading.Event())
        db.session.expire_all()

        self.assertEqual(self._latest_log().status, 'SUCCESS')
        self.assertEqual(db.session.get(Account, self.account_id).status, 'OK')
        self.assertEqual(Channel.query.filter_by(account_id=self.account_id).count(), 3)


if __name__ == '__main__':
    unittest.main()
