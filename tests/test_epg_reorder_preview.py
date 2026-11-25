"""The reorder preview: what a change to an EPG order would do, worked out before it happens
(DESIGN-epg-sources.md §15.3, dev/changelog/1723).

Every test that names an action runs its preview, then the action itself, and holds the two
equal: which channels' guide would start with a different source, which would be continued
by different sources, and which would end sooner. The preview is the same resolver and the same placement run as a dry run,
so a difference here is the preview judging a second way.

  - AccountOrderTests: Move up / Move down and Stop using on one account; the listing
    comparison for switched channels; nothing written; already first is no change.
  - ProviderOrderTests: a provider list move, Follow again, and joining a provider - with a
    source the account does not read yet read through the donor its copy would use.
  - LinesTests: the sentences say what the comparison can stand behind, never that the
    programs change; a program named another way and a placeholder are counted
    (dev/changelog/1725).
  - RouteTests: the preview routes answer and write nothing.

No network: every feed is a local byte string (CLAUDE.md §Testing).

    python3 -m unittest tests.test_epg_reorder_preview
"""
import os
import sys
import unittest
from collections import Counter
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app import account_links as links  # noqa: E402
from app import epg_sources as es  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import (Account, Channel, EPGEntry, EpgAlternateEntry, EpgChannelStretch,  # noqa: E402
                          EpgSource, EpgSourceSubscription, ProviderEpgOrder)
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402

CFG = {'sync': {'epg_collapse_threshold_percent': 20}}


def _slots(xml_id, first, last, title, sub=False, desc=False, minutes=30, start=60):
    """Slots `first`..`last` of one channel: slot n is '<title> n', `start` + n * `minutes`
    minutes from the base. Two sources given the same title list the same program."""
    return [(xml_id, start + n * minutes, minutes, f'{title} {n}',
             f'Ep {n}' if sub else None, f'About {title} {n}' if desc else None)
            for n in range(first, last + 1)]


def _feed(base, listings):
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for xml_id, offset, dur, title, sub, desc in listings:
        start = base + timedelta(minutes=offset)
        stop = start + timedelta(minutes=dur)
        parts.append(f'<programme start="{start:%Y%m%d%H%M%S} +0000" '
                     f'stop="{stop:%Y%m%d%H%M%S} +0000" channel="{xml_id}">'
                     f'<title>{title}</title>'
                     + (f'<sub-title>{sub}</sub-title>' if sub else '')
                     + (f'<desc>{desc}</desc>' if desc else '') + '</programme>')
    parts.append('</tv>')
    return ''.join(parts).encode()


class _Rig(unittest.TestCase):

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.app.test_client()
        self.base = datetime.utcnow().replace(second=0, microsecond=0)

    def tearDown(self):
        self.t.cleanup()

    def _url_source(self, account, name, priority):
        src = EpgSource(kind='url', owner_account_id=account.id, name=name,
                        url=f'http://{name.lower()}.test/epg.xml')
        db.session.add(src)
        db.session.flush()
        db.session.add(EpgSourceSubscription(source_id=src.id, account_id=account.id,
                                             priority=priority))
        db.session.commit()
        return src

    def _import(self, source_id, listings):
        import_source(db.session.get(EpgSource, source_id), _feed(self.base, listings),
                      epg_days=3, cfg=CFG)
        db.session.expire_all()

    def _state(self, channel_ids):
        """({channel: first source}, {channel: last stop in epg_entries},
        {channel: [the sources continuing its guide]})."""
        db.session.expire_all()
        first = {cid: db.session.get(Channel, cid).epg_source_id for cid in channel_ids}
        ends = {cid: db.session.query(db.func.max(EPGEntry.stop_time)).filter(
            EPGEntry.channel_id == cid).scalar() for cid in channel_ids}
        return first, ends, es.filling_sources(channel_ids)

    def _actual(self, before, after):
        """What the action did, counted the way the preview counts it."""
        (f0, e0, x0), (f1, e1, x1) = before, after
        changes = Counter((f0[c], f1[c]) for c in f0 if f0[c] != f1[c])
        refilled = sum(1 for c in f0 if f0[c] == f1[c] and f0[c] is not None
                       and x0.get(c, []) != x1.get(c, []))
        shorter = sum(1 for c in f0 if f1[c] is not None and e0[c] is not None
                      and e1[c] is not None and e1[c] < e0[c])
        return dict(changes), refilled, shorter

    def _assert_predicts(self, trial, action, channel_ids):
        preview = es.preview_orders(trial.trials, False)
        before = self._state(channel_ids)
        action()
        changes, refilled, shorter = self._actual(before, self._state(channel_ids))
        self.assertEqual(preview.changes, changes, 'the channels whose guide starts elsewhere')
        self.assertEqual(preview.refilled, refilled, 'the guides continued by other sources')
        self.assertEqual(preview.shorter, shorter, 'the guides that end sooner')
        return preview

    def _fingerprint(self):
        """Everything an order change writes."""
        db.session.expire_all()
        return (sorted((r.channel_id, r.source_id, r.start_time) for r in EPGEntry.query),
                sorted((r.channel_id, r.source_id, r.start_time) for r in EpgAlternateEntry.query),
                sorted((r.channel_id, r.source_id, r.outcome) for r in EpgChannelStretch.query),
                sorted((s.account_id, s.source_id, s.priority) for s in EpgSourceSubscription.query),
                sorted((c.id, c.epg_source_id) for c in Channel.query),
                sorted((e.provider_id, e.position, e.entry, e.source_id)
                       for e in ProviderEpgOrder.query))


class _OneAccount(_Rig):
    """Alpha reads its own guide (`src`, first) and `ext`. The channels:
      c1 - both list the same programs; src describes them, ext gives episode titles and
           runs twice as long, so ext continues c1's guide;
      c2 - the two list unrelated programs;
      c3 - only src lists it;
      c6 - the same programs, ext running 60 hours, so the fill reaches days further."""

    def setUp(self):
        super().setUp()
        self.acct = seed.make_account(name='Alpha')
        self.src = seed.make_epg_source(self.acct)
        db.session.commit()
        self.ext = self._url_source(self.acct, 'External', 2)
        self.c1 = seed.make_channel(self.acct, name='One', epg_channel_id='c1.test').id
        self.c2 = seed.make_channel(self.acct, name='Two', epg_channel_id='c2.test').id
        self.c3 = seed.make_channel(self.acct, name='Three', epg_channel_id='c3.test').id
        self.c6 = seed.make_channel(self.acct, name='Six', epg_channel_id='c6.test').id
        db.session.commit()
        self.src_id, self.ext_id, self.acct_id = self.src.id, self.ext.id, self.acct.id
        self._import(self.src_id, _slots('c1.test', 0, 9, 'Show', desc=True)
                     + _slots('c2.test', 0, 9, 'Show', desc=True)
                     + _slots('c3.test', 0, 9, 'Solo', desc=True)
                     + _slots('c6.test', 0, 9, 'Long', desc=True))
        self._import(self.ext_id, _slots('c1.test', 0, 19, 'Show', sub=True)
                     + _slots('c2.test', 0, 9, 'Other')
                     + _slots('c6.test', 0, 119, 'Long'))
        self.channels = [self.c1, self.c2, self.c3, self.c6]


class AccountOrderTests(_OneAccount):

    def test_moving_a_source_up_is_predicted(self):
        trial = es.move_trial(self.acct_id, self.ext_id, -1)
        p = self._assert_predicts(
            trial, lambda: es.move_source(self.acct_id, self.ext_id, -1, False), self.channels)
        self.assertEqual(p.changes, {(self.src_id, self.ext_id): 3})
        self.assertIsNone(trial.own_order)

    def test_the_switched_channels_listings_are_compared(self):
        p = es.preview_orders(es.move_trial(self.acct_id, self.ext_id, -1).trials, False)
        self.assertEqual((p.same, p.moved, p.different), (20, 0, 10),
                         'c1 and c6 list the same programs, c2 a different title each slot')
        self.assertEqual(p.unaligned, 1, 'c2 lines up nowhere')
        self.assertEqual((p.described_before, p.described_after), (20, 0))
        self.assertEqual((p.episodes_before, p.episodes_after), (0, 10))

    def test_a_program_named_another_way_and_a_placeholder_are_counted(self):
        """dev/changelog/1725: c7's External guide names five programs another way and
        lists the channel's own name in one slot."""
        seed.make_channel(db.session.get(Account, self.acct_id), name='UK: TALKSPORT',
                          epg_channel_id='c7.test')
        db.session.commit()
        ext_c7 = ([(x[0], x[1], x[2], f'The {x[3]} Special', None, None)
                   for x in _slots('c7.test', 0, 4, 'Seven')]
                  + [('c7.test', 60 + 5 * 30, 30, 'talkSPORT', None, None)]
                  + _slots('c7.test', 6, 9, 'Seven'))
        self._import(self.src_id, _slots('c1.test', 0, 9, 'Show', desc=True)
                     + _slots('c2.test', 0, 9, 'Show', desc=True)
                     + _slots('c3.test', 0, 9, 'Solo', desc=True)
                     + _slots('c6.test', 0, 9, 'Long', desc=True)
                     + _slots('c7.test', 0, 9, 'Seven', desc=True))
        self._import(self.ext_id, _slots('c1.test', 0, 19, 'Show', sub=True)
                     + _slots('c2.test', 0, 9, 'Other')
                     + _slots('c6.test', 0, 119, 'Long') + ext_c7)
        p = es.preview_orders(es.move_trial(self.acct_id, self.ext_id, -1).trials, False)
        self.assertEqual((p.same, p.renamed, p.different), (29, 5, 10))
        self.assertEqual((p.placeholder_before, p.placeholder_after, p.only_before), (0, 1, 1))
        text = ' '.join(es.preview_lines(p, {self.src_id: 'Guide', self.ext_id: 'External'}))
        self.assertIn('29 listings are the same program (5 of them named differently)', text)
        self.assertIn("0 listings from the source used now and 1 from the new one are only the "
                      "channel's name, not a program", text)

    def test_a_preview_writes_nothing(self):
        before = self._fingerprint()
        es.preview_orders(es.move_trial(self.acct_id, self.ext_id, -1).trials, False)
        es.preview_orders(es.stop_trial(self.acct_id, self.ext_id).trials, False)
        self.assertEqual(self._fingerprint(), before)

    def test_already_first_is_no_change(self):
        trial = es.move_trial(self.acct_id, self.src_id, -1)
        self.assertTrue(trial.noop)
        self.assertEqual(trial.trials, [])
        self.assertIsNone(es.move_trial(self.acct_id, 9999, -1), 'not a source it reads')

    def test_stopping_a_continuing_source_is_predicted_shorter(self):
        trial = es.stop_trial(self.acct_id, self.ext_id)
        p = self._assert_predicts(
            trial, lambda: es.stop_using_source(self.acct_id, self.ext_id, False), self.channels)
        self.assertEqual(p.changes, {}, 'no guide starts with it')
        self.assertEqual(p.refilled, 2, 'c1 and c6 lose their continuation')
        self.assertEqual(p.shorter, 2)
        self.assertEqual(p.most_hours_shorter, 55, "c6's 60 hours cut to 5")
        self.assertNotEqual(es.preview_lines(p, {}), ["No channel's guide would change."])

    def test_stopping_the_first_source_is_predicted(self):
        trial = es.stop_trial(self.acct_id, self.src_id)
        p = self._assert_predicts(
            trial, lambda: es.stop_using_source(self.acct_id, self.src_id, False), self.channels)
        self.assertEqual(p.changes, {(self.src_id, self.ext_id): 3, (self.src_id, None): 1})
        self.assertEqual(p.lost, 1, 'c3 has no other source')

    def test_a_source_holding_nothing_for_a_channel_never_takes_it(self):
        """c7 is in ext's file but arrived after ext's last import, so ext's directory covers
        it and the database holds none of its listings: stopping src leaves it with no
        guide (reresolve_channels() never moves a channel onto a source holding nothing for
        it), and the preview says so rather than naming ext or keeping src."""
        self._import(self.ext_id, _slots('c1.test', 0, 19, 'Show', sub=True)
                     + _slots('c2.test', 0, 9, 'Other') + _slots('c6.test', 0, 119, 'Long')
                     + _slots('c7.test', 0, 9, 'Late'))
        c7 = seed.make_channel(db.session.get(Channel, self.c1).account, name='Seven',
                               epg_channel_id='c7.test').id
        db.session.commit()
        self._import(self.src_id, _slots('c1.test', 0, 9, 'Show', desc=True)
                     + _slots('c2.test', 0, 9, 'Show', desc=True)
                     + _slots('c3.test', 0, 9, 'Solo', desc=True)
                     + _slots('c6.test', 0, 9, 'Long', desc=True)
                     + _slots('c7.test', 0, 9, 'Late', desc=True))
        self.assertEqual(self._state([c7])[0][c7], self.src_id)
        self.assertEqual(EpgAlternateEntry.query.filter_by(channel_id=c7).count(), 0)
        trial = es.stop_trial(self.acct_id, self.src_id)
        p = self._assert_predicts(
            trial, lambda: es.stop_using_source(self.acct_id, self.src_id, False),
            self.channels + [c7])
        self.assertEqual(p.changes[(self.src_id, None)], 2, 'c3 and c7')

    def test_moving_back_keeps_the_continuation(self):
        """With ext first, src's listings end before ext's and continue nothing; moved back
        down, src is first again and ext continues it as far as before - no guide ends
        sooner, which the preview sees only by counting the continuing sources' end."""
        es.move_source(self.acct_id, self.ext_id, -1, False)
        trial = es.move_trial(self.acct_id, self.ext_id, 1)
        p = self._assert_predicts(
            trial, lambda: es.move_source(self.acct_id, self.ext_id, 1, False), self.channels)
        self.assertEqual(p.changes, {(self.ext_id, self.src_id): 3})
        self.assertEqual(p.shorter, 0)


class ProviderOrderTests(_Rig):
    """Alpha (own guide AG, its own `full` second) and Beta (own guide BG). Alpha's a1 reads
    a.test, which AG and full both list with unrelated titles; Alpha's a2 and Beta's b1 read
    b.test, which only full and BG list."""

    def setUp(self):
        super().setUp()
        alpha = seed.make_account(name='Alpha')
        beta = seed.make_account(name='Beta')
        ag, bg = seed.make_epg_source(alpha), seed.make_epg_source(beta)
        db.session.commit()
        full = self._url_source(alpha, 'Full', 2)
        self.a1 = seed.make_channel(alpha, name='A1', epg_channel_id='a.test').id
        self.a2 = seed.make_channel(alpha, name='A2', epg_channel_id='b.test').id
        self.b1 = seed.make_channel(beta, name='B1', epg_channel_id='b.test').id
        db.session.commit()
        self.alpha, self.beta = alpha.id, beta.id
        self.ag, self.bg, self.full = ag.id, bg.id, full.id
        self._import(self.ag, _slots('a.test', 0, 9, 'Guide', desc=True))
        self._import(self.bg, _slots('b.test', 0, 9, 'BGuide'))
        self._import(self.full, _slots('a.test', 0, 19, 'Full') + _slots('b.test', 0, 19, 'Full'))
        self.channels = [self.a1, self.a2, self.b1]

    def _strong(self):
        provider, _ = links.create_provider('Strong', [self.alpha], case_sensitive=False)
        es.move_source_to_provider(self.full, provider.id, 24, False)
        return provider.id

    def _full_entry(self):
        return ProviderEpgOrder.query.filter_by(source_id=self.full).one().id

    def test_creating_a_provider_is_predicted(self):
        es.move_source(self.alpha, self.full, -1, False)
        self.assertEqual(self._state([self.a1])[0][self.a1], self.full)
        trial = es.join_trial([self.alpha], None)
        p = self._assert_predicts(
            trial, lambda: links.create_provider('Strong', [self.alpha], case_sensitive=False),
            self.channels)
        self.assertEqual(p.changes, {(self.full, self.ag): 1}, 'its own guide fills the slot first')

    def test_moving_an_entry_on_the_providers_list_is_predicted(self):
        pid = self._strong()
        trial = es.provider_move_trial(pid, self._full_entry(), -1)
        p = self._assert_predicts(
            trial, lambda: es.move_provider_entry(pid, self._full_entry(), -1, False),
            self.channels)
        self.assertEqual(p.changes, {(self.ag, self.full): 1})
        self.assertTrue(es.provider_move_trial(pid, self._full_entry(), -1).noop)
        self.assertIsNone(es.provider_move_trial(pid, 9999, -1))

    def test_follow_again_is_predicted(self):
        pid = self._strong()
        es.move_source(self.alpha, self.full, -1, False, own_order_ok=True)
        trial = es.follow_trial(self.alpha)
        self.assertIsNotNone(trial)
        p = self._assert_predicts(
            trial, lambda: es.follow_provider_again(self.alpha, False), self.channels)
        self.assertEqual(p.changes, {(self.full, self.ag): 1})
        self.assertIsNotNone(pid)
        self.assertIsNone(es.follow_trial(self.beta), 'Beta is on no provider')

    def test_joining_reads_a_source_it_does_not_read_yet_through_its_donor(self):
        pid = self._strong()
        es.move_provider_entry(pid, self._full_entry(), -1, False)
        self.assertEqual(EpgAlternateEntry.query.filter_by(channel_id=self.b1).count()
                         + EPGEntry.query.filter_by(channel_id=self.b1, source_id=self.full).count(),
                         0, 'Beta holds nothing of full before it joins')
        trial = es.join_trial([self.beta], pid)
        p = self._assert_predicts(
            trial, lambda: links.move_account_provider(self.beta, pid, False), self.channels)
        self.assertEqual(p.changes, {(self.bg, self.full): 1})
        self.assertEqual((p.same, p.different, p.unaligned), (0, 10, 1),
                         "BG's titles against the donor's copy of full")
        self.assertEqual(es.join_trial([self.beta], pid).trials, [], 'already on it')


class LinesTests(unittest.TestCase):

    def _preview(self, **kw):
        counts = {f: 0 for f in es._PREVIEW_COUNTS}
        counts.update(kw.pop('counts', {}))
        return es.OrderPreview(1, kw.pop('changes', {}), *(counts[f] for f in es._PREVIEW_COUNTS))

    def test_nothing_changing_says_so(self):
        self.assertEqual(es.preview_lines(self._preview(), {}), ["No channel's guide would change."])

    def test_a_different_title_is_never_called_a_different_program(self):
        p = self._preview(changes={(1, 2): 3}, counts={
            'same': 20, 'different': 10, 'only_before': 4, 'only_after': 2, 'unaligned': 1,
            'described_before': 20, 'described_after': 5, 'episodes_after': 10})
        text = ' '.join(es.preview_lines(p, {1: 'Guide', 2: 'Full'}))
        self.assertIn('3 channels would start their guide with a different source: 3 from '
                      'Guide to Full.', text)
        self.assertIn('10 have a different title in the same slot (some of those may still be '
                      'one program named two ways)', text)
        self.assertIn('100% have a description now and 25% would after', text)
        self.assertNotIn('different program', text)
        self.assertNotIn('end sooner', text, 'reach is only a warning')

    def test_a_guide_ending_sooner_is_a_warning_when_it_happens(self):
        p = self._preview(counts={'shorter': 2, 'most_hours_shorter': 30})
        self.assertEqual(es.preview_lines(p, {}),
                         ["2 channels' guides would end sooner, by up to 30 hours."])

    def test_a_change_of_continuing_source_alone_is_a_change(self):
        lines = es.preview_lines(self._preview(counts={'refilled': 1}), {})
        self.assertEqual(lines, ['1 channel would keep the source its guide starts with but be '
                                 'continued past its end by different sources.'])

    def test_a_lost_and_a_gained_guide_are_named(self):
        lines = es.preview_lines(self._preview(changes={(1, None): 1, (None, 2): 2}), {})
        self.assertEqual(lines, ['2 channels with no guide now would get one.',
                                 '1 channel would have no guide.'])


class RouteTests(_OneAccount):

    def test_the_move_preview_answers_and_writes_nothing(self):
        before = self._fingerprint()
        r = self.client.get(f'/api/accounts/{self.acct_id}/epg-sources/{self.ext_id}/'
                            'move-preview?direction=up')
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertEqual(body['switched'], 3)
        self.assertFalse(body['noop'])
        self.assertTrue(body['lines'][0].startswith('3 channels would start their guide'))
        self.assertIsNone(body['own_order'])
        self.assertEqual(self._fingerprint(), before)

    def test_bad_requests(self):
        base = f'/api/accounts/{self.acct_id}/epg-sources'
        self.assertEqual(self.client.get(f'{base}/{self.ext_id}/move-preview').status_code, 400)
        self.assertEqual(self.client.get(f'{base}/9999/move-preview?direction=up').status_code, 400)
        self.assertEqual(self.client.get(f'{base}/9999/stop-using-preview').status_code, 400)
        self.assertEqual(self.client.get(
            f'/api/accounts/{self.acct_id}/epg-order/follow-preview').status_code, 400)
        self.assertTrue(self.client.get(
            f'{base}/{self.src_id}/move-preview?direction=up').get_json()['noop'])

    def test_the_stop_preview_and_the_own_order_sentence(self):
        pid, _ = links.create_provider('Strong', [self.acct_id], case_sensitive=False)
        r = self.client.get(f'/api/accounts/{self.acct_id}/epg-sources/{self.src_id}/'
                            'stop-using-preview')
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn('its own EPG order', r.get_json()['own_order'],
                      'stopping its own guide stops following the list')
        r = self.client.get(f'/api/providers/join-preview?account_ids={self.acct_id}')
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.client.get(
            f'/api/providers/{pid.id}/epg-order/9999/move-preview?direction=up').status_code, 404)
        self.assertEqual(self.client.get(
            '/api/providers/join-preview?account_ids=1&provider_id=9999').status_code, 404)
