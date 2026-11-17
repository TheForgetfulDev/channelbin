"""A recording made by hand says where it was made, and one macro feeds the dialog.

Guards dev/docs/BUGS.md 2026-11-16 (creation event), dev/changelog/1592 and 1593. Prod's
recording 41 had no events at all and no rule: only the request log could say it was made
from the TV Guide's record dialog. And the seven pages that open that dialog each
hand-wrote the settings guide.js reads, so a new key was seven edits.

Covers:
  - CreatedEventTests: the event, its source, and that an unknown source is recorded, not
    refused; a rule's recording does not get one.
  - DialogConfigTests: every page's GUIDE_CONFIG comes from the one macro, names a source
    the server knows, and no template spells the shared keys itself.

  python3 -m unittest tests.test_recording_created_event
"""
import glob
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.config import load_config  # noqa: E402
from app.database import (  # noqa: E402
    Recording, RecordingEvent, RecordingProfile, RECORDING_CREATED, RECORDING_SOURCE_BULK,
    RECORDING_SOURCE_UNKNOWN, RECORDING_SOURCES, recording_source,
)
from app.routes import recordings as rec_routes  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_REPO_ROOT, 'templates')
_MACRO_CALL = re.compile(r"record_dialog_config\(\s*\w+\s*,\s*'([^']*)'\s*\)")
_SHARED_KEYS = ('savedSearchesUrl', 'deleteSavedSearchUrlBase', 'addChannelUrlBase',
                'editRecordingUrlBase', 'recordSource', 'prePaddingMinutes')


class CreatedEventTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app(csrf=False)
        self.addCleanup(self.t.cleanup)
        for name in ('schedule_recording', 'unschedule_recording'):
            patcher = mock.patch(f'app.routes.recordings.{name}')
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self, name, **extra):
        start = datetime.utcnow() + timedelta(hours=2)
        stop = start + timedelta(hours=1)
        data = {'name': name, 'url': 'http://example.test/live/1',
                'start_time': start.strftime('%Y-%m-%dT%H:%M'),
                'start_time_utc': start.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                'stop_time': stop.strftime('%Y-%m-%dT%H:%M'),
                'stop_time_utc': stop.strftime('%Y-%m-%dT%H:%M:%S.000Z'), **extra}
        resp = self.t.client.post('/recordings/new-json', data=data)
        self.assertEqual(200, resp.status_code, resp.get_json())
        return Recording.query.filter_by(name=name).one()

    def _created(self, rec_id):
        return RecordingEvent.query.filter_by(
            recording_id=rec_id, event_type=RECORDING_CREATED).all()

    def test_the_dialog_names_its_page(self):
        rec = self._post('from the guide', source='guide')
        events = self._created(rec.id)
        self.assertEqual(1, len(events))
        self.assertEqual({'source': 'guide'}, json.loads(events[0].extra_data))
        self.assertEqual('Scheduled by hand from the TV Guide.', events[0].detail)

    def test_a_request_that_names_no_page_is_recorded_as_that(self):
        rec = self._post('from nowhere')
        events = self._created(rec.id)
        self.assertEqual(1, len(events))
        self.assertEqual(RECORDING_SOURCE_UNKNOWN, json.loads(events[0].extra_data)['source'])
        self.assertIn('named no page', events[0].detail)

    def test_a_word_the_server_does_not_know_is_unknown_and_never_shown(self):
        rec = self._post('made up', source='<b>the moon</b>')
        event = self._created(rec.id)[0]
        self.assertEqual(RECORDING_SOURCE_UNKNOWN, json.loads(event.extra_data)['source'])
        self.assertNotIn('moon', event.detail)

    def _create(self, **kw):
        now = datetime.utcnow()
        return rec_routes._create_scheduled_recording(
            load_config(), name='direct', url='http://example.test/live/1',
            start_utc=now + timedelta(hours=1), stop_utc=now + timedelta(hours=2),
            channel_id=None, group_id=None, profile_id=None, **kw)

    def test_a_rule_recording_gets_the_rule_event_and_not_this_one(self):
        rec = self._create(link=lambda r: True, source='guide')
        self.assertEqual([], self._created(rec.id))

    def test_the_bulk_schedule_names_itself(self):
        account = seed.make_account(name='Solo', max_connections=1)
        channel = seed.make_channel(account, name='One')
        entry = seed.make_epg_entry(channel, title='Alpha', offset_minutes=60)
        db.session.commit()
        resp = self.t.client.post('/api/recordings/bulk-schedule',
                                  json={'items': [{'epg_id': entry.id}], 'profile': 'default'})
        self.assertEqual(1, len(resp.get_json()['created']), resp.get_json())
        rec = Recording.query.filter_by(program_title='Alpha').one()
        event = self._created(rec.id)[0]
        self.assertEqual(RECORDING_SOURCE_BULK, json.loads(event.extra_data)['source'])
        self.assertIn('several showings', event.detail)

    def test_record_from_url_names_itself(self):
        """The word static/js/manual_record.js sends."""
        rec = self._post('typed address', source='url')
        self.assertIn('Record from URL', self._created(rec.id)[0].detail)

    def test_reading_a_source(self):
        self.assertEqual('guide', recording_source(' Guide '))
        for raw in (None, '', 'nope', 7):
            self.assertEqual(RECORDING_SOURCE_UNKNOWN, recording_source(raw))


def _template_sources():
    found = {}
    for path in glob.glob(os.path.join(_TEMPLATES, '**', '*.html'), recursive=True):
        rel = os.path.relpath(path, _TEMPLATES)
        if rel == '_macros.html':
            continue
        with open(path) as fh:
            text = fh.read()
        calls = _MACRO_CALL.findall(text)
        if calls:
            found[rel] = (calls, text)
    return found


class DialogConfigTests(unittest.TestCase):
    def test_every_page_with_the_dialog_config_calls_the_macro_once(self):
        found = _template_sources()
        self.assertGreaterEqual(len(found), 7, sorted(found))
        for rel, (calls, text) in found.items():
            self.assertEqual(1, len(calls), rel)
            self.assertEqual(1, text.count('const GUIDE_CONFIG = {'), rel)

    def test_every_source_a_page_sends_is_one_the_server_knows(self):
        for rel, (calls, _) in _template_sources().items():
            self.assertIn(calls[0], RECORDING_SOURCES, rel)
            self.assertNotEqual(RECORDING_SOURCE_UNKNOWN, calls[0], rel)

    def test_no_page_defines_guide_config_without_the_macro(self):
        with_macro = set(_template_sources())
        for path in glob.glob(os.path.join(_TEMPLATES, '**', '*.html'), recursive=True):
            rel = os.path.relpath(path, _TEMPLATES)
            if rel == '_macros.html':
                continue
            with open(path) as fh:
                text = fh.read()
            if 'const GUIDE_CONFIG = {' in text:
                self.assertIn(rel, with_macro, f'{rel} hand-writes GUIDE_CONFIG')

    def test_no_template_spells_a_shared_key_itself(self):
        for path in glob.glob(os.path.join(_TEMPLATES, '**', '*.html'), recursive=True):
            rel = os.path.relpath(path, _TEMPLATES)
            if rel == '_macros.html':
                continue
            with open(path) as fh:
                text = fh.read()
            for key in _SHARED_KEYS:
                self.assertNotIn(f'{key}:', text, f'{rel} spells {key} outside the macro')

    def test_the_dialog_sends_its_source(self):
        with open(os.path.join(_REPO_ROOT, 'static', 'js', 'guide.js')) as fh:
            self.assertIn("data.set('source', GUIDE_CONFIG.recordSource || '')", fh.read())
        with open(os.path.join(_REPO_ROOT, 'static', 'js', 'manual_record.js')) as fh:
            self.assertIn("source: 'url'", fh.read())
        self.assertIn('url', RECORDING_SOURCES)


class RenderedConfigTests(unittest.TestCase):
    """The macro's output in a real page: every key present once, as valid JS values."""

    def setUp(self):
        self.t = make_test_app()
        self.addCleanup(self.t.cleanup)
        seed.seed_all()
        db.session.add(RecordingProfile(name='Padded', pre_padding_minutes=2,
                                        post_padding_minutes=3))
        db.session.commit()

    def test_the_recordings_page_renders_every_shared_key(self):
        body = self.t.client.get('/recordings').get_data(as_text=True)
        block = body[body.index('const GUIDE_CONFIG = {'):]
        block = block[:block.index('</script>')]
        for key in ('savedSearchesUrl', 'deleteSavedSearchUrlBase', 'addChannelUrlBase',
                    'newRecordingUrl', 'editRecordingUrlBase', 'recDetailUrlBase',
                    'recordSource', 'profiles'):
            self.assertEqual(1, block.count(f'{key}:'), key)
        self.assertIn('recordSource: "recordings"', block)
        self.assertIn('newRecordingUrl: "/recordings/new"', block)
        self.assertIn('prePaddingMinutes: 2, postPaddingMinutes: 3', block)


if __name__ == '__main__':
    unittest.main()
