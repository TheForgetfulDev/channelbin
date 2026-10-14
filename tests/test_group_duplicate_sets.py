"""A group's duplicate sets: one line, which members, and removing the extras keeping each
set's lead (dev/changelog/1175, DESIGN-account-providers.md §9).

A duplicate set is the members that are one stream - the same stream URL, or the same stored
duplicate cluster (one channel through two accounts on a provider). The group page counts
them in one banner, badges each extra copy with the copy it duplicates and how, and its
Remove duplicates dialog starts every set on its lead: the copy the group records from first.
This replaced dev/changelog/1173's one sentence per pair.

What this file pins:

  - DuplicateSetsTests: `duplicate_streams.duplicate_sets()` - which sets it finds, the merge
    of URL and cluster matches into one set, the lead, the situation each copy is told with,
    and the queries it costs.
  - AddTimeWarningTests: the add route's warning carries the sets, keeps an existing member
    as each set's keeper, rides the recording gate, and `force` proceeds.
  - GroupPageTests: the page payload's sets, roles and badge text; the lead rule; one banner.
  - RemoveExtrasTests: removing a provider copy transfers onto the kept copy rather than
    skipping it, and the guide-membership gate still applies.
  - GroupDetailSourceTests: group-detail.js, asserted as source because it has no jsdom
    harness (see tests/test_group_member_filters.py) - one banner worded by the shared
    dupSetsSummary(), a badge on extras only, the Duplicates filter, Show without the
    recording-enabled floor. dupSetsSummary() itself runs in jsdom in test_group_modal_js.

No network, no ffmpeg. Run standalone:
  python3 -m unittest tests.test_group_duplicate_sets
"""
import os
import re
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event  # noqa: E402

from app import db, duplicate_streams  # noqa: E402
from app import account_links as links  # noqa: E402
from app.database import Channel, ChannelGroupMember  # noqa: E402
from app.duplicate_streams import (  # noqa: E402
    COPY_OTHER_LOGIN, COPY_SAME_ACCOUNT, COPY_SAME_LOGIN, COPY_SAME_URL, copy_note,
    duplicate_sets)
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

    def queries(self, fn):
        seen = []
        listener = lambda *args: seen.append(args[2])  # noqa: E731
        event.listen(db.engine, 'before_cursor_execute', listener)
        try:
            result = fn()
        finally:
            event.remove(db.engine, 'before_cursor_execute', listener)
        return result, seen


class DuplicateSetsTests(_Case):

    def _pair(self):
        a = self.channel(self.curated, 101, 'Freeform')
        b = self.channel(self.direct, 101, 'Freeform East')
        db.session.commit()
        return a.id, b.id

    def _only_copy(self, sets):
        self.assertEqual(len(sets), 1)
        (cid, copy), = sets[0]['copies'].items()
        return cid, copy

    def test_different_logins_are_one_set_led_by_the_first(self):
        a, b = self._pair()
        self.link(self.curated, self.direct)
        self.fold()
        sets = duplicate_sets(self.chans(a, b))
        self.assertEqual([ch.id for ch in sets[0]['channels']], [a, b])
        self.assertEqual(sets[0]['lead'].id, a)
        self.assertEqual(sets[0]['provider'], 'skyline')
        cid, copy = self._only_copy(sets)
        self.assertEqual((cid, copy['of'].id, copy['situation'], copy['provider']),
                         (b, a, COPY_OTHER_LOGIN, 'skyline'))

    def test_a_shared_login_is_named(self):
        a, b = self._pair()
        self.link(self.curated, self.direct)
        self.share_login()
        self.fold()
        _, copy = self._only_copy(duplicate_sets(self.chans(a, b)))
        self.assertEqual(copy['situation'], COPY_SAME_LOGIN)

    def test_two_copies_on_one_account_are_named(self):
        a = self.channel(self.curated, 101, 'Freeform')
        b = self.channel(self.curated, 101, 'Freeform HD', host='alt.example')
        db.session.commit()
        self.link(self.curated)
        self.fold()
        _, copy = self._only_copy(duplicate_sets(self.chans(a.id, b.id)))
        self.assertEqual(copy['situation'], COPY_SAME_ACCOUNT)

    def test_a_shared_url_is_a_set_with_no_provider(self):
        a = self.channel(self.curated, 101, 'Freeform', host='same.example')
        b = self.channel(self.other, 101, 'Freeform East', host='same.example')
        db.session.commit()
        sets = duplicate_sets(self.chans(a.id, b.id))
        self.assertIsNone(sets[0]['provider'])
        _, copy = self._only_copy(sets)
        self.assertEqual((copy['situation'], copy['provider']), (COPY_SAME_URL, None))

    def test_unrelated_channels_and_no_provider_make_no_set(self):
        a, b = self._pair()
        c = self.channel(self.other, 101, 'Harbor 101')
        db.session.commit()
        self.fold()
        self.assertEqual(duplicate_sets(self.chans(a, b, c.id)), [])

    def test_a_url_match_and_a_provider_match_are_one_set(self):
        """One set per stream: a channel that matches one copy by URL and another by stream
        id must not land in two sets, or the dialog would keep it in one and remove it in
        the other. A copy that does not match the lead directly is told against the copy it
        does match, never as a duplicate of the lead."""
        a, b = self._pair()
        c = self.channel(self.other, 999, 'Freeform West', host='skyline-direct.example')
        c.stream_url = c.raw_stream_url = db.session.get(Channel, b).stream_url
        db.session.commit()
        self.link(self.curated, self.direct)
        self.fold()
        sets = duplicate_sets(self.chans(a, b, c.id))
        self.assertEqual(len(sets), 1)
        self.assertEqual({ch.id for ch in sets[0]['channels']}, {a, b, c.id})
        copies = sets[0]['copies']
        self.assertEqual((copies[b]['of'].id, copies[b]['situation']), (a, COPY_OTHER_LOGIN))
        self.assertEqual((copies[c.id]['of'].id, copies[c.id]['situation']), (b, COPY_SAME_URL))

    def test_three_copies_are_one_set_with_two_extras(self):
        a = self.channel(self.curated, 101, 'Freeform')
        b = self.channel(self.direct, 101, 'Freeform East')
        c = self.channel(self.curated, 101, 'Freeform HD', host='alt.example')
        db.session.commit()
        self.link(self.curated, self.direct)
        self.fold()
        sets = duplicate_sets(self.chans(a.id, b.id, c.id))
        self.assertEqual(len(sets), 1)
        self.assertEqual(set(sets[0]['copies']), {b.id, c.id})

    def test_lead_of_picks_the_lead_and_copies_are_told_against_it(self):
        a, b = self._pair()
        self.link(self.curated, self.direct)
        self.fold()
        sets = duplicate_sets(self.chans(a, b), lambda chs: chs[-1])
        self.assertEqual(sets[0]['lead'].id, b)
        self.assertEqual(list(sets[0]['copies']), [a])
        self.assertEqual(sets[0]['copies'][a]['of'].id, b)

    def test_nothing_clustered_costs_no_query(self):
        a = self.channel(self.curated, 101, 'Freeform')
        db.session.commit()
        chans = self.chans(a.id)
        result, seen = self.queries(lambda: duplicate_sets(chans))
        self.assertEqual((result, seen), ([], []))

    def test_a_url_only_set_costs_no_query(self):
        a = self.channel(self.curated, 101, 'Freeform', host='same.example')
        b = self.channel(self.other, 101, 'Freeform East', host='same.example')
        db.session.commit()
        chans = self.chans(a.id, b.id)
        result, seen = self.queries(lambda: duplicate_sets(chans))
        self.assertEqual(len(result), 1)
        self.assertEqual(seen, [])


class CopyNoteTests(unittest.TestCase):

    def test_each_situation_is_a_few_words(self):
        self.assertEqual(copy_note(COPY_SAME_URL, None), 'Same stream URL.')
        self.assertEqual(copy_note(COPY_OTHER_LOGIN, 'Strong'),
                         'Same channel on Strong, through a different login.')
        self.assertEqual(copy_note(COPY_SAME_LOGIN, 'Strong'),
                         'Same channel on Strong, on the same login.')
        self.assertEqual(copy_note(COPY_SAME_ACCOUNT, 'Strong'),
                         'Same channel on Strong, on the same account.')

    def test_an_unknown_situation_is_an_error(self):
        with self.assertRaises(ValueError):
            copy_note('nope', None)


class AddTimeWarningTests(_Case):

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

    def test_a_recording_group_is_warned_with_the_set_and_nothing_is_added(self):
        gid = self._group()
        data = self._add(gid)
        self.assertFalse(data['success'])
        self.assertTrue(data['duplicate_warning'])
        self.assertNotIn('provider_copies', data)
        (dset,) = data['dup_groups']
        self.assertEqual(dset['provider'], 'skyline')
        self.assertEqual({c['channel_id'] for c in dset['channels']}, {self.a.id, self.b.id})
        self.assertEqual(dset['suggested_keep_id'], self.a.id,
                         'the member the group already holds is the one kept by default')
        self.assertEqual(self._members(gid), {self.a.id})

    def test_force_proceeds(self):
        gid = self._group()
        data = self._add(gid, force=True)
        self.assertTrue(data['success'])
        self.assertEqual(self._members(gid), {self.a.id, self.b.id})

    def test_a_group_nobody_records_from_is_not_interrupted(self):
        gid = self._group(recording=False)
        data = self._add(gid)
        self.assertTrue(data['success'])
        self.assertEqual(self._members(gid), {self.a.id, self.b.id})


class GroupPageTests(_Case):

    def setUp(self):
        super().setUp()
        self.a = self.channel(self.curated, 101, 'Freeform')
        self.b = self.channel(self.direct, 101, 'Freeform East')
        db.session.commit()

    def _group(self, **kw):
        g = seed.make_group(name='Freeform', members=[self.a, self.b], **kw)
        db.session.commit()
        self.gid = g.id
        return g.id

    def _rows(self):
        return self.client.get(f'/api/channel-groups/{self.gid}/detail-rows').get_json()

    def test_the_set_appears_and_goes_with_the_provider(self):
        self._group()
        self.assertEqual(self._rows()['dup_groups'], [])
        provider = self.link(self.curated, self.direct)
        data = self._rows()
        self.assertEqual(len(data['dup_groups']), 1)
        self.assertEqual(data['dup_groups'][0]['provider'], 'skyline')
        self.assertNotIn('provider_copies', data)
        links.delete_provider(provider.id)
        db.session.expire_all()
        self.assertEqual(self._rows()['dup_groups'], [])

    def test_the_extra_is_badged_with_its_lead_and_the_lead_is_not(self):
        self._group()
        self.link(self.curated, self.direct)
        rows = {r['channel_id']: r for r in self._rows()['rows']}
        lead, extra = rows[self.a.id], rows[self.b.id]
        self.assertEqual((lead['dup_role'], lead['duplicate_title']), ('lead', None))
        self.assertEqual(extra['dup_role'], 'extra')
        self.assertEqual(extra['duplicate_title'],
                         'Duplicate of Freeform (skyline-curated), the lead copy.\n'
                         'Same channel on skyline, through a different login.')

    def test_a_member_in_no_set_has_no_role(self):
        c = self.channel(self.other, 555, 'Unrelated')
        db.session.commit()
        g = seed.make_group(name='Mixed', members=[self.a, c])
        db.session.commit()
        self.gid = g.id
        self.assertEqual({r['dup_role'] for r in self._rows()['rows']}, {None})

    def test_the_lead_is_the_recording_enabled_copy_even_when_ranked_lower(self):
        """The lead is what the group would record from first, so keeping it never changes
        the feed a recording uses. A recording-off copy that scores higher ranks above it
        in the member list, and is still the extra."""
        db.session.get(Channel, self.b.id).health_score = 95
        db.session.get(Channel, self.a.id).health_score = 40
        db.session.commit()
        self._group(disabled={self.b.id})
        self.link(self.curated, self.direct)
        data = self._rows()
        self.assertEqual([r['channel_id'] for r in data['rows']][0], self.b.id,
                         'precondition: the recording-off copy ranks first')
        self.assertEqual(data['dup_groups'][0]['suggested_keep_id'], self.a.id)
        roles = {r['channel_id']: r['dup_role'] for r in data['rows']}
        self.assertEqual(roles, {self.a.id: 'lead', self.b.id: 'extra'})

    def test_the_page_has_one_duplicates_banner(self):
        self._group()
        self.link(self.curated, self.direct)
        html = self.client.get(f'/channel-groups/{self.gid}').get_data(as_text=True)
        self.assertIn('id="gd-dup-banner"', html)
        self.assertNotIn('gd-copies-banner', html)
        self.assertNotIn('providerCopies', html)

    def test_reading_the_page_moves_nothing(self):
        self._group()
        self.link(self.curated, self.direct)

        def state():
            return [(m.channel_id, m.recording_enabled, m.test_enabled)
                    for m in ChannelGroupMember.query.filter_by(group_id=self.gid)
                    .order_by(ChannelGroupMember.channel_id)]
        before = state()
        self._rows()
        db.session.expire_all()
        self.assertEqual(before, state())


class RemoveExtrasTests(_Case):

    def setUp(self):
        super().setUp()
        self.a = self.channel(self.curated, 101, 'Freeform')
        self.b = self.channel(self.direct, 101, 'Freeform East')
        self.b.in_guide = True
        db.session.commit()
        g = seed.make_group(name='Freeform', members=[self.a, self.b])
        db.session.commit()
        self.gid = g.id
        self.link(self.curated, self.direct)
        self.fold()

    def _remove(self, transfer=True, confirm=False):
        return self.client.post(
            f'/api/channel-groups/{self.gid}/members/remove',
            json={'removals': [{'channel_id': self.b.id, 'keep_channel_id': self.a.id}],
                  'transfer': transfer, 'confirm': confirm})

    def test_a_provider_copy_transfers_onto_the_lead(self):
        """Transfer used to demand the same stream URL, so a provider copy was skipped and its
        guide listing stayed behind while the dialog's Transfer box said otherwise."""
        data = self._remove().get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['transfer_skipped'], [])
        self.assertEqual(data['transferred'], [self.b.id])
        db.session.expire_all()
        self.assertEqual({m.channel_id for m in ChannelGroupMember.query.filter_by(
            group_id=self.gid)}, {self.a.id})
        self.assertTrue(db.session.get(Channel, self.a.id).in_guide)
        self.assertFalse(db.session.get(Channel, self.b.id).in_guide)

    def test_an_unrelated_keeper_is_still_refused_the_transfer(self):
        c = self.channel(self.other, 555, 'Unrelated')
        db.session.commit()
        db.session.add(ChannelGroupMember(group_id=self.gid, channel_id=c.id, position=5,
                                          recording_enabled=True, test_enabled=True))
        db.session.commit()
        data = self.client.post(
            f'/api/channel-groups/{self.gid}/members/remove',
            json={'removals': [{'channel_id': self.b.id, 'keep_channel_id': c.id}],
                  'transfer': True}).get_json()
        self.assertEqual([s['channel_id'] for s in data['transfer_skipped']], [self.b.id])
        self.assertEqual(data['removed'], [self.b.id])

    def test_removing_every_recording_member_still_asks_first(self):
        """The guide-membership gate covers this path like every other removal."""
        resp = self.client.post(
            f'/api/channel-groups/{self.gid}/members/remove',
            json={'removals': [{'channel_id': self.a.id, 'keep_channel_id': self.b.id},
                               {'channel_id': self.b.id, 'keep_channel_id': self.a.id}]})
        self.assertEqual(resp.status_code, 409)
        db.session.expire_all()
        self.assertEqual(ChannelGroupMember.query.filter_by(group_id=self.gid).count(), 2)


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


def _function(js, name):
    start = js.index(f'function {name}(')
    return js[start:js.index('\n  }\n', start)]


class GroupDetailSourceTests(unittest.TestCase):

    def setUp(self):
        self.js = _read('static/js/group-detail.js')

    def test_one_banner_worded_by_the_shared_summary(self):
        self.assertNotIn('renderCopiesBanner', self.js)
        self.assertNotIn('gd-copies-banner', self.js)
        self.assertIn('dupSetsSummary(DUP_GROUPS)', _function(self.js, 'renderDupBanner'))

    def test_the_badge_is_on_extras_only_and_both_layouts_share_it(self):
        badge = _function(self.js, 'dupBadge')
        self.assertIn("r.dup_role !== 'extra'", badge)
        self.assertEqual(len(re.findall(r'(?<!function )dupBadge\(r\)', self.js)), 3,
                         'the desktop name cell once, the phone flags twice (test, push)')
        self.assertNotIn('Duplicate feed.', self.js)

    def test_the_duplicates_filter_is_a_registry_entry(self):
        dims = self.js[self.js.index('const FILTER_DIMS = ['):
                       self.js.index('const filterBar = createFilterBar(')]
        self.assertIn("k: 'dup'", dims)
        self.assertIn('match: (r, v) => r.dup_role === v', dims)

    def test_show_selects_every_set_member_without_the_recording_floor(self):
        review = _function(self.js, 'reviewMembers')
        branch = review[review.index("if (target === 'dup')"):review.index('let revealed')]
        self.assertIn("filterBar.toggle('dup', 'extra')", branch)
        self.assertIn("filterBar.toggle('dup', 'lead')", branch)
        self.assertIn("} else {\n      filterBar.toggle('rec', 'on');", branch)

    def test_the_dialog_does_not_promise_a_return_to_the_guide(self):
        """dev/docs/BUGS.md 2026-10-02 @ 10:18:11 PM - the group page's Remove duplicates
        intro said removed channels "return to the guide as individual channels", which
        stopped being true when joining a group stopped hiding a channel
        (dev/changelog/751)."""
        intro = _function(self.js, 'openDedup')
        self.assertNotIn('return to the guide', intro)
        self.assertIn('lead copy', intro)


if __name__ == '__main__':
    unittest.main()
