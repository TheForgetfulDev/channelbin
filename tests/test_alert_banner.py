"""The nav's alert payload and the banner markup it fills (app/routes/alerts.py,
templates/base.html). Built in dev/changelog/924 from the rules in dev/changelog/923.

The banner used to show the NEWEST unread alert, so on 2026-09-11 an INFO "stream URLs were
built" note sat above four unread errors, and the nav badge was one grey count of all 207
unread alerts. The payload now picks the most severe unread alert (never an INFO one) and
splits the count into red (ERROR + CRIT) and yellow (WARN). What the script does with it is
tests/test_nav_alerts_js.py.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
    python3 -m unittest tests.test_alert_banner
"""
import re
import unittest
from datetime import datetime, timedelta

from app import db
from app.database import Alert
from tests.support.app import make_test_app

T0 = datetime(2026, 9, 11, 5, 0)


class _AppCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _alert(self, severity, title, minutes=0, **kw):
        kw.setdefault('alert_type', 'TEST')
        db.session.add(Alert(severity=severity, title=title, body=kw.pop('body', ''),
                             created_at=T0 + timedelta(minutes=minutes), **kw))

    def _summary(self):
        db.session.commit()
        return self.client.get('/api/nav-status').get_json()['alerts']


class BannerPickTests(_AppCase):
    def test_a_newer_info_does_not_displace_an_older_error(self):
        self._alert('ERROR', 'Move failed: stale file handle', minutes=0)
        self._alert('INFO', '12 stream URL(s) were built', minutes=30)
        self.assertEqual(self._summary()['banner']['title'], 'Move failed: stale file handle')

    def test_a_worse_severity_beats_a_newer_one(self):
        self._alert('CRIT', 'Crit', minutes=0)
        self._alert('ERROR', 'Error', minutes=10)
        self._alert('WARN', 'Warn', minutes=20)
        self.assertEqual(self._summary()['banner']['title'], 'Crit')

    def test_error_beats_a_newer_warning(self):
        self._alert('ERROR', 'Error', minutes=0)
        self._alert('WARN', 'Warn', minutes=20)
        self.assertEqual(self._summary()['banner']['title'], 'Error')

    def test_the_newest_wins_within_one_severity(self):
        self._alert('WARN', 'Older warn', minutes=0)
        self._alert('WARN', 'Newer warn', minutes=5)
        self.assertEqual(self._summary()['banner']['title'], 'Newer warn')

    def test_info_alone_never_reaches_the_banner_or_the_counts(self):
        for i in range(3):
            self._alert('INFO', f'Info {i}', minutes=i)
        s = self._summary()
        self.assertIsNone(s['banner'])
        self.assertEqual((s['count'], s['error_count'], s['warn_count'], s['more']), (3, 0, 0, 0))

    def test_read_and_dismissed_alerts_are_not_candidates(self):
        self._alert('CRIT', 'Read crit', minutes=0, read_at=T0)
        self._alert('ERROR', 'Dismissed error', minutes=1, dismissed_at=T0)
        self._alert('WARN', 'Open warn', minutes=2)
        self.assertEqual(self._summary()['banner']['title'], 'Open warn')

    def test_the_banner_carries_its_destination_as_a_noun(self):
        """The details view renders `link_label →` (DESIGN.md §4's forward jump-off)."""
        self._alert('WARN', 'EPG fetch failed', source='account:5:epg-fetch', body='why')
        banner = self._summary()['banner']
        self.assertIn('/accounts/5', banner['link'])
        self.assertEqual(banner['link_label'], 'Account')
        self.assertEqual(banner['body'], 'why')
        self.assertTrue(banner['created_label'])


class BannerTimestampTests(_AppCase):
    """The banner says WHEN, so a row that recovered weeks ago stops reading as urgent
    (dev/changelog/939). A 9-day-old sync failure sat on every page looking exactly like a
    live one, because the only timestamp was on the Alerts page."""

    def test_the_banner_carries_a_compact_stamp_distinct_from_the_full_label(self):
        self._alert('ERROR', 'Sync failed')
        banner = self._summary()['banner']
        # Not the same spelling as the details view's: the banner is one line beside a
        # truncating title, so it takes m/d/yy, and the long label keeps its own key.
        self.assertRegex(banner['created_short'], r'^\d{1,2}/\d{1,2}/\d{2} ')
        self.assertNotEqual(banner['created_short'], banner['created_label'])

    def test_the_age_is_rendered_server_side_and_reads_as_an_age(self):
        """Computed in Python, not from `created_at` in the browser: that key is naive UTC
        with no offset, so JS Date() would read it as local time and be off by the
        viewer's offset."""
        self._alert('ERROR', 'Sync failed')
        db.session.query(Alert).update(
            {'created_at': datetime.utcnow() - timedelta(hours=2, minutes=30, seconds=1)})
        self.assertEqual(self._summary()['banner']['created_age'], '2h 30m ago')


class CountSplitTests(_AppCase):
    def _seed(self):
        self._alert('CRIT', 'c')
        self._alert('ERROR', 'e')
        self._alert('WARN', 'w1')
        self._alert('WARN', 'w2')
        self._alert('INFO', 'i')
        self._alert('ERROR', 'read error', read_at=T0)
        self._alert('WARN', 'dismissed warn', dismissed_at=T0)
        db.session.commit()

    def test_nav_status_splits_unread_by_severity(self):
        self._seed()
        s = self._summary()
        self.assertEqual(s['count'], 5)
        self.assertEqual(s['error_count'], 2)
        self.assertEqual(s['warn_count'], 2)
        # Every other unread error and warning behind the one on the banner.
        self.assertEqual(s['more'], 3)

    def test_unread_count_endpoint_carries_the_same_split(self):
        """The Alerts page refreshes the nav counts from this endpoint after each click."""
        self._seed()
        d = self.client.get('/api/alerts/unread_count').get_json()
        self.assertEqual(d, {'count': 5, 'error_count': 2, 'warn_count': 2})


class BannerMarkupTests(_AppCase):
    def setUp(self):
        super().setUp()
        self.html = self.client.get('/').get_data(as_text=True)
        m = re.search(r'<div id="alert-banner"[\s\S]*?\n      </div>', self.html)
        self.assertIsNotNone(m, 'no #alert-banner rendered')
        self.banner = m.group(0)

    def test_banner_starts_hidden(self):
        self.assertIn('<div id="alert-banner" class="alert-banner" style="display:none">', self.banner)

    def test_title_is_a_link_role_button_alone_in_the_truncating_span(self):
        """A <button> inside the ellipsis span is elided whole by Chromium, and a sibling
        after the title is laid out past the clip (both measured on mockup 39)."""
        self.assertIn(
            '<span><span class="alert-severity" id="alert-banner-sev"></span>'
            '<a href="#" role="button" class="alert-banner-title" id="alert-banner-title"></a></span>',
            self.banner,
            'the truncating span must hold the severity badge and the title link, and nothing else')

    def test_the_timestamp_sits_outside_the_truncating_span(self):
        """Inside it the ellipsis would clip the stamp's paint while still laying it out,
        so it would never be seen - the same rule the title comment in style.css states."""
        self.assertIn('<span class="alert-banner-time" id="alert-banner-time"></span>',
                      self.banner)
        self.assertLess(self.banner.index('id="alert-banner-title"></a></span>'),
                        self.banner.index('id="alert-banner-time"'))

    def test_banner_has_more_link_and_one_click_mark_read_and_nothing_else(self):
        self.assertIn('id="alert-banner-more"', self.banner)
        self.assertIn('href="/alerts"', self.banner)
        self.assertRegex(self.banner, r'<button type="button" class="alert-banner-dismiss" '
                                      r'id="alert-banner-read" aria-label="Mark read"')
        for gone in ('Show details', 'View all', 'onclick='):
            self.assertNotIn(gone, self.banner)
        self.assertEqual(self.banner.count('<button'), 1)
        self.assertEqual(self.banner.count('<a '), 2)

    def test_script_loads_before_the_poller_that_calls_it(self):
        self.assertLess(self.html.index('js/nav-alerts.js'), self.html.index('function fetchNavStatus'))
        self.assertNotIn('dismissAlertBanner', self.html)
        self.assertNotIn('showAlertBannerDetails', self.html)

    def test_alerts_page_renders_no_banner(self):
        html = self.client.get('/alerts').get_data(as_text=True)
        self.assertNotIn('id="alert-banner"', html)


if __name__ == '__main__':
    unittest.main()
