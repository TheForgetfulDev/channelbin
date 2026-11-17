"""The schedule-time connection checks count seats per login, the way record start does
(app/routes/recordings.py::_WindowSeats; dev/docs/BUGS.md 2026-11-16 @ 09:40:06 PM).

  - two accounts holding one single-seat login: a recording on one warns the other;
  - an account listing two logins has both their seats, whatever its max_connections says;
  - a sibling's recording takes its own first login before the shared one;
  - a group member on an account sharing a full login counts as committed;
  - an auto-record rule's seat choice no longer takes a sibling the plan reads as free;
  - an account with no logins answers exactly as before.

Throwaway temp SQLite DB through make_test_app(); no scheduler job is armed.

    python3 -m unittest tests.test_schedule_login_seats
"""
import os
import sys
import unittest
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.clock import tomorrow_local_noon  # noqa: E402
from app import account_links, admission, auto_rules, db  # noqa: E402
from app.database import (AccountLogin, Recording, REC_STATUS_SCHEDULED,  # noqa: E402
                          RuleMatch)
from app.routes import recordings as rec_routes  # noqa: E402


class _Case(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        patch('app.routes.recordings.schedule_recording').start()
        self.addCleanup(patch.stopall)
        self.start = tomorrow_local_noon()
        self.stop = self.start + timedelta(hours=1)
        self.one = seed.make_account(name='Account One', max_connections=1)
        self.two = seed.make_account(name='Account Two', max_connections=1)
        db.session.commit()
        self._stream = 500

    def tearDown(self):
        admission.reset_for_tests()
        self.t.cleanup()

    def channel(self, account, name, **kw):
        self._stream += 1
        ch = seed.make_channel(account, stream_id=self._stream, name=name, **kw)
        db.session.commit()
        return ch

    def login(self, account, name, seats=1):
        return account_links.add_login(account.id, name, f'{name}-user', 'pw', seats).id

    def share(self, login_id, account, position=0):
        # A provider is required by share_login(); the seat model only reads the rows.
        db.session.add(AccountLogin(account_id=account.id, login_id=login_id, position=position))
        db.session.commit()

    def busy(self, channel, name='Busy'):
        rec = seed.make_recording(status=REC_STATUS_SCHEDULED, name=name, channel_id=channel.id,
                                  start_time=self.start - timedelta(minutes=15),
                                  stop_time=self.stop + timedelta(minutes=15))
        db.session.commit()
        return rec

    def warning(self, channel):
        db.session.expire_all()
        return rec_routes._check_account_connection_limit(
            channel.account_id, channel.id, self.start, self.stop)


class SharedLoginTests(_Case):

    def test_a_recording_on_an_account_sharing_the_login_warns(self):
        main = self.login(self.one, 'main')
        self.share(main, self.two)
        mine = self.channel(self.one, 'Mine')
        self.busy(self.channel(self.two, 'Theirs'))
        w = self.warning(mine)
        self.assertIsNotNone(w, 'one seat, held through the other account')
        self.assertIn('Its login is shared with Account Two', w['message'])
        self.assertEqual([c['channel_name'] for c in w['conflicts']], ['Theirs'])

    def test_two_logins_give_the_account_both_their_seats(self):
        self.login(self.one, 'main')
        self.login(self.one, 'spare')
        mine = self.channel(self.one, 'Mine')
        self.busy(self.channel(self.one, 'Other'))
        self.assertIsNone(self.warning(mine), 'two logins of one seat each: room for a second')
        self.busy(self.channel(self.one, 'Third'), 'Busy 2')
        w = self.warning(mine)
        self.assertIsNotNone(w)
        self.assertIn('connection limit (2)', w['message'])

    def test_a_sibling_fills_its_own_login_before_the_shared_one(self):
        shared = self.login(self.one, 'shared')
        self.login(self.two, 'own')
        self.share(shared, self.two, position=1)
        mine = self.channel(self.one, 'Mine')
        self.busy(self.channel(self.two, 'Theirs A'), 'A')
        self.assertIsNone(self.warning(mine), "the sibling's first recording sits on its own login")
        self.busy(self.channel(self.two, 'Theirs B'), 'B')
        self.assertIsNotNone(self.warning(mine), 'the second spills onto the shared login')

    def test_a_group_member_on_a_sibling_of_a_full_login_is_committed(self):
        main = self.login(self.one, 'main')
        self.share(main, self.two)
        member = self.channel(self.two, 'Member')
        self.busy(self.channel(self.one, 'Busy here'))
        db.session.expire_all()
        committed = rec_routes._members_committed_in_window([member], self.start, self.stop)
        self.assertEqual(committed, {member.id})

    def test_no_logins_answers_as_before(self):
        mine = self.channel(self.one, 'Mine')
        self.assertIsNone(self.warning(mine))
        self.busy(self.channel(self.two, 'Other account'))
        self.assertIsNone(self.warning(mine), 'another account with no shared login is not counted')
        self.busy(self.channel(self.one, 'Same account'), 'Busy 2')
        w = self.warning(mine)
        self.assertIsNotNone(w)
        self.assertNotIn('shared', w['message'])
        self.busy(mine, 'Same channel')
        db.session.expire_all()
        self.assertEqual(len(self.warning(mine)['conflicts']), 1,
                         'same-channel overlap is a handoff, never a conflict')


class RuleSeatChoiceTests(_Case):

    def test_a_rule_does_not_move_to_an_account_sharing_the_full_login(self):
        """dev/changelog/1602 tried a login sibling last because the plan could not see the
        shared seat; now the plan sees it, so the sibling is refused on its own merits."""
        three = seed.make_account(name='Account Three', max_connections=1)
        db.session.commit()
        main = self.login(self.one, 'main')
        self.share(main, self.two)
        first = self.channel(self.one, 'Sports One HD', health_score=90)
        sibling = self.channel(self.two, 'Sports One Sibling', health_score=80)
        apart = self.channel(three, 'Sports One Apart', health_score=40)
        for ch in (first, sibling, apart):
            seed.make_epg_entry(ch, title='Cup Race One', start_time=self.start,
                                duration_minutes=60)
        self.busy(self.channel(self.one, 'Busy channel'))
        rid = auto_rules.create_rule(auto_rules.read_rule_values(
            {'name': 'Test rule', 'search': 'grain=airings&q=Cup+Race'}))
        auto_rules.evaluate_now(rid)
        db.session.expire_all()
        rec = Recording.query.filter_by(rule_id=rid).one()
        self.assertEqual(rec.channel_id, apart.id)
        row = RuleMatch.query.filter_by(rule_id=rid, recording_id=rec.id).one()
        self.assertIn('"Sports One Sibling": This would exceed "Account Two"', row.detail)


if __name__ == '__main__':
    unittest.main()
