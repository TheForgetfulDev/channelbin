"""The account page's Activity card is the account's whole story (app/account_activity.py,
dev/changelog/1475): its syncs, the alerts about it - its own standing alerts, a refused login
of its, the provider it is on - and its blocks, over the last 30 days, with the history-filter
chips and Show more paging. Until dev/changelog/1475 the card listed only syncs, which the
Sync history table beside it already listed run by run.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_account_activity
"""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import account_activity as aa, account_blocks, account_links as links, db  # noqa: E402
from app.database import Alert, AccountSyncLog  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

CHIP = re.compile(r'<a class="chip([^"]*)" data-hist="(\w+)" href="([^"]+)">(.*?)</a>', re.S)


def _alert(source, title, created_at=None, severity='WARN', **kw):
    row = Alert(alert_type='ACCOUNT_HOST_ROLLED', severity=severity, title=title, body='why',
                source=source, created_at=created_at or datetime.utcnow(), **kw)
    db.session.add(row)
    db.session.flush()
    return row


def _sync(account, when, status='SUCCESS'):
    db.session.add(AccountSyncLog(account_id=account.id, started_at=when, completed_at=when,
                                  status=status, channels_synced=3, epg_entries_synced=4))


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.acc = seed.make_account(name='Storied')
        self.other = seed.make_account(name='Other')
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def card(self, qs=''):
        resp = self.t.client.get(f'/accounts/{self.acc.id}{qs}')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        return re.split(r'<div class="card[" ]', html.split('id="acct-activity-body"', 1)[1], 1)[0]

    def chips(self, part):
        return [(k, ' '.join(re.sub(r'<[^>]+>', ' ', t).split()), 'active' in cls)
                for cls, k, _h, t in CHIP.findall(part)]

    def lines(self, part):
        return [re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', m)).strip()
                for m in re.findall(r'<div class="acct-act-line">(.*?)</div>', part, re.S)]


class SourcesTests(_Base):
    def test_the_accounts_own_alerts_a_refused_login_and_its_provider_count(self):
        login = links.add_login(self.acc.id, 'main', 'u', 'p', 1)
        pid = links.create_provider('skyline', [self.acc.id, self.other.id])[0].id
        db.session.commit()
        prefixes = aa.alert_sources(db.session.get(type(self.acc), self.acc.id))
        self.assertEqual(sorted(prefixes),
                         sorted([f'account:{self.acc.id}:', f'login:{login.id}:', f'provider:{pid}:']))

    def test_another_accounts_alert_and_a_longer_id_are_not_this_accounts(self):
        _alert(f'account:{self.acc.id}:sync-failed', 'Mine')
        _alert(f'account:{self.other.id}:sync-failed', 'Not mine')
        _alert(f'account:{self.acc.id}0:sync-failed', 'A longer id, not mine either')
        db.session.commit()
        titles = [e['obj'].title for e in aa.entries(self.acc) if e['kind'] == aa.KIND_ALERT]
        self.assertEqual(titles, ['Mine'])


class CardTests(_Base):
    def test_syncs_alerts_and_blocks_are_one_list_newest_first_with_chips(self):
        now = datetime.utcnow()
        _sync(self.acc, now - timedelta(hours=3))
        _alert(f'account:{self.acc.id}:sync-failed', 'Sync of Storied failed',
               created_at=now - timedelta(hours=2))
        account_blocks.add_account_block(self.acc.id, now + timedelta(hours=2),
                                         start=now - timedelta(hours=1))
        db.session.commit()
        part = self.card()
        self.assertEqual(self.chips(part), [('all', 'All 3', True), ('syncs', 'Syncs 1', False),
                                            ('alerts', 'Alerts 1', False), ('blocks', 'Blocks 1', False)])
        lines = self.lines(part)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith('Blocked from'), lines[0])
        self.assertIn('Still in force.', lines[0])
        self.assertEqual(lines[1], 'Sync of Storied failed')
        self.assertTrue(lines[2].startswith('Sync finished: 3 channels'), lines[2])

    def test_a_chip_narrows_and_an_unknown_chip_is_all(self):
        now = datetime.utcnow()
        _sync(self.acc, now - timedelta(hours=3))
        _alert(f'account:{self.acc.id}:overdue', 'Overdue', created_at=now - timedelta(hours=1))
        db.session.commit()
        part = self.card('?hist=alerts')
        self.assertEqual(self.lines(part), ['Overdue'])
        self.assertIn(('alerts', 'Alerts 1', True), self.chips(part))
        self.assertEqual(len(self.lines(self.card('?hist=nonsense'))), 2)

    def test_a_cleared_alert_says_so_and_an_old_one_is_outside_the_window(self):
        now = datetime.utcnow()
        _alert(f'account:{self.acc.id}:host-rolled', 'Rolled', created_at=now - timedelta(days=2),
               dismissed_at=now - timedelta(days=1), cleared_by_app=True)
        _alert(f'account:{self.acc.id}:host-rolled', 'Ancient',
               created_at=now - timedelta(days=aa.ACTIVITY_DAYS + 5))
        db.session.commit()
        part = self.card()
        self.assertEqual(self.lines(part), ['Rolled'])
        self.assertIn('cleared itself', part)

    def test_a_block_tied_to_a_recording_names_it(self):
        rec = seed.make_recording(status='SCHEDULED', name='The Game')
        db.session.commit()
        account_blocks.set_recording_blocks(rec.id, {self.acc.id: None})
        line = self.lines(self.card())[0]
        self.assertTrue(line.startswith('Blocked for the recording The Game'), line)

    def test_show_more_pages_the_list(self):
        now = datetime.utcnow()
        for i in range(aa.ACTIVITY_PAGE + 3):
            _sync(self.acc, now - timedelta(minutes=i + 1))
        db.session.commit()
        part = self.card()
        self.assertEqual(len(self.lines(part)), aa.ACTIVITY_PAGE)
        self.assertRegex(part, rf'data-hist-more="1" href="/accounts/{self.acc.id}\?shown={aa.ACTIVITY_PAGE * 2}">\s*Show 3 more\s*</a>')
        self.assertEqual(len(self.lines(self.card(f'?shown={aa.ACTIVITY_PAGE * 2}'))), aa.ACTIVITY_PAGE + 3)

    def test_an_account_with_nothing_keeps_its_empty_state(self):
        self.assertIn('Nothing has happened yet', self.card())

    def test_history_older_than_the_window_says_so_rather_than_nothing_ever(self):
        _sync(self.acc, datetime.utcnow() - timedelta(days=aa.ACTIVITY_DAYS + 10))
        db.session.commit()
        part = self.card()
        self.assertIn(f'Nothing in the last {aa.ACTIVITY_DAYS} days', part)
        self.assertNotIn('Nothing has happened yet', part)


if __name__ == '__main__':
    unittest.main()
