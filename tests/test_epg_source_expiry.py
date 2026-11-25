"""An expiration date on an EPG source (dev/changelog/1724).

A paid XMLTV guide stops working the day its subscription ends, and without a date the first
sign is a run of failed refreshes that do not say why. A url source may carry the day it ends
(`expires_on`, the user's); a week before, EPG_SOURCE_EXPIRING says so, and from the day itself
EPG_SOURCE_EXPIRED does. Nothing stops using the source. These tests pin:

  - FieldTests: the Add / Edit form's date - blank is none, a bad one is refused.
  - StateTests: expiry_state()'s boundaries and the row's line.
  - AlertTests: which of the two alerts stands for each state, that a re-check keeps a row's
    time and read state, and every way they clear - the date moved or taken off, the source
    turned off or deleted, the heads-up giving way to the expired alert.
  - CheckTests: the hourly pass over every dated source and every alert left on a source
    that is gone.
  - RouteTests: both settings dialogs' routes store and serve the date and raise at once; the
    account page and the Accounts page's provider card show the line.

No network: no test fetches a feed.
  python3 -m unittest tests.test_epg_source_expiry
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app import account_links as links  # noqa: E402
from app import epg_sources as es  # noqa: E402
from app.tz_utils import full_date_label  # noqa: E402
from app.accounts import (check_source_expiries, resolve_source_alerts,  # noqa: E402
                          update_source_expiry_alerts)
from app.alerts import SELF_CLEARING_ALERT_TYPES  # noqa: E402
from app.database import Alert, EpgSource, EpgSourceSubscription  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

EXPIRING, EXPIRED = 'EPG_SOURCE_EXPIRING', 'EPG_SOURCE_EXPIRED'
TODAY = date(2026, 12, 1)


class FieldTests(unittest.TestCase):

    def _clean(self, **extra):
        return es.clean_url_source_fields({'url': 'http://guide.test/x.xml', **extra}, 'Alpha')

    def test_blank_or_missing_is_no_date(self):
        for extra in ({}, {'expires_on': ''}, {'expires_on': '  '}, {'expires_on': None}):
            errors, fields = self._clean(**extra)
            self.assertEqual(errors, [], extra)
            self.assertIsNone(fields['expires_on'], extra)

    def test_a_date_is_kept(self):
        errors, fields = self._clean(expires_on='2026-12-31')
        self.assertEqual(errors, [])
        self.assertEqual(fields['expires_on'], date(2026, 12, 31))

    def test_a_past_date_is_accepted(self):
        errors, fields = self._clean(expires_on='2020-01-01')
        self.assertEqual(errors, [])
        self.assertEqual(fields['expires_on'], date(2020, 1, 1))

    def test_a_bad_date_is_refused(self):
        for bad in ('31/12/2026', '2026-13-01', 'soon', '12345'):
            errors, _fields = self._clean(expires_on=bad)
            self.assertEqual(len(errors), 1, bad)
            self.assertIn('expiration date', errors[0])

    def test_a_providers_source_takes_it_too(self):
        errors, fields = es.clean_provider_source_fields(
            {'url': 'http://guide.test/x.xml', 'refresh_interval_hours': 24,
             'expires_on': '2027-03-01'}, 'Strong')
        self.assertEqual(errors, [])
        self.assertEqual(fields['expires_on'], date(2027, 3, 1))


class StateTests(unittest.TestCase):

    def test_boundaries(self):
        cases = [(None, None),
                 (TODAY + timedelta(days=es.EXPIRY_NOTICE_DAYS + 1), es.EXPIRY_LATER),
                 (TODAY + timedelta(days=es.EXPIRY_NOTICE_DAYS), es.EXPIRY_SOON),
                 (TODAY + timedelta(days=1), es.EXPIRY_SOON),
                 (TODAY, es.EXPIRY_PASSED),
                 (TODAY - timedelta(days=30), es.EXPIRY_PASSED)]
        for expires_on, want in cases:
            self.assertEqual(es.expiry_state(expires_on, TODAY), want, expires_on)

    def test_the_rows_line_reads_the_local_day(self):
        tz = ZoneInfo('America/New_York')
        # 03:00 UTC on Dec 2 is still Dec 1 in New York: the date has not come there yet.
        now = datetime(2026, 12, 2, 3, 0)
        self.assertEqual(es.expiry_view(date(2026, 12, 2), now, tz),
                         {'text': 'Expires tomorrow', 'warn': True, 'state': es.EXPIRY_SOON})
        self.assertEqual(es.expiry_view(date(2026, 12, 1), now, tz),
                         {'text': 'Expired Dec 1, 2026', 'warn': True,
                          'state': es.EXPIRY_PASSED})
        self.assertEqual(es.expiry_view(date(2026, 12, 6), now, tz)['text'],
                         'Expires Dec 6, 2026, in 5 days')
        self.assertEqual(es.expiry_view(date(2027, 6, 1), now, tz),
                         {'text': 'Expires Jun 1, 2027', 'warn': False,
                          'state': es.EXPIRY_LATER})
        self.assertIsNone(es.expiry_view(None, now, tz))


class _Fixture(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.app.test_client()
        self.acct = seed.make_account(name='Alpha')
        seed.make_epg_source(self.acct)
        src = EpgSource(kind='url', owner_account_id=self.acct.id, name='Paid',
                        url='http://paid.test/epg.xml')
        db.session.add(src)
        db.session.flush()
        db.session.add(EpgSourceSubscription(source_id=src.id, account_id=self.acct.id,
                                             priority=2))
        db.session.commit()
        self.sid = src.id

    def tearDown(self):
        self.t.cleanup()

    def _set(self, **cols):
        src = db.session.get(EpgSource, self.sid)
        for k, v in cols.items():
            setattr(src, k, v)
        db.session.commit()

    def _open(self):
        db.session.expire_all()
        return {a.alert_type: a for a in Alert.query.filter(
            Alert.alert_type.in_((EXPIRING, EXPIRED)), Alert.dismissed_at.is_(None))}

    def _cleared_by_app(self):
        db.session.expire_all()
        return sorted(a.alert_type for a in Alert.query.filter(
            Alert.alert_type.in_((EXPIRING, EXPIRED)), Alert.dismissed_at.isnot(None),
            Alert.cleared_by_app.is_(True)))


class AlertTests(_Fixture):

    def test_both_types_clear_themselves(self):
        self.assertIn(EXPIRING, SELF_CLEARING_ALERT_TYPES)
        self.assertIn(EXPIRED, SELF_CLEARING_ALERT_TYPES)

    def test_no_date_no_alert(self):
        update_source_expiry_alerts(self.sid, TODAY)
        self.assertEqual(self._open(), {})

    def test_far_off_says_nothing(self):
        self._set(expires_on=TODAY + timedelta(days=30))
        update_source_expiry_alerts(self.sid, TODAY)
        self.assertEqual(self._open(), {})

    def test_a_week_out_raises_the_heads_up_only(self):
        self._set(expires_on=date(2026, 12, 5))
        update_source_expiry_alerts(self.sid, TODAY)
        alerts = self._open()
        self.assertEqual(set(alerts), {EXPIRING})
        a = alerts[EXPIRING]
        self.assertEqual(a.severity, 'INFO')
        self.assertEqual(a.source, f'epg-source:{self.sid}:expiring')
        self.assertIn('"Paid"', a.title)
        self.assertIn('Dec 5, 2026', a.title)
        self.assertIn('Alpha', a.body)

    def test_the_day_itself_raises_expired_and_takes_down_the_heads_up(self):
        self._set(expires_on=date(2026, 12, 5))
        update_source_expiry_alerts(self.sid, TODAY)
        update_source_expiry_alerts(self.sid, date(2026, 12, 5))
        alerts = self._open()
        self.assertEqual(set(alerts), {EXPIRED})
        a = alerts[EXPIRED]
        self.assertEqual(a.severity, 'WARN')
        self.assertIn('expired on Dec 5, 2026', a.body)
        self.assertIn('not had a good refresh', a.body)
        self.assertEqual(self._cleared_by_app(), [EXPIRING])

    def test_the_expired_body_names_the_last_good_refresh(self):
        self._set(expires_on=TODAY, last_success_at=datetime(2026, 11, 30, 12, 0))
        update_source_expiry_alerts(self.sid, TODAY)
        self.assertIn('Its last good refresh was', self._open()[EXPIRED].body)

    def test_a_recheck_keeps_the_rows_time_and_read_state(self):
        self._set(expires_on=TODAY)
        update_source_expiry_alerts(self.sid, TODAY)
        a = self._open()[EXPIRED]
        read_at = datetime(2026, 12, 1, 9, 0)
        created = a.created_at
        a.read_at = read_at
        db.session.commit()
        update_source_expiry_alerts(self.sid, TODAY + timedelta(days=1))
        alerts = self._open()
        self.assertEqual(Alert.query.filter_by(alert_type=EXPIRED).count(), 1)
        self.assertEqual((alerts[EXPIRED].created_at, alerts[EXPIRED].read_at),
                         (created, read_at))

    def test_a_new_date_rewrites_the_standing_row(self):
        self._set(expires_on=date(2026, 12, 5))
        update_source_expiry_alerts(self.sid, TODAY)
        self._set(expires_on=date(2026, 12, 6))
        update_source_expiry_alerts(self.sid, TODAY)
        self.assertIn('Dec 6, 2026', self._open()[EXPIRING].title)
        self.assertEqual(Alert.query.filter_by(alert_type=EXPIRING).count(), 1)

    def test_moving_the_date_on_clears_it(self):
        self._set(expires_on=TODAY)
        update_source_expiry_alerts(self.sid, TODAY)
        self._set(expires_on=TODAY + timedelta(days=365))
        update_source_expiry_alerts(self.sid, TODAY)
        self.assertEqual(self._open(), {})
        self.assertEqual(self._cleared_by_app(), [EXPIRED])

    def test_taking_the_date_off_clears_it(self):
        self._set(expires_on=TODAY)
        update_source_expiry_alerts(self.sid, TODAY)
        self._set(expires_on=None)
        update_source_expiry_alerts(self.sid, TODAY)
        self.assertEqual(self._open(), {})

    def test_turning_the_source_off_clears_it(self):
        self._set(expires_on=TODAY)
        update_source_expiry_alerts(self.sid, TODAY)
        self._set(enabled=False)
        update_source_expiry_alerts(self.sid, TODAY)
        self.assertEqual(self._open(), {})
        self.assertEqual(self._cleared_by_app(), [EXPIRED])

    def test_deleting_the_source_clears_it(self):
        self._set(expires_on=date(2026, 12, 5))
        update_source_expiry_alerts(self.sid, TODAY)
        resolve_source_alerts(self.sid)
        self.assertEqual(self._open(), {})


class CheckTests(_Fixture):

    def test_the_pass_raises_for_a_date_that_has_come(self):
        self._set(expires_on=date.today() - timedelta(days=10))
        self.assertEqual(check_source_expiries(), 1)
        self.assertEqual(set(self._open()), {EXPIRED})

    def test_the_pass_clears_an_alert_whose_source_is_gone(self):
        self._set(expires_on=date.today() - timedelta(days=10))
        check_source_expiries()
        EpgSourceSubscription.query.filter_by(source_id=self.sid).delete()
        db.session.delete(db.session.get(EpgSource, self.sid))
        db.session.commit()
        self.assertEqual(check_source_expiries(), 1)
        self.assertEqual(self._open(), {})

    def test_the_pass_clears_an_alert_whose_date_was_taken_off_elsewhere(self):
        self._set(expires_on=date.today() - timedelta(days=10))
        check_source_expiries()
        self._set(expires_on=None)
        check_source_expiries()
        self.assertEqual(self._open(), {})

    def test_the_job_is_registered_and_runs_the_pass(self):
        from app import scheduler as sched
        from app.routes.jobs import PROBE_JOBS, RECURRING_META
        self.assertIn(sched.EPG_EXPIRY_JOB_ID, RECURRING_META)
        self.assertIn(sched.EPG_EXPIRY_JOB_ID, PROBE_JOBS)
        self.assertIn(sched.EPG_EXPIRY_JOB_ID, sched.RUN_NOW_SYSTEM_JOBS)
        with mock.patch.object(sched, '_app', self.t.app), \
                mock.patch('app.accounts.check_source_expiries') as check:
            sched._epg_source_expiry_job()
        check.assert_called_once_with()


class RouteTests(_Fixture):

    def _url(self):
        return f'/api/accounts/{self.acct.id}/epg-sources/{self.sid}'

    def test_the_settings_dialog_reads_the_date(self):
        self._set(expires_on=date(2027, 1, 15))
        self.assertEqual(self.client.get(self._url()).get_json()['source']['expires_on'],
                         '2027-01-15')
        self._set(expires_on=None)
        self.assertEqual(self.client.get(self._url()).get_json()['source']['expires_on'], '')

    def test_save_stores_the_date_and_raises_at_once(self):
        past = (date.today() - timedelta(days=3)).isoformat()
        r = self.client.post(self._url(), json={'url': 'http://paid.test/epg.xml',
                                                'name': 'Paid', 'expires_on': past})
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        self.assertEqual(db.session.get(EpgSource, self.sid).expires_on.isoformat(), past)
        self.assertEqual(set(self._open()), {EXPIRED})
        r = self.client.post(self._url(), json={'url': 'http://paid.test/epg.xml',
                                                'name': 'Paid', 'expires_on': ''})
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        self.assertIsNone(db.session.get(EpgSource, self.sid).expires_on)
        self.assertEqual(self._open(), {})

    def test_a_bad_date_is_refused_by_the_route(self):
        r = self.client.post(self._url(), json={'url': 'http://paid.test/epg.xml',
                                                'expires_on': 'next week'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('expiration date', r.get_json()['error'])

    def test_add_stores_the_date(self):
        with mock.patch('app.routes.accounts.start_source_refresh',
                        return_value=(False, 'Not now.')):
            r = self.client.post(f'/api/accounts/{self.acct.id}/epg-sources',
                                 json={'url': 'http://new.test/x.xml', 'name': 'New',
                                       'expires_on': '2030-05-01'})
        self.assertEqual(r.status_code, 200, r.get_json())
        db.session.expire_all()
        src = db.session.get(EpgSource, r.get_json()['source_id'])
        self.assertEqual(src.expires_on, date(2030, 5, 1))

    def test_stop_using_and_use_again_ask_at_once(self):
        """Stop using turns off a source nobody else reads, which takes its expiry alert
        down; Use again turns it back on, which puts it back."""
        self._set(expires_on=date.today() - timedelta(days=3))
        update_source_expiry_alerts(self.sid)
        self.assertEqual(set(self._open()), {EXPIRED})
        r = self.client.post(f'{self._url()}/stop-using', json={})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self._open(), {})
        r = self.client.post(f'{self._url()}/use-again', json={})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(set(self._open()), {EXPIRED})

    def test_a_providers_new_source_takes_the_date_and_raises_at_once(self):
        other = seed.make_account(name='Beta')
        db.session.commit()
        provider, _ = links.create_provider('Strong', [self.acct.id, other.id],
                                            case_sensitive=False)
        past = date.today() - timedelta(days=3)
        with mock.patch('app.accounts.start_source_refresh', return_value=(False, 'Not now.')):
            r = self.client.post(f'/api/providers/{provider.id}/epg-sources',
                                 json={'url': 'http://shared.test/x.xml', 'name': 'Shared',
                                       'refresh_interval_hours': 24,
                                       'expires_on': past.isoformat()})
        self.assertEqual(r.status_code, 200, r.get_json())
        new_id = r.get_json()['source_id']
        db.session.expire_all()
        self.assertEqual(db.session.get(EpgSource, new_id).expires_on, past)
        self.assertEqual(self._open()[EXPIRED].source, f'epg-source:{new_id}:expired')

    def test_the_account_page_shows_the_line(self):
        self._set(expires_on=date.today() - timedelta(days=3))
        html = self.client.get(f'/accounts/{self.acct.id}').get_data(as_text=True)
        self.assertIn(f'Expired {full_date_label(date.today() - timedelta(days=3))}', html)

    def test_a_providers_source_takes_the_date_and_shows_it_on_its_card(self):
        # The Accounts page draws its Providers section from two accounts up.
        other = seed.make_account(name='Beta')
        db.session.commit()
        provider, _ = links.create_provider('Strong', [self.acct.id, other.id],
                                            case_sensitive=False)
        es.move_source_to_provider(self.sid, provider.id, 24, False)
        url = f'/api/providers/{provider.id}/epg-sources/{self.sid}'
        far = date.today() + timedelta(days=200)
        r = self.client.post(url, json={'url': 'http://paid.test/epg.xml', 'name': 'Paid',
                                        'refresh_interval_hours': 24,
                                        'expires_on': far.isoformat()})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.client.get(url).get_json()['source']['expires_on'],
                         far.isoformat())
        html = self.client.get('/accounts').get_data(as_text=True)
        self.assertIn(f'Expires {full_date_label(far)}', html)


if __name__ == '__main__':
    unittest.main(verbosity=2)
