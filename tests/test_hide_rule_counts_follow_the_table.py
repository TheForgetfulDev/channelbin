"""The hide rules' match counts, and an account's hidden count, follow the channel table.

dev/docs/BUGS.md 2026-10-26 08:57:25 AM and 08:57:26 AM: on a new install Hide Rules read
"Hidden now 3" beside five default rules each at "Matches 0", and the Accounts list read
"0 channels, 3 hidden" while the first sync ran. The counts are a display cache (`ChannelHideRule.match_count`, written by
`channel_hiding.refresh_rule_stats()`), and the sync's hide pass skipped the recount, so the
last count was the one taken at startup over an empty table. Every door that adds or removes
channels now recounts: the sync, the missing-channel delete and the account delete.

The sync cases run ChannelBin's real sync over HTTP against the simulator's small world,
whose lineups carry the divider rows the default rules exist to hide. dev/changelog/1291.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_hide_rule_counts_follow_the_table
"""
import datetime as dt
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import accounts, channel_hiding, db  # noqa: E402
from app.accounts import _do_sync  # noqa: E402
from app.database import (Account, Channel, ChannelHideRule,  # noqa: E402
                          HIDE_TARGET_NAME_GLOB, XtreamAccount)
from sim import load_world, serve_for_test  # noqa: E402
from tests.support import make_test_app, seed  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMALL = os.path.join(ROOT, 'sim', 'worlds', 'small.yaml')


def _expected_matches(rule):
    return channel_hiding.preview(rule.target, rule.pattern, rule.account_id)['matched']


class FirstSyncCountsTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
        self.sim = serve_for_test(self, load_world(SMALL), clock=now)
        view = self.sim.account('harbor-tv')
        account = XtreamAccount(name='Harbor TV', base_url=view.base_url,
                                username=view.username, password=view.password,
                                status='UNSYNCED')
        db.session.add(account)
        db.session.commit()
        self.account_id = account.id

    def _sync(self):
        _do_sync(self.account_id, threading.Event())
        db.session.expire_all()

    def test_every_rule_counts_what_it_matches_after_the_first_sync(self):
        rules = ChannelHideRule.query.all()
        self.assertTrue(rules, 'a new install starts with the divider-row rules')
        self._sync()
        hidden = db.session.get(Account, self.account_id).hidden_channel_count
        self.assertGreater(hidden, 0, 'the world lists divider rows for the rules to hide')
        for rule in ChannelHideRule.query.all():
            self.assertEqual(rule.match_count, _expected_matches(rule), rule.pattern)
        self.assertGreaterEqual(sum(r.match_count for r in ChannelHideRule.query.all()), hidden)

    def test_the_channel_count_is_written_before_the_hidden_count(self):
        seen = []
        real = accounts._apply_hide_rules_after_upsert

        def watch(account_id, name):
            real(account_id, name)
            db.session.expire_all()
            acct = db.session.get(Account, account_id)
            seen.append((acct.channel_count, acct.hidden_channel_count,
                         Channel.query.filter_by(account_id=account_id).count()))

        with mock.patch.object(accounts, '_apply_hide_rules_after_upsert', watch):
            self._sync()
        self.assertEqual(len(seen), 1)
        channel_count, hidden, in_table = seen[0]
        self.assertGreater(hidden, 0)
        self.assertEqual(channel_count, in_table,
                         'while the guide import runs, the account has its channel count')


class DeletesRecountTests(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        ChannelHideRule.query.delete()
        self.keep = seed.make_account(name='Keep')
        self.gone = seed.make_account(name='Gone')
        for acct in (self.keep, self.gone):
            seed.make_channel(acct, name=f'## {acct.name} ##')
        self.rule = ChannelHideRule(target=HIDE_TARGET_NAME_GLOB, pattern='##*##')
        db.session.add(self.rule)
        db.session.commit()
        channel_hiding.materialize('rule added')
        db.session.expire_all()
        self.assertEqual(db.session.get(ChannelHideRule, self.rule.id).match_count, 2)

    def test_an_account_delete_takes_its_channels_out_of_the_counts(self):
        from app.routes.accounts import _delete_account_and_jobs
        deleted, _ = _delete_account_and_jobs(self.gone.id)
        self.assertTrue(deleted)
        db.session.expire_all()
        self.assertEqual(db.session.get(ChannelHideRule, self.rule.id).match_count, 1)

    def test_a_missing_channel_delete_takes_them_out_too(self):
        now = dt.datetime.utcnow()
        Channel.query.filter_by(account_id=self.gone.id).one().last_seen_at = (
            now - dt.timedelta(days=30))
        db.session.get(Account, self.gone.id).last_sync_at = now - dt.timedelta(days=1)
        db.session.commit()
        res = self.t.app.test_client().post('/channels/missing-delete',
                                            json={'account_id': self.gone.id})
        self.assertEqual(res.get_json().get('deleted_count'), 1, res.get_data(as_text=True))
        db.session.expire_all()
        self.assertEqual(db.session.get(ChannelHideRule, self.rule.id).match_count, 1)


if __name__ == '__main__':
    unittest.main()
