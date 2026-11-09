"""A recording's TheSportsDB event, on its page and in its .nfo (dev/changelog/1464,
dev/docs/DESIGN-sports-data.md 9, 11).

A recording is linked to the event its guide listing was matched to, as a snapshot that outlives the
event, by one writer that never fails or touches the recording. A person can remove a wrong link, and
a removed link is never brought back by anything but their undo. Made-up leagues and teams: nothing
received from TheSportsDB ships (design 13). Throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_recording_sports
"""
import json
import os
import sys
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as cfgmod  # noqa: E402
import app.postprocessor as ppmod  # noqa: E402
from app import db, sports_data, sports_match  # noqa: E402
from app.database import (  # noqa: E402
    REC_STATUS_COMPLETED, REC_STATUS_CONVERTING, RECORDING_SPORTS_LINKED, RECORDING_SPORTS_REMOVED,
    SPORTS_AIRING_FIRST, SPORTS_MATCH_KIND_GUIDE, SPORTS_MATCH_STRONG, SPORTS_MATCH_WEAK, Recording,
    RecordingEvent, RecordingSports, SportsEvent, SportsFollow, SportsLeague, SportsMatch, SportsTeam,
    detach_recording_references,
)
from app.metadata_sidecar import render_nfo  # noqa: E402
from app.tz_utils import get_display_tz, local_input_value  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.clock import tomorrow_local_noon  # noqa: E402

KICKOFF = datetime(2099, 11, 8, 18, 0)


def _types(rid):
    db.session.expire_all()
    return [e.event_type for e in RecordingEvent.query.filter_by(recording_id=rid).order_by(RecordingEvent.id)]


class _Case(unittest.TestCase):
    start_scheduler = False

    def setUp(self):
        self.t = make_test_app(csrf=False, start_scheduler=self.start_scheduler)
        self.addCleanup(self.t.cleanup)
        self.account = seed.make_account()
        self.league = SportsLeague(tsdb_id=9001, name='Pretend Football League',
                                   match_terms=json.dumps(['Pretend Football League', 'PFL']))
        db.session.add(self.league)
        db.session.commit()

    def event(self, tsdb_id=7001, start=KICKOFF, home='Ironvale Bears', away='Harborview Gulls', **kw):
        e = SportsEvent(tsdb_id=tsdb_id, league_id=self.league.id, name=f'{home} vs {away}',
                        home_team_id=kw.pop('home_id', 2), away_team_id=kw.pop('away_id', 1),
                        home_name=home, away_name=away, season=kw.pop('season', '2099'),
                        round=kw.pop('round', 10), venue=kw.pop('venue', 'Ironvale Field'),
                        city=kw.pop('city', 'Ironvale'), start_at=start, local_date=start.date(), **kw)
        db.session.add(e)
        db.session.commit()
        return e

    def match(self, event, channel, start=KICKOFF, strength=SPORTS_MATCH_STRONG, title='PFL Football', **kw):
        m = SportsMatch(event_id=event.id, channel_id=channel.id, kind=SPORTS_MATCH_KIND_GUIDE,
                        program_start_time=start, program_stop_time=start + timedelta(hours=3),
                        title_key=title.lower(), program_title=title, strength=strength,
                        airing=kw.pop('airing', SPORTS_AIRING_FIRST),
                        reason=kw.pop('reason', 'The league and both teams by name, and the start agrees.'), **kw)
        db.session.add(m)
        db.session.commit()
        return m

    def recording(self, channel=None, start=KICKOFF, **kw):
        rec = seed.make_recording(channel_id=channel.id if channel else None, program_start_time=start,
                                  program_stop_time=start + timedelta(hours=3),
                                  program_title=kw.pop('program_title', 'PFL Football'),
                                  start_time=start, stop_time=start + timedelta(hours=3), **kw)
        db.session.commit()
        return rec

    def link(self, rec):
        out = sports_data.link_recording(rec.id)
        db.session.expire_all()
        return out


class LinkTests(_Case):
    """What link_recording() links, and what it leaves alone."""

    def test_a_match_on_its_channel_at_its_start_links_a_snapshot(self):
        ch = seed.make_channel(self.account)
        event = self.event()
        self.match(event, ch)
        rec = self.recording(ch)
        self.assertEqual(self.link(rec), sports_data.LINK_LINKED)
        link = db.session.get(RecordingSports, rec.id)
        self.assertEqual((link.tsdb_event_id, link.league_name, link.season, link.round, link.venue),
                         (7001, 'Pretend Football League', '2099', 10, 'Ironvale Field'))
        self.assertEqual(sports_data.event_title(link), 'Harborview Gulls at Ironvale Bears')
        self.assertEqual(link.event_start, KICKOFF)
        self.assertEqual(_types(rec.id).count(RECORDING_SPORTS_LINKED), 1)

    def test_linking_again_refreshes_without_a_second_event(self):
        ch = seed.make_channel(self.account)
        event = self.event()
        self.match(event, ch)
        rec = self.recording(ch)
        self.link(rec)
        db.session.get(SportsEvent, event.id).venue = 'New Ironvale Field'
        db.session.commit()
        self.assertEqual(self.link(rec), sports_data.LINK_REFRESHED)
        self.assertEqual(db.session.get(RecordingSports, rec.id).venue, 'New Ironvale Field')
        self.assertEqual(_types(rec.id).count(RECORDING_SPORTS_LINKED), 1)

    def test_a_group_recording_finds_the_listing_on_another_member(self):
        a, b = seed.make_channel(self.account, name='A'), seed.make_channel(self.account, name='B')
        grp = seed.make_group(members=[a, b])
        self.match(self.event(), b)
        rec = self.recording(a, group_id=grp.id)
        self.assertEqual(self.link(rec), sports_data.LINK_LINKED)

    def test_two_events_tied_on_the_listing_link_neither(self):
        ch = seed.make_channel(self.account)
        self.match(self.event(7001), ch)
        self.match(self.event(7002, home='Metro City Giants', away='Metro City Jets', home_id=5, away_id=6), ch)
        rec = self.recording(ch)
        self.assertEqual(self.link(rec), sports_data.LINK_AMBIGUOUS)
        self.assertIsNone(db.session.get(RecordingSports, rec.id))

    def test_strong_beats_weak(self):
        ch = seed.make_channel(self.account)
        self.match(self.event(7001), ch, strength=SPORTS_MATCH_WEAK)
        self.match(self.event(7002, home='Metro City Giants', away='Metro City Jets', home_id=5, away_id=6), ch)
        rec = self.recording(ch)
        self.link(rec)
        self.assertEqual(db.session.get(RecordingSports, rec.id).tsdb_event_id, 7002)

    def test_a_listing_marked_wrong_is_never_linked(self):
        ch = seed.make_channel(self.account)
        self.match(self.event(), ch, rejected_at=KICKOFF)
        rec = self.recording(ch)
        self.assertEqual(self.link(rec), sports_data.LINK_NONE)

    def test_no_listing_time_means_no_link(self):
        ch = seed.make_channel(self.account)
        self.match(self.event(), ch)
        rec = self.recording(ch, program_title=None)
        rec.program_start_time = None
        db.session.commit()
        self.assertEqual(self.link(rec), sports_data.LINK_NONE)

    def test_a_match_that_disappears_keeps_the_link(self):
        """The listing aging out, or the event being pruned with its league, is no reason to forget it."""
        ch = seed.make_channel(self.account)
        self.match(self.event(), ch)
        rec = self.recording(ch)
        self.link(rec)
        sports_data.delete_league(self.league.id)
        self.assertEqual(self.link(rec), sports_data.LINK_NONE)
        self.assertEqual(db.session.get(RecordingSports, rec.id).tsdb_event_id, 7001)

    def test_a_removed_link_is_never_linked_again(self):
        ch = seed.make_channel(self.account)
        self.match(self.event(), ch)
        rec = self.recording(ch)
        self.link(rec)
        sports_data.set_link_removed(rec.id, True)
        db.session.expire_all()
        # Even a fresh, unrejected match to another event does not bring it back.
        self.match(self.event(7002, home='Metro City Giants', away='Metro City Jets', home_id=5, away_id=6), ch)
        self.assertEqual(self.link(rec), sports_data.LINK_REMOVED)
        self.assertEqual(db.session.get(RecordingSports, rec.id).tsdb_event_id, 7001)

    def test_a_wrong_match_marked_on_the_league_page_drops_the_link(self):
        """The league page and the recording must not disagree about one listing."""
        ch = seed.make_channel(self.account)
        m = self.match(self.event(), ch)
        rec = self.recording(ch)
        self.link(rec)
        sports_match.mark_wrong(m.event_id, [m.id], True)
        self.assertEqual(self.link(rec), sports_data.LINK_DROPPED)
        self.assertIsNone(db.session.get(RecordingSports, rec.id))
        self.assertIn(RECORDING_SPORTS_REMOVED, _types(rec.id))
        # Not a person's removal of the recording's own link, so the right event can still link.
        self.match(self.event(7002, home='Metro City Giants', away='Metro City Jets', home_id=5, away_id=6), ch)
        self.assertEqual(self.link(rec), sports_data.LINK_LINKED)
        self.assertEqual(db.session.get(RecordingSports, rec.id).tsdb_event_id, 7002)

    def test_a_failure_is_logged_and_swallowed(self):
        ch = seed.make_channel(self.account)
        rec = self.recording(ch)
        with mock.patch.object(sports_data, '_link_recording', side_effect=RuntimeError('boom')), \
                self.assertLogs('app.sports_data', level='WARNING'):
            self.assertEqual(sports_data.link_recording(rec.id), sports_data.LINK_FAILED)

    def test_deleting_the_recording_deletes_its_link(self):
        ch = seed.make_channel(self.account)
        self.match(self.event(), ch)
        rec = self.recording(ch)
        self.link(rec)
        detach_recording_references(rec.id)
        db.session.delete(db.session.get(Recording, rec.id))
        db.session.commit()
        self.assertIsNone(db.session.get(RecordingSports, rec.id))


class RemoveTests(_Case):
    """Wrong match on the recording page, and its undo."""

    def setUp(self):
        super().setUp()
        self.ch = seed.make_channel(self.account)
        self.event_row = self.event()
        self.m = self.match(self.event_row, self.ch)
        self.rec = self.recording(self.ch)
        self.link(self.rec)

    def post(self, body):
        return self.t.client.post(f'/recordings/{self.rec.id}/sports-json', json=body)

    def test_remove_marks_the_link_and_the_listing(self):
        resp = self.post({'removed': True})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(RecordingSports, self.rec.id).removed_at)
        self.assertIsNotNone(db.session.get(SportsMatch, self.m.id).rejected_at)
        self.assertIn(RECORDING_SPORTS_REMOVED, _types(self.rec.id))
        page = self.t.client.get(f'/recordings/{self.rec.id}').get_data(as_text=True)
        self.assertIn('data-sports-restore>Undo', page)
        self.assertNotIn('data-sports-remove data-event', page)

    def test_undo_puts_both_back(self):
        self.post({'removed': True})
        resp = self.post({'removed': False})
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        self.assertIsNone(db.session.get(RecordingSports, self.rec.id).removed_at)
        self.assertIsNone(db.session.get(SportsMatch, self.m.id).rejected_at)
        self.assertEqual(_types(self.rec.id).count(RECORDING_SPORTS_LINKED), 2)

    def test_undo_leaves_a_listing_marked_wrong_on_the_league_page_alone(self):
        other_ch = seed.make_channel(self.account)
        grp = seed.make_group(members=[self.ch, other_ch])
        rec = db.session.get(Recording, self.rec.id)
        rec.group_id = grp.id
        other = self.match(self.event_row, other_ch, rejected_at=KICKOFF)
        db.session.commit()
        self.post({'removed': True})
        self.post({'removed': False})
        db.session.expire_all()
        self.assertIsNone(db.session.get(SportsMatch, self.m.id).rejected_at)
        self.assertIsNotNone(db.session.get(SportsMatch, other.id).rejected_at)

    def test_remove_rewrites_a_written_sidecar_without_the_event(self):
        dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(dvr, exist_ok=True)
        self.t.sandbox_config({'recording': {'dvr_output_dir': dvr, 'metadata_sidecar': {'enabled': True}}})
        out = os.path.join(dvr, 'game.mp4')
        with open(out, 'wb') as fh:
            fh.write(b'm' * 64)
        rec = db.session.get(Recording, self.rec.id)
        rec.status, rec.output_path = REC_STATUS_COMPLETED, out
        db.session.commit()
        resp = self.post({'removed': True})
        self.assertTrue(resp.get_json()['sidecar_rewritten'])
        root = ET.parse(os.path.join(dvr, 'game.nfo')).getroot()
        self.assertIsNone(root.find('set'))

    def test_a_payload_that_is_not_a_boolean_is_refused(self):
        self.assertEqual(self.post({'removed': 'yes'}).status_code, 400)

    def test_a_recording_with_no_link_is_a_404(self):
        other = self.recording(self.ch, start=KICKOFF + timedelta(days=2))
        resp = self.t.client.post(f'/recordings/{other.id}/sports-json', json={'removed': True})
        self.assertEqual(resp.status_code, 404)


class PageTests(_Case):
    """The Program card: the event's facts and why it matched, or why it did not."""

    def page(self, rec):
        rid = rec.id
        db.session.remove()
        return self.t.client.get(f'/recordings/{rid}').get_data(as_text=True)

    def test_a_linked_recording_shows_its_event(self):
        ch = seed.make_channel(self.account)
        self.match(self.event(start=KICKOFF + timedelta(minutes=20)), ch)
        rec = self.recording(ch)
        self.link(rec)
        html = self.page(rec)
        for text in ('Harborview Gulls at Ironvale Bears', 'Pretend Football League 2099, round 10',
                     'Ironvale Field, Ironvale', 'Strong, Live. The league and both teams by name',
                     'https://www.thesportsdb.com/event/7001', 'Event details from TheSportsDB',
                     'data-sports-remove data-event', '20 minutes after the guide'):
            self.assertIn(text, html)

    def test_a_snapshot_reads_the_same_after_its_event_is_gone(self):
        ch = seed.make_channel(self.account)
        self.match(self.event(), ch)
        rec = self.recording(ch)
        self.link(rec)
        sports_data.delete_league(self.league.id)
        self.assertIn('Harborview Gulls at Ironvale Bears', self.page(rec))

    def test_an_unfollowed_league_says_nothing(self):
        ch = seed.make_channel(self.account)
        rec = self.recording(ch)
        self.assertNotIn('TheSportsDB:', self.page(rec))

    def follow(self):
        db.session.add(SportsFollow(league_id=self.league.id))
        db.session.commit()

    def test_a_followed_league_with_no_event_near_says_so(self):
        self.follow()
        rec = self.recording(seed.make_channel(self.account))
        self.assertIn('No Pretend Football League event within 13 hours', self.page(rec))

    def test_a_followed_league_whose_events_did_not_match_says_what_a_match_needs(self):
        self.follow()
        self.event()
        rec = self.recording(seed.make_channel(self.account))
        html = self.page(rec)
        self.assertIn('The one Pretend Football League event around then did not match its listing', html)
        self.assertIn('the description is never read', html)

    def test_a_listing_marked_wrong_says_so(self):
        self.follow()
        ch = seed.make_channel(self.account)
        self.match(self.event(), ch, rejected_at=KICKOFF)
        rec = self.recording(ch)
        self.assertIn('marked a wrong match on the Pretend Football League page', self.page(rec))


class NfoTests(_Case):
    """What the .nfo carries for a linked recording."""

    def nfo(self, rec, link):
        return ET.fromstring(render_nfo(rec, get_display_tz(), sports=link))

    def test_league_season_round_and_venue_reach_the_file(self):
        ch = seed.make_channel(self.account)
        self.match(self.event(), ch)
        rec = self.recording(ch, metadata_description='The Gulls visit the Bears.')
        self.link(rec)
        root = self.nfo(db.session.get(Recording, rec.id), db.session.get(RecordingSports, rec.id))
        self.assertEqual(root.findtext('set/name'), 'Pretend Football League 2099')
        self.assertEqual(root.findtext('tag'), 'Pretend Football League')
        uid = root.find('uniqueid')
        self.assertEqual((uid.get('type'), uid.text), ('thesportsdb', '7001'))
        self.assertEqual(root.findtext('plot'), 'The Gulls visit the Bears.\n\nPretend Football League, 2099 season, '
                                                'round 10, at Ironvale Field, Ironvale. Event details from TheSportsDB.')
        self.assertEqual(root.findtext('title'), 'PFL Football', 'the title stays the guide\'s')

    def test_a_round_code_is_left_out(self):
        ch = seed.make_channel(self.account)
        self.match(self.event(round=500, venue=None, city=None), ch)
        rec = self.recording(ch)
        self.link(rec)
        root = self.nfo(db.session.get(Recording, rec.id), db.session.get(RecordingSports, rec.id))
        self.assertEqual(root.findtext('plot'),
                         'Pretend Football League, 2099 season. Event details from TheSportsDB.')

    def test_without_a_link_the_file_is_as_it_was(self):
        rec = self.recording(seed.make_channel(self.account))
        root = self.nfo(rec, None)
        for tag in ('set', 'tag', 'uniqueid'):
            self.assertIsNone(root.find(tag))


class TriggerTests(_Case):
    """The three places a recording is linked: its create, a matcher run, and its completion."""

    def test_a_matcher_run_links_an_unfinished_recording_and_not_a_finished_one(self):
        ch = seed.make_channel(self.account)
        for tid, full, nick in ((1, 'Harborview Gulls', 'Gulls'), (2, 'Ironvale Bears', 'Bears')):
            db.session.add(SportsTeam(tsdb_id=tid, full_name=full, names=json.dumps([full, nick]),
                                      city=full[:-len(nick)].strip(), last_looked_up=KICKOFF))
        self.event()
        seed.make_epg_entry(ch, title='PFL Football', sub_title='Harborview Gulls at Ironvale Bears',
                            start_time=KICKOFF, duration_minutes=180)
        waiting = self.recording(ch)
        done = self.recording(ch, status=REC_STATUS_COMPLETED)
        note = sports_match.run(now=KICKOFF - timedelta(hours=6), pause=0)
        db.session.expire_all()
        self.assertIsNotNone(db.session.get(RecordingSports, waiting.id))
        self.assertIsNone(db.session.get(RecordingSports, done.id))
        self.assertIn('Linked 1 recording', note)

    def test_completion_links_it_and_writes_it_into_the_nfo(self):
        dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(dvr, exist_ok=True)
        overrides = {'recording': {'dvr_output_dir': dvr, 'live_thumbnail': {'enabled': False},
                                   'move_on_complete': {'enabled': False}, 'post_script': {'enabled': False},
                                   'metadata_sidecar': {'enabled': True}}}
        self.t.sandbox_config(overrides)
        cfg = cfgmod._deep_merge(cfgmod.load_config(), overrides)
        out = os.path.join(dvr, 'game.mp4')
        with open(out, 'wb') as fh:
            fh.write(b'm' * 64)
        ch = seed.make_channel(self.account)
        self.match(self.event(), ch)
        rec = self.recording(ch, status=REC_STATUS_CONVERTING, output_path=out)
        ppmod._finish_postprocess(self.t.app, rec.id, out, cfg, stop_if_cancelled=lambda detail: False)
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rec.id).status, REC_STATUS_COMPLETED)
        self.assertIsNotNone(db.session.get(RecordingSports, rec.id))
        root = ET.parse(os.path.join(dvr, 'game.nfo')).getroot()
        self.assertEqual(root.findtext('set/name'), 'Pretend Football League 2099')

    def test_a_link_that_fails_at_completion_still_completes_the_recording(self):
        dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(dvr, exist_ok=True)
        overrides = {'recording': {'dvr_output_dir': dvr, 'live_thumbnail': {'enabled': False},
                                   'move_on_complete': {'enabled': False}, 'post_script': {'enabled': False}}}
        self.t.sandbox_config(overrides)
        cfg = cfgmod._deep_merge(cfgmod.load_config(), overrides)
        out = os.path.join(dvr, 'game.mp4')
        with open(out, 'wb') as fh:
            fh.write(b'm' * 64)
        rec = self.recording(seed.make_channel(self.account), status=REC_STATUS_CONVERTING, output_path=out)
        with mock.patch.object(sports_data, '_link_recording', side_effect=RuntimeError('boom')), \
                self.assertLogs('app.sports_data', level='WARNING'):
            ppmod._finish_postprocess(self.t.app, rec.id, out, cfg, stop_if_cancelled=lambda detail: False)
        db.session.expire_all()
        self.assertEqual(db.session.get(Recording, rec.id).status, REC_STATUS_COMPLETED)


class CreateTriggerTests(_Case):
    """POST /recordings/new-json links a guide recording as it is created."""
    start_scheduler = True

    def test_a_guide_recording_is_linked_when_it_is_created(self):
        ch = seed.make_channel(self.account, in_guide=True)
        entry = seed.make_epg_entry(ch, title='PFL Football', offset_minutes=60, duration_minutes=180)
        db.session.commit()
        self.match(self.event(start=entry.start_time), ch, start=entry.start_time)
        start = tomorrow_local_noon()
        resp = self.t.client.post('/recordings/new-json', data={
            'name': 'game', 'url': 'http://example.test/live/1', 'start_time': local_input_value(start),
            'stop_time': local_input_value(start + timedelta(hours=3)), 'channel_id': str(ch.id),
            'source_epg_id': str(entry.id)})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        rec = Recording.query.filter_by(name='game').one()
        self.assertIsNotNone(db.session.get(RecordingSports, rec.id))


if __name__ == '__main__':
    unittest.main()
