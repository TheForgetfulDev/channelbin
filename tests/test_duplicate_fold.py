"""app/duplicate_streams.py - the duplicate fold keyed on (provider, stream id), and the stored
answer to "which copy is folded away" (dev/changelog/1172, DESIGN-account-providers.md §8).

What this file pins, and why each matters:

  - UnlinkedIsUnchangedTests: with no account on a provider the fold is exactly the URL fold
    it replaced. That is the promise to every install that never links anything.
  - ProviderKeyTests: accounts on one provider fold on the stream id whatever their URLs
    say, and every edge the key has (no id, two providers, the unlinked account).
  - FeedRungTests: a copy the provider stopped listing never holds a live copy folded
    behind it.
  - StoredAnswerStaysCurrentTests: the loser column is a cache, so every input the keep
    rule reads has to re-rank it. A missed hook shows the wrong copy as kept with no error
    anywhere, which is why each input gets its own test.
  - WhereTheFoldIsOffTests: the group picker and the airing grain keep provider duplicates
    on screen and still fold URL duplicates.

URL-key flagging itself is tests/test_duplicate_flag_recompute.py. No network, no ffmpeg.
Run standalone:
  python3 -m unittest tests.test_duplicate_fold
"""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, duplicate_streams  # noqa: E402
from app import account_links as links  # noqa: E402
from app import channel_hiding  # noqa: E402
from app.channel_search import (  # noqa: E402
    GRAIN_AIRINGS, OTHER_DUP_URL, OTHER_PROVIDER, DimensionFilter, SearchContext,
    SearchState, search)
from app.channel_search_rows import (  # noqa: E402
    KEEP_REASON_HEALTH, KEEP_REASON_ID, KEEP_REASON_IN_FEED, build_rows)
from app.database import Channel, ChannelGroupMember  # noqa: E402
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import only_hiding  # noqa: E402


class _Case(unittest.TestCase):
    """skyline-curated and skyline-direct reach one backend through different hosts;
    harbor-direct is unrelated. Nothing is on a provider until a test says so."""

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

    def channel(self, account, sid, host=None, name=None, **kw):
        """A channel whose URL carries stream id `sid` on `host` - a different host per
        account by default, so nothing here is a URL duplicate unless a test makes it one."""
        host = host or f'{account.name}.example'
        kw.setdefault('last_seen_at', self.now)
        ch = seed.make_channel(account, name=name or f'{account.name} {sid}', **kw)
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

    def state_of(self):
        """{name: (in a cluster, folded away)} as the DATABASE holds it."""
        db.session.expire_all()
        return {c.name: (c.duplicate_cluster_id is not None, bool(c.is_duplicate_loser))
                for c in Channel.query.all()}

    def losers(self):
        return sorted(n for n, (_dup, loser) in self.state_of().items() if loser)

    def clustered(self):
        return sorted(n for n, (dup, _loser) in self.state_of().items() if dup)

    def names(self, **kw):
        kw.setdefault('standing', only_hiding('showdup'))
        kw.setdefault('facets', ())
        state = SearchState(**kw)
        return sorted(r.name for r in search(state, SearchContext.build()).rows
                      if isinstance(r, Channel))


def _reference_url_fold(channels, group_member_ids):
    """The fold as it was defined before the provider key existed, written out longhand:
    channels sharing a real stream_url are a cluster, and the copy kept is the first by
    (not hidden, in guide, in a group, best health with unscored last, lowest id)."""
    by_url = {}
    for ch in channels:
        if '://' in ch.stream_url:
            by_url.setdefault(ch.stream_url, []).append(ch)
    flagged, losers = set(), set()
    for cluster in by_url.values():
        if len(cluster) < 2:
            continue

        def rank(ch):
            health = (None if ch.health_score is None
                      else ch.health_score + (ch.manual_health_adjustment or 0))
            return (1 if ch.hidden else 0, 0 if ch.in_guide else 1,
                    0 if ch.id in group_member_ids else 1,
                    0 if health is not None else 1, -(health or 0), ch.id)

        ranked = sorted(cluster, key=rank)
        flagged |= {ch.id for ch in cluster}
        losers |= {ch.id for ch in ranked[1:]}
    return flagged, losers


class UnlinkedIsUnchangedTests(_Case):
    def test_a_database_with_no_provider_folds_exactly_as_the_url_fold_did(self):
        """DESIGN-account-providers.md §11: an unlinked database produces identical fold
        results before and after. Every rung of the old cascade decides a cluster here."""
        shared = 'shared.example'
        a1 = self.channel(self.curated, 1, host=shared, name='guide wins', in_guide=True)
        self.channel(self.direct, 1, host=shared, name='guide loses', health_score=99.0)
        b1 = self.channel(self.curated, 2, host=shared, name='group wins')
        self.channel(self.direct, 2, host=shared, name='group loses', health_score=99.0)
        self.channel(self.curated, 3, host=shared, name='health loses', health_score=10.0)
        self.channel(self.direct, 3, host=shared, name='health wins', health_score=80.0)
        self.channel(self.other, 3, host=shared, name='unscored loses')
        self.channel(self.curated, 4, host=shared, name='id wins')
        self.channel(self.direct, 4, host=shared, name='id loses')
        # Same stream id on two accounts with different URLs: NOT a duplicate while unlinked.
        self.channel(self.curated, 5, name='alone a')
        self.channel(self.direct, 5, name='alone b')
        for placeholder in (self.curated, self.direct):
            ch = seed.make_channel(placeholder, name=f'placeholder {placeholder.id}')
            ch.stream_url = 'http'
        seed.make_group(name='G', members=[b1])
        self.assertTrue(a1.in_guide)
        self.fold()

        channels = Channel.query.all()
        members = {m.channel_id for m in ChannelGroupMember.query.all()}
        want_flagged, want_losers = _reference_url_fold(channels, members)
        self.assertEqual({c.id for c in channels if c.duplicate_cluster_id is not None},
                         want_flagged)
        self.assertEqual({c.id for c in channels if c.is_duplicate_loser}, want_losers)
        self.assertEqual(self.losers(), ['group loses', 'guide loses', 'health loses',
                                         'id loses', 'unscored loses'])

    def test_the_search_hides_exactly_the_stored_losers(self):
        self.channel(self.curated, 1, host='shared.example', name='kept')
        self.channel(self.direct, 1, host='shared.example', name='folded')
        self.fold()
        self.assertEqual(self.names(), ['kept'])
        self.assertEqual(self.names(standing=only_hiding()), ['folded', 'kept'])


class ProviderKeyTests(_Case):
    def setUp(self):
        super().setUp()
        self.cur = self.channel(self.curated, 4471, name='curated Freeform')
        self.dir = self.channel(self.direct, 4471, name='direct Freeform')
        self.oth = self.channel(self.other, 4471, name='harbor 4471')
        db.session.commit()

    def test_same_id_different_urls_is_nothing_until_the_accounts_are_linked(self):
        self.fold()
        self.assertEqual(self.clustered(), [])

    def test_accounts_on_one_provider_fold_on_the_stream_id(self):
        """The provider change itself recomputes - no sync in between (§8.1)."""
        self.link(self.curated, self.direct)
        self.assertEqual(self.clustered(), ['curated Freeform', 'direct Freeform'])
        self.assertEqual(self.losers(), ['direct Freeform'])
        self.assertEqual(self.names(), ['curated Freeform', 'harbor 4471'])

    def test_the_same_id_on_an_unlinked_account_is_a_coincidence(self):
        """Ids overlap between unrelated backends by chance (measured: up to 1,498 on real
        accounts), which is why the provider is part of the key."""
        self.link(self.curated, self.direct)
        self.assertNotIn('harbor 4471', self.clustered())

    def test_the_same_id_on_two_providers_is_two_streams(self):
        self.link(self.curated, name='skyline')
        self.link(self.direct, self.other, name='harbor')
        self.assertEqual(self.clustered(), ['direct Freeform', 'harbor 4471'])

    def test_a_channel_with_no_id_on_a_provider_account_keys_on_its_url(self):
        radio_a = seed.make_channel(self.curated, name='radio a', last_seen_at=self.now)
        radio_b = seed.make_channel(self.direct, name='radio b', last_seen_at=self.now)
        radio_a.stream_url = radio_b.stream_url = 'http://radio.example/mount'
        lone = seed.make_channel(self.direct, name='lone mount', last_seen_at=self.now)
        lone.stream_url = 'http://radio.example/other'
        db.session.commit()
        self.link(self.curated, self.direct)
        self.assertEqual(self.clustered(),
                         ['curated Freeform', 'direct Freeform', 'radio a', 'radio b'])

    def test_a_shared_url_across_the_provider_boundary_no_longer_folds(self):
        """§8.3, the accepted edge: a channel keyed by provider and a channel on an account
        that was never put on it are in different partitions even with one URL. The fix is
        to put the second account on the provider."""
        self.oth.stream_url = self.cur.stream_url
        db.session.commit()
        self.fold()
        self.assertEqual(self.clustered(), ['curated Freeform', 'harbor 4471'])

        self.link(self.curated, self.direct)
        self.assertEqual(self.clustered(), ['curated Freeform', 'direct Freeform'])

        links.set_account_provider(self.other.id, self.curated.provider_id)
        self.assertEqual(self.clustered(),
                         ['curated Freeform', 'direct Freeform', 'harbor 4471'])

    def test_leaving_and_deleting_a_provider_unfold(self):
        provider = self.link(self.curated, self.direct)
        links.set_account_provider(self.direct.id, None)
        self.assertEqual(self.clustered(), [])

        links.set_account_provider(self.direct.id, provider.id)
        self.assertEqual(len(self.clustered()), 2)
        links.delete_provider(provider.id)
        self.assertEqual(self.clustered(), [])
        self.assertEqual(self.losers(), [])

    def test_the_row_says_what_made_it_a_duplicate(self):
        """§8.2: the badge names the key, so the payload has to carry it."""
        self.link(self.curated, self.direct)
        url_a = self.channel(self.other, 9, host='shared.example', name='url a')
        self.channel(self.other, 9, host='shared.example', name='url b')
        self.fold()
        state = SearchState(standing=only_hiding(), facets=())
        ctx = SearchContext.build()
        rows = {r['name']: r for r in build_rows(search(state, ctx), state, ctx)}
        self.assertEqual(rows['direct Freeform']['dup']['key'],
                         {'kind': 'provider', 'stream_id': '4471', 'provider': 'skyline'})
        self.assertEqual(rows['direct Freeform']['dup']['kept_id'], self.cur.id)
        self.assertEqual(rows['url b']['dup']['key'], {'kind': 'url'})
        self.assertEqual(rows['url b']['dup']['kept_id'], url_a.id)

    def test_the_channel_page_names_the_key_and_lists_the_other_copy(self):
        self.link(self.curated, self.direct)
        html = self.client.get(f'/channels/{self.dir.id}').get_data(as_text=True)
        self.assertIn('id 4471 on skyline', html)
        self.assertIn('curated Freeform', html)

    def test_the_facet_values_find_provider_channels_and_duplicates(self):
        self.link(self.curated, self.direct)

        def filtered(value, ex=False):
            f = DimensionFilter('other', () if ex else (value,), (value,) if ex else ())
            return self.names(standing=only_hiding(), filters=(f,))

        self.assertEqual(filtered(OTHER_PROVIDER), ['curated Freeform', 'direct Freeform'])
        self.assertEqual(filtered(OTHER_PROVIDER, ex=True), ['harbor 4471'])
        self.assertEqual(filtered(OTHER_DUP_URL), ['curated Freeform', 'direct Freeform'])

    def test_deleting_the_account_that_held_the_kept_copy_unfolds_the_other(self):
        """The stored answer must not outlive the row it points at: the copy on the
        surviving account would otherwise stay folded behind nothing until the next sync."""
        from app.routes.accounts import _delete_account_and_jobs
        self.link(self.curated, self.direct)
        self.assertEqual(self.losers(), ['direct Freeform'])
        deleted, _name = _delete_account_and_jobs(self.curated.id)
        self.assertTrue(deleted)
        self.assertEqual(self.state_of()['direct Freeform'], (False, False))
        self.assertIn('direct Freeform', self.names())


class FeedRungTests(_Case):
    """dev/docs/BUGS.md 2026-10-02 @ 07:33:49 AM - a copy the provider dropped kept the live copy
    folded behind it, because the keep rule never asked whether a copy was still listed."""

    def setUp(self):
        super().setUp()
        self.link(self.curated, self.direct)
        long_ago = self.now - timedelta(days=30)
        # The lower id, so it wins on every other rung - and the provider dropped it.
        self.gone = self.channel(self.curated, 7, name='dropped copy', last_seen_at=long_ago)
        self.live = self.channel(self.direct, 7, name='live copy')
        db.session.commit()

    def test_a_missing_copy_loses_to_one_still_in_the_feed(self):
        self.fold()
        self.assertEqual(self.losers(), ['dropped copy'])
        self.assertEqual(self.names(), ['live copy'])

    def test_the_badge_says_the_feed_decided_it(self):
        self.fold()
        state = SearchState(standing=only_hiding(), facets=())
        ctx = SearchContext.build()
        rows = {r['name']: r for r in build_rows(search(state, ctx), state, ctx)}
        self.assertEqual(rows['live copy']['dup']['kept_id'], self.live.id)
        self.assertEqual(rows['live copy']['dup']['kept_reason'], KEEP_REASON_IN_FEED)

    def test_hidden_still_outranks_the_feed(self):
        """A hidden channel must never win a cluster, even against a copy that is gone -
        the visible one is the only one a search can show."""
        self.fold()
        channel_hiding.set_hidden_override(self.live, True)
        channel_hiding.recompute([self.live.id])
        db.session.commit()
        self.assertEqual(self.losers(), ['live copy'])

    def test_two_missing_copies_fall_through_to_the_next_rung(self):
        self.live.last_seen_at = self.now - timedelta(days=30)
        db.session.commit()
        self.fold()
        self.assertEqual(self.losers(), ['live copy'])


class StoredAnswerStaysCurrentTests(_Case):
    """Each test changes ONE input the keep rule reads, the way the app changes it, and
    never calls recompute() afterward - a hook that does not fire is the thing being looked
    for."""

    def setUp(self):
        super().setUp()
        self.link(self.curated, self.direct)
        self.first = self.channel(self.curated, 7, name='first')
        self.second = self.channel(self.direct, 7, name='second')
        self.fold()
        self.assertEqual(self.losers(), ['second'])

    def test_a_health_score_write_re_ranks(self):
        self.second.health_score = 90.0
        db.session.commit()
        self.assertEqual(self.losers(), ['first'])

    def test_a_manual_adjustment_re_ranks(self):
        self.first.health_score = self.second.health_score = 50.0
        db.session.commit()
        self.assertEqual(self.losers(), ['second'])
        self.second.manual_health_adjustment = 10
        db.session.commit()
        self.assertEqual(self.losers(), ['first'])

    def test_a_guide_flag_write_re_ranks(self):
        self.second.in_guide = True
        db.session.commit()
        self.assertEqual(self.losers(), ['first'])

    def test_a_group_membership_re_ranks_both_ways(self):
        grp = seed.make_group(name='G', members=[self.second])
        db.session.commit()
        self.assertEqual(self.losers(), ['first'])
        for member in ChannelGroupMember.query.filter_by(group_id=grp.id).all():
            db.session.delete(member)
        db.session.commit()
        self.assertEqual(self.losers(), ['second'])

    def test_hiding_the_kept_copy_re_ranks(self):
        channel_hiding.set_hidden_override(self.first, True)
        channel_hiding.recompute([self.first.id])
        db.session.commit()
        self.assertEqual(self.losers(), ['first'])
        self.assertEqual(self.names(), ['second'])

    def test_a_whole_table_hide_pass_re_ranks(self):
        """A rule edit recomputes hidden for every channel in one UPDATE the session never
        sees; that pass has to refold too."""
        channel_hiding.set_hidden_override(self.first, True)
        channel_hiding.recompute()
        db.session.commit()
        self.assertEqual(self.losers(), ['first'])

    def test_a_cluster_always_keeps_exactly_one_copy(self):
        for score in (10.0, 95.0, 40.0):
            self.second.health_score = score
            self.first.health_score = 50.0
            db.session.commit()
            self.assertEqual(len(self.losers()), 1)

    def test_the_kept_reason_follows_the_re_rank(self):
        state = SearchState(standing=only_hiding(), facets=())

        def reason():
            ctx = SearchContext.build()
            rows = {r['name']: r for r in build_rows(search(state, ctx), state, ctx)}
            return rows['first']['dup']['kept_reason']

        self.assertEqual(reason(), KEEP_REASON_ID)
        self.second.health_score = 90.0
        db.session.commit()
        self.assertEqual(reason(), KEEP_REASON_HEALTH)

    def test_a_channel_in_no_cluster_costs_no_write(self):
        lone = self.channel(self.other, 99, name='lone')
        db.session.commit()
        lone.health_score = 70.0
        db.session.commit()
        self.assertEqual(self.losers(), ['second'])


class WhereTheFoldIsOffTests(_Case):
    """§8.4 and §8.5. Both searches keep "Show duplicates" off, which is the point: the
    option still folds URL duplicates there."""

    def setUp(self):
        super().setUp()
        self.link(self.curated, self.direct)
        self.channel(self.curated, 7, name='provider kept')
        self.channel(self.direct, 7, name='provider copy')
        self.channel(self.other, 8, host='shared.example', name='url kept')
        self.channel(self.other, 8, host='shared.example', name='url copy')
        self.group = seed.make_group(name='Backups', members=[])
        self.fold()
        for ch in Channel.query.all():
            seed.make_epg_entry(ch, title=f'Show on {ch.name}')
        db.session.commit()

    def test_the_channel_search_folds_both_kinds(self):
        self.assertEqual(self.names(), ['provider kept', 'url kept'])

    def test_the_group_picker_shows_provider_copies_and_folds_url_copies(self):
        self.assertEqual(self.names(add_to_group=self.group.id),
                         ['provider copy', 'provider kept', 'url kept'])

    def test_the_picker_calls_no_provider_copy_the_kept_one(self):
        """Nothing was folded away from that cluster there, so KEPT would be a claim about
        a choice the list did not make."""
        state = SearchState(standing=only_hiding('showdup'), facets=(),
                            add_to_group=self.group.id)
        result = search(state, SearchContext.build())
        kept = sorted(r.name for r in result.rows if r.id in result.kept_ids)
        self.assertEqual(kept, ['url kept'])

        plain = SearchState(standing=only_hiding('showdup'), facets=())
        result = search(plain, SearchContext.build())
        kept = sorted(r.name for r in result.rows if r.id in result.kept_ids)
        self.assertEqual(kept, ['provider kept', 'url kept'])

    def test_the_airing_grain_searches_every_guide(self):
        state = SearchState(grain=GRAIN_AIRINGS, facets=(),
                            standing=only_hiding('showdup'))
        titles = sorted(e.title for e in search(state, SearchContext.build()).rows)
        self.assertEqual(titles, ['Show on provider copy', 'Show on provider kept',
                                  'Show on url kept'])

    def test_the_picker_endpoint_carries_the_same_answer(self):
        from tests.support.search import unfolded_query
        resp = self.client.get(
            f'/api/channels/search?add_to_group={self.group.id}&{unfolded_query()}')
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        names = sorted(r['name'] for r in resp.get_json()['rows'] if r['kind'] == 'channel')
        self.assertEqual(names, ['provider copy', 'provider kept', 'url kept'])


if __name__ == '__main__':
    unittest.main()
