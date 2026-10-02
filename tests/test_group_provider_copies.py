"""A group says what a second copy of a channel covers (dev/changelog/1173,
DESIGN-account-providers.md §9).

Two members whose channels share a provider key are one channel reached twice. Through a
different login that copy is a real backup against a problem with one account; on the same
login it covers nothing an account problem would spare. The group names which one it is
looking at, at add time (a soft note in `_pending_warnings()`) and on the group page (a
standing line), and touches nothing: no member removed, no switch moved.

What this file pins:

  - ProviderCopiesTests: `duplicate_streams.provider_copies()` - which pairs it finds, the
    situation it names, the sentence, and the pairs it leaves to the URL warning.
  - AddTimeNoteTests: the add route's note, its recording gate, and `force`.
  - GroupPageLineTests: the page payload's line, present while the condition holds and gone
    when it does not, on both the first render and the live refresh.

No network, no ffmpeg. Run standalone:
  python3 -m unittest tests.test_group_provider_copies
"""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event  # noqa: E402

from app import db, duplicate_streams  # noqa: E402
from app import account_links as links  # noqa: E402
from app.database import Channel, ChannelGroupMember  # noqa: E402
from app.duplicate_streams import (  # noqa: E402
    COPY_OTHER_LOGIN, COPY_SAME_ACCOUNT, COPY_SAME_LOGIN, provider_copies)
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402


class _Case(unittest.TestCase):
    """skyline-curated and skyline-direct reach one backend; harbor-direct is unrelated.
    Nothing is on a provider until a test says so."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        now = datetime.utcnow()
        self.now = now
        self.curated = seed.make_account(name='skyline-curated', last_sync_at=now)
        self.direct = seed.make_account(name='skyline-direct', last_sync_at=now)
        self.other = seed.make_account(name='harbor-direct', last_sync_at=now)
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def channel(self, account, sid, name, host=None):
        host = host or f'{account.name}.example'
        ch = seed.make_channel(account, name=name, last_seen_at=self.now)
        ch.stream_url = ch.raw_stream_url = f'http://{host}/live/user/pw/{sid}.ts'
        ch.provider_stream_id = str(sid)
        return ch

    def link(self, *accounts, name='skyline'):
        provider, _ = links.create_provider(name, [a.id for a in accounts])
        db.session.expire_all()
        return provider

    def fold(self):
        duplicate_streams.recompute()
        db.session.commit()
        db.session.expire_all()

    def share_login(self):
        """skyline-curated's login `main`, shared to skyline-direct."""
        login = links.add_login(self.curated.id, 'main', 'mainuser', 'pw', 1)
        links.share_login(login.id, self.direct.id)
        db.session.expire_all()

    def chans(self, *ids):
        by_id = {c.id: c for c in Channel.query.filter(Channel.id.in_(ids)).all()}
        return [by_id[i] for i in ids]


class ProviderCopiesTests(_Case):

    def _pair(self):
        a = self.channel(self.curated, 101, 'Freeform')
        b = self.channel(self.direct, 101, 'Freeform East')
        db.session.commit()
        return a.id, b.id

    def test_different_logins_name_a_real_backup(self):
        a, b = self._pair()
        self.link(self.curated, self.direct)
        self.fold()
        out = provider_copies(self.chans(a, b))
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual((c['channel_id'], c['other_id']), (b, a))
        self.assertEqual(c['situation'], COPY_OTHER_LOGIN)
        self.assertEqual(
            c['text'],
            'skyline-direct: Freeform East is the same channel as skyline-curated: Freeform, '
            'through a different login on skyline. It covers a problem with one account, '
            'not an outage at skyline.')

    def test_a_shared_login_says_both_go_together(self):
        a, b = self._pair()
        self.link(self.curated, self.direct)
        self.share_login()
        self.fold()
        out = provider_copies(self.chans(a, b))
        self.assertEqual([c['situation'] for c in out], [COPY_SAME_LOGIN])
        self.assertIn('on the same login, so a problem with that account takes out both',
                      out[0]['text'])
        self.assertIn('list the hosts on one account instead', out[0]['text'])

    def test_two_copies_on_one_account_say_so(self):
        a = self.channel(self.curated, 101, 'Freeform')
        b = self.channel(self.curated, 101, 'Freeform HD', host='alt.example')
        db.session.commit()
        self.link(self.curated)
        self.fold()
        out = provider_copies(self.chans(a.id, b.id))
        self.assertEqual([c['situation'] for c in out], [COPY_SAME_ACCOUNT])
        self.assertIn('on the same account', out[0]['text'])

    def test_unrelated_channels_say_nothing(self):
        a = self.channel(self.curated, 101, 'Freeform')
        b = self.channel(self.direct, 202, 'Something else')
        c = self.channel(self.other, 101, 'Harbor 101')
        db.session.commit()
        self.link(self.curated, self.direct)
        self.fold()
        self.assertEqual(provider_copies(self.chans(a.id, b.id, c.id)), [])

    def test_no_provider_says_nothing(self):
        a, b = self._pair()
        self.fold()
        self.assertEqual(provider_copies(self.chans(a, b)), [])

    def test_a_shared_url_is_left_to_the_url_warning(self):
        a = self.channel(self.curated, 101, 'Freeform', host='same.example')
        b = self.channel(self.direct, 101, 'Freeform East', host='same.example')
        db.session.commit()
        self.link(self.curated, self.direct)
        self.fold()
        self.assertEqual(provider_copies(self.chans(a.id, b.id)), [])

    def test_an_add_is_told_against_an_existing_member(self):
        a, b = self._pair()
        self.link(self.curated, self.direct)
        self.fold()
        out = provider_copies(self.chans(b, a), new_ids={b})
        self.assertEqual([(c['channel_id'], c['other_id']) for c in out], [(b, a)])
        self.assertIn('Freeform, which is already a member, through', out[0]['text'])

    def test_two_new_copies_are_told_against_each_other_not_as_members(self):
        a, b = self._pair()
        self.link(self.curated, self.direct)
        self.fold()
        out = provider_copies(self.chans(a, b), new_ids={a, b})
        self.assertEqual([(c['channel_id'], c['other_id']) for c in out], [(b, a)])
        self.assertNotIn('already a member', out[0]['text'])

    def test_nothing_clustered_costs_no_query(self):
        a = self.channel(self.curated, 101, 'Freeform')
        db.session.commit()
        chans = self.chans(a.id)
        seen = []
        engine = db.engine
        listener = lambda *args: seen.append(args[2])  # noqa: E731
        event.listen(engine, 'before_cursor_execute', listener)
        try:
            self.assertEqual(provider_copies(chans), [])
        finally:
            event.remove(engine, 'before_cursor_execute', listener)
        self.assertEqual(seen, [])


class AddTimeNoteTests(_Case):

    def setUp(self):
        super().setUp()
        self.a = self.channel(self.curated, 101, 'Freeform')
        self.b = self.channel(self.direct, 101, 'Freeform East')
        db.session.commit()
        self.link(self.curated, self.direct)
        self.fold()

    def _group(self, recording=True):
        g = seed.make_group(name='Freeform', members=[db.session.get(Channel, self.a.id)],
                            recording=recording)
        db.session.commit()
        return g.id

    def _add(self, gid, force=False):
        return self.client.post(f'/api/channel-groups/{gid}/members',
                                json={'channel_ids': [self.b.id], 'force': force}).get_json()

    def _members(self, gid):
        return {m.channel_id for m in ChannelGroupMember.query.filter_by(group_id=gid)}

    def test_a_recording_group_is_told_and_nothing_is_added(self):
        gid = self._group()
        data = self._add(gid)
        self.assertFalse(data['success'])
        self.assertEqual(len(data['provider_copies']), 1)
        note = data['provider_copies'][0]
        self.assertEqual(note['situation'], COPY_OTHER_LOGIN)
        self.assertIn('which is already a member, through a different login on skyline',
                      note['text'])
        self.assertNotIn('duplicate_warning', data)
        self.assertEqual(self._members(gid), {self.a.id})

    def test_the_shared_login_sentence_reaches_the_route(self):
        self.share_login()
        gid = self._group()
        data = self._add(gid)
        self.assertEqual(data['provider_copies'][0]['situation'], COPY_SAME_LOGIN)

    def test_force_proceeds(self):
        gid = self._group()
        data = self._add(gid, force=True)
        self.assertTrue(data['success'])
        self.assertEqual(self._members(gid), {self.a.id, self.b.id})

    def test_a_group_nobody_records_from_is_not_interrupted(self):
        """Same gate as the URL warning: both are sentences about failover."""
        gid = self._group(recording=False)
        data = self._add(gid)
        self.assertTrue(data['success'])
        self.assertEqual(self._members(gid), {self.a.id, self.b.id})


class GroupPageLineTests(_Case):

    def setUp(self):
        super().setUp()
        self.a = self.channel(self.curated, 101, 'Freeform')
        self.b = self.channel(self.direct, 101, 'Freeform East')
        db.session.commit()
        g = seed.make_group(name='Freeform', members=[self.a, self.b])
        db.session.commit()
        self.gid = g.id

    def _rows(self):
        return self.client.get(f'/api/channel-groups/{self.gid}/detail-rows').get_json()

    def test_the_line_appears_and_goes_with_the_condition(self):
        self.assertEqual(self._rows()['provider_copies'], [])
        provider = self.link(self.curated, self.direct)
        copies = self._rows()['provider_copies']
        self.assertEqual(len(copies), 1)
        self.assertIn('through a different login on skyline', copies[0]['text'])
        links.delete_provider(provider.id)
        db.session.expire_all()
        self.assertEqual(self._rows()['provider_copies'], [])

    def test_the_first_render_carries_it_for_the_banner(self):
        self.link(self.curated, self.direct)
        html = self.client.get(f'/channel-groups/{self.gid}').get_data(as_text=True)
        self.assertIn('id="gd-copies-banner"', html)
        self.assertIn('through a different login on skyline', html)

    def test_nothing_is_moved(self):
        self.link(self.curated, self.direct)
        before = [(m.channel_id, m.recording_enabled, m.test_enabled)
                  for m in ChannelGroupMember.query.filter_by(group_id=self.gid)
                  .order_by(ChannelGroupMember.channel_id)]
        self._rows()
        db.session.expire_all()
        after = [(m.channel_id, m.recording_enabled, m.test_enabled)
                 for m in ChannelGroupMember.query.filter_by(group_id=self.gid)
                 .order_by(ChannelGroupMember.channel_id)]
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
