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
  - FoldFollowsTheResultsTests, FoldFollowsTheResultsOnAiringsTests,
    AddBackRespectsGroupCollapseTests, FacetsAgreeWithTheirFilterTests: a duplicate is hidden only while its kept copy is in the
    results, and the count line and the facets say the same (dev/docs/BUGS.md 2026-11-04).

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
    GRAIN_AIRINGS, GRAIN_CHANNELS, GROUP_ANY, OTHER_DUP_URL, OTHER_PROVIDER, DimensionFilter,
    SearchContext, SearchState, search, search_counts, search_facets)
from app.channel_search_rows import (  # noqa: E402
    KEEP_REASON_HEALTH, KEEP_REASON_ID, KEEP_REASON_IN_FEED, build_rows)
from app.accounts import NORM_DISABLED, NORM_MPEGTS  # noqa: E402
from app.database import (  # noqa: E402
    Account, Channel, ChannelGroupMember, EPGEntry, Tag, TagPattern)
from tests.support import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.search import only_hiding  # noqa: E402


class _Case(unittest.TestCase):
    """skyline-curated and skyline-direct reach one backend through different hosts;
    harbor-direct is unrelated. Nothing is on a provider until a test says so."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
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


def _cat(*values):
    return (DimensionFilter('cat', tuple(values)),)


class FoldFollowsTheResultsTests(_Case):
    """dev/docs/BUGS.md 2026-11-04. The provider listed NHL Network twice at one URL: the guide
    copy was kept across the whole table, a category filter removed it, and the other copy was
    still hidden, so the channel appeared in neither."""

    def setUp(self):
        super().setUp()
        self.kept = self.channel(self.other, 1, host='shared.example', name='NHL Network',
                                 category_name='TV Guide', in_guide=True)
        self.copy = self.channel(self.other, 1, host='shared.example', name='US NHL Network',
                                 category_name='NHL Package')
        self.bystander = self.channel(self.other, 2, name='Hockey Night',
                                      category_name='NHL Package')
        self.fold()

    def result(self, **kw):
        kw.setdefault('standing', only_hiding('showdup'))
        kw.setdefault('facets', ())
        return search(SearchState(**kw), SearchContext.build())

    def test_a_filter_that_removes_the_kept_copy_brings_the_other_back(self):
        self.assertEqual(self.names(filters=_cat('NHL Package')),
                         ['Hockey Night', 'US NHL Network'])

    def test_the_count_line_follows_the_rows(self):
        """"1 duplicates" beside a list that does not show the kept copy is the reported bug."""
        result = self.result(filters=_cat('NHL Package'))
        self.assertEqual(result.total, 2)
        self.assertEqual(result.standing_hidden, {})

    def test_with_the_kept_copy_out_every_other_copy_shows(self):
        """None of them was the one chosen, so none of them folds into another."""
        self.channel(self.other, 1, host='shared.example', name='Third copy',
                     category_name='NHL Package')
        self.fold()
        result = self.result(filters=_cat('NHL Package'))
        self.assertEqual(sorted(r.name for r in result.rows),
                         ['Hockey Night', 'Third copy', 'US NHL Network'])
        self.assertEqual(result.standing_hidden, {})

    def test_the_kept_copy_in_the_results_still_folds_the_other(self):
        result = self.result()
        self.assertEqual(sorted(r.name for r in result.rows), ['Hockey Night', 'NHL Network'])
        self.assertEqual(result.standing_hidden, {'showdup': 1})

    def test_a_filter_both_copies_pass_still_folds(self):
        self.copy.category_name = 'TV Guide'
        db.session.commit()
        self.assertEqual(self.names(filters=_cat('TV Guide')), ['NHL Network'])

    def test_an_account_filter_that_removes_the_kept_copy_brings_the_other_back(self):
        self.link(self.curated, self.direct)
        self.channel(self.curated, 7, name='Curated copy', in_guide=True)
        self.channel(self.direct, 7, name='Direct copy')
        self.fold()
        by_account = (DimensionFilter('acct', (str(self.direct.id),)),)
        self.assertEqual(self.names(filters=by_account), ['Direct copy'])

    def test_typed_text_that_misses_the_kept_copy_brings_the_other_back(self):
        """Names differ on most hidden copies on the production database, so a search for one
        found nothing and reported a duplicate of something it did not show."""
        self.assertEqual(self.names(q='"US NHL"'), ['US NHL Network'])

    def test_typed_text_matching_both_copies_still_folds(self):
        result = self.result(q='Network')
        self.assertEqual([r.name for r in result.rows], ['NHL Network'])
        self.assertEqual(result.standing_hidden, {'showdup': 1})

    def test_which_copy_is_kept_does_not_move(self):
        self.names(filters=_cat('NHL Package'))
        self.assertEqual(self.losers(), ['US NHL Network'])
        result = self.result(filters=_cat('NHL Package'))
        self.assertEqual(result.kept_ids, frozenset())

    def test_a_standing_option_that_removes_the_kept_copy_brings_the_other_back(self):
        """"Show channels with no EPG data" off takes out a kept copy with no guide. Hiding its
        duplicate as well, which has one, would lose the channel."""
        seed.make_epg_entry(self.copy, title='Hockey')
        db.session.commit()
        names = self.names(standing=only_hiding('showdup', 'shownoepg'))
        self.assertEqual(names, ['US NHL Network'])

    def test_not_normalized_takes_a_kept_copy_only_where_accounts_differ(self):
        """One account normalizes and one does not, so the option can take the kept copy and leave
        the other. Where both do, it takes the pair, which the unfiltered page relies on to skip
        the check."""
        hiding = only_hiding('showdup', 'shownotnorm')
        normalizes = seed.make_account(name='normalizes', url_normalization=NORM_MPEGTS,
                                       last_sync_at=self.now)
        leaves_alone = seed.make_account(name='leaves-alone', url_normalization=NORM_DISABLED,
                                         last_sync_at=self.now)
        db.session.commit()
        kept = self.channel(normalizes, 9, host='raw.example', name='Raw kept', in_guide=True,
                            url_normalizable=False)
        copy = self.channel(leaves_alone, 9, host='raw.example', name='Raw copy',
                            url_normalizable=False)
        kept.provider_stream_id = copy.provider_stream_id = None
        self.fold()
        self.assertEqual(self.losers().count('Raw copy'), 1)
        self.assertIn('Raw copy', self.names(standing=hiding))

        Account.query.update({'url_normalization': NORM_MPEGTS})
        db.session.commit()
        names = self.names(standing=hiding)
        self.assertNotIn('Raw copy', names)
        self.assertNotIn('Raw kept', names)

    def test_a_kept_copy_folded_into_its_group_is_still_there(self):
        """The group's row stands for it, so its duplicate stays hidden."""
        seed.make_group(name='NHL', members=[self.kept])
        self.kept.in_guide = False
        db.session.commit()
        self.fold()
        names = self.names(standing=only_hiding('showdup', 'showmembers'))
        self.assertEqual(names, ['Hockey Night'])

    def test_show_duplicates_ticked_shows_every_copy_as_before(self):
        self.assertEqual(self.names(standing=only_hiding()),
                         ['Hockey Night', 'NHL Network', 'US NHL Network'])

    def test_the_row_says_why_it_is_on_the_page(self):
        """Principle 1: a copy the option would normally hide says why it is here. The kept copy
        leads `others`, which is what the tooltip names."""
        state = SearchState(standing=only_hiding('showdup'), facets=(), filters=_cat('NHL Package'))
        ctx = SearchContext.build()
        rows = {r['name']: r for r in build_rows(search(state, ctx), state, ctx)}
        dup = rows['US NHL Network']['dup']
        self.assertTrue(dup['kept_absent'])
        self.assertEqual(dup['others'][0]['id'], self.kept.id)
        self.assertEqual(dup['kept_id'], self.kept.id)

    def test_the_row_makes_no_such_claim_when_nothing_is_being_hidden(self):
        state = SearchState(standing=only_hiding(), facets=())
        ctx = SearchContext.build()
        rows = {r['name']: r for r in build_rows(search(state, ctx), state, ctx)}
        self.assertFalse(rows['US NHL Network']['dup']['kept_absent'])
        self.assertFalse(rows['NHL Network']['dup']['kept_absent'])

    def test_the_row_makes_no_such_claim_for_a_provider_copy_left_unfolded(self):
        """The airing grain never hides these, so being there says nothing about the kept copy."""
        self.link(self.curated, self.direct)
        kept = self.channel(self.curated, 7, name='Curated copy', in_guide=True)
        copy = self.channel(self.direct, 7, name='Direct copy')
        self.fold()
        seed.make_epg_entry(kept, title='Show')
        seed.make_epg_entry(copy, title='Show')
        db.session.commit()
        state = SearchState(grain=GRAIN_AIRINGS, standing=only_hiding('showdup'), facets=())
        ctx = SearchContext.build()
        rows = build_rows(search(state, ctx), state, ctx)
        dups = {r['channel']['name']: r['channel']['dup'] for r in rows
                if r['channel']['name'] in ('Curated copy', 'Direct copy')}
        self.assertEqual(sorted(dups), ['Curated copy', 'Direct copy'])
        self.assertFalse(dups['Direct copy']['kept_absent'])


class FoldFollowsTheResultsOnAiringsTests(_Case):
    """The same rule on Programs, where being in the results means having a showing in them."""

    def setUp(self):
        super().setUp()
        self.kept = self.channel(self.other, 1, host='shared.example', name='NHL Network',
                                 category_name='TV Guide', in_guide=True)
        self.copy = self.channel(self.other, 1, host='shared.example', name='NHL Network copy',
                                 category_name='NHL Package')
        self.fold()

    def rows(self, **kw):
        kw.setdefault('standing', only_hiding('showdup'))
        kw.setdefault('facets', ())
        state = SearchState(grain=GRAIN_AIRINGS, **kw)
        result = search(state, SearchContext.build())
        return result, sorted((e.channel_id, e.title) for e in result.rows)

    def both_carry(self, title='Flyers at Penguins'):
        seed.make_epg_entry(self.kept, title=title)
        seed.make_epg_entry(self.copy, title=title)
        db.session.commit()

    def test_a_listing_on_both_copies_is_found_with_the_kept_copy_filtered_out(self):
        """The reported search: the listing exists twice and appeared on neither."""
        self.both_carry()
        result, rows = self.rows(filters=_cat('NHL Package'))
        self.assertEqual(rows, [(self.copy.id, 'Flyers at Penguins')])
        self.assertEqual(result.total, 1)
        self.assertEqual(result.channels_matched, 1)
        self.assertEqual(result.standing_hidden, {})

    def test_a_listing_on_both_copies_shows_once_with_no_filter(self):
        self.both_carry()
        result, rows = self.rows()
        self.assertEqual(rows, [(self.kept.id, 'Flyers at Penguins')])
        self.assertEqual(result.standing_hidden, {'showdup': 1})

    def test_a_kept_copy_with_no_guide_does_not_hide_the_one_with_a_guide(self):
        """Nothing filtered at all. 96 of the channels this grain folds are in this shape on the
        production database, which hid 4,424 listings."""
        seed.make_epg_entry(self.copy, title='Only here')
        db.session.commit()
        result, rows = self.rows()
        self.assertEqual(rows, [(self.copy.id, 'Only here')])
        self.assertEqual(result.standing_hidden, {})

    def test_a_kept_copy_whose_showings_have_all_ended_does_not_hide_the_other(self):
        seed.make_epg_entry(self.kept, title='Earlier', offset_minutes=-300)
        seed.make_epg_entry(self.copy, title='Later', offset_minutes=60)
        db.session.commit()
        _result, rows = self.rows(standing=only_hiding('showdup', 'showpast'))
        self.assertEqual(rows, [(self.copy.id, 'Later')])

    def test_typed_text_only_the_other_copy_matches_is_found(self):
        seed.make_epg_entry(self.kept, title='Something else')
        seed.make_epg_entry(self.copy, title='Flyers at Penguins')
        db.session.commit()
        _result, rows = self.rows(q='Flyers')
        self.assertEqual(rows, [(self.copy.id, 'Flyers at Penguins')])

    def test_the_counts_only_endpoint_says_the_same_as_the_rows(self):
        self.both_carry()
        state = SearchState(grain=GRAIN_AIRINGS, standing=only_hiding('showdup'),
                            filters=_cat('NHL Package'))
        hidden, total, _pages, _groups, matched = search_counts(state, SearchContext.build())
        self.assertEqual((hidden, total, matched), ({}, 1, 1))


class AddBackRespectsGroupCollapseTests(_Case):
    """A copy added back to a facet count still has to survive "Collapse channel groups". With a
    filter on another dimension the collapse ranks over the same rows either way, so the facet
    can be held to the list's own total."""

    def setUp(self):
        super().setUp()
        kept = self.channel(self.other, 1, host='a.example', name='Kept', category_name='Guide',
                            in_guide=True)
        self.copy = self.channel(self.other, 1, host='a.example', name='Copy',
                                 category_name='Package', health_score=30.0)
        self.member = self.channel(self.other, 2, name='Member', category_name='Package',
                                   health_score=60.0)
        seed.make_group(name='Feeds', members=[self.copy, self.member])
        db.session.commit()
        self.fold()
        for ch in (kept, self.copy, self.member):
            seed.make_epg_entry(ch, title='The game', start_time=self.now + timedelta(hours=1))
        db.session.commit()

    def counts(self):
        state = SearchState(grain=GRAIN_AIRINGS, filters=_cat('Package'),
                            standing=only_hiding('showdup', 'grpdedup'))
        ctx = SearchContext.build()
        result = search(state, ctx)
        return result.total, sum(search_facets(state, SearchContext.build())['acct'].values())

    def test_a_copy_the_collapse_folds_is_not_added_back(self):
        total, facet = self.counts()
        self.assertEqual(total, 1)
        self.assertEqual(facet, 1)

    def test_a_copy_the_collapse_keeps_is_added_back(self):
        self.copy.health_score = 90.0
        db.session.commit()
        self.fold()
        total, facet = self.counts()
        self.assertEqual(total, 1)
        self.assertEqual(facet, 1)
        state = SearchState(grain=GRAIN_AIRINGS, filters=_cat('Package'),
                            standing=only_hiding('showdup', 'grpdedup'))
        rows = search(state, SearchContext.build()).rows
        self.assertEqual([e.channel_id for e in rows], [self.copy.id])


class FacetsAgreeWithTheirFilterTests(_Case):
    """A facet count says what picking that value gives. On the production database the obvious
    count read 11,925 for an account whose filter returned 14,285 (dev/docs/BUGS.md 2026-11-04)."""

    GRAIN_DIMENSIONS = {
        GRAIN_CHANNELS: ('acct', 'cat', 'health', 'other', 'group', 'tag'),
        GRAIN_AIRINGS: ('acct', 'cat', 'health', 'other', 'group', 'tag', 'when', 'dow'),
    }

    def setUp(self):
        super().setUp()
        self.link(self.curated, self.direct)
        make = self.channel
        # A URL pair split across categories and health bands.
        make(self.other, 1, host='a.example', name='Hockey kept', category_name='Guide',
             in_guide=True, health_score=95.0)
        copy = make(self.other, 1, host='a.example', name='Hockey copy', category_name='Package',
                    health_score=30.0)
        # A URL pair in one category, which folds whichever way it is filtered.
        make(self.other, 2, host='b.example', name='Soccer kept', category_name='Package',
             in_guide=True, health_score=90.0)
        make(self.other, 2, host='b.example', name='Soccer copy', category_name='Package',
             health_score=90.0)
        # A pair across two accounts, keyed by the provider, whose kept copy is in a group its
        # duplicate is not, and the reverse.
        linked_kept = make(self.curated, 7, name='Linked kept', category_name='Guide',
                           in_guide=True)
        linked_copy = make(self.direct, 7, name='Linked copy', category_name='Other')
        grouped_kept = make(self.curated, 8, name='Grouped kept', category_name='Guide',
                            in_guide=True)
        grouped_copy = make(self.direct, 8, name='Grouped copy', category_name='Guide')
        # A copy missing from the feed, so the Status facet has a value only it carries.
        missing_kept = make(self.other, 3, host='c.example', name='Fading kept',
                            category_name='Package')
        make(self.other, 3, host='c.example', name='Fading copy', category_name='Package',
             last_seen_at=self.now - timedelta(days=30))
        make(self.other, 4, name='Alone', category_name='Package', health_score=60.0)
        seed.make_group(name='Both', members=[grouped_kept, grouped_copy])
        seed.make_group(name='Only copy', members=[copy, linked_copy])
        seed.make_group(name='Only kept', members=[linked_kept], in_guide=False)
        tag = Tag(name='hockey-tag')
        db.session.add(tag)
        db.session.flush()
        db.session.add(TagPattern(tag_id=tag.id, pattern='Hockey'))
        db.session.commit()
        self.fold()
        for ch in Channel.query.all():
            title = f'Hockey on {ch.name}' if 'Linked' in ch.name else f'Show on {ch.name}'
            seed.make_epg_entry(ch, title=title, offset_minutes=-10)
            seed.make_epg_entry(ch, title=title, offset_minutes=120)
        # The kept copy of this cluster has no guide at all.
        EPGEntry.query.filter_by(channel_id=missing_kept.id).delete()
        db.session.commit()

    def total_for(self, state, key, value):
        narrowed = [f for f in state.filters if f.key != key] + [DimensionFilter(key, (value,))]
        picked = SearchState(grain=state.grain, q=state.q, standing=state.standing,
                             filters=tuple(narrowed), facets=())
        return search(picked, SearchContext.build()).channel_total

    def assert_facets_agree(self, **kw):
        kw.setdefault('standing', only_hiding('showdup'))
        grain = kw.setdefault('grain', GRAIN_CHANNELS)
        state = SearchState(**kw)
        facets = search_facets(state, SearchContext.build())
        checked = 0
        for key in self.GRAIN_DIMENSIONS[grain]:
            for value, count in facets[key].items():
                with self.subTest(grain=grain, dimension=key, value=value):
                    self.assertEqual(count, self.total_for(state, key, value))
                checked += 1
        self.assertGreater(checked, 10)

    def test_the_channel_facets_equal_what_picking_the_value_gives(self):
        self.assert_facets_agree()

    def test_the_airing_facets_equal_what_picking_the_value_gives(self):
        self.assert_facets_agree(grain=GRAIN_AIRINGS)

    def test_they_agree_with_a_filter_on_another_dimension(self):
        self.assert_facets_agree(filters=(DimensionFilter('health', ('untested',)),))

    def test_they_agree_with_a_filter_on_their_own_dimension(self):
        self.assert_facets_agree(filters=_cat('Package'))

    def test_they_agree_on_airings_with_the_past_hidden(self):
        self.assert_facets_agree(grain=GRAIN_AIRINGS, standing=only_hiding('showdup', 'showpast'))

    def test_they_agree_on_the_airing_grain_with_a_filter(self):
        self.assert_facets_agree(grain=GRAIN_AIRINGS, filters=_cat('Guide'))

    def test_they_agree_with_typed_text(self):
        self.assert_facets_agree(q='copy')

    def test_they_agree_on_airings_with_typed_text(self):
        self.assert_facets_agree(grain=GRAIN_AIRINGS, q='Hockey')

    def test_they_agree_with_another_option_hiding_rows(self):
        self.assert_facets_agree(standing=only_hiding('showdup', 'showuntested'))

    def test_the_any_group_count_is_there_and_is_not_a_group_named_any(self):
        facets = search_facets(SearchState(standing=only_hiding('showdup')),
                               SearchContext.build())['group']
        self.assertEqual(facets[GROUP_ANY], self.total_for(
            SearchState(standing=only_hiding('showdup')), 'group', GROUP_ANY))
        self.assertGreater(facets[GROUP_ANY], 0)

    def test_a_blank_category_is_not_a_facet_value(self):
        """The plain counts drop it, so the add-back has to as well."""
        Channel.query.filter_by(name='Hockey copy').update({'category_name': ''})
        db.session.commit()
        facets = search_facets(SearchState(standing=only_hiding('showdup')),
                               SearchContext.build())
        self.assertNotIn('', facets['cat'])

    def test_a_time_of_day_or_length_count_includes_a_copy_with_no_kept_guide(self):
        """These two count only the value in the state, so the sweep above never reaches them.
        Fading copy's kept copy has no guide, so its showings count."""
        for key, value in (('tod', 'tod:00:00..23:59'), ('duration', 'dur:30..')):
            with self.subTest(dimension=key):
                state = SearchState(grain=GRAIN_AIRINGS, standing=only_hiding('showdup'),
                                    filters=(DimensionFilter(key, (value,)),))
                facets = search_facets(state, SearchContext.build())
                self.assertEqual(facets[key][value], self.total_for(state, key, value))

    def test_show_duplicates_ticked_adds_nothing_back(self):
        """The counts are of every copy already, so adding the hidden ones back would double them."""
        facets = search_facets(SearchState(standing=only_hiding()), SearchContext.build())
        self.assertEqual(sum(facets['cat'].values()), Channel.query.count())


if __name__ == '__main__':
    unittest.main()
