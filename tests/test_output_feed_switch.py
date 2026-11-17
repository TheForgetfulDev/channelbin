"""The IPTV output's own switch, its play log and ChannelBin's address (dev/changelog/1525).

The switch: a channel or a group a person took off the output is in the TV Guide and in
nothing the feed serves, keeps its feed id, and a play of it is refused by name. The log:
every play that ended, was refused or was redirected is one line in output_feed_plays,
written by the feed's own writer and never inline. No dev/docs/BUGS.md entry: a feature.
"""
import os
import sys
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.output_feed import FeedCase as _Case, PASS, USER, feed_cfg as _cfg  # noqa: E402,F401

import app.output_feed as feed  # noqa: E402
from app import db  # noqa: E402
from app.database import (Alert, ChannelEvent, ChannelGroupEvent, FEED_PLAY_ENDED,  # noqa: E402
                          FEED_PLAY_REDIRECTED, FEED_PLAY_REFUSED, OUTPUT_FEED_SWITCHED,
                          OutputFeedPlay)


class SwitchTests(_Case):
    def test_the_one_writer_moves_the_column_and_logs_the_move(self):
        row, moved = self.switch(feed.KIND_CHANNEL, self.alpha.id, False)
        self.assertTrue(moved)
        self.assertFalse(row.on_output)
        event = ChannelEvent.query.filter_by(channel_id=self.alpha.id,
                                             event_type=OUTPUT_FEED_SWITCHED).one()
        self.assertIn('Taken off the IPTV output', event.detail)
        _row, moved = self.switch(feed.KIND_GROUP, self.group.id, False)
        self.assertTrue(moved)
        event = ChannelGroupEvent.query.filter_by(group_id=self.group.id,
                                                  event_type=OUTPUT_FEED_SWITCHED).one()
        self.assertIsNone(event.channel_id)

    def test_a_switch_that_did_not_move_logs_nothing(self):
        _row, moved = self.switch(feed.KIND_CHANNEL, self.alpha.id, True)
        self.assertFalse(moved)
        self.assertEqual(ChannelEvent.query.filter_by(event_type=OUTPUT_FEED_SWITCHED).count(), 0)

    def test_only_a_guide_row_has_the_switch(self):
        self.assertEqual(feed.set_on_output(feed.KIND_CHANNEL, self.loose.id, False), (None, False))
        self.assertEqual(feed.set_on_output(feed.KIND_CHANNEL, 99999, False), (None, False))
        with self.assertRaises(ValueError):
            feed.set_on_output(feed.KIND_SPORTS, 1, False)

    def test_the_switch_never_touches_the_guide_flag(self):
        self.switch(feed.KIND_CHANNEL, self.alpha.id, False)
        self.switch(feed.KIND_GROUP, self.group.id, False)
        db.session.expire_all()
        self.assertTrue(self.alpha.in_guide)
        self.assertTrue(self.group.in_guide)


class ServedSetTests(_Case):
    def test_a_row_taken_off_leaves_every_answer_and_the_rest_move_up(self):
        before = {r['name']: (r['num'], r['stream_id']) for r in self.streams()}
        self.assertEqual([before[n][0] for n in ('Alpha', 'Game feeds', 'Beta')], [1, 2, 3])
        self.switch(feed.KIND_GROUP, self.group.id, False)
        after = {r['name']: (r['num'], r['stream_id']) for r in self.streams()}
        self.assertEqual(after, {'Alpha': (1, before['Alpha'][1]), 'Beta': (2, before['Beta'][1])})
        gid = str(feed.feed_id_for_group(self.group.id))
        self.assertNotIn('Game feeds', self.get('/get.php').get_data(as_text=True))
        self.assertNotIn(f'channel id="{gid}"', self.get('/xmltv.php').get_data(as_text=True))

    def test_a_row_put_back_is_the_same_channel_it_was(self):
        before = {r['name']: r['stream_id'] for r in self.streams()}
        self.switch(feed.KIND_CHANNEL, self.alpha.id, False)
        self.switch(feed.KIND_CHANNEL, self.alpha.id, True)
        self.assertEqual({r['name']: r['stream_id'] for r in self.streams()}, before)

    def test_the_pages_view_keeps_what_is_not_offered_and_says_why(self):
        self.switch(feed.KIND_CHANNEL, self.beta.id, False)
        self.t.sandbox_config(_cfg(accounts=[self.other.id]))
        rows = {r.name: r for r in feed.catalog(everything=True)}
        self.assertEqual(set(rows), {'Alpha', 'Game feeds', 'Beta'})
        self.assertEqual(rows['Alpha'].left_out, feed.LEFT_OUT_ACCOUNT)
        self.assertFalse(rows['Beta'].on_output)
        self.assertEqual([rows[n].num for n in ('Alpha', 'Game feeds', 'Beta')], [0, 1, 0])
        self.assertEqual([r.name for r in feed.catalog()], ['Game feeds'])

    def test_a_group_with_nothing_to_play_says_why_on_its_row(self):
        self.t.sandbox_config(_cfg(accounts=[self.account.id]))
        rows = {r.name: r for r in feed.catalog(everything=True)}
        self.assertIn('not allowed', rows['Game feeds'].why_none)
        self.assertTrue(rows['Game feeds'].offered)


class RefusedPlayTests(_Case):
    def test_a_kick_left_over_from_an_exited_writer_does_not_write_the_line_early(self):
        """dev/docs/BUGS.md 2026-11-16 (play log kick): discard_play_log() can set the kick
        just after the writer it meant has exited, and the next writer then drained the
        queue at once, so a caller's flush_play_log() found nothing to write."""
        feed._log_kick.set()
        self.addCleanup(feed._log_kick.clear)
        self.switch(feed.KIND_CHANNEL, self.alpha.id, False)
        fid = feed.feed_id_for_channel(self.alpha.id)
        self.client.get(f'/live/{USER}/{PASS}/{fid}.ts')
        time.sleep(0.2)   # long enough for a writer that did not wait to have drained it
        self.assertEqual(feed.flush_play_log(), 1)

    def test_a_play_of_a_row_taken_off_is_refused_by_name_with_no_alert(self):
        self.switch(feed.KIND_CHANNEL, self.alpha.id, False)
        fid = feed.feed_id_for_channel(self.alpha.id)
        resp = self.client.get(f'/live/{USER}/{PASS}/{fid}.ts')
        self.assertEqual(resp.status_code, 409)
        self.assertIn(feed.WHY_TAKEN_OFF, resp.get_json()['error'])
        self.assertEqual(Alert.query.count(), 0)
        self.assertEqual(feed.flush_play_log(), 1)
        line = OutputFeedPlay.query.one()
        self.assertEqual((line.kind, line.feed_id), (FEED_PLAY_REFUSED, fid))
        self.assertIn(feed.WHY_TAKEN_OFF, line.message)

    def test_redirect_mode_refuses_it_too(self):
        self.t.sandbox_config(_cfg(mode='redirect'))
        self.switch(feed.KIND_GROUP, self.group.id, False)
        fid = feed.feed_id_for_group(self.group.id)
        resp = self.client.get(f'/live/{USER}/{PASS}/{fid}.ts')
        self.assertEqual(resp.status_code, 409)
        self.assertIn(feed.WHY_TAKEN_OFF, resp.get_json()['error'])


class PlayLogTests(_Case):
    def test_a_refusal_asked_again_is_one_line_that_counts(self):
        self.switch(feed.KIND_CHANNEL, self.alpha.id, False)
        fid = feed.feed_id_for_channel(self.alpha.id)
        for _ in range(3):
            self.client.get(f'/live/{USER}/{PASS}/{fid}.ts')
        feed.flush_play_log()
        self.client.get(f'/live/{USER}/{PASS}/{fid}.ts')
        feed.flush_play_log()
        line = OutputFeedPlay.query.one()
        self.assertEqual(line.count, 4)
        self.assertGreaterEqual(line.at, line.first_at)

    def test_the_same_refusal_after_a_long_gap_is_a_new_line(self):
        old = datetime.utcnow() - feed.REFUSAL_RUN_GAP - timedelta(minutes=5)
        db.session.add(OutputFeedPlay(kind=FEED_PLAY_REFUSED, at=old, first_at=old, feed_id=7,
                                      label='x', message='why'))
        db.session.commit()
        feed._write_plays([{'kind': FEED_PLAY_REFUSED, 'at': datetime.utcnow(), 'feed_id': 7,
                            'label': 'x', 'client': 'c', 'message': 'why'}])
        self.assertEqual(OutputFeedPlay.query.count(), 2)

    def test_a_redirect_is_a_line(self):
        self.t.sandbox_config(_cfg(mode='redirect'))
        fid = feed.feed_id_for_channel(self.alpha.id)
        self.assertEqual(self.client.get(f'/live/{USER}/{PASS}/{fid}.ts').status_code, 302)
        feed.flush_play_log()
        line = OutputFeedPlay.query.one()
        self.assertEqual((line.kind, line.channel_id, line.channel_name),
                         (FEED_PLAY_REDIRECTED, self.alpha.id, 'Alpha'))

    def test_a_play_that_ended_is_a_line_with_its_reason_and_numbers(self):
        s = self.session(feed.feed_id_for_channel(self.alpha.id), app=False)
        self.assertTrue(feed.stop_session(s.id))
        feed.flush_play_log()
        line = OutputFeedPlay.query.one()
        self.assertEqual((line.kind, line.reason, line.bytes_sent),
                         (FEED_PLAY_ENDED, feed.REASON_STOPPED, 4096))
        self.assertGreaterEqual(line.seconds, 90)
        self.assertEqual(line.message, feed.REASON_TEXT[feed.REASON_STOPPED])
        self.assertFalse(feed.stop_session(s.id))

    def test_the_writer_thread_writes_without_being_asked(self):
        s = self.session(feed.feed_id_for_channel(self.alpha.id))
        feed.stop_session(s.id)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            db.session.remove()
            if OutputFeedPlay.query.count():
                break
            time.sleep(0.05)
        self.assertEqual(OutputFeedPlay.query.count(), 1)

    def test_a_play_cut_by_shutdown_starts_no_writer(self):
        self.session(feed.feed_id_for_channel(self.alpha.id))
        feed.stop_all(feed.REASON_SHUTDOWN)
        self.assertEqual(feed._play_queue, [])
        self.assertIsNone(feed._log_writer)

    def test_taking_a_row_off_ends_only_its_own_plays(self):
        a = self.session(feed.feed_id_for_channel(self.alpha.id), app=False)
        b = self.session(feed.feed_id_for_channel(self.beta.id), app=False)
        self.assertEqual(feed.live_count_for(a.feed_id), 1)
        self.assertEqual(feed.stop_feed_id(a.feed_id, feed.REASON_TAKEN_OFF), 1)
        self.assertEqual((a.reason, b.live), (feed.REASON_TAKEN_OFF, True))

    def test_old_lines_are_pruned_on_the_back_of_a_write(self):
        old = datetime.utcnow() - timedelta(days=feed.PLAY_LOG_DAYS + 1)
        db.session.add(OutputFeedPlay(kind=FEED_PLAY_ENDED, at=old, first_at=old, feed_id=1))
        db.session.commit()
        feed._write_plays([{'kind': FEED_PLAY_REDIRECTED, 'at': datetime.utcnow(), 'feed_id': 2,
                            'label': 'x', 'client': 'c'}])
        self.assertEqual([p.feed_id for p in OutputFeedPlay.query.all()], [2])

    def test_every_reason_has_its_text(self):
        for reason in (feed.REASON_TAKEN_OFF, feed.REASON_STOPPED):
            self.assertTrue(feed.REASON_TEXT[reason])


class AddressTests(unittest.TestCase):
    def test_the_old_key_moves_to_the_new_one(self):
        from app.config import _cfg_m009_public_url, public_url
        cfg = _cfg_m009_public_url({'notifications': {'base_url': ' http://192.168.1.50:5000/ ',
                                                      'push_rate_limit_seconds': 60}})
        self.assertEqual(public_url(cfg), 'http://192.168.1.50:5000')
        self.assertNotIn('base_url', cfg['notifications'])

    def test_a_value_already_under_the_new_key_wins_and_an_empty_one_carries_nothing(self):
        from app.config import _cfg_m009_public_url, public_url
        cfg = _cfg_m009_public_url({'flask': {'public_url': 'http://new'},
                                    'notifications': {'base_url': 'http://old', 'x': 1}})
        self.assertEqual(public_url(cfg), 'http://new')
        cfg = _cfg_m009_public_url({'notifications': {'base_url': '', 'x': 1}})
        self.assertEqual(public_url(cfg), '')

    def test_a_push_link_is_built_from_the_new_key(self):
        from app import alerts
        with mock.patch('app.config.load_config',
                        return_value={'flask': {'public_url': 'http://dvr.local:5000/'}}):
            self.assertEqual(alerts._build_recording_link(44),
                             'http://dvr.local:5000/recordings/44')


if __name__ == '__main__':
    unittest.main()
