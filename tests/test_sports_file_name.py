"""TheSportsDB's fields in the filename template (dev/changelog/1498, dev/docs/DESIGN-sports-data.md 11).

A template may carry {sports:league}, {sports:event} and the rest. They are left in place when a
recording is scheduled (Recording.sports_name_template) and filled from its TheSportsDB link at
the join, which also gives the recording that name; with no link, or a removed one, they render
empty and collapse like an empty {sub_title}. A name a person typed is recorded as typed - the
template is kept only while the name is exactly its rendering - and is never filled in. The date
is the event's day in the display timezone, or TheSportsDB's own date for a field event at the
00:00 placeholder; never strFilename. Made-up leagues and teams: nothing received from TheSportsDB
ships (design 13). Throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_sports_file_name
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import SPORTS_FILE_NAMES_REMOVED, _retire_sports_file_names, concatenator, db, sports_data  # noqa: E402
from app import config as cfgmod  # noqa: E402
from app.accounts import fill_sports_fields, render_filename_template, split_sports_name  # noqa: E402
from app.database import (  # noqa: E402
    CONCATENATION_DONE, SPORTS_MATCH_STRONG, SPORTS_MATCH_WEAK, Alert, Recording, RecordingEvent, RecordingSegment,
    RecordingSports, Tag, TagPattern,
)
from app.sports_view import recording_context  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402
from tests.support.config_sandbox import ConfigSandbox  # noqa: E402

NY = ZoneInfo('America/New_York')
# 00:30 UTC on Nov 9 is 7:30 PM on Nov 8 in New York: the UTC date is the wrong day.
KICKOFF = datetime(2099, 11, 9, 0, 30)
PLEX = '{sports:league} {sports:date} {sports:event}'
PLEX_NAME = 'Pretend Football League 2099-11-08 Ironvale Bears vs Harborview Gulls'
PROGRAM = {'title': 'PFL Football', 'sub_title': '', 'description': '', 'channel_name': 'Ironvale Sports',
           'category': '', 'start_time': KICKOFF, 'stop_time': KICKOFF + timedelta(hours=3)}


def _link(rid, **kw):
    row = RecordingSports(recording_id=rid, tsdb_event_id=kw.pop('tsdb_event_id', 7001),
                          league_name=kw.pop('league_name', 'Pretend Football League'),
                          event_name=kw.pop('event_name', 'Ironvale Bears vs Harborview Gulls'),
                          home_name=kw.pop('home_name', 'Ironvale Bears'),
                          away_name=kw.pop('away_name', 'Harborview Gulls'),
                          event_start=kw.pop('event_start', KICKOFF),
                          local_date=kw.pop('local_date', date(2099, 11, 8)),
                          strength=kw.pop('strength', SPORTS_MATCH_STRONG), airing='first',
                          reason='The league and both teams by name, and the start agrees.', **kw)
    db.session.add(row)
    db.session.commit()
    return row


def _render(template, **kw):
    return render_filename_template(template, PROGRAM, tag_cleanup=[], tags_by_name={}, tz=NY, **kw)


def _split(template, title=''):
    return split_sports_name(template, _render(template, keep_sports=True), title)


class RenderTests(unittest.TestCase):
    """The renderer leaves the fields for the join, or renders them empty, never literally."""

    def test_keep_sports_leaves_the_fields_in_place_and_renders_the_rest(self):
        self.assertEqual(_render('{sports:league} - {title} - {channel}', keep_sports=True),
                         '{sports:league} - PFL Football - Ironvale Sports')

    def test_without_values_the_fields_render_empty_and_collapse(self):
        self.assertEqual(_render('{sports:league} - {title} - {sports:home} - {channel}'),
                         'PFL Football - Ironvale Sports')

    def test_values_fill_the_fields(self):
        self.assertEqual(_render('{title} - {sports:home} v {sports:away}',
                                 sports={'home': 'Ironvale Bears', 'away': 'Harborview Gulls'}),
                         'PFL Football - Ironvale Bears v Harborview Gulls')

    def test_a_template_with_no_sports_field_is_unchanged(self):
        self.assertEqual(_split('{date} - {title}'), ('2099-11-08 - PFL Football', None))

    def test_split_gives_the_empty_name_and_the_template_to_keep(self):
        self.assertEqual(_split(PLEX + ' - {title}'), ('PFL Football', PLEX + ' - PFL Football'))

    def test_a_template_of_only_sports_fields_is_named_by_the_title_until_the_join(self):
        """The designer's Plex example renders empty before the join; a recording is never
        created with no name (dev/changelog/1498, fresh-context review)."""
        self.assertEqual(_split(PLEX, title='PFL Football'), ('PFL Football', PLEX))
        self.assertEqual(_split(PLEX + ' - {title}', title='Other'),
                         ('PFL Football', PLEX + ' - PFL Football'), 'a template naming the program keeps it')

    def test_a_template_naming_no_program_is_named_by_the_title_not_its_literal_text(self):
        """`{sports:home} vs {sports:away}` with no match would be named `vs` for good."""
        self.assertEqual(_split('{sports:home} vs {sports:away}', title='PFL Football'),
                         ('PFL Football', '{sports:home} vs {sports:away}'))
        self.assertEqual(_split('{tag:live} {sports:home} vs {sports:away}', title='PFL Football')[0],
                         'PFL Football', 'a tag field does not name the program')
        self.assertEqual(_split('{date} {sports:home} vs {sports:away}', title='PFL Football')[0],
                         '2099-11-08  vs', 'a template with a program field keeps its own rendering')

    def test_a_tag_cleanup_pattern_cannot_rewrite_a_field_or_its_value(self):
        """`sports` and a digit are both plausible tag patterns; the fields are held out of the
        cleanup pass, so neither the token nor TheSportsDB's value is touched by it."""
        tag = Tag(name='junk', color='#888888')
        tag.patterns = [TagPattern(pattern='sports'), TagPattern(pattern='0')]
        kept = render_filename_template('{sports:league} {sports:round} - {title} 10', PROGRAM,
                                        tag_cleanup=[('junk', 'remove')], tags_by_name={'junk': tag},
                                        tz=NY, keep_sports=True)
        self.assertEqual(kept, '{sports:league} {sports:round} - PFL Football 1')
        self.assertEqual(fill_sports_fields(kept, {'league': 'sports league', 'round': '10'}),
                         'sports league 10 - PFL Football 1')


class TemplateValueTests(unittest.TestCase):
    """The values, from a link's own fields."""

    def setUp(self):
        self.t = make_test_app(csrf=False, start_scheduler=False)
        self.addCleanup(self.t.cleanup)
        rec = seed.make_recording(name='PFL Football')
        db.session.commit()
        self.rid = rec.id

    def test_a_matchup(self):
        v = sports_data.template_values(_link(self.rid, season='2099', round=10, venue='Bear Field',
                                              city='Ironvale'), NY)
        self.assertEqual(v, {'league': 'Pretend Football League', 'event': 'Ironvale Bears vs Harborview Gulls',
                             'home': 'Ironvale Bears', 'away': 'Harborview Gulls', 'season': '2099',
                             'round': '10', 'venue': 'Bear Field', 'city': 'Ironvale',
                             'date': '2099-11-08', 'start_time': '1930'})

    def test_the_date_is_the_display_timezones_day_not_utcs(self):
        link = _link(self.rid, local_date=date(2099, 11, 9))
        self.assertEqual(sports_data.template_values(link, NY)['date'], '2099-11-08')
        self.assertEqual(sports_data.template_values(link, ZoneInfo('UTC'))['date'], '2099-11-09')

    def test_a_field_event_at_the_midnight_placeholder_uses_its_own_date_and_no_time(self):
        """A race with no real time: 00:00 UTC moved to New York would be the day before."""
        link = _link(self.rid, league_name='Pretend Stock Car Series', event_name='Ironvale 400',
                     home_name=None, away_name=None, event_start=datetime(2099, 11, 1, 0, 0),
                     local_date=date(2099, 11, 1))
        v = sports_data.template_values(link, NY)
        self.assertEqual((v['date'], v['start_time'], v['home']), ('2099-11-01', '', ''))

    def test_a_playoff_round_code_is_left_out(self):
        self.assertEqual(sports_data.template_values(_link(self.rid, round=125), NY)['round'], '')


class JoinNameTests(unittest.TestCase):
    """recording_file_name(): what the join names a recording, and what it says."""

    def setUp(self):
        self.t = make_test_app(csrf=False, start_scheduler=False)
        self.addCleanup(self.t.cleanup)

    def _rec(self, template=PLEX, name=None):
        rec = seed.make_recording(name=name if name is not None else (fill_sports_fields(template, None)
                                                                      if template else 'PFL Football'),
                                  sports_name_template=template)
        db.session.commit()
        return rec

    def test_no_template_keeps_the_name_and_says_nothing(self):
        rec = self._rec(template=None)
        _link(rec.id)
        self.assertEqual(sports_data.recording_file_name(rec, NY), ('PFL Football', None))

    def test_linked_fills_the_fields_and_says_from_what(self):
        rec = self._rec(template=PLEX + ' - PFL Football')
        _link(rec.id, strength=SPORTS_MATCH_WEAK)
        name, note = sports_data.recording_file_name(rec, NY)
        self.assertEqual(name, PLEX_NAME + ' - PFL Football')
        self.assertIn('Harborview Gulls at Ironvale Bears', note)
        self.assertIn('weak match', note)

    def test_no_link_keeps_the_empty_rendering_and_says_so(self):
        rec = self._rec(template=PLEX + ' - PFL Football')
        self.assertEqual(sports_data.recording_file_name(rec, NY),
                         ('PFL Football', 'no TheSportsDB event was matched, so its TheSportsDB fields are empty'))

    def test_a_removed_match_leaves_the_fields_empty_and_says_why(self):
        rec = self._rec(template=PLEX + ' - PFL Football')
        _link(rec.id, removed_at=datetime.utcnow())
        name, note = sports_data.recording_file_name(rec, NY)
        self.assertEqual(name, 'PFL Football')
        self.assertIn('was removed', note)

    def test_a_name_changed_since_scheduling_is_never_filled(self):
        """The guard at the join, for any writer that changed the name and left the template."""
        rec = self._rec(template=PLEX + ' - PFL Football', name='My own name')
        _link(rec.id)
        name, note = sports_data.recording_file_name(rec, NY)
        self.assertEqual(name, 'My own name')
        self.assertIn('changed after it was scheduled', note)

    def test_a_sports_only_template_named_by_the_title_is_filled(self):
        rec = seed.make_recording(name='PFL Football', program_title='PFL Football',
                                  sports_name_template=PLEX)
        db.session.commit()
        _link(rec.id)
        self.assertEqual(sports_data.recording_file_name(rec, NY)[0], PLEX_NAME)

    def test_a_sports_only_template_with_no_link_keeps_the_title(self):
        rec = seed.make_recording(name='PFL Football', program_title='PFL Football',
                                  sports_name_template=PLEX)
        db.session.commit()
        self.assertEqual(sports_data.recording_file_name(rec, NY)[0], 'PFL Football')

    def test_a_sports_only_template_with_a_typed_name_is_never_filled(self):
        rec = seed.make_recording(name='My own name', program_title='PFL Football',
                                  sports_name_template=PLEX)
        db.session.commit()
        _link(rec.id)
        name, note = sports_data.recording_file_name(rec, NY)
        self.assertEqual(name, 'My own name')
        self.assertIn('changed after it was scheduled', note)


class JoinTests(unittest.TestCase):
    """The join writes the file under the filled name, renames the recording to match, and says so."""

    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        self.dvr = os.path.join(self.t._tmpdir, 'dvr')
        os.makedirs(self.dvr, exist_ok=True)
        self.t.sandbox_config({
            'display': {'timezone': 'America/New_York'},
            'recording': {
                'dvr_output_dir': self.dvr,
                'capture_log_dir': os.path.join(self.t._tmpdir, 'caplogs'),
                'live_thumbnail': {'enabled': False},
                'post_process': {'enabled': False},
            }})

    def _join(self, template=PLEX + ' - PFL Football', linked=True, **link_kw):
        rec = seed.make_recording(status='IN_PROGRESS', name='PFL Football', sports_name_template=template)
        db.session.commit()
        rid = rec.id
        if linked:
            _link(rid, **link_kw)
        seg = os.path.join(self.dvr, f'PFL_Football_{rid}_seg_001.ts')
        with open(seg, 'wb') as fh:
            fh.write(b'\x47' * 512)
        now = datetime.utcnow()
        db.session.add(RecordingSegment(recording_id=rid, segment_number=1, file_path=seg,
                                        started_at=now - timedelta(minutes=5), ended_at=now,
                                        exit_reason='STOP_TIME_REACHED', bytes_recorded=512))
        db.session.commit()
        with mock.patch('app.postprocessor.do_postprocess'):
            concatenator.do_concatenation(self.t.app, rid)
        db.session.expire_all()
        done = (RecordingEvent.query.filter_by(recording_id=rid, event_type=CONCATENATION_DONE)
                .order_by(RecordingEvent.id.desc()).first())
        return db.session.get(Recording, rid), done

    def test_linked_the_file_and_the_recording_carry_the_filled_name(self):
        rec, done = self._join()
        self.assertEqual(os.path.basename(rec.output_path), PLEX_NAME.replace(' ', '_') + '_-_PFL_Football.ts')
        self.assertTrue(os.path.exists(rec.output_path))
        self.assertEqual(rec.name, PLEX_NAME + ' - PFL Football')
        self.assertIsNone(rec.sports_name_template, 'the fields are settled once the file is written')
        self.assertIn('TheSportsDB fields filled from', done.detail)
        self.assertIn('was "PFL Football"', done.detail)

    def test_no_template_the_file_keeps_the_recordings_name_quietly(self):
        rec, done = self._join(template=None)
        self.assertEqual(os.path.basename(rec.output_path), 'PFL_Football.ts')
        self.assertEqual(rec.name, 'PFL Football')
        self.assertNotIn('TheSportsDB', done.detail)

    def test_with_the_match_removed_the_file_keeps_its_name_and_says_why(self):
        rec, done = self._join(removed_at=datetime.utcnow())
        self.assertEqual(os.path.basename(rec.output_path), 'PFL_Football.ts')
        self.assertIn('was removed', done.detail)
        self.assertIsNone(rec.sports_name_template)

    def test_with_no_link_the_file_keeps_its_name_and_says_so(self):
        rec, done = self._join(linked=False)
        self.assertEqual(os.path.basename(rec.output_path), 'PFL_Football.ts')
        self.assertIn('no TheSportsDB event was matched', done.detail)


class CreatePathTests(unittest.TestCase):
    """new_recording_json keeps the template only while the name is exactly its rendering."""

    def setUp(self):
        self.t = make_test_app(csrf=False, start_scheduler=False)
        self.addCleanup(self.t.cleanup)

    def _create(self, name, template):
        start = datetime.utcnow() + timedelta(days=2)
        with mock.patch('app.routes.recordings.schedule_recording'):
            r = self.t.client.post('/recordings/new-json', data={
                'name': name, 'url': 'http://example.test/live/1',
                'start_time': start.strftime('%Y-%m-%dT%H:%M'),
                'stop_time': (start + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),
                'name_template': template, 'force': '1'})
        body = r.get_json()
        self.assertTrue(body.get('success'), body)
        db.session.remove()
        return db.session.get(Recording, body['id'])

    def test_the_suggested_name_keeps_its_template(self):
        rec = self._create('PFL Football', PLEX + ' - PFL Football')
        self.assertEqual(rec.sports_name_template, PLEX + ' - PFL Football')

    def test_a_typed_name_keeps_no_template(self):
        rec = self._create('PFL Football at the Bears', PLEX + ' - PFL Football')
        self.assertIsNone(rec.sports_name_template)

    def test_a_template_with_no_sports_field_is_not_kept(self):
        rec = self._create('PFL Football', 'PFL Football')
        self.assertIsNone(rec.sports_name_template)

    def test_editing_the_name_clears_the_template(self):
        rec = self._create('PFL Football', PLEX + ' - PFL Football')
        rid, url, start, stop = rec.id, rec.url, rec.start_time, rec.stop_time
        from app.routes.recordings import _apply_edit_and_reschedule
        with mock.patch('app.routes.recordings.schedule_recording'):
            _apply_edit_and_reschedule(rid, 'My own name', url, start, stop)
        db.session.remove()
        self.assertIsNone(db.session.get(Recording, rid).sports_name_template)


class ServerNamingTests(unittest.TestCase):
    """Every place that suggests or makes a name carries the template for the join: the record
    dialog's program, the profile rename, and a bulk schedule (which auto-record rules share)."""

    def setUp(self):
        self.t = make_test_app(csrf=False, start_scheduler=False)
        self.addCleanup(self.t.cleanup)
        from app.database import RecordingProfile
        self.prof = RecordingProfile(name='Games', filename_template='{sports:league} - {title}')
        db.session.add(self.prof)
        acct = seed.make_account(name='Solo')
        self.ch = seed.make_channel(acct, name='Ironvale Sports')
        db.session.flush()
        self.ch.default_profile_id = self.prof.id
        self.entry = seed.make_epg_entry(self.ch, title='PFL Football', offset_minutes=120)
        db.session.commit()

    def test_the_record_dialogs_program_carries_the_template(self):
        prog = self.t.client.get(f'/api/channels/airings/{self.entry.id}/record-context').get_json()['program']
        self.assertEqual(prog['suggested_name'], 'PFL Football')
        self.assertEqual(prog['suggested_name_template'], '{sports:league} - PFL Football')

    def test_a_program_with_no_sports_field_carries_none(self):
        self.ch.default_profile_id = None
        db.session.commit()
        prog = self.t.client.get(f'/api/channels/airings/{self.entry.id}/record-context').get_json()['program']
        self.assertNotIn('suggested_name_template', prog)

    def test_the_profile_rename_carries_the_template(self):
        d = self.t.client.post('/api/guide/suggested-name', json={
            'profile_id': self.prof.id, 'title': 'PFL Football', 'channel_name': 'Ironvale Sports',
            'start_time': '2099-11-09T00:30:00', 'stop_time': '2099-11-09T03:30:00'}).get_json()
        self.assertEqual((d['name'], d['name_template']), ('PFL Football', '{sports:league} - PFL Football'))

    def test_a_bulk_schedule_keeps_the_template(self):
        with mock.patch('app.routes.recordings.schedule_recording'):
            r = self.t.client.post('/api/recordings/bulk-schedule',
                                   json={'items': [{'epg_id': self.entry.id}], 'profile': self.prof.id})
        self.assertEqual(len(r.get_json()['created']), 1, r.get_json())
        db.session.remove()
        rec = Recording.query.one()
        self.assertEqual((rec.name, rec.sports_name_template), ('PFL Football', '{sports:league} - PFL Football'))

    def test_a_sports_only_template_names_a_bulk_schedule_by_the_title(self):
        """Never an empty name, which would stay empty for good if no game is matched."""
        self.prof.filename_template = PLEX
        db.session.commit()
        with mock.patch('app.routes.recordings.schedule_recording'):
            self.t.client.post('/api/recordings/bulk-schedule',
                               json={'items': [{'epg_id': self.entry.id}], 'profile': self.prof.id})
        db.session.remove()
        rec = Recording.query.one()
        self.assertEqual((rec.name, rec.sports_name_template), ('PFL Football', PLEX))

    def test_a_sports_only_template_survives_the_record_dialog(self):
        """The dialog submits the title it showed; the server accepts it against the listing's
        own title, and still records a typed name as typed."""
        self.prof.filename_template = PLEX
        db.session.commit()
        prog = self.t.client.get(f'/api/channels/airings/{self.entry.id}/record-context').get_json()['program']
        self.assertEqual((prog['suggested_name'], prog['suggested_name_template']), ('PFL Football', PLEX))
        kept = {}
        eid = self.entry.id
        start, stop = (self.entry.start_time.strftime('%Y-%m-%dT%H:%M'),
                       self.entry.stop_time.strftime('%Y-%m-%dT%H:%M'))
        for name in ('PFL Football', 'Typed by hand'):
            with mock.patch('app.routes.recordings.schedule_recording'):
                body = self.t.client.post('/recordings/new-json', data={
                    'name': name, 'url': 'http://example.test/live/1',
                    'start_time': start, 'stop_time': stop,
                    'source_epg_id': str(eid), 'name_template': PLEX,
                    'force': '1'}).get_json()
            self.assertTrue(body.get('success'), body)
            db.session.remove()
            kept[name] = db.session.get(Recording, body['id']).sports_name_template
        self.assertEqual(kept, {'PFL Football': PLEX, 'Typed by hand': None})


class RecordingPageTests(unittest.TestCase):
    """The Program card says what the file will be called before the join, and nothing after."""

    def setUp(self):
        self.t = make_test_app(csrf=False, start_scheduler=False)
        self.addCleanup(self.t.cleanup)

    def _ctx(self, rec):
        return recording_context(rec, datetime.utcnow(), NY, False, {'sports_data': {'download_images': False}})

    def _rec(self, template=PLEX + ' - PFL Football', **kw):
        rec = seed.make_recording(name='PFL Football', sports_name_template=template, **kw)
        db.session.commit()
        return rec

    def test_a_scheduled_linked_recording_shows_the_name_on_disk(self):
        rec = self._rec(status='SCHEDULED')
        _link(rec.id)
        self.assertEqual(self._ctx(rec)['file_name'], PLEX_NAME.replace(' ', '_') + '_-_PFL_Football')

    def test_no_template_shows_no_file_name(self):
        rec = self._rec(template=None, status='SCHEDULED')
        _link(rec.id)
        self.assertIsNone(self._ctx(rec)['file_name'])

    def test_once_the_file_is_written_it_says_nothing(self):
        rec = self._rec(status='COMPLETED', output_path='/x/PFL_Football.ts')
        _link(rec.id)
        self.assertIsNone(self._ctx(rec)['file_name'])

    def test_an_unlinked_recording_says_when_the_fields_fill_in(self):
        rec = self._rec(status='SCHEDULED')
        ctx = self._ctx(rec)
        self.assertEqual(ctx['state'], 'none')
        self.assertIn('fill in if the game is matched before its file is written', ctx['note'])

    def test_an_unlinked_recording_with_no_template_says_nothing(self):
        self.assertIsNone(self._ctx(self._rec(template=None, status='SCHEDULED')))

    def test_the_page_renders_the_file_name_row(self):
        self.t.sandbox_config({'display': {'timezone': 'America/New_York'},
                               'sports_data': {'download_images': False}})
        rec = self._rec(status='SCHEDULED', program_title='PFL Football')
        _link(rec.id)
        page = self.t.client.get(f'/recordings/{rec.id}').get_data(as_text=True)
        self.assertIn('File name', page)
        self.assertIn(PLEX_NAME.replace(' ', '_') + '_-_PFL_Football', page)

    def test_a_removed_match_says_the_fields_stay_empty(self):
        rec = self._rec(status='SCHEDULED')
        _link(rec.id, removed_at=datetime.utcnow())
        self.assertTrue(self._ctx(rec)['keeps_name'])
        plain = self._rec(template=None, status='SCHEDULED')
        _link(plain.id, removed_at=datetime.utcnow())
        self.assertFalse(self._ctx(plain)['keeps_name'])


class RetiredSwitchTests(ConfigSandbox):
    """`sports_data.file_names` shipped in v0.40.0. Startup removes it from config.yaml and, when
    it was on, raises one alert saying so; a leftover `false` goes quietly (dev/changelog/1498)."""

    def _start(self, file_names):
        self._write_cfg({'config_version': cfgmod.CURRENT_CONFIG_VERSION,
                         'sports_data': {'file_names': file_names, 'download_images': False}})
        t = make_test_app(csrf=False, start_scheduler=False)
        self.addCleanup(t.cleanup)
        return t

    def _alerts(self):
        return Alert.query.filter_by(alert_type='SETTING_REMOVED').all()

    def test_on_raises_one_alert_and_removes_the_key(self):
        self._start(True)
        alerts = self._alerts()
        self.assertEqual([a.body for a in alerts], [SPORTS_FILE_NAMES_REMOVED])
        self.assertEqual(alerts[0].severity, 'WARN')
        sd = cfgmod._load_config_file()['sports_data']
        self.assertNotIn('file_names', sd)
        self.assertIs(sd['download_images'], False, 'the rest of the section is kept')
        _retire_sports_file_names()
        self.assertEqual(len(self._alerts()), 1, 'the key is gone, so it is said once')

    def test_off_is_removed_quietly(self):
        self._start(False)
        self.assertEqual(self._alerts(), [])
        self.assertNotIn('file_names', cfgmod._load_config_file()['sports_data'])


if __name__ == '__main__':
    unittest.main()
