"""An account's login list (app/account_links.py, app/connection_limits.py,
DESIGN-account-providers.md §5, dev/changelog/1169): the other username/passwords an account
accepts, each with its own seats; a capture takes a seat on one and is launched with it.

The properties guarded here, each of which fails in its own way:

  * an account with no list takes seats and renders byte-identical URLs - the whole promise
    to the one-account user, and what every pre-existing connection_limits test already
    asserts by reading the holder dict by account id;
  * two logins are two seats, taken in list order; the seat taken decides the credentials
    the capture is launched with, at all three launch sites; a URL whose credentials the
    list does not know is launched as-is and the seat still counts;
  * release goes to the pool the seat was taken in, never re-resolved from an edited list;
  * a block takes seats from every pool the account holds, in order;
  * a refused login is skipped while another has a free seat and tried again when none
    does; the recording's next segment re-seats; the stamp, the event and the alert are
    written, the alert at most once per cooldown;
  * the list is the user's: add, edit (blank password keeps, a changed credential clears
    the refusal stamp), move, remove; the password is never served back; nothing else
    writes a row; the account's limit is the sum of its logins' seats and the page says so.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_account_logins
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db, channel_tester, preview, recorder  # noqa: E402
from app import account_links as links  # noqa: E402
from app import connection_limits as connlim  # noqa: E402
from app.account_blocks import account_limits, add_account_block  # noqa: E402
from app.alerts import ACCOUNT_LOGIN_REFUSED  # noqa: E402
from app.database import (  # noqa: E402
    AccountLogin, Alert, Login, RecordingEvent, RECORDING_LOGIN_REFUSED,
    REC_STATUS_IN_PROGRESS,
)

UNAUTHORIZED_TAIL = ('[in#0 @ 0x60a32827ff40] Error opening input: Server returned 401 '
                     'Unauthorized (authorization failed)\n'
                     'Error opening input file http://h1.harbor.example/live/main/pw1/1.ts.')
FORBIDDEN_TAIL = ('[http @ 0x5f3e58a5cb00] HTTP error 403 Forbidden\n'
                  '[in#0 @ 0x5f3e58a52f40] Error opening input: Server returned 403 Forbidden '
                  '(access denied)')
NOT_FOUND_TAIL = ('[http @ 0x5b9c9a264b00] HTTP error 404 Not Found\n'
                  '[in#0 @ 0x5b9c9a25af40] Error opening input: Server returned 404 Not Found')
SERVER_ERROR_TAIL = ('[http @ 0x55a8187a4b00] HTTP error 500 Internal Server Error\n'
                     '[in#0 @ 0x55a81879af40] Error opening input: Server returned 5XX Server Error reply')
RESOLVE_TAIL = ('[tcp @ 0x5972c611fe00] Failed to resolve hostname h1.harbor.example: '
                'Name or service not known')
STALL_TAIL = 'frame= 1500 fps= 25 q=-1.0 size=   12288kB time=00:01:00.00 bitrate=1677.7kbits/s'


class _Pair:
    def __init__(self, username, password):
        self.username, self.password = username, password


MAIN = _Pair('main', 'pw1')
SPARE = _Pair('spare', 'pw2')


def _open_alerts(login_id):
    return Alert.query.filter_by(alert_type=ACCOUNT_LOGIN_REFUSED,
                                 source=f'login:{login_id}:refused').all()


class RenderTests(unittest.TestCase):
    """render_login() is pure; the account with nothing listed gets the same object back."""

    def test_an_empty_list_or_no_login_renders_every_url_unchanged(self):
        for url in ('http://h1.harbor.example/live/main/pw1/1.ts', 'http://radio.example:8000/mount',
                    'http://h1.harbor.example/main/pw1/7', ''):
            self.assertIs(links.render_login(url, [], None), url)
            self.assertIs(links.render_login(url, [MAIN], None), url)
            self.assertIs(links.render_login(url, [], MAIN), url)

    def test_a_listed_pair_is_replaced_by_the_seated_login_and_nothing_else_moves(self):
        cases = {
            'http://h1.harbor.example/live/main/pw1/1.ts': 'http://h1.harbor.example/live/spare/pw2/1.ts',
            'http://h1.harbor.example:8080/main/pw1/7': 'http://h1.harbor.example:8080/spare/pw2/7',
            'https://h1.harbor.example/live/main/pw1/9.m3u8?x=1': 'https://h1.harbor.example/live/spare/pw2/9.m3u8?x=1',
        }
        for url, want in cases.items():
            self.assertEqual(links.render_login(url, [MAIN, SPARE], SPARE), want)

    def test_the_seated_login_renders_its_own_urls_unchanged(self):
        url = 'http://h1.harbor.example/live/main/pw1/1.ts'
        self.assertEqual(links.render_login(url, [MAIN, SPARE], MAIN), url)

    def test_unlisted_credentials_and_triplet_less_urls_are_untouched(self):
        for url in ('http://cdn.other.example/live/zz/yy/5.ts',   # a third-party feed's own
                    'http://radio.example:8000/mount',             # no triplet at all
                    'http://h1.harbor.example/get.php?username=main&password=pw1'):
            self.assertEqual(links.render_login(url, [MAIN, SPARE], SPARE), url)


class ClassifierTests(unittest.TestCase):
    """The spellings this toolchain's ffmpeg 7.1 produces for 401 and 403, measured."""

    def test_a_401_and_a_403_are_credential_refusals(self):
        self.assertTrue(links.is_credential_refusal(UNAUTHORIZED_TAIL))
        self.assertTrue(links.is_credential_refusal(FORBIDDEN_TAIL))

    def test_a_404_a_5xx_a_resolution_failure_a_stall_and_nothing_are_not(self):
        for tail in (NOT_FOUND_TAIL, SERVER_ERROR_TAIL, RESOLVE_TAIL, STALL_TAIL, '', None):
            self.assertFalse(links.is_credential_refusal(tail), tail)


class _Case(unittest.TestCase):
    """One account limited to one connection on its own, streams on h1 with the `main`
    credentials, plus a radio mount and a third-party feed with foreign credentials."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.acc = seed.make_account(name='harbor-direct', max_connections=1)
        self.chans = []
        for i in range(1, 3):
            ch = seed.make_channel(self.acc, stream_id=i, name=f'ch{i}')
            ch.stream_url = ch.raw_stream_url = f'http://h1.harbor.example/live/main/pw1/{i}.ts'
            self.chans.append(ch)
        self.foreign = seed.make_channel(self.acc, stream_id=5, name='foreign')
        self.foreign.stream_url = self.foreign.raw_stream_url = 'http://cdn.other.example/live/zz/yy/5.ts'
        db.session.commit()
        self.acc_id = self.acc.id
        self.ch_id = self.chans[0].id
        self.foreign_id = self.foreign.id
        connlim._holders.clear()
        connlim._seat_of.clear()

    def tearDown(self):
        connlim._holders.clear()
        connlim._seat_of.clear()
        self.t.cleanup()

    def _list(self, *specs):
        """specs: (name, username, password, seats). Returns the ids in order."""
        ids = [links.add_login(self.acc_id, *spec).id for spec in specs]
        db.session.expire_all()
        return ids

    def _pool(self, login_id):
        return connlim._holders.get(('login', login_id), [])

    def _stamp_refused(self, login_id, minutes_ago=0):
        row = db.session.get(Login, login_id)
        row.last_refused_at = datetime.utcnow() - timedelta(minutes=minutes_ago)
        row.last_refused_detail = 'HTTP error 403 Forbidden'
        db.session.commit()


class SeatTests(_Case):

    def test_an_account_with_no_list_takes_one_seat_in_its_own_pool_as_before(self):
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 11))
        self.assertFalse(connlim.try_acquire(self.acc_id, 'recording', 12))
        self.assertEqual(connlim._holders[self.acc_id], [('recording', 11)])
        self.assertIsNone(connlim.held_login_id('recording', 11))
        url = 'http://h1.harbor.example/live/main/pw1/1.ts'
        self.assertIs(links.render_held_login(url, self.acc_id, 'recording', 11)[0], url)
        connlim.release(self.acc_id, 'recording', 11)
        self.assertEqual(connlim._holders.get(self.acc_id, []), [])

    def test_two_logins_are_two_seats_taken_in_list_order(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 11))
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 12))
        self.assertFalse(connlim.try_acquire(self.acc_id, 'recording', 13),
                         'the account itself allows 1, but its two logins allow 2 - and no more')
        self.assertEqual(self._pool(main), [('recording', 11)])
        self.assertEqual(self._pool(spare), [('recording', 12)])
        self.assertEqual(connlim.held_login_id('recording', 12), spare)
        self.assertTrue(connlim.at_limit(self.acc_id))
        self.assertEqual(connlim.holder_counts(), {self.acc_id: 2})
        self.assertEqual(connlim.describe_holders(self.acc_id), 'a recording')
        self.assertEqual(connlim.pool_holder_counts(self.acc_id), {main: 1, spare: 1})
        self.assertEqual(account_limits([self.acc_id])[self.acc_id], 2)

    def test_the_seat_taken_decides_the_credentials_rendered(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        connlim.try_acquire(self.acc_id, 'test', 501)
        connlim.try_acquire(self.acc_id, 'preview', 'sess-1')
        url = 'http://h1.harbor.example/live/main/pw1/1.ts'
        self.assertEqual(links.render_held_login(url, self.acc_id, 'test', 501),
                         (url, main))
        self.assertEqual(links.render_held_login(url, self.acc_id, 'preview', 'sess-1'),
                         ('http://h1.harbor.example/live/spare/pw2/1.ts', spare))

    def test_unlisted_credentials_launch_as_is_and_the_seat_still_counts(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        connlim.try_acquire(self.acc_id, 'recording', 11)
        connlim.try_acquire(self.acc_id, 'recording', 12)
        url = 'http://cdn.other.example/live/zz/yy/5.ts'
        rendered, login_id = links.render_held_login(url, self.acc_id, 'recording', 12)
        self.assertEqual(rendered, url)
        self.assertEqual(login_id, spare)
        self.assertFalse(connlim.try_acquire(self.acc_id, 'recording', 13))

    def test_release_after_a_reorder_releases_from_the_original_pool(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        connlim.try_acquire(self.acc_id, 'recording', 11)
        self.assertEqual(self._pool(main), [('recording', 11)])
        links.move_login(self.acc_id, spare, 'up')
        connlim.release(self.acc_id, 'recording', 11)
        self.assertEqual(self._pool(main), [])
        self.assertEqual(self._pool(spare), [])
        self.assertNotIn(('recording', 11), connlim._seat_of)

    def test_a_login_removed_while_held_keeps_its_seat_until_release(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        connlim.try_acquire(self.acc_id, 'recording', 11)
        links.remove_login(self.acc_id, main)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 11),
                        'the held seat is still this account\'s - idempotent, not a second seat')
        self.assertEqual(connlim.holder_counts(), {self.acc_id: 1})
        url = 'http://h1.harbor.example/live/main/pw1/1.ts'
        self.assertEqual(links.render_held_login(url, self.acc_id, 'recording', 11), (url, main))
        connlim.release(self.acc_id, 'recording', 11)
        self.assertEqual(connlim._holders, {})

    def test_preempting_a_test_strips_it_from_whichever_pool_it_sits_in(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        connlim.try_acquire(self.acc_id, 'recording', 11)
        connlim.try_acquire(self.acc_id, 'test', 501)
        self.assertEqual(connlim.preempt_tests_for_slot(self.acc_id), [501])
        self.assertEqual(self._pool(spare), [])
        self.assertEqual(self._pool(main), [('recording', 11)])
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 12))

    def test_accounts_without_a_free_recording_slot_looks_across_the_pools(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        connlim.try_acquire(self.acc_id, 'recording', 11)
        self.assertEqual(connlim.accounts_without_free_recording_slot([self.acc_id]), set())
        connlim.try_acquire(self.acc_id, 'recording', 12)
        self.assertEqual(connlim.accounts_without_free_recording_slot([self.acc_id]), {self.acc_id})
        self.assertEqual(connlim.accounts_without_free_recording_slot(
            [self.acc_id], exclude_holder=('recording', 12)), set())


class BlockTests(_Case):

    def test_a_whole_account_block_refuses_every_pool(self):
        self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        add_account_block(self.acc_id, datetime.utcnow() + timedelta(hours=1))
        self.assertFalse(connlim.try_acquire(self.acc_id, 'recording', 11))
        self.assertTrue(connlim.at_limit(self.acc_id))

    def test_a_partial_block_takes_seats_from_the_pools_in_order(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 2))
        add_account_block(self.acc_id, datetime.utcnow() + timedelta(hours=1), slots=2)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 11))
        self.assertEqual(self._pool(main), [], 'the block took main\'s one seat first')
        self.assertEqual(self._pool(spare), [('recording', 11)])
        self.assertFalse(connlim.try_acquire(self.acc_id, 'recording', 12),
                         'two blocked of three: one seat left, and it is taken')
        self.assertTrue(connlim.must_yield_to_block(self.acc_id, 11) is False)


class RefusalTests(_Case):

    def test_a_refused_login_is_skipped_while_another_has_a_seat(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        self._stamp_refused(main)
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 11))
        self.assertEqual(self._pool(spare), [('recording', 11)])
        self.assertTrue(connlim.try_acquire(self.acc_id, 'recording', 12),
                        'with no other seat free the refused login is tried again')
        self.assertEqual(self._pool(main), [('recording', 12)])

    def test_a_refusal_past_the_cooldown_no_longer_skips(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        self._stamp_refused(main, minutes_ago=31)
        connlim.try_acquire(self.acc_id, 'recording', 11)
        self.assertEqual(self._pool(main), [('recording', 11)])

    def test_the_hook_stamps_the_seated_login_logs_the_event_and_alerts_once_per_window(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, channel_id=self.ch_id)
        db.session.commit()
        rid = rec.id
        connlim.try_acquire(self.acc_id, 'recording', rid)
        stamped = links.note_refusal_for_holder('recording', rid, trigger=f'Recording {rid} (segment 1)',
                                                stderr_tail=UNAUTHORIZED_TAIL, recording_id=rid)
        self.assertEqual(stamped, main)
        db.session.expire_all()
        row = db.session.get(Login, main)
        self.assertIsNotNone(row.last_refused_at)
        self.assertIn('Server returned 401', row.last_refused_detail)
        events = RecordingEvent.query.filter_by(recording_id=rid, event_type=RECORDING_LOGIN_REFUSED).all()
        self.assertEqual(len(events), 1)
        self.assertIn('"main"', events[0].detail)
        alerts = _open_alerts(main)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].recording_id, rid)
        self.assertIn('main', alerts[0].title)
        self.assertNotIn('pw1', alerts[0].body)
        # The next segment hits the same refusal: stamped and logged again, no second alert.
        links.note_refusal_for_holder('recording', rid, trigger=f'Recording {rid} (segment 2)',
                                      stderr_tail=UNAUTHORIZED_TAIL, recording_id=rid)
        self.assertEqual(len(_open_alerts(main)), 1)
        self.assertEqual(RecordingEvent.query.filter_by(
            recording_id=rid, event_type=RECORDING_LOGIN_REFUSED).count(), 2)

    def test_the_hook_does_nothing_for_an_account_with_no_list_and_never_raises(self):
        connlim.try_acquire(self.acc_id, 'recording', 11)
        self.assertIsNone(links.note_refusal_for_holder('recording', 11, trigger='Recording 11',
                                                        stderr_tail=UNAUTHORIZED_TAIL))
        self.assertEqual(Alert.query.filter_by(alert_type=ACCOUNT_LOGIN_REFUSED).count(), 0)
        with mock.patch.object(links, 'note_refusal', side_effect=RuntimeError('boom')):
            self.assertIsNone(links.note_refusal_for_holder('recording', 11, trigger='x',
                                                            stderr_tail=UNAUTHORIZED_TAIL, login_id=999))

    def test_the_next_segment_reseats_onto_a_working_login(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        connlim.try_acquire(self.acc_id, 'recording', 11)
        self.assertEqual(connlim.held_login_id('recording', 11), main)
        self.assertEqual(connlim.reseat_if_refused(self.acc_id, 'recording', 11), main,
                         'nothing refused: the seat stays')
        self._stamp_refused(main)
        self.assertEqual(connlim.reseat_if_refused(self.acc_id, 'recording', 11), spare)
        self.assertEqual(self._pool(main), [])
        self.assertEqual(self._pool(spare), [('recording', 11)])
        # Another recording now holds spare's one seat: a refused login with nowhere to go
        # keeps its seat rather than stranding the recording.
        connlim.try_acquire(self.acc_id, 'recording', 12)
        self._stamp_refused(spare)
        self.assertEqual(connlim.reseat_if_refused(self.acc_id, 'recording', 11), spare)

    def test_a_recording_with_no_login_seat_reseats_to_nothing(self):
        connlim.try_acquire(self.acc_id, 'recording', 11)
        self.assertIsNone(connlim.reseat_if_refused(self.acc_id, 'recording', 11))
        self.assertIsNone(connlim.reseat_if_refused(self.acc_id, 'recording', 404))


class _Capture(RuntimeError):
    """Raised from a patched command builder to stop a launch once the URL is known."""


class LaunchSiteTests(_Case):
    """Each launch site renders the seated login into the command and nothing is stored."""

    def _recording(self):
        rec = seed.make_recording(status=REC_STATUS_IN_PROGRESS, channel_id=self.ch_id,
                                  url='http://h1.harbor.example/live/main/pw1/1.ts')
        db.session.commit()
        return rec.id

    def _launch_url(self, rid):
        seen = {}

        def _build(cfg, url, *a, **kw):
            seen['url'] = url
            raise _Capture()

        with mock.patch.object(recorder, 'build_capture_cmd', _build), \
                self.assertRaises(_Capture):
            recorder._launch_segment(self.t.app, rid, 1)
        return seen['url']

    def test_the_recorder_launches_with_the_seated_login_and_reseats_off_a_refused_one(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        rid = self._recording()
        connlim.try_acquire(self.acc_id, 'recording', rid)
        self.assertEqual(self._launch_url(rid), 'http://h1.harbor.example/live/main/pw1/1.ts')
        self._stamp_refused(main)
        self.assertEqual(self._launch_url(rid), 'http://h1.harbor.example/live/spare/pw2/1.ts')
        self.assertEqual(connlim.held_login_id('recording', rid), spare)
        db.session.expire_all()
        rec = db.session.get(recorder.Recording, rid)
        self.assertEqual(rec.url, 'http://h1.harbor.example/live/main/pw1/1.ts',
                         'the substitution lives on the command, never on the row')

    def test_the_recorder_launches_todays_url_for_an_account_with_no_list(self):
        rid = self._recording()
        connlim.try_acquire(self.acc_id, 'recording', rid)
        self.assertEqual(self._launch_url(rid), 'http://h1.harbor.example/live/main/pw1/1.ts')

    def test_the_tester_launches_with_the_seated_login(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        connlim.try_acquire(self.acc_id, 'recording', 11)   # main is taken; the test seats on spare
        seen = {}

        def _build(cfg, url, *a, **kw):
            seen['url'] = url
            raise _Capture()

        with mock.patch('app.proc_utils.build_capture_cmd', _build), self.assertRaises(_Capture):
            channel_tester.run_channel_test(self.t.app, self.ch_id)
        self.assertEqual(seen['url'], 'http://h1.harbor.example/live/spare/pw2/1.ts')
        self.assertEqual(self._pool(spare), [], 'the seat is released however the test ends')

    def test_the_preview_launches_with_the_seated_login_and_remembers_it(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        connlim.try_acquire(self.acc_id, 'recording', 11)
        seen = {}
        real_build = preview.build_preview_cmd

        def _build(cfg, url, *a, **kw):
            seen['url'] = url
            return real_build(cfg, url, *a, **kw)

        with mock.patch.object(preview, 'build_preview_cmd', _build), \
                mock.patch.object(preview.subprocess, 'Popen', side_effect=OSError('no ffmpeg')), \
                self.t.app.test_request_context():
            with self.assertRaises(preview.PreviewRefused):
                preview.start_preview(self.ch_id)
        self.assertEqual(seen['url'], 'http://h1.harbor.example/live/spare/pw2/1.ts')
        session = next(iter(preview._sessions.values()))
        self.assertEqual(session.login_id, spare)
        self.assertEqual(self._pool(spare), [], 'a failed start releases the seat')
        # The reaper reads the stderr after the seat is gone: the session's own login_id is
        # what the refusal is stamped on.
        preview._roll_if_unresolved(session, FORBIDDEN_TAIL)
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(Login, spare).last_refused_at)
        self.assertIsNone(db.session.get(Login, main).last_refused_at)


class ListWriterTests(_Case):

    def test_add_validates_and_refuses_a_duplicate_username_or_name(self):
        links.add_login(self.acc_id, 'main', 'main', 'pw1', 2)
        for bad in (('', 'u', 'p', 1), ('x', '', 'p', 1), ('x', 'u', '', 1), ('x', 'u', 'p', 0),
                    ('x', 'u', 'p', 'two'), ('x', 'a/b', 'p', 1), ('other', 'main', 'p', 1),
                    ('MAIN', 'u2', 'p', 1)):
            with self.assertRaises(ValueError, msg=str(bad)):
                links.add_login(self.acc_id, *bad)
        self.assertEqual([lg.name for lg in links.logins_for_account(self.acc_id)], ['main'])

    def test_edit_keeps_the_password_when_blank_and_clears_the_stamp_on_a_new_credential(self):
        main, = self._list(('main', 'main', 'pw1', 1))
        self._stamp_refused(main)
        links.update_login(self.acc_id, main, 'primary', 'main', '', 3)
        db.session.expire_all()
        row = db.session.get(Login, main)
        self.assertEqual((row.name, row.username, row.password, row.max_connections),
                         ('primary', 'main', 'pw1', 3))
        self.assertIsNotNone(row.last_refused_at, 'same credentials: the refusal stands')
        links.update_login(self.acc_id, main, 'primary', 'main', 'pw9', 3)
        db.session.expire_all()
        row = db.session.get(Login, main)
        self.assertEqual(row.password, 'pw9')
        self.assertIsNone(row.last_refused_at, 'a new credential has not been refused yet')
        with self.assertRaises(LookupError):
            links.update_login(self.acc_id, 12345, 'x', 'y', 'z', 1)

    def test_move_and_remove(self):
        main, spare, third = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1),
                                        ('third', 'third', 'pw3', 1))
        self.assertEqual(links.move_login(self.acc_id, third, 'up'), [main, third, spare])
        self.assertEqual(links.move_login(self.acc_id, main, 'up'), [main, third, spare],
                         'the first login moving up is a no-op')
        self.assertEqual(links.remove_login(self.acc_id, third), 'third')
        self.assertIsNone(db.session.get(Login, third), 'held by no other account: the row goes')
        self.assertEqual([lg.id for lg in links.logins_for_account(self.acc_id)], [main, spare])
        with self.assertRaises(LookupError):
            links.remove_login(self.acc_id, third)

    def test_deleting_the_account_takes_its_logins_but_not_one_another_account_holds(self):
        main, spare = self._list(('main', 'main', 'pw1', 1), ('spare', 'spare', 'pw2', 1))
        other = seed.make_account(name='harbor-curated')
        db.session.add(AccountLogin(account_id=other.id, login_id=main, position=0))
        db.session.commit()
        resp = self.client.delete(f'/api/accounts/{self.acc_id}')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertIsNone(db.session.get(Login, spare))
        self.assertIsNotNone(db.session.get(Login, main), 'still held elsewhere')
        self.assertEqual(AccountLogin.query.filter_by(account_id=self.acc_id).count(), 0)


class RouteAndPageTests(_Case):

    def test_the_page_shows_the_hint_with_no_list_and_the_rows_and_the_sum_with_one(self):
        html = self.client.get(f'/accounts/{self.acc_id}').get_data(as_text=True)
        self.assertIn('data-section="logins"', html)
        self.assertIn("One login, the account's own.", html)
        self.assertEqual(html.count('data-act="login-add"'), 1)
        main, spare = self._list(('main', 'main', 'harbor-secret-one', 1),
                                 ('spare', 'spare', 'harbor-secret-two', 2))
        self._stamp_refused(spare)
        connlim.try_acquire(self.acc_id, 'recording', 11)
        html = self.client.get(f'/accounts/{self.acc_id}').get_data(as_text=True)
        self.assertIn('<strong>main</strong>', html)
        self.assertIn('<code>spare</code>', html)
        self.assertIn('1 of 1 in use', html)
        self.assertIn('0 of 2 in use', html)
        self.assertIn('Skipped while another login has a free seat.', html)
        self.assertIn('(logins: main, spare)', html)
        self.assertNotIn('(global)', html.split('Max connections')[1][:400])
        self.assertNotIn('harbor-secret-one', html)
        self.assertNotIn('harbor-secret-two', html)

    def test_the_routes_add_edit_move_and_remove_and_never_serve_the_password(self):
        resp = self.client.get(f'/api/accounts/{self.acc_id}/logins')
        self.assertEqual(resp.get_json()['seat_prefill'], 1)
        resp = self.client.post(f'/api/accounts/{self.acc_id}/logins',
                                json={'name': 'main', 'username': 'main',
                                      'password': 'harbor-secret-one', 'max_connections': '2'})
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200, body)
        main = body['login_id']
        resp = self.client.post(f'/api/accounts/{self.acc_id}/logins',
                                json={'name': 'spare', 'username': 'spare', 'password': 'pw2',
                                      'max_connections': 1})
        spare = resp.get_json()['login_id']
        resp = self.client.post(f'/api/accounts/{self.acc_id}/logins',
                                json={'name': 'dup', 'username': 'main', 'password': 'x',
                                      'max_connections': 1})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('already', resp.get_json()['error'])

        body = self.client.get(f'/api/accounts/{self.acc_id}/logins').get_json()
        self.assertEqual([lg['name'] for lg in body['logins']], ['main', 'spare'])
        self.assertNotIn('harbor-secret-one', resp.get_data(as_text=True))
        self.assertFalse(any('password' in lg for lg in body['logins']))

        resp = self.client.post(f'/api/accounts/{self.acc_id}/logins/{main}',
                                json={'name': 'primary', 'username': 'main', 'password': '',
                                      'max_connections': 3})
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(db.session.get(Login, main).password, 'harbor-secret-one')
        resp = self.client.post(f'/api/accounts/{self.acc_id}/logins/{spare}/move',
                                json={'direction': 'up'})
        self.assertEqual(resp.get_json()['order'], [spare, main])
        resp = self.client.post(f'/api/accounts/{self.acc_id}/logins/{spare}/move',
                                json={'direction': 'sideways'})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.delete(f'/api/accounts/{self.acc_id}/logins/{spare}')
        self.assertEqual(resp.status_code, 200, resp.get_json())
        resp = self.client.delete(f'/api/accounts/{self.acc_id}/logins/{spare}')
        self.assertEqual(resp.status_code, 404)
        self.assertEqual([lg.name for lg in links.logins_for_account(self.acc_id)], ['primary'])

    def test_the_seat_prefill_prefers_the_provider_reported_cap(self):
        self.acc.provider_max_connections = 4
        db.session.commit()
        resp = self.client.get(f'/api/accounts/{self.acc_id}/logins')
        self.assertEqual(resp.get_json()['seat_prefill'], 4)


class HookPresenceTests(unittest.TestCase):
    """The three capture paths ask the classifier and stamp the refusal, and the three
    launch sites render the seated login. A static check, since driving a real refusal
    through the watchdog is a whole-process test; the hook and the render are covered
    above."""

    def test_each_capture_path_hooks_the_refusal_and_renders_the_login(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel in ('app/watchdog.py', 'app/channel_tester.py', 'app/preview.py'):
            with open(os.path.join(root, rel), encoding='utf-8') as fh:
                src = fh.read()
            self.assertIn('is_credential_refusal(', src, rel)
            self.assertIn('note_refusal_for_holder(', src, rel)
        for rel in ('app/recorder.py', 'app/channel_tester.py', 'app/preview.py'):
            with open(os.path.join(root, rel), encoding='utf-8') as fh:
                src = fh.read()
            self.assertIn('render_held_login(', src, rel)


if __name__ == '__main__':
    unittest.main()
