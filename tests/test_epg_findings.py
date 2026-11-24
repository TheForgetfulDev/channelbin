"""The EPG findings (app/epg_scorecard.py, dev/changelog/1718).

The EPG check finds, from what the database holds, the channels a guide's names would give a
schedule, an id that gives unrelated channels a wrong one, channels another source would
serve better and a source that adds nothing; it stores them per account and the pages show
them. These tests pin:

  - PureTests: account_findings() - which channels each kind counts and which it leaves
    out (a pinned channel, an order-only difference, a source with no directory) - and
    finding_text() naming every kind.
  - CheckTests: check_findings() stores them, rewrites them whole, and run_check() clears an
    account that reads nothing.
  - TeardownTests: stopping a source, deleting it, and deleting the account take its rows;
    a row for a source no longer read is never shown.
  - SurfaceTests: the account page's line and link, the Dashboard line, the picks endpoint
    behind Accept all, and the check queued after a person's change.
  - LookupIndexTests: the case-folded lookups a key write makes use their indexes, the cost
    that made accepting thousands of matches take minutes.
  - AcceptAllJsTests: the review page's Accept all, driven in jsdom: every pick listed is
    posted, in chunks, after a confirm that counts them.

No network - every feed is a local byte string (CLAUDE.md §Testing).
  python3 -m unittest tests.test_epg_findings
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app import db  # noqa: E402
from app.accounts import import_source  # noqa: E402
from app.database import (Channel, EpgFinding, EpgSource,  # noqa: E402
                          EpgSourceSubscription, EPG_FINDING_ADDS_NOTHING,
                          EPG_FINDING_BETTER_SOURCE, EPG_FINDING_KINDS,
                          EPG_FINDING_NAME_MATCHES, EPG_FINDING_SHARED_ID)
from app.epg_scorecard import (AccountScorecard, ChannelScore, ChannelSource,  # noqa: E402
                               Finding, Reliability, RULE_ORDER, RULE_REACH, SourceScore,
                               account_findings, check_findings, dashboard_text, finding_text,
                               run_check,
                               stored_findings)
from app.epg_sources import (ChannelForMatch, DirectoryEntry, MATCH_DISAGREE,  # noqa: E402
                             MATCH_NEW, NameProposal, SharedId, accept_name_matches,
                             delete_sources_for_account, delete_url_source, key_answers,
                             set_channel_key, stop_using_source)
from tests.support import make_test_app, seed  # noqa: E402

ET = ZoneInfo('America/New_York')
CFG = {'sync': {'epg_collapse_threshold_percent': 20}}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSDOM = os.path.join(REPO, 'node_modules', 'jsdom')
HARNESS = os.path.join(REPO, 'tests', 'support', 'epg_review_accept_all.mjs')
TITLES = ['Morning', 'Noon', 'Evening']


def _xmltv(listings, channels=()):
    """listings: (xml_id, titles), one 30-minute program per title from an hour ahead;
    channels: (xml_id, [display names])."""
    now = datetime.utcnow()
    parts = ['<?xml version="1.0" encoding="UTF-8"?><tv>']
    for xml_id, names in channels:
        parts.append(f'<channel id="{xml_id}">'
                     + ''.join(f'<display-name>{n}</display-name>' for n in names)
                     + '</channel>')
    for xml_id, titles in listings:
        for i, title in enumerate(titles):
            start = now + timedelta(minutes=60 + 30 * i)
            stop = start + timedelta(minutes=30)
            parts.append(f'<programme start="{start:%Y%m%d%H%M%S} +0000" '
                         f'stop="{stop:%Y%m%d%H%M%S} +0000" channel="{xml_id}">'
                         f'<title>{title}</title></programme>')
    parts.append('</tv>')
    return ''.join(parts).encode()


# ── Pure ──────────────────────────────────────────────────────────────────────

def _score(sid, **kw):
    base = dict(source_id=sid, name=f'S{sid}', priority=sid, has_directory=True, covered=10,
                only_here=0, guides=0, real_schedule=10, one_title=0, programs=0, counted=0,
                described=0, subtitled=0, categorized=0, rated=0, flagged=0,
                doubled_channels=0, doubled_unknown=0, reach=None, compared=0, agree=0,
                disagree=0, outvoted=0,
                reliability=Reliability(0, 0, 0, 0, 0, None, None, None))
    base.update(kw)
    return SourceScore(**base)


def _cs(sid):
    return ChannelSource(sid, 10, 5, None, None, None, None, None, None, None)


def _ch(cid, current, best, why, pinned=False, sources=(1, 2)):
    return ChannelScore(cid, f'C{cid}', current, best, [], [_cs(s) for s in sources], {},
                        pinned, why)


def _proposal(kind, cid, guided):
    ch = ChannelForMatch(cid, f'C{cid}', 1, None, 1 if guided else None)
    entry = DirectoryEntry(f'x{cid}', [f'C{cid}'], 10, 5, None, None, [])
    return NameProposal(kind, ch, entry, f'C{cid}', entry if kind == MATCH_DISAGREE else None)


def _kinds(found):
    return {(f.source_id, f.kind): f for f in found}


class PureTests(unittest.TestCase):

    def test_name_matches_count_only_new_matches_for_channels_with_no_guide(self):
        card = AccountScorecard(1, [_score(1, only_here=3)], None, [])
        props = {1: [_proposal(MATCH_NEW, 1, False), _proposal(MATCH_NEW, 2, False),
                     _proposal(MATCH_NEW, 3, True), _proposal(MATCH_DISAGREE, 4, True)]}
        f = _kinds(account_findings(card, props, []))[(1, EPG_FINDING_NAME_MATCHES)]
        self.assertEqual(f.count, 2)
        self.assertEqual(f.detail, {'guided': 1, 'disagree': 1})

    def test_no_name_match_finding_when_every_match_already_has_a_guide(self):
        card = AccountScorecard(1, [_score(1, only_here=3)], None, [])
        found = account_findings(card, {1: [_proposal(MATCH_NEW, 1, True)]}, [])
        self.assertNotIn((1, EPG_FINDING_NAME_MATCHES), _kinds(found))

    def test_a_shared_id_is_one_finding_counting_the_misled_channels(self):
        entry = DirectoryEntry('ts.test', ['GR: Radio'], 10, 5, None, None, [])
        chans = tuple(ChannelForMatch(i, f'C{i}', 1, 'ts.test') for i in range(4))
        shared = SharedId(1, 1, entry, chans, chans[:3], chans[3:])
        card = AccountScorecard(1, [_score(1, only_here=3)], None, [])
        f = _kinds(account_findings(card, {}, [shared]))[(1, EPG_FINDING_SHARED_ID)]
        self.assertEqual((f.subject, f.count), ('ts.test', 3))
        self.assertEqual(f.detail, {'channels': 4, 'name': 'GR: Radio'})

    def test_better_source_leaves_out_a_pin_an_order_tie_and_a_channel_with_no_guide(self):
        card = AccountScorecard(1, [_score(1, only_here=1), _score(2)], None, [
            _ch(1, 1, 2, RULE_REACH),
            _ch(2, 1, 2, RULE_REACH),
            _ch(3, 1, 2, RULE_REACH, pinned=True),
            _ch(4, 1, 2, RULE_ORDER),
            _ch(5, None, 2, None),
            _ch(6, 3, 2, None),          # its guide comes from a source not covering it here
            _ch(7, 2, 2, None),
        ])
        f = _kinds(account_findings(card, {}, []))[(2, EPG_FINDING_BETTER_SOURCE)]
        self.assertEqual(f.count, 2)
        self.assertEqual(f.detail, {'why': {RULE_REACH: 2}, 'from': {'1': 2}})

    def test_a_source_adds_nothing_only_when_nothing_is_only_or_best_there(self):
        card = AccountScorecard(1, [_score(1, only_here=5), _score(2, covered=4)], None,
                                [_ch(1, 1, 1, None)])
        self.assertIn((2, EPG_FINDING_ADDS_NOTHING), _kinds(account_findings(card, {}, [])))
        self.assertEqual(_kinds(account_findings(card, {}, []))[
            (2, EPG_FINDING_ADDS_NOTHING)].count, 4)

    def test_a_source_best_for_one_channel_adds_something(self):
        card = AccountScorecard(1, [_score(1, only_here=5), _score(2)], None,
                                [_ch(1, 1, 2, RULE_REACH)])
        self.assertNotIn((2, EPG_FINDING_ADDS_NOTHING), _kinds(account_findings(card, {}, [])))

    def test_a_source_whose_names_would_guide_a_channel_adds_something(self):
        card = AccountScorecard(1, [_score(1, only_here=5), _score(2)], None, [])
        found = account_findings(card, {2: [_proposal(MATCH_NEW, 1, False)]}, [])
        self.assertNotIn((2, EPG_FINDING_ADDS_NOTHING), _kinds(found))

    def test_a_source_channels_are_pinned_to_is_never_called_useless(self):
        card = AccountScorecard(1, [_score(1, only_here=5), _score(2)], None,
                                [_ch(1, 2, 1, RULE_REACH, pinned=True)])
        self.assertNotIn((2, EPG_FINDING_ADDS_NOTHING), _kinds(account_findings(card, {}, [])))

    def test_a_source_never_refreshed_is_not_called_useless(self):
        card = AccountScorecard(1, [_score(1, only_here=5), _score(2, has_directory=False)],
                                None, [])
        self.assertNotIn((2, EPG_FINDING_ADDS_NOTHING), _kinds(account_findings(card, {}, [])))

    def test_every_kind_has_words_and_an_unknown_one_raises(self):
        details = {EPG_FINDING_NAME_MATCHES: {'guided': 1, 'disagree': 2},
                   EPG_FINDING_SHARED_ID: {'channels': 4, 'name': 'Radio'},
                   EPG_FINDING_BETTER_SOURCE: {'why': {RULE_REACH: 3}, 'from': {'1': 3}},
                   EPG_FINDING_ADDS_NOTHING: {}}
        for kind in EPG_FINDING_KINDS:
            line, tip = finding_text(Finding(2, kind, 'ts.test', 3, details[kind]), {1: 'One'})
            self.assertTrue(line and tip, kind)
        self.assertIn('runs further ahead', finding_text(
            Finding(2, EPG_FINDING_BETTER_SOURCE, None, 3, details[EPG_FINDING_BETTER_SOURCE]),
            {1: 'One'})[1])
        with self.assertRaises(ValueError):
            finding_text(Finding(2, 'nope', None, 1, {}), {})


# ── Against the database ──────────────────────────────────────────────────────

class _Fixture(unittest.TestCase):
    """Account Alpha reads A (its own guide) then B (an XMLTV file). Channel One has A's id;
    channel "UK: Two" has no id, and B's file lists a channel by that name."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.client = self.t.app.test_client()
        self.acct = seed.make_account(name='Alpha')
        self.a = self._source('A', 1)
        self.b = self._source('B', 2)
        self.one = seed.make_channel(self.acct, name='One', epg_channel_id='one.test')
        self.two = seed.make_channel(self.acct, name='UK: Two', epg_channel_id=None)
        db.session.commit()
        import_source(db.session.get(EpgSource, self.a.id),
                      _xmltv([('one.test', TITLES)]), epg_days=3, cfg=CFG)
        import_source(db.session.get(EpgSource, self.b.id),
                      _xmltv([('two.b', TITLES)], channels=[('two.b', ['UK: Two'])]),
                      epg_days=3, cfg=CFG)
        db.session.expire_all()

    def tearDown(self):
        self.t.cleanup()

    def _source(self, name, priority):
        src = EpgSource(kind='url', owner_account_id=self.acct.id, name=name,
                        url=f'http://{name.lower()}.test/epg.xml')
        db.session.add(src)
        db.session.flush()
        db.session.add(EpgSourceSubscription(source_id=src.id, account_id=self.acct.id,
                                             priority=priority))
        db.session.commit()
        return src

    def _check(self):
        n = check_findings({self.acct.id: [self.a.id, self.b.id]}, datetime.utcnow())
        db.session.expire_all()
        return n

    def _rows(self, **kw):
        return EpgFinding.query.filter_by(account_id=self.acct.id, **kw).all()


class CheckTests(_Fixture):

    def test_a_name_match_for_a_channel_with_no_guide_is_stored(self):
        self._check()
        rows = self._rows(kind=EPG_FINDING_NAME_MATCHES)
        self.assertEqual([(r.source_id, r.count) for r in rows], [(self.b.id, 1)])

    def test_the_next_check_drops_it_once_the_match_is_accepted(self):
        self._check()
        accept_name_matches(db.session.get(EpgSource, self.b.id),
                            [(self.two.id, 'two.b')], False)
        self._check()
        self.assertEqual(self._rows(kind=EPG_FINDING_NAME_MATCHES), [])

    def test_reach_is_judged_by_the_local_day_not_the_utc_one(self):
        # 01:00 UTC on the 25th is 8 PM on the 24th in New York: the same evening as a guide
        # ending at 23:00 UTC, so neither runs further, as the scorecard API judges it.
        import app.epg_scorecard as sc
        seen = []
        real = sc.account_scorecard

        def spy(*a, **kw):
            seen.append(kw.get('day_of'))
            return real(*a, **kw)
        with mock.patch.object(sc, 'account_scorecard', spy), \
                mock.patch('app.tz_utils.get_display_tz', return_value=ET):
            self._check()
        day_of = seen[0]
        self.assertEqual(day_of(datetime(2026, 11, 25, 1, 0)), day_of(datetime(2026, 11, 24, 23, 0)))

    def test_a_finding_counts_only_its_own_accounts_channels(self):
        other = seed.make_account(name='Beta')
        db.session.add(EpgSourceSubscription(source_id=self.b.id, account_id=other.id,
                                             priority=1))
        theirs = seed.make_channel(other, name='UK: Two', epg_channel_id=None)
        db.session.commit()
        n = check_findings({self.acct.id: [self.a.id, self.b.id], other.id: [self.b.id]},
                           datetime.utcnow())
        self.assertTrue(n)
        db.session.expire_all()
        mine = self._rows(kind=EPG_FINDING_NAME_MATCHES)
        self.assertEqual([r.count for r in mine], [1])
        picks = self.client.get(
            f'/api/epg-sources/{self.b.id}/name-matches?unguided=1&account={self.acct.id}'
        ).get_json()['picks']
        self.assertEqual([p['channel_id'] for p in picks], [self.two.id])
        both = self.client.get(f'/api/epg-sources/{self.b.id}/name-matches?unguided=1').get_json()
        self.assertEqual({p['channel_id'] for p in both['picks']}, {self.two.id, theirs.id})

    def test_run_check_clears_an_account_that_reads_nothing(self):
        other = seed.make_account(name='Beta')
        db.session.add(EpgFinding(account_id=other.id, source_id=self.a.id,
                                  kind=EPG_FINDING_ADDS_NOTHING, count=0,
                                  found_at=datetime.utcnow()))
        db.session.commit()
        run_check()
        db.session.expire_all()
        self.assertEqual(EpgFinding.query.filter_by(account_id=other.id).count(), 0)
        self.assertEqual(len(self._rows(kind=EPG_FINDING_NAME_MATCHES)), 1)


class TeardownTests(_Fixture):

    def setUp(self):
        super().setUp()
        self._check()
        self.assertTrue(self._rows(source_id=self.b.id))

    def test_stopping_a_source_takes_its_findings(self):
        stop_using_source(self.acct.id, self.b.id, False)
        db.session.expire_all()
        self.assertEqual(self._rows(source_id=self.b.id), [])

    def test_deleting_a_source_takes_its_findings(self):
        delete_url_source(self.b.id, False)
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(EpgFinding.query.filter_by(source_id=self.b.id).count(), 0)

    def test_deleting_the_account_takes_its_findings(self):
        # Including one about another account's source, which that source's delete misses.
        other = seed.make_account(name='Beta')
        theirs = EpgSource(kind='url', owner_account_id=other.id, name='C',
                           url='http://c.test/epg.xml')
        db.session.add(theirs)
        db.session.flush()
        db.session.add(EpgFinding(account_id=self.acct.id, source_id=theirs.id,
                                  kind=EPG_FINDING_ADDS_NOTHING, count=0,
                                  found_at=datetime.utcnow()))
        db.session.commit()
        delete_sources_for_account(self.acct.id)
        db.session.commit()
        self.assertEqual(EpgFinding.query.filter_by(account_id=self.acct.id).count(), 0)

    def test_a_row_for_a_source_no_longer_read_is_never_shown(self):
        db.session.query(EpgSourceSubscription).filter_by(source_id=self.b.id).delete()
        db.session.commit()
        self.assertEqual(stored_findings([self.acct.id])[self.acct.id], [])


class SurfaceTests(_Fixture):

    def test_the_account_page_says_so_under_the_source_with_a_link(self):
        self._check()
        html = self.client.get(f'/accounts/{self.acct.id}').get_data(as_text=True)
        self.assertIn('1 channel with no guide has a name that matches a channel in this guide.',
                      html)
        self.assertIn(f'/epg-sources/{self.b.id}/review?unguided=1&amp;account={self.acct.id}',
                      html)

    def test_the_dashboard_names_what_to_act_on_and_nothing_else(self):
        self._check()
        db.session.add(EpgFinding(account_id=self.acct.id, source_id=self.a.id,
                                  kind=EPG_FINDING_BETTER_SOURCE, count=7,
                                  detail=json.dumps({'why': {RULE_REACH: 7}}),
                                  found_at=datetime.utcnow()))
        db.session.commit()
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('Alpha: 1 channel with no guide matches a name in B.', html)
        self.assertIn(f'/accounts/{self.acct.id}#acct-sources-head', html)
        self.assertNotIn('better than', html)

    def test_the_dashboard_counts_an_id_read_through_two_guides_once(self):
        found = [Finding(1, EPG_FINDING_SHARED_ID, 'ts.test', 180, {}),
                 Finding(2, EPG_FINDING_SHARED_ID, 'ts.test', 179, {}),
                 Finding(1, EPG_FINDING_NAME_MATCHES, None, 16, {}),
                 Finding(2, EPG_FINDING_NAME_MATCHES, None, 6499, {}),
                 Finding(2, EPG_FINDING_BETTER_SOURCE, None, 9, {})]
        self.assertEqual(dashboard_text(found, {1: 'Provider', 2: 'Full'}),
                         '6,499 channels with no guide match a name in Full, and more in 1 '
                         "other guide; 180 channels get another channel's schedule from a "
                         'shared id (ts.test)')
        self.assertIsNone(dashboard_text(found[-1:], {}))

    def test_the_picks_endpoint_lists_every_proposal_under_the_filters(self):
        three = seed.make_channel(self.acct, name='Three', epg_channel_id='one.test')
        db.session.commit()
        import_source(db.session.get(EpgSource, self.a.id),
                      _xmltv([('one.test', TITLES)]), epg_days=3, cfg=CFG)
        import_source(db.session.get(EpgSource, self.b.id),
                      _xmltv([('two.b', TITLES), ('three.b', TITLES)],
                             channels=[('two.b', ['UK: Two']), ('three.b', ['Three'])]),
                      epg_days=3, cfg=CFG)
        all_ = self.client.get(f'/api/epg-sources/{self.b.id}/name-matches').get_json()
        self.assertEqual({(p['channel_id'], p['xml_id']) for p in all_['picks']},
                         {(self.two.id, 'two.b'), (three.id, 'three.b')})
        unguided = self.client.get(
            f'/api/epg-sources/{self.b.id}/name-matches?unguided=1').get_json()
        self.assertEqual([p['channel_id'] for p in unguided['picks']], [self.two.id])

    def test_a_person_accepting_a_match_queues_the_check(self):
        with mock.patch('app.scheduler.queue_epg_check') as queued:
            r = self.client.post(f'/api/epg-sources/{self.b.id}/name-matches/accept',
                                 json={'picks': [{'channel_id': self.two.id, 'xml_id': 'two.b'}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['accepted'], 1)
        queued.assert_called_once()

    def test_deleting_a_source_queues_the_check(self):
        with mock.patch('app.scheduler.queue_epg_check') as queued:
            r = self.client.delete(f'/api/accounts/{self.acct.id}/epg-sources/{self.b.id}')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        queued.assert_called_once()

    def test_a_key_set_on_the_channel_page_queues_the_check(self):
        with mock.patch('app.scheduler.queue_epg_check') as queued:
            r = self.client.post(f'/api/channels/{self.two.id}/epg-key',
                                 json={'source_id': self.b.id, 'key': 'two.b'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        queued.assert_called_once()


class LookupIndexTests(_Fixture):
    """A key write looks ids up case-folded four ways; each used to scan its table, 75 ms a
    channel on prod's data, so accepting 6,499 matches took eight minutes
    (dev/changelog/1718)."""

    def _plan(self, stmt):
        sql = str(stmt.compile(db.engine, compile_kwargs={'literal_binds': True}))
        return ' '.join(r[3] for r in db.session.execute(db.text('EXPLAIN QUERY PLAN ' + sql)))

    def test_the_channels_a_provider_gave_an_id_are_found_by_index(self):
        from app.database import EpgChannelKey, EpgSourceChannel
        # The shape _key_donor() sends: the folded id alone, since with its account and
        # hidden filters in the statement SQLite may plan through ix_channels_standing.
        self.assertIn('ix_channels_lower_epg_id', self._plan(
            select(Channel.id, Channel.account_id, Channel.hidden).where(
                func.lower(Channel.epg_channel_id) == 'one.test')))
        self.assertIn('ix_epg_channel_keys_lower_key', self._plan(
            select(EpgChannelKey.channel_id).where(EpgChannelKey.source_id == 1,
                                                   func.lower(EpgChannelKey.key) == 'x')))
        self.assertIn('ix_epg_source_channels_lower_id', self._plan(
            select(EpgSourceChannel.xml_id).where(
                EpgSourceChannel.source_id == 1, EpgSourceChannel.entry_count > 0,
                func.lower(EpgSourceChannel.xml_id).in_(['x']))))

    def test_key_answers_for_a_few_channels_reads_only_theirs(self):
        set_channel_key(db.session.get(Channel, self.one.id), db.session.get(EpgSource, self.b.id),
                        'two.b', case_sensitive=False)
        set_channel_key(db.session.get(Channel, self.two.id), db.session.get(EpgSource, self.b.id),
                        'two.b', case_sensitive=False)
        db.session.commit()
        self.assertEqual(set(key_answers([self.b.id])),
                         {(self.one.id, self.b.id), (self.two.id, self.b.id)})
        self.assertEqual(set(key_answers([self.b.id], [self.two.id])), {(self.two.id, self.b.id)})


# ── Accept all, in a DOM ──────────────────────────────────────────────────────

@unittest.skipUnless(shutil.which('node') and os.path.isdir(JSDOM), 'needs node and jsdom')
class AcceptAllJsTests(_Fixture):

    TOTAL = 1234

    def _drive(self, scenario):
        html = self.client.get(f'/epg-sources/{self.b.id}/review?unguided=1').get_data(
            as_text=True)
        tmp = tempfile.mkdtemp(prefix='epg_accept_all_')
        try:
            page = os.path.join(tmp, 'page.html')
            with open(page, 'w') as fh:
                fh.write(html)
            out = subprocess.run(['node', HARNESS, page, REPO, str(self.b.id), str(self.TOTAL),
                                  scenario], capture_output=True, text=True, timeout=60)
        finally:
            shutil.rmtree(tmp)
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout)
        self.assertEqual(result['errors'], [])
        return result

    def test_every_listed_pick_is_posted_in_chunks_after_a_counted_confirm(self):
        r = self._drive('confirm')
        self.assertIn('Accept all 1', r['buttonBefore'])
        self.assertEqual(r['picksQuery'], 'unguided=1')
        self.assertIn('1,234 channels take the matched id', r['modal'])
        self.assertEqual(r['chunkSizes'], [500, 500, 234])
        self.assertEqual(r['distinctPosted'], self.TOTAL)
        self.assertIn('Accepted 1,233 name matches.', r['toast'])
        self.assertIn('1 were no longer proposed', r['toast'])
        self.assertEqual(r['buttonAfter'], r['buttonBefore'])
        self.assertEqual(r['pageReads'], 1)

    def test_cancelling_the_confirm_posts_nothing(self):
        r = self._drive('cancel')
        self.assertEqual(r['chunkSizes'], [])

    def test_a_chunk_that_fails_stops_the_rest_and_says_so(self):
        r = self._drive('fail-second')
        self.assertEqual(r['chunkSizes'], [500, 500])
        self.assertIn('Accepted 500 name matches.', r['toast'])
        self.assertIn('Stopped before the rest: The source is refreshing.', r['toast'])


if __name__ == '__main__':
    unittest.main()
