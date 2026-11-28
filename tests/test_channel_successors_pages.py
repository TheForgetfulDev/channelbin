"""The pages of replacing a channel the playlist stopped listing
(dev/docs/DESIGN-channel-successors.md section 9; the approved mockup is dev/changelog/1789).

The Replacements page and its accept API, the menu count and the Dashboard line, both lines on
a channel page and its Replace and Count its history controls, the delete dialog's replacement
counts, and channel search's two new Other values. The engine underneath is
tests/test_channel_successors.py.

Run standalone:
  python3 -m unittest tests.test_channel_successors_pages
"""
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import channel_successors as successors  # noqa: E402
from app import db  # noqa: E402
from app.channel_search import (  # noqa: E402
    DimensionFilter, OTHER_REPLACED, OTHER_UNLISTED, SearchContext, SearchState, search,
)
from app.config import load_config  # noqa: E402
from app.database import (  # noqa: E402
    AccountSyncLog, Channel, ChannelGroupMember, ChannelReplacement, Provider,
)
from tests.support import make_test_app, seed  # noqa: E402
from tests.support.search import only_hiding  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.client = self.t.app.test_client()
        self.now = datetime.utcnow().replace(microsecond=0)
        self.acc = seed.make_account(name='Account A')
        self.sync_start = self.now - timedelta(hours=1)
        self.synced(self.acc)
        db.session.commit()

    def synced(self, account):
        done = self.sync_start + timedelta(minutes=5)
        account.last_sync_at = done
        db.session.add(AccountSyncLog(account_id=account.id, started_at=self.sync_start,
                                      completed_at=done, status='SUCCESS'))
        db.session.flush()

    def old_channel(self, name='ESPN', psid='1001', account=None, **kw):
        ch = seed.make_channel(account or self.acc, name=name, **kw)
        ch.provider_stream_id = psid
        ch.last_seen_at = self.sync_start - timedelta(hours=3)
        db.session.flush()
        return ch

    def new_channel(self, name='ESPN HD', psid='1001', account=None, **kw):
        ch = seed.make_channel(account or self.acc, name=name, **kw)
        ch.provider_stream_id = psid
        ch.last_seen_at = self.sync_start + timedelta(minutes=1)
        db.session.flush()
        return ch

    def boot(self, html):
        m = re.search(r'<script type="application/json" id="rs-boot">(.*?)</script>', html, re.S)
        self.assertIsNotNone(m, 'the review page has no boot block')
        return json.loads(m.group(1))

    def post(self, url, body):
        return self.client.post(url, data=json.dumps(body), content_type='application/json')


class ReviewPageTests(_Base):
    def test_lists_each_stranded_channel_once_with_its_proposals_and_carry(self):
        """One row per old channel with every group it is in, its proposals, and what an
        accept would bring over or leave on a value the new channel has (sections 8, 9)."""
        old = self.old_channel(notes='old note')
        old.health_score_sample_count = 4
        new = self.new_channel(notes='its own note')
        other = self.old_channel(name='CNN', psid='2002')
        seed.make_group(name='Sports', members=[old])
        seed.make_group(name='More', members=[old, other])
        db.session.commit()
        r = self.client.get('/channels/replacements')
        self.assertEqual(r.status_code, 200)
        payload = self.boot(r.get_data(as_text=True))['payload']
        rows = {row['old_id']: row for row in payload['rows']}
        self.assertEqual(set(rows), {old.id, other.id})
        self.assertEqual(len(rows[old.id]['groups']), 2)
        self.assertEqual([m['id'] for m in rows[old.id]['matches']], [new.id])
        carry = {p['k']: p['state'] for p in rows[old.id]['matches'][0]['carry']}
        self.assertEqual(carry[successors.CARRY_HEALTH], successors.PREVIEW_CARRY)
        self.assertEqual(carry[successors.CARRY_NOTES], successors.PREVIEW_KEPT)
        self.assertEqual(rows[other.id]['matches'], [])

    def test_every_drawn_cell_carries_a_label_for_the_phone_cards(self):
        """The page's .tbl-cards table is drawn by replacements.js::draw(); every cell but the
        trailing actions cell and the account header's spanning cell says what it is
        (DESIGN.md 3.2, tests/test_table_card_reflow.py's registry points here)."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'static', 'js', 'replacements.js'), encoding='utf-8') as fh:
            src = fh.read()
        body = src.split('  function draw() {')[1].split('\n  function ')[0]
        cells = [c for c in re.findall(r'<td\b[^>]*>', body) if 'colspan=' not in c]
        self.assertGreaterEqual(len(cells), 5)
        unlabeled = [c for c in cells[:-1] if 'data-label=' not in c]
        self.assertEqual(unlabeled, [])

    def test_offers_only_accounts_on_the_same_provider_as_targets(self):
        prov = Provider(name='Strong')
        db.session.add(prov)
        db.session.flush()
        self.acc.provider_id = prov.id
        peer = seed.make_account(name='Peer', provider_id=prov.id)
        stranger = seed.make_account(name='Stranger')
        self.synced(peer)
        self.synced(stranger)
        old = self.old_channel()
        seed.make_group(name='G', members=[old])
        db.session.commit()
        payload = self.boot(self.client.get('/channels/replacements').get_data(as_text=True))['payload']
        values = [t['v'] for t in payload['targets']]
        self.assertEqual(values, ['own', str(peer.id)])

    def test_nothing_stranded_is_an_empty_page_and_no_menu_link(self):
        ch = self.new_channel()
        seed.make_group(name='G', members=[ch])
        db.session.commit()
        page = self.client.get('/channels/replacements').get_data(as_text=True)
        self.assertIn('Nothing to replace', page)
        dash = self.client.get('/').get_data(as_text=True)
        self.assertNotIn('nav-count-replacements', dash)
        self.assertNotIn("account's last sync. <a", dash)

    def test_menu_and_dashboard_show_the_count_while_something_waits(self):
        old = self.old_channel()
        seed.make_group(name='G', members=[old])
        db.session.commit()
        dash = self.client.get('/').get_data(as_text=True)
        self.assertRegex(dash, r'nav-count-replacements[^>]*>1<')
        self.assertIn('1 channel in your groups or guide was not in its account', dash)


class AcceptApiTests(_Base):
    def setUp(self):
        super().setUp()
        self.old = self.old_channel()
        self.new = self.new_channel()
        self.group = seed.make_group(name='G', members=[self.old])
        db.session.commit()

    def test_accept_one_moves_the_member_and_says_what_it_did(self):
        r = self.post('/api/replacements/accept',
                      {'pairs': [{'old_id': self.old.id, 'new_id': self.new.id}],
                       'carry': list(successors.CARRY_PIECES)})
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertTrue(body['success'])
        self.assertEqual([d['old_id'] for d in body['replaced']], [self.old.id])
        db.session.expire_all()
        self.assertEqual([m.channel_id for m in ChannelGroupMember.query.filter_by(group_id=self.group.id)],
                         [self.new.id])

    def test_a_single_refused_pair_is_a_409_with_the_reason(self):
        stranger = self.new_channel(name='Other', psid='9')
        db.session.commit()
        r = self.post('/api/replacements/accept',
                      {'pairs': [{'old_id': self.old.id, 'new_id': stranger.id}]})
        self.assertEqual(r.status_code, 409)
        self.assertIn('not the same stream', r.get_json()['error'])

    def test_a_picked_pair_is_accepted_and_recorded_as_picked(self):
        stranger = self.new_channel(name='Other', psid='9')
        db.session.commit()
        r = self.post('/api/replacements/accept',
                      {'pairs': [{'old_id': self.old.id, 'new_id': stranger.id, 'picked': True}]})
        self.assertEqual(r.status_code, 200, r.get_json())
        link = ChannelReplacement.query.filter_by(old_channel_id=self.old.id).one()
        self.assertEqual(link.match_kind, 'picked')

    def test_bulk_accept_reports_each_skip(self):
        r = self.post('/api/replacements/accept', {'pairs': [
            {'old_id': self.old.id, 'new_id': self.new.id},
            {'old_id': self.old.id, 'new_id': self.new.id}]})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(len(body['replaced']), 1)
        self.assertEqual(len(body['skipped']), 1)
        self.assertIn('already replaced', body['skipped'][0]['reason'])

    def test_bad_bodies_are_400(self):
        for body in ({}, {'pairs': []}, {'pairs': [{'old_id': 'x'}]},
                     {'pairs': [{'old_id': 1, 'new_id': 2}], 'carry': ['nope']}):
            self.assertEqual(self.post('/api/replacements/accept', body).status_code, 400, body)

    def test_health_switch_replays_the_score_and_404s_a_gone_link(self):
        seed.make_channel_test(self.old, all_null=False, status='COMPLETED', quality_score=90,
                               duration_seconds=60, test_started_at=self.now - timedelta(days=1),
                               test_ended_at=self.now - timedelta(days=1))
        db.session.commit()
        successors.accept(self.old.id, self.new.id, successors.CARRY_PIECES, cfg=load_config())
        link = ChannelReplacement.query.filter_by(old_channel_id=self.old.id).one()
        r = self.post(f'/api/replacements/{link.id}/health', {'on': False})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['changed'])
        self.assertIsNone(r.get_json()['score_after'])
        self.assertEqual(self.post(f'/api/replacements/{link.id}/health', {'on': 'yes'}).status_code, 400)
        self.assertEqual(self.post('/api/replacements/99999/health', {'on': True}).status_code, 404)


class ChannelPageTests(_Base):
    def setUp(self):
        super().setUp()
        self.old = self.old_channel()
        self.new = self.new_channel()
        seed.make_group(name='G', members=[self.old])
        self.test_at = self.now - timedelta(days=1)
        seed.make_channel_test(self.old, all_null=False, status='COMPLETED', quality_score=90,
                               duration_seconds=60, test_started_at=self.test_at,
                               test_ended_at=self.test_at)
        db.session.commit()

    def page(self, channel_id):
        r = self.client.get(f'/channels/{channel_id}')
        self.assertEqual(r.status_code, 200)
        return r.get_data(as_text=True)

    def test_an_unlisted_channel_offers_its_replacement(self):
        html = self.page(self.old.id)
        self.assertIn('Not in last sync', html)
        self.assertIn('Replacement available', html)
        self.assertIn('data-act="replace"', html)

    def test_after_the_accept_both_pages_say_so_and_the_new_lists_the_old_test(self):
        old_id, new_id = self.old.id, self.new.id
        successors.accept(old_id, new_id, successors.CARRY_PIECES, cfg=load_config())
        db.session.remove()
        old_html = self.page(old_id)
        self.assertIn('Replaced by', old_html)
        self.assertNotIn('Replacement available', old_html)
        new_html = self.page(new_id)
        self.assertIn('Continues the history of', new_html)
        self.assertIn('data-act="history"', new_html)
        self.assertIn('class="cd-inherited"', new_html)
        self.assertIn('On ESPN', new_html)

    def test_with_the_history_switched_off_the_old_test_is_not_listed(self):
        successors.accept(self.old.id, self.new.id, successors.CARRY_PIECES, cfg=load_config())
        link = ChannelReplacement.query.filter_by(old_channel_id=self.old.id).one()
        successors.set_health_carried(link.id, False, cfg=load_config())
        new_id = self.new.id
        db.session.remove()
        html = self.page(new_id)
        self.assertIn('History not counted', html)
        self.assertNotIn('class="cd-inherited"', html)


class DeleteDialogTests(_Base):
    def test_the_preview_counts_waiting_and_replaced_channels(self):
        cfg = load_config()
        days = cfg['sync'].get('channel_missing_after_days', 7)
        long_ago = self.sync_start - timedelta(days=days + 3)
        waiting = self.old_channel(name='Waiting', psid='1')
        self.new_channel(name='Waiting HD', psid='1')
        done = self.old_channel(name='Done', psid='2')
        done_new = self.new_channel(name='Done HD', psid='2')
        lone = self.old_channel(name='Lone', psid='3')
        for ch in (waiting, done, lone):
            ch.last_seen_at = long_ago
        group = seed.make_group(name='G', members=[waiting])
        db.session.commit()
        successors.accept(done.id, done_new.id, successors.CARRY_PIECES, cfg=cfg)
        # The group's own delete reaches its stranded member, which has a replacement.
        r = self.client.get(f'/api/channels/missing-delete-preview?group_id={group.id}')
        body = r.get_json()
        self.assertEqual(r.status_code, 200, body)
        self.assertEqual(body['eligible_count'], 1)
        self.assertEqual(body['replacement_waiting_count'], 1)
        self.assertIn(f'group={group.id}', body['replacements_url'])
        # Any other scope leaves a channel in a group out of the batch, so nothing it can
        # delete has a place to lose: the waiting count is 0, never a claim about groups.
        r = self.client.get('/api/channels/missing-delete-preview?channel_ids='
                            f'{waiting.id},{done.id},{lone.id}')
        body = r.get_json()
        self.assertEqual(body['blocked_inuse_count'], 1)
        self.assertEqual(body['replacement_waiting_count'], 0)
        self.assertEqual(body['replaced_count'], 1)


class SearchValueTests(_Base):
    def ids(self, value):
        state = SearchState(standing=only_hiding(), facets=(),
                            filters=(DimensionFilter('other', (value,)),))
        return {r.id for r in search(state, SearchContext.build()).rows if isinstance(r, Channel)}

    def test_unlisted_and_replaced_are_their_own_values(self):
        old = self.old_channel()
        new = self.new_channel()
        lone = self.old_channel(name='Lone', psid='7')
        seed.make_group(name='G', members=[old])
        db.session.commit()
        self.assertEqual(self.ids(OTHER_UNLISTED), {old.id, lone.id})
        self.assertEqual(self.ids(OTHER_REPLACED), set())
        successors.accept(old.id, new.id, successors.CARRY_PIECES, cfg=load_config())
        self.assertEqual(self.ids(OTHER_REPLACED), {old.id})


if __name__ == '__main__':
    unittest.main()
