"""app/routes/alerts.py::_resolve_alert_link - the single place an alert becomes a URL.

Guards dev/changelog/522 (generalizing the GROUP_FORMAT_MISMATCH-only deep link into a
standing rule: every alert that names an account/group/job/recording links to it, on
every surface that shows the alert). Each case here is one of the `source`/`recording_id`
shapes a real create_alert() call site in the app actually produces.

    python3 -m unittest tests.test_alert_links
"""
import unittest

from app.database import Alert
from app.routes.alerts import _resolve_alert_link
from tests.support.app import make_test_app


class ResolveAlertLinkTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.test_request_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _alert(self, **kw):
        kw.setdefault('alert_type', 'TEST')
        kw.setdefault('severity', 'INFO')
        kw.setdefault('title', 'x')
        return Alert(**kw)

    def test_recording_id_wins_regardless_of_source(self):
        a = self._alert(recording_id=7, source='account:5:epg-fetch')
        link, label = _resolve_alert_link(a)
        self.assertIn('/recordings/7', link)
        self.assertEqual(label, 'Recording')

    def test_sync_channels_new_links_to_filtered_channel_browser(self):
        a = self._alert(alert_type='SYNC_CHANNELS_NEW', source='account:5:channels-new')
        link, label = _resolve_alert_link(a)
        self.assertIn('/channels', link)
        self.assertIn('f.acct=5', link)
        self.assertEqual(label, 'Channels')

    def test_sync_channels_missing_links_to_filtered_channel_browser(self):
        a = self._alert(alert_type='SYNC_CHANNELS_MISSING', source='account:9:channels-missing')
        link, _ = _resolve_alert_link(a)
        self.assertIn('f.acct=9', link)

    def test_group_source_links_to_group_detail(self):
        a = self._alert(alert_type='GROUP_FORMAT_MISMATCH', source='group:3:ch:42')
        link, label = _resolve_alert_link(a)
        self.assertIn('/channel-groups/3', link)
        self.assertEqual(label, 'Group')

    def test_generic_account_source_links_to_account_detail(self):
        a = self._alert(alert_type='SYNC_EPG_COLLAPSE_REFUSED', source='account:12:epg-collapse')
        link, label = _resolve_alert_link(a)
        self.assertIn('/accounts/12', link)
        self.assertEqual(label, 'Account')

    def test_search_index_source_links_to_maintenance(self):
        a = self._alert(alert_type='SEARCH_INDEX_REBUILD_FAILED', source='search-index:channels')
        link, label = _resolve_alert_link(a)
        self.assertIn('/maintenance', link)
        self.assertEqual(label, 'Maintenance')

    def test_od_job_source_links_to_on_demand_job_detail(self):
        a = self._alert(alert_type='JOB_SKIPPED', source='od_job_14')
        link, label = _resolve_alert_link(a)
        self.assertIn('/channel-tests/on-demand/14', link)
        self.assertEqual(label, 'Health check')

    def test_account_sync_source_links_to_account_detail(self):
        a = self._alert(alert_type='JOB_SKIPPED', source='account_sync_8')
        link, label = _resolve_alert_link(a)
        self.assertIn('/accounts/8', link)
        self.assertEqual(label, 'Account')

    def test_account_sync_retry_source_links_to_account_detail(self):
        a = self._alert(alert_type='JOB_SKIPPED', source='account_sync_retry_8')
        link, _ = _resolve_alert_link(a)
        self.assertIn('/accounts/8', link)

    def test_sync_failed_source_links_to_account_detail(self):
        """dev/changelog/930: SYNC_FAILED uses the account:<id>:... shape precisely so the
        alert lands on the account whose sync failed, with no new parsing rule."""
        a = self._alert(alert_type='SYNC_FAILED', source='account:4:sync-failed')
        link, label = _resolve_alert_link(a)
        self.assertIn('/accounts/4', link)
        self.assertEqual(label, 'Account')

    def test_broken_guide_row_source_links_to_the_group(self):
        """dev/changelog/933: GROUP_GUIDE_NO_RECORDING_MEMBER carried a bare
        'channel_groups' source, which resolves to no link at all. Keying it on the group
        so each group's alert can clear independently also lands it on the group page,
        which is where the switch that fixes it lives."""
        a = self._alert(alert_type='GROUP_GUIDE_NO_RECORDING_MEMBER',
                        source='group:6:guide-no-recording-member')
        link, label = _resolve_alert_link(a)
        self.assertIn('/channel-groups/6', link)
        self.assertEqual(label, 'Group')

    def test_unaddressable_source_has_no_link(self):
        for source in ('postprocessor', 'check_window', 'accounts.sync', 'auth', 'startup'):
            a = self._alert(source=source)
            self.assertIsNone(_resolve_alert_link(a), source)

    def test_no_source_has_no_link(self):
        self.assertIsNone(_resolve_alert_link(self._alert()))

    def test_malformed_account_source_is_not_mistaken_for_valid(self):
        """Defensive parsing: a non-numeric or wrong-shape id must not blow up or
        silently resolve to a wrong/garbage URL."""
        for source in ('account:notanumber:epg-fetch', 'account:', 'group:notanumber:ch:1'):
            self.assertIsNone(_resolve_alert_link(self._alert(source=source)), source)
