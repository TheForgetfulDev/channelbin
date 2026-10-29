"""The group page's What's On card (dev/changelog/1324, mockup 46).

On a channel group's page the card lines up every distinct EPG schedule the group's channels
carry under the group's own row - what the TV Guide shows for it - and tints each program by
how it differs, so the EPG banner's "N different schedules" can be looked at instead of only
counted. Each case here is a decision a careless edit would quietly undo:

  * **A schedule is (active source, the file id the channel's key reads)** -
    epg_sources.keys_read(). The key is the user's accepted key, else the provider's id.
    Keys differing only in case are NOT one schedule when the file lists both spellings: prod
    has exactly that (`foxsports2.us` / `FoxSports2.us` on one source, two different feeds,
    dev/changelog/1139), and the mockup's case-folded key would have merged them.
  * **Channels on one id whose listings differ split into rows of their own** - checked,
    not assumed, so a row can never claim a channel it does not describe.
  * **Agreement is the comparator's, never decided twice** - listing_agreement() maps
    compare_listings()' ROW_* kinds, and the card only draws what it was sent.
  * **The reference row is the group's own guide row**, built by the guide's code, in or out
    of the guide; a group nobody records from still gets one and is marked as not recording.
  * **A schedule's cell records from the group** - its program dicts carry the group's
    record target, never the channel the card happened to show.
  * **The endpoint's query count does not grow with the member count** (the per-row-I/O
    rule's mandatory guard).
  * **The card's controls only filter what is drawn**, and its Layout saves under its own
    keys, never the TV Guide's.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_group_whats_on
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.iocount import IOCounter, all_engines  # noqa: E402
from app import db  # noqa: E402
from app import config as config_mod  # noqa: E402
from app.database import (EpgChannelKey, EpgSourceChannel, ChannelGroup,  # noqa: E402
                          EPG_KEY_ACCEPTED, EPG_KEY_ORIGIN_MANUAL)
from app import epg_sources  # noqa: E402
from app.epg_sources import (Listing, keys_read, listing_agreement,  # noqa: E402
                             AGREE_SAME, AGREE_MOVED, AGREE_DIFFERENT, AGREE_ONLY_HERE,
                             AGREE_DOUBLED, AGREE_OUTSIDE, AGREE_PAST, GAP_REFERENCE, GAP_OTHER)
from app.channel_groups import member_schedules  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, 'tests', 'support', 'group_whats_on.mjs')
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')


def _hour(n):
    """A naive-UTC instant n hours past the next whole hour - always in the future."""
    base = datetime.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return base + timedelta(hours=n)


def _window():
    start = datetime.utcnow().replace(second=0, microsecond=0)
    start = start.replace(minute=0 if start.minute < 30 else 30)
    return start, start + timedelta(days=3)


def _epg_url(group_id, schedules=True):
    ws, we = _window()
    url = (f'/api/guide/epg?start={ws:%Y-%m-%dT%H:%M:%S}&end={we:%Y-%m-%dT%H:%M:%S}'
           f'&group_id={group_id}')
    return url + ('&schedules=1' if schedules else '')


# The station's day: (hour offset, minutes, title, description).
BASE = [(0, 60, 'Morning News', 'Headlines.'), (1, 60, 'Talk Show', 'Guests.'),
        (2, 60, 'Judge Show', 'Cases.'), (3, 60, 'Game Show', 'Prizes.'),
        (4, 60, 'Evening News', 'The day.'), (5, 60, 'Late Show', 'Comedy.'),
        (6, 60, 'Movie', 'A film.'), (7, 60, 'Overnight', 'Repeats.')]


def _listings(ch, rows, source_id):
    for h, mins, title, desc in rows:
        seed.make_epg_entry(ch, title=title, description=desc or None, start_time=_hour(h),
                            duration_minutes=mins, source_id=source_id)


def _seed_card_group(in_guide=True, recording=True):
    """A group whose channels carry every verdict the comparator gives, against the
    serving channel's schedule. Returns {name: channel} and the group."""
    a, b, c = (seed.make_account(name=n) for n in ('Harbor', 'Northgate', 'Summit'))
    sa, sb, sc = (seed.make_epg_source(x, name=f'{x.name} guide') for x in (a, b, c))
    chans = {}

    def chan(name, acc, src, eid, health, rows):
        ch = seed.make_channel(acc, name=name, epg_channel_id=eid, health_score=health,
                               epg_source_id=src.id if eid else None)
        if rows is not None:
            _listings(ch, rows, src.id)
        chans[name] = ch
        return ch

    chan('Serving HD', a, sa, 'WNYW.us', 95, BASE)
    chan('Serving SD', a, sa, 'wnyw.us', 60, BASE)                       # case variant
    chan('North HD', b, sb, 'WNYW.us', 90, BASE)                         # identical
    chan('Summit', c, sc, 'FOX5.us', 80, [(h, m, t, None if h in (1, 2) else d)
                                          for h, m, t, d in BASE])       # descriptions
    dt = [x for x in BASE if x[0] != 3]                                  # a missing hour
    dt = [(h, m, 'Infomercial' if h == 2 else t, d) for h, m, t, d in dt]
    dt.append((5, 30, 'Late Show Extra', ''))                           # listed twice
    dt = [(h, 30 if h == 6 else m, t, d) for h, m, t, d in dt]
    dt.append((6.5, 30, 'Local Update', ''))                            # only here
    chan('North alt', b, sb, 'WNYW-DT.us', 70, dt)                       # mostly
    chan('West', c, sc, 'FOX.west.us', 50, [(h + 3, m, t, d) for h, m, t, d in BASE])
    chan('Backup', a, sa, 'FS1.us', 40, [(h, 60, f'Sports {h}', '') for h in range(8)])
    chan('Empty', c, sc, 'EMPTY.us', 30, None)                           # no listings
    chan('No id', c, sc, None, 85, None)
    group = seed.make_group(name='FOX 5', members=list(chans.values()), in_guide=in_guide,
                            recording=recording,
                            disabled={chans['West'].id} if recording else None)
    db.session.commit()
    return chans, group


class _AppCase(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)


# ── keys_read: which schedule a channel's listings are ─────────────────────────

class KeysReadTests(_AppCase):
    def setUp(self):
        super().setUp()
        self.acc = seed.make_account()
        self.src = seed.make_epg_source(self.acc)
        for xml_id, n in (('FoxSports2.us', 57), ('foxsports2.us', 58), ('ESPN.us', 40)):
            db.session.add(EpgSourceChannel(source_id=self.src.id, xml_id=xml_id, entry_count=n))
        db.session.commit()

    def _ch(self, eid, **kw):
        return seed.make_channel(self.acc, name=eid or 'none', epg_channel_id=eid,
                                 epg_source_id=self.src.id, **kw)

    def test_case_variant_spellings_in_the_file_are_two_schedules(self):
        """The prod case (dev/changelog/1139): a file listing both spellings gives each
        its own schedule, and the import reads the exact spelling."""
        lower, upper = self._ch('foxsports2.us'), self._ch('FoxSports2.us')
        db.session.commit()
        reads = keys_read([lower, upper], case_sensitive=False)
        self.assertEqual(reads[lower.id].file_id, 'foxsports2.us')
        self.assertEqual(reads[upper.id].file_id, 'FoxSports2.us')

    def test_a_spelling_the_file_does_not_carry_reads_the_fullest_variant(self):
        odd = self._ch('FOXSPORTS2.us')
        espn = self._ch('espn.us')
        db.session.commit()
        reads = keys_read([odd, espn], case_sensitive=False)
        self.assertEqual(reads[odd.id].file_id, 'foxsports2.us')   # 58 beats 57
        self.assertEqual(reads[espn.id].file_id, 'ESPN.us')

    def test_case_sensitive_matching_folds_nothing(self):
        espn = self._ch('espn.us')
        db.session.commit()
        self.assertIsNone(keys_read([espn], case_sensitive=True)[espn.id].file_id)

    def test_an_accepted_user_key_wins_over_the_provider_id(self):
        ch = self._ch('Wrong.us')
        db.session.flush()
        db.session.add(EpgChannelKey(channel_id=ch.id, source_id=self.src.id, key='ESPN.us',
                                     origin=EPG_KEY_ORIGIN_MANUAL, status=EPG_KEY_ACCEPTED))
        db.session.commit()
        r = keys_read([ch], case_sensitive=False)[ch.id]
        self.assertEqual((r.key, r.file_id), ('ESPN.us', 'ESPN.us'))

    def test_an_id_the_directory_does_not_list_has_no_file_id(self):
        ch = self._ch('Nowhere.us')
        db.session.commit()
        r = keys_read([ch], case_sensitive=False)[ch.id]
        self.assertEqual((r.source_id, r.key, r.file_id), (self.src.id, 'Nowhere.us', None))

    def test_a_channel_with_no_source_keeps_its_id_and_no_source(self):
        ch = seed.make_channel(self.acc, name='orphan', epg_channel_id='X.us', epg_source_id=None)
        db.session.commit()
        r = keys_read([ch], case_sensitive=False)[ch.id]
        self.assertEqual((r.source_id, r.key, r.file_id), (None, 'X.us', None))

    def test_two_queries_whatever_the_channel_count(self):
        for i in range(30):
            self._ch(f'id{i}.us')
        db.session.commit()
        from app.database import Channel
        chans = Channel.query.all()       # loaded before the count, as the route's members are
        with IOCounter(all_engines()) as counter:
            keys_read(chans, case_sensitive=False)
        self.assertEqual(counter.queries, 2)


# ── member_schedules: the collapse ─────────────────────────────────────────────

class MemberSchedulesTests(_AppCase):
    def setUp(self):
        super().setUp()
        self.acc = seed.make_account()
        self.src = seed.make_epg_source(self.acc)

    def _schedules(self, chans, cs=False):
        db.session.commit()
        from app.database import EPGEntry
        by = {}
        for e in EPGEntry.query.order_by(EPGEntry.start_time).all():
            by.setdefault(e.channel_id, []).append(e)
        return member_schedules(chans, keys_read(chans, cs), by, cs)

    def _ch(self, eid, rows=BASE):
        ch = seed.make_channel(self.acc, name=eid or 'none', epg_channel_id=eid,
                               epg_source_id=self.src.id)
        if rows:
            _listings(ch, rows, self.src.id)
        return ch

    def test_case_variants_with_no_directory_fold_together(self):
        """A source never refreshed has no directory, so there is no file id to tell the
        spellings apart; they fold the way the import matches them."""
        a, b = self._ch('WNYW.us'), self._ch('wnyw.us')
        s = self._schedules([a, b])
        self.assertEqual(len(s), 1)
        self.assertEqual(sorted(s[0].spellings), ['WNYW.us', 'wnyw.us'])
        self.assertFalse(s[0].split)

    def test_case_sensitive_matching_keeps_them_apart(self):
        a, b = self._ch('WNYW.us'), self._ch('wnyw.us')
        self.assertEqual(len(self._schedules([a, b], cs=True)), 2)

    def test_two_file_ids_are_two_schedules_however_they_fold(self):
        for xml_id in ('WNYW.us', 'wnyw.us'):
            db.session.add(EpgSourceChannel(source_id=self.src.id, xml_id=xml_id, entry_count=8))
        a, b = self._ch('WNYW.us'), self._ch('wnyw.us')
        self.assertEqual(len(self._schedules([a, b])), 2)

    def test_channels_on_one_id_holding_different_rows_are_split(self):
        a, b = self._ch('WNYW.us'), self._ch('WNYW.us', rows=BASE[:3])
        c = self._ch('WNYW.us')
        s = self._schedules([a, b, c])
        self.assertEqual([len(x.channels) for x in s], [2, 1])
        self.assertTrue(all(x.split for x in s))
        self.assertEqual([ch.id for ch in s[1].channels], [b.id])

    def test_a_channel_with_no_key_carries_no_schedule(self):
        a, none = self._ch('WNYW.us'), self._ch(None, rows=None)
        s = self._schedules([a, none])
        self.assertEqual([[ch.id for ch in x.channels] for x in s], [[a.id]])


# ── listing_agreement: the comparator's rows, per listing ──────────────────────

class ListingAgreementTests(unittest.TestCase):
    NOW = datetime(2026, 10, 28, 12, 0)

    def L(self, h, title, mins=60, desc='d'):
        start = self.NOW + timedelta(hours=h)
        return Listing(title, None, desc, start, start + timedelta(minutes=mins))

    def test_every_kind_on_the_other_side(self):
        ref = [self.L(-2, 'Past'), self.L(0, 'A'), self.L(1, 'B'), self.L(2, 'C', desc=''),
               self.L(3, 'D'), self.L(4, 'E'), self.L(5, 'F')]
        other = [self.L(-2, 'Past'),                    # over before now
                 self.L(0, 'A', desc=''),               # same, described only in the guide
                 self.L(1.5, 'B'),                      # moved 30 minutes
                 self.L(2, 'C'),                        # same, described only here
                 self.L(3, 'Other'),                    # a different program in D's slot
                 self.L(3, 'Twice'),                    # a second listing at D's moment
                 self.L(4.5, 'Extra', mins=30),         # nothing in the guide near it
                 self.L(9, 'Late')]                     # past the stretch both list
        cmp, agree, lacking = listing_agreement(ref, other, self.NOW)
        self.assertEqual([a.kind for a in agree],
                         [AGREE_PAST, AGREE_SAME, AGREE_MOVED, AGREE_SAME, AGREE_DIFFERENT,
                          AGREE_DOUBLED, AGREE_ONLY_HERE, AGREE_OUTSIDE])
        self.assertEqual(agree[1].gap, GAP_REFERENCE)
        self.assertEqual(agree[3].gap, GAP_OTHER)
        self.assertEqual(agree[2].partner.title, 'B')
        self.assertEqual(agree[4].partner.title, 'D')
        # E has no partner but 'Extra' overlaps it, so no outline is drawn over a real
        # listing; F overlaps nothing on this side and is outlined.
        self.assertEqual([x.title for x in lacking], ['F'])

    def test_no_listings_is_no_overlap(self):
        cmp, agree, lacking = listing_agreement([self.L(0, 'A')], [], self.NOW)
        self.assertEqual((cmp.verdict, agree, lacking), (epg_sources.CMP_NO_OVERLAP, [], []))


# ── The endpoint ───────────────────────────────────────────────────────────────

class GroupScheduleEndpointTests(_AppCase):
    def setUp(self):
        super().setUp()
        self.chans, self.group = _seed_card_group()
        resp = self.t.client.get(_epg_url(self.group.id))
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:500])
        self.data = resp.get_json()
        self.rows = self.data['schedules']['rows']
        self.by_id = {}
        for r in self.rows:
            self.by_id.setdefault((r['source_name'], r['epg_id']), r)

    def row(self, source, eid):
        return self.by_id[(source, eid)]

    def test_one_row_the_groups_own(self):
        self.assertEqual([c['id'] for c in self.data['channels']], [f'g{self.group.id}'])
        self.assertEqual(self.data['channels'][0]['active_channel_name'], 'Serving HD')

    def test_every_verdict_and_the_lead_first(self):
        self.assertEqual(
            {(r['source_name'], r['epg_id']): r['verdict'] for r in self.rows},
            {('Harbor guide', 'WNYW.us'): 'identical',
             ('Northgate guide', 'WNYW.us'): 'identical',
             ('Summit guide', 'FOX5.us'): 'descriptions',
             ('Northgate guide', 'WNYW-DT.us'): 'mostly',
             ('Summit guide', 'FOX.west.us'): 'shifted',
             ('Harbor guide', 'FS1.us'): 'unrelated',
             ('Summit guide', 'EMPTY.us'): 'no-overlap'})
        self.assertTrue(self.rows[0]['lead'])
        self.assertEqual(self.rows[0]['epg_id'], 'WNYW.us')
        self.assertEqual(sum(r['lead'] for r in self.rows), 1)
        self.assertEqual(self.data['schedules']['lead_key'], self.rows[0]['key'])

    def test_case_variant_channels_share_the_leads_row(self):
        lead = self.row('Harbor guide', 'WNYW.us')
        self.assertEqual(sorted(m['name'] for m in lead['members']), ['Serving HD', 'Serving SD'])
        self.assertEqual(lead['spellings'], ['WNYW.us', 'wnyw.us'])

    def test_one_id_on_two_sources_is_two_schedules(self):
        self.assertIsNot(self.row('Harbor guide', 'WNYW.us'), self.row('Northgate guide', 'WNYW.us'))

    def test_programs_carry_their_agreement_and_the_outline_is_sent(self):
        alt = self.row('Northgate guide', 'WNYW-DT.us')
        kinds = {p['agreement'] for p in alt['programs']}
        self.assertTrue({AGREE_SAME, AGREE_DIFFERENT, AGREE_DOUBLED} <= kinds, kinds)
        self.assertEqual([m['title'] for m in alt['missing']], ['Game Show'])
        diff = next(p for p in alt['programs'] if p['agreement'] == AGREE_DIFFERENT)
        self.assertEqual(diff['partner']['title'], 'Judge Show')
        west = self.row('Summit guide', 'FOX.west.us')
        self.assertIn(AGREE_MOVED, {p['agreement'] for p in west['programs']})
        self.assertEqual(west['counts']['largest_move_min'], 180)
        gaps = {p['gap'] for p in self.row('Summit guide', 'FOX5.us')['programs']}
        self.assertEqual(gaps, {None, GAP_REFERENCE})

    def test_the_groups_own_programs_are_the_reference(self):
        kinds = {p['agreement'] for p in self.data['channels'][0]['programs']}
        self.assertEqual(kinds, {epg_sources.AGREE_REFERENCE})

    def test_a_schedules_cell_records_from_the_group(self):
        """Enforcement stays on the group's path: a cell on another channel's schedule
        carries the group and its serving channel, never the channel it came from."""
        serving = self.chans['Serving HD'].id
        for r in self.rows:
            for p in r['programs']:
                self.assertEqual((p['group_id'], p['channel_id']), (self.group.id, serving))
                self.assertTrue(p['on_schedule'])

    def test_members_carry_their_recording_switch(self):
        west = self.row('Summit guide', 'FOX.west.us')
        self.assertEqual([m['recording_enabled'] for m in west['members']], [False])
        self.assertTrue(all(m['recording_enabled'] for m in self.row('Harbor guide', 'FS1.us')['members']))

    def test_a_channel_with_no_id_is_named_not_rowed(self):
        self.assertEqual([m['name'] for m in self.data['schedules']['no_epg_id']], ['No id'])

    def test_without_schedules_the_reply_is_the_guides_shape(self):
        data = self.t.client.get(_epg_url(self.group.id, schedules=False)).get_json()
        self.assertNotIn('schedules', data)
        self.assertEqual(len(data['channels']), 1)

    def test_an_unknown_group_is_a_404(self):
        resp = self.t.client.get(_epg_url(999999))
        self.assertEqual(resp.status_code, 404)
        self.assertIn('error', resp.get_json())


class GroupOutOfTheGuideTests(_AppCase):
    def test_a_group_not_in_the_guide_still_gets_its_row(self):
        _chans, group = _seed_card_group(in_guide=False)
        data = self.t.client.get(_epg_url(group.id)).get_json()
        self.assertEqual(len(data['channels']), 1)
        self.assertTrue(data['schedules']['records'])

    def test_a_group_nobody_records_from_is_marked_as_not_recording(self):
        chans, group = _seed_card_group(in_guide=False, recording=False)
        data = self.t.client.get(_epg_url(group.id)).get_json()
        self.assertFalse(data['schedules']['records'])
        # The row a guide would hold is still built, led by the best-ranked channel.
        self.assertEqual(data['channels'][0]['active_channel_name'], 'Serving HD')
        self.assertTrue(data['schedules']['rows'])


class GroupScheduleScalingTests(unittest.TestCase):
    """CLAUDE.md "No hidden I/O in per-row loops": the card's endpoint and the group page
    that hosts it cost the same queries and config parses at 4 members as at 40."""

    def _measure(self, n, path_fn):
        t = make_test_app()
        try:
            accs = [seed.make_account(name=f'acc{i}') for i in range(2)]
            srcs = [seed.make_epg_source(a) for a in accs]
            members = []
            for i in range(n):
                k = i % 2
                ch = seed.make_channel(accs[k], name=f'M{i}', epg_channel_id=f'ID{i % 3}.us',
                                       epg_source_id=srcs[k].id, health_score=50 + i)
                _listings(ch, BASE, srcs[k].id)
                members.append(ch)
            group = seed.make_group(name='Scaling', members=members)
            db.session.commit()
            url = path_fn(group.id)
            config_mod._yaml_cache = None
            with IOCounter(all_engines()) as counter:
                resp = t.client.get(url)
            self.assertEqual(resp.status_code, 200)
            return counter
        finally:
            t.cleanup()

    def _assert_flat(self, path_fn):
        small, large = self._measure(4, path_fn), self._measure(40, path_fn)
        self.assertEqual(small.queries, large.queries,
                         f'query count grows with members: {small.queries} vs {large.queries}')
        self.assertEqual(small.config_parses, large.config_parses)

    def test_the_endpoint(self):
        self._assert_flat(_epg_url)

    def test_the_group_page_with_the_card(self):
        self._assert_flat(lambda gid: f'/channel-groups/{gid}')


# ── The page ───────────────────────────────────────────────────────────────────

class CardMarkupTests(_AppCase):
    def _page(self, group_id):
        resp = self.t.client.get(f'/channel-groups/{group_id}')
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _boot(self, html):
        m = re.search(r'sections: (\[.*?\]),', html)
        return json.loads(m.group(1))

    def test_the_card_and_its_grid_render_for_a_group_with_an_epg_id(self):
        _chans, group = _seed_card_group()
        html = self._page(group.id)
        self.assertIn('data-section="whatson"', html)
        self.assertIn("embed: 'schedules'", html)
        self.assertIn(f'groupId: {group.id}', html)
        for asset in ('css/guide.css', 'js/guide.js', 'js/guide-schedules.js', 'js/program-chooser.js'):
            self.assertIn(asset, html)
        self.assertIn('id="record-modal"', html)
        self.assertEqual(self._boot(html), ['summary', 'settings', 'channels', 'whatson', 'activity'])
        # The card's own Layout keys, never the TV Guide's.
        self.assertIn('group_whatson_layout_desktop', html)
        self.assertIn('group_whatson_layout_mobile', html)
        self.assertNotIn('"guide_layout_desktop"', html)

    def test_no_card_for_a_group_with_no_epg_id(self):
        acc = seed.make_account()
        chans = [seed.make_channel(acc, name=f'c{i}', epg_channel_id=None) for i in range(2)]
        group = seed.make_group(name='Bare', members=chans)
        db.session.commit()
        html = self._page(group.id)
        self.assertNotIn('data-section="whatson"', html)
        self.assertNotIn('guide-schedules.js', html)
        self.assertNotIn('whatson', self._boot(html))

    def test_no_card_on_the_system_group(self):
        system = ChannelGroup.query.filter_by(is_system=True).first()
        self.assertIsNotNone(system)
        self.assertNotIn('data-section="whatson"', self._page(system.id))

    def test_the_chips_and_layout_button_carry_no_tip_plain(self):
        """`.tip-plain` sets border-bottom: none after .chip and .btn in style.css, which
        took their bottom borders off (mockup 46, round 3)."""
        _chans, group = _seed_card_group()
        html = self._page(group.id)
        card = html[html.index('id="sched-card"'):html.index('data-section="activity"')]
        for tag in re.findall(r'<button[^>]*class="(?:chip|btn btn-sm sched-layout-btn)[^"]*"[^>]*>', card):
            self.assertNotIn('tip-plain', tag)

    def test_the_banner_offers_compare_schedules_only_where_the_card_is(self):
        with open(os.path.join(REPO, 'static/js/group-detail.js'), encoding='utf-8') as fh:
            src = fh.read()
        self.assertRegex(src, r"byId\('sched-card'\)\s*\?\s*'<button[^']*data-act=\"compare-schedules\"")
        self.assertIn("case 'compare-schedules': showScheduleCard(); return;", src)
        self.assertIn('whatson: "What\'s On"', src)


class LayoutPrefTests(_AppCase):
    def test_the_card_reads_its_own_key_and_defaults(self):
        from app.database import UserPref
        from app.routes.guide import (read_group_whatson_layout, read_guide_layout,
                                      GUIDE_LAYOUT_PREF_KEY, GROUP_WHATSON_LAYOUT_PREF_KEY)
        card = read_group_whatson_layout({})
        self.assertEqual((card['fold_same'], card['start_time'], card['end_time']), (False, True, True))
        db.session.add(UserPref(key=GROUP_WHATSON_LAYOUT_PREF_KEY,
                                value=json.dumps({'fold_same': True, 'subtitle': False})))
        db.session.commit()
        card = read_group_whatson_layout({})
        self.assertTrue(card['fold_same'])
        self.assertFalse(card['subtitle'])
        guide = read_guide_layout({}, GUIDE_LAYOUT_PREF_KEY)
        self.assertNotIn('fold_same', guide)
        self.assertTrue(guide['subtitle'], "the card's pref leaked into the guide")
        self.assertFalse(read_group_whatson_layout({}, mobile=True)['description'])


class VocabularyMirrorTests(unittest.TestCase):
    """guide-schedules.js spells the payload's words once, as constants; they must be
    exactly epg_sources' (CLAUDE.md: states are enumerated, compared against constants)."""

    def _js_map(self, name):
        with open(os.path.join(REPO, 'static/js/guide-schedules.js'), encoding='utf-8') as fh:
            src = fh.read()
        body = re.search(r'const %s = \{(.*?)\};' % name, src, re.S).group(1)
        return set(re.findall(r"'([a-z-]+)'", body))

    def test_verdicts(self):
        self.assertEqual(self._js_map('SCHED_VERDICTS'), set(epg_sources.COMPARE_VERDICTS))

    def test_agreements(self):
        self.assertEqual(self._js_map('SCHED_AGREE'), set(epg_sources.AGREEMENTS))

    def test_gap_sides(self):
        self.assertEqual(self._js_map('SCHED_GAP'), {GAP_REFERENCE, GAP_OTHER})


# ── The card in jsdom ──────────────────────────────────────────────────────────

_OBS = {}


def _observe(width):
    if width in _OBS:
        return _OBS[width]
    t = make_test_app()
    tmp = tempfile.mkdtemp(prefix='group_whats_on_js_')
    try:
        _chans, group = _seed_card_group()
        page = t.client.get(f'/channel-groups/{group.id}').get_data(as_text=True)
        epg = t.client.get(_epg_url(group.id)).get_json()
        with open(os.path.join(tmp, 'page.html'), 'w', encoding='utf-8') as fh:
            fh.write(page)
        with open(os.path.join(tmp, 'epg.json'), 'w', encoding='utf-8') as fh:
            json.dump(epg, fh)
        proc = subprocess.run(['node', HARNESS, tmp, REPO, str(width)],
                              capture_output=True, text=True, timeout=120, cwd=REPO)
        if proc.returncode != 0:
            raise AssertionError(f'harness failed:\n{proc.stderr[-4000:]}')
        _OBS[width] = json.loads(proc.stdout)
        return _OBS[width]
    finally:
        t.cleanup()
        shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class CardDesktopJsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.obs = _observe(1440)

    def test_boots_clean_and_fetches_the_schedules_once(self):
        self.assertEqual(self.obs['errors'], [])
        self.assertEqual(len(self.obs['epg_requests']), 1)
        self.assertIn('schedules=1', self.obs['epg_requests'][0])

    def test_one_column_cell_per_row_the_guides_first(self):
        b = self.obs['boot']
        self.assertTrue(b['col_rows_match'])
        self.assertEqual(b['col_keys'][0], 'guide')
        self.assertEqual(len(b['col_keys']), 8)        # the group's row + seven schedules
        self.assertEqual(b['col_badges'][1], 'In the guide')
        self.assertIn('No listings', b['col_badges'])

    def test_every_tint_is_drawn(self):
        kinds = self.obs['boot']['kinds']
        for k in ('sched-k-reference', 'sched-k-same', 'sched-k-moved', 'sched-k-different',
                  'sched-k-only-here', 'sched-k-doubled', 'ghost'):
            self.assertIn(k, kinds)
        self.assertGreater(self.obs['boot']['tinted'], 0)
        self.assertTrue(self.obs['boot']['gap_marks'])
        self.assertTrue(any(b.startswith('No listings for EMPTY.us') for b in self.obs['boot']['bands']))

    def test_recording_on_drops_a_schedule_carried_only_by_channels_with_it_off(self):
        self.assertEqual(len(self.obs['rec']['row_ids']), len(self.obs['boot']['row_ids']) - 1)
        self.assertNotIn('Shifted', self.obs['rec']['col_badges'])
        self.assertEqual(self.obs['rec']['chips'][0], 'Recording onRec on')

    def test_same_and_different_split_the_schedules(self):
        same, diff = self.obs['same']['col_badges'][2:], self.obs['different']['col_badges'][1:]
        self.assertTrue(set(same) <= {'Identical', 'Identical, descriptions differ'}, same)
        self.assertFalse(set(diff) & {'Identical', 'Identical, descriptions differ', 'In the guide'})
        self.assertEqual(self.obs['same']['row_ids'][0], self.obs['different']['row_ids'][0])

    def test_the_fold_folds_and_saves_under_the_cards_own_key(self):
        f = self.obs['folded']
        self.assertEqual(f['fold'], '+3 the same')
        self.assertIn('3 folded into the guide row', f['counts'])
        self.assertEqual(self.obs['fold_saved'], ['/api/user-prefs/group_whatson_layout_desktop'])

    def test_summary_counts_legend_and_no_id_note(self):
        b = self.obs['boot']
        self.assertTrue(b['summary'].startswith('7 schedules across 8 channels'), b['summary'])
        self.assertEqual(b['counts'], '7 schedules · 8 channels')
        self.assertIn('Description only in the guide', b['legend'])
        self.assertIn('No id has no EPG id', b['noid'])

    def test_a_schedule_cell_opens_its_details(self):
        self.assertIn('Recording on', self.obs['details'])

    def test_descriptions_are_drawn_on_desktop(self):
        self.assertGreater(self.obs['boot']['descriptions'], 0)


@unittest.skipIf(shutil.which('node') is None, 'node not installed')
@unittest.skipIf(not os.path.isdir(JSDOM), 'jsdom not installed (npm install)')
class CardPhoneJsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.obs = _observe(375)

    def test_boots_clean(self):
        self.assertEqual(self.obs['errors'], [])

    def test_no_description_in_a_phone_cell_and_short_verdicts(self):
        """DESIGN.md 13.9: a description never renders in a phone-width cell."""
        self.assertEqual(self.obs['boot']['descriptions'], 0)
        self.assertIn('Guide', self.obs['boot']['col_badges'])
        self.assertIn('None', self.obs['boot']['col_badges'])

    def test_the_phone_saves_under_the_phone_key(self):
        self.assertEqual(self.obs['fold_saved'], ['/api/user-prefs/group_whatson_layout_mobile'])


if __name__ == '__main__':
    unittest.main()
