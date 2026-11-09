"""Tier 2 - a source whose refreshes keep failing until the listings it kept have all passed
(dev/docs/BUGS.md 2026-11-05 @ 07:25 PM, dev/changelog/1453).

A failed refresh keeps the source's previous listings. Before the fix the source's
directory went on saying it covered its channels after those listings had expired, so the
channels showed an empty guide with no alert while another source's listings sat unused
in the alternates table.

  - RanOutTests: the failed refresh that finds the listings passed moves each channel to a
    source still holding listings for it, leaves the rest with no source, says which, and
    never touches a healthy source, an unexpired row or the user's override.
  - RanOutPageTests: both pages say the listings ran out.

No network: `requests.get` is patched to fail.
"""
import os
import sys
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.accounts import import_source, refresh_source  # noqa: E402
from app.database import (Alert, Channel, ChannelEvent, EPGEntry, EpgAlternateEntry,  # noqa: E402
                          EpgSource, EpgSourceChannel, CHANNEL_EPG_SOURCE_CHANGED,
                          EPG_STATUS_FAILED)
from app.epg_sources import retire_expired_listings, set_source_override  # noqa: E402
from tests.test_epg_source_priority import OTHER, _TwoSources  # noqa: E402
from tests.test_epg_sources import CFG, REAL, _schedule, _xmltv  # noqa: E402


class _RanOut(_TwoSources):
    """src is first and wins `both` and `src_only`; ext wins `banner` and `ext_only`, and
    its listings for `both` are in the alternates."""

    def _days_pass(self, source, hours=30):
        """What the clock does to a source nobody could refresh: everything it holds, and
        its directory's horizons, are `hours` further in the past."""
        shift = timedelta(hours=hours)
        for model in (EPGEntry, EpgAlternateEntry):
            for e in model.query.filter_by(source_id=source.id):
                e.start_time -= shift
                e.stop_time -= shift
        for d in EpgSourceChannel.query.filter_by(source_id=source.id):
            if d.horizon_until is not None:
                d.horizon_until -= shift
        db.session.commit()

    def _failed_refresh(self, source, cfg=CFG):
        with mock.patch('app.accounts.requests.get',
                        side_effect=ConnectionError('404 Not Found')):
            out = refresh_source(db.session.get(EpgSource, source.id), 5, 3, cfg,
                                 track_progress=False)
        db.session.expire_all()
        return out

    def _alert(self, source):
        return Alert.query.filter_by(alert_type='EPG_SOURCE_COVERAGE_LOST',
                                     source=f'epg-source:{source.id}:coverage').first()


class RanOutTests(_RanOut):

    def test_a_failed_refresh_with_listings_still_ahead_changes_nothing(self):
        _n, reason = self._failed_refresh(self.src)
        self.assertIn('fetch failed', reason)
        self.assertEqual(db.session.get(EpgSource, self.src.id).last_status, EPG_STATUS_FAILED)
        self.assertEqual(self._winner(self.both), self.src.id)
        self.assertEqual(self._winner(self.src_only), self.src.id)
        self.assertIsNone(self._alert(self.src))

    def test_a_channel_moves_to_the_source_that_still_holds_listings_for_it(self):
        self._days_pass(self.src)
        with mock.patch('app.accounts.import_source') as imported:
            self._failed_refresh(self.src)
        imported.assert_not_called()
        self.assertEqual(self._winner(self.both), self.ext.id)
        self.assertEqual({t for _s, t in self._active(self.both)}, set(OTHER),
                         'the alternates were promoted, nothing was fetched')
        ev = ChannelEvent.query.filter_by(channel_id=self.both.id,
                                          event_type=CHANNEL_EPG_SOURCE_CHANGED).one()
        self.assertIn(self.ext.name, ev.detail)

    def test_a_channel_no_other_source_lists_is_left_with_none_and_named(self):
        self._days_pass(self.src)
        self._failed_refresh(self.src, {'sync': {'epg_coverage_lost_alert_percent': 10}})
        self.assertIsNone(self._winner(self.src_only))
        alert = self._alert(self.src)
        self.assertIsNotNone(alert, 'channels going dark must not be silent')
        self.assertIn('Src Only', alert.body)
        self.assertIn('1 now take their guide from another source', alert.body)
        self.assertIn('1 have no guide from any source now', alert.body)
        self.assertNotIn('Both', alert.body.split('no guide from any source now')[1])
        self.assertIn('In all 1 of the 2 channel(s) it was the guide for have none now.',
                      alert.body)

    def test_channels_that_run_out_a_few_at_a_time_are_judged_on_the_total(self):
        """A file whose channels end at different times loses them one failed refresh at a
        time. Each loss alone is under the threshold; the alert reads the running total."""
        two = self._channel('Src Two', 'src2.test')
        import_source(db.session.get(EpgSource, self.src.id),
                      _xmltv(_schedule('both.test', REAL) + _schedule('src.test', REAL)
                             + _schedule('src2.test', REAL)), epg_days=3, cfg=CFG)
        cfg = {'sync': {'epg_coverage_lost_alert_percent': 60}}

        def run_out(xml_id):
            d = EpgSourceChannel.query.filter_by(source_id=self.src.id, xml_id=xml_id).one()
            d.horizon_until = datetime.utcnow() - timedelta(hours=2)
            db.session.commit()
            self._failed_refresh(self.src, cfg)

        run_out('src.test')
        self.assertIsNone(self._winner(self.src_only))
        self.assertIsNone(self._alert(self.src), '1 of 3 is under 60%')
        run_out('src2.test')
        self.assertIsNone(self._winner(two))
        alert = self._alert(self.src)
        self.assertIsNotNone(alert, '2 of 3 is over it, though this refresh lost only one')
        self.assertIn('Src Two', alert.body)
        self.assertIn('In all 2 of the 3 channel(s)', alert.body)

    def test_a_sync_log_carries_the_sentence(self):
        self._days_pass(self.src)
        notes = []
        with mock.patch('app.accounts.requests.get',
                        side_effect=ConnectionError('404 Not Found')):
            refresh_source(db.session.get(EpgSource, self.src.id), 5, 3, CFG,
                           track_progress=False, notes=notes)
        self.assertEqual(len(notes), 1)
        self.assertIn('have all passed for 2 channel(s)', notes[0])

    def test_a_second_failed_refresh_does_not_alert_again(self):
        self._days_pass(self.src)
        self._failed_refresh(self.src)
        Alert.query.delete()
        db.session.commit()
        self._failed_refresh(self.src)
        self.assertIsNone(self._alert(self.src))

    def test_a_loss_under_the_threshold_is_logged_and_does_not_alert(self):
        self._days_pass(self.src)
        with self.assertLogs('app.accounts', level='INFO') as logs:
            self._failed_refresh(self.src, {'sync': {'epg_coverage_lost_alert_percent': 90}})
        self.assertIsNone(self._alert(self.src))
        self.assertTrue(any('have all passed for 2 channel(s)' in line for line in logs.output),
                        logs.output)

    def test_only_the_rows_whose_horizon_passed_are_retired(self):
        d = EpgSourceChannel.query.filter_by(source_id=self.src.id, xml_id='both.test').one()
        d.horizon_until = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()
        self._failed_refresh(self.src)
        self.assertEqual(self._winner(self.both), self.ext.id)
        self.assertEqual(self._winner(self.src_only), self.src.id)

    def test_the_other_source_and_its_channels_are_untouched(self):
        self._days_pass(self.src)
        self._failed_refresh(self.src)
        self.assertEqual(self._winner(self.banner), self.ext.id)
        self.assertEqual(self._winner(self.ext_only), self.ext.id)
        self.assertEqual(EpgSourceChannel.query.filter(
            EpgSourceChannel.source_id == self.ext.id,
            EpgSourceChannel.entry_count > 0).count(), 3)

    def test_the_users_override_is_kept_and_wins_again_when_the_source_recovers(self):
        set_source_override(self.banner, self.src, case_sensitive=False)
        self.assertEqual(self._winner(self.banner), self.src.id)
        self._days_pass(self.src)
        self._failed_refresh(self.src)
        ch = db.session.get(Channel, self.banner.id)
        self.assertEqual(ch.epg_source_id, self.ext.id)
        self.assertEqual(ch.epg_source_override_id, self.src.id, 'the override is the user\'s')
        import_source(db.session.get(EpgSource, self.src.id),
                      _xmltv(_schedule('both.test', REAL) + _schedule('banner.test', REAL)
                             + _schedule('src.test', REAL)), epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(self.banner), self.src.id)
        self.assertEqual(self._winner(self.both), self.src.id)
        self.assertEqual(self._winner(self.src_only), self.src.id)

    def test_a_later_import_of_the_other_source_does_not_hand_the_channels_back(self):
        self._days_pass(self.src)
        self._failed_refresh(self.src)
        import_source(db.session.get(EpgSource, self.ext.id),
                      _xmltv(_schedule('both.test', OTHER) + _schedule('ext.test', OTHER)
                             + _schedule('banner.test', OTHER)), epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(self.both), self.ext.id)

    def test_a_run_that_died_before_the_channels_moved_is_finished_by_the_next(self):
        self._days_pass(self.src)
        with mock.patch('app.epg_sources.reresolve_channels', side_effect=RuntimeError('died')):
            with self.assertRaises(RuntimeError):
                retire_expired_listings(self.src.id, False)
        db.session.rollback()
        self.assertEqual(self._winner(self.both), self.src.id, 'the first run moved nothing')
        ran_out = retire_expired_listings(self.src.id, False)
        self.assertEqual(self._winner(self.both), self.ext.id)
        self.assertIn(self.both.id, ran_out.channel_ids)

    def test_a_healthy_source_whose_file_ends_early_is_left_alone(self):
        """Retiring runs only where a refresh kept the previous listings: a good import
        rewrites the directory and nothing here reads the clock."""
        self._days_pass(self.src)
        import_source(db.session.get(EpgSource, self.ext.id),
                      _xmltv(_schedule('both.test', OTHER) + _schedule('ext.test', OTHER)
                             + _schedule('banner.test', OTHER)), epg_days=3, cfg=CFG)
        self.assertEqual(self._winner(self.both), self.src.id)


class RanOutPageTests(_RanOut):

    def test_the_sources_card_says_the_kept_listings_ran_out(self):
        self._days_pass(self.src)
        self._failed_refresh(self.src)
        acct_id = self.acct.id
        db.session.remove()
        html = self.client.get(f'/accounts/{acct_id}').get_data(as_text=True)
        self.assertIn('Every listing it kept has passed. It has none until a refresh works.',
                      html)

    def test_the_sources_card_says_some_when_only_some_ran_out(self):
        d = EpgSourceChannel.query.filter_by(source_id=self.src.id, xml_id='both.test').one()
        d.horizon_until = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()
        self._failed_refresh(self.src)
        acct_id = self.acct.id
        db.session.remove()
        html = self.client.get(f'/accounts/{acct_id}').get_data(as_text=True)
        self.assertIn('The listings it kept have passed for some channels.', html)
        self.assertNotIn('Every listing it kept has passed', html)

    def test_a_source_that_is_fine_says_nothing(self):
        acct_id = self.acct.id
        db.session.remove()
        html = self.client.get(f'/accounts/{acct_id}').get_data(as_text=True)
        self.assertNotIn('it kept ha', html)

    def test_the_channel_page_says_when_the_source_ran_out(self):
        self._days_pass(self.src)
        self._failed_refresh(self.src)
        ch_id = self.both.id
        db.session.remove()
        html = self.client.get(f'/channels/{ch_id}').get_data(as_text=True)
        self.assertIn('this source has not refreshed since', html)
