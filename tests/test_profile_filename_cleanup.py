"""A recording profile names its files the way Settings does: a template AND its own
tag-cleanup lists, as one unit (dev/changelog/1161).

Covered here: the pair comes from one place (a profile with a template brings its own lists,
even empty ones; a profile without one takes the global template and the global lists
together); the profile API stores and returns the lists, and stores none without a
template; every surface that suggests a name - the guide grid, the search's record context,
the bulk planner - uses the pair; the bulk planner names each showing with the profile it is
actually scheduled with (dev/docs/BUGS.md 2026-09-29 @ 08:56); the record modal's rename
endpoint; and migration 83's backfill, which keeps an existing profile's filenames unchanged.
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app import migrations as M  # noqa: E402
from app.accounts import (backfill_profile_filename_cleanup, filename_naming_for,  # noqa: E402
                          profile_tag_cleanup)
from app.config import load_config as _real_load_config  # noqa: E402
from app.database import Recording, RecordingProfile, Tag, TagPattern  # noqa: E402


def _cfg(template='{title} GLOBAL', remove=(), replace=()):
    cfg = _real_load_config()
    cfg['recording']['filename_template'] = template
    cfg['recording']['filename_tags_remove'] = list(remove)
    cfg['recording']['filename_tags_replace'] = list(replace)
    return cfg


class _Case(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.client
        # A fresh database seeds a `live` tag; its pattern set is pinned here so the
        # assertions do not depend on what the seed happens to carry.
        tag = Tag.query.filter_by(name='live').first()
        if tag is None:
            tag = Tag(name='live', color='#58a6ff')
            db.session.add(tag)
            db.session.flush()
        TagPattern.query.filter_by(tag_id=tag.id).delete()
        db.session.add(TagPattern(tag_id=tag.id, pattern='LIVE'))
        db.session.commit()

    def tearDown(self):
        self.t.cleanup()

    def _profile(self, **kw):
        p = RecordingProfile(name=kw.pop('name', 'Sports'), **kw)
        db.session.add(p)
        db.session.commit()
        return p


class NamingPairTests(_Case):

    def test_a_profile_with_a_template_brings_its_own_lists(self):
        p = self._profile(filename_template='{title}', filename_tags_remove='["live"]',
                          filename_tags_replace='[]')
        template, cleanup = filename_naming_for(_cfg(replace=['live']), p)
        self.assertEqual(template, '{title}')
        self.assertEqual(cleanup, [('live', 'remove')])

    def test_a_template_with_no_lists_means_no_cleanup_not_the_global_lists(self):
        p = self._profile(filename_template='{title}')
        self.assertEqual(filename_naming_for(_cfg(remove=['live']), p), ('{title}', []))

    def test_without_a_template_the_global_template_and_lists_come_together(self):
        p = self._profile(filename_tags_remove='["other"]')
        self.assertEqual(filename_naming_for(_cfg(remove=['live']), p),
                         ('{title} GLOBAL', [('live', 'remove')]))
        self.assertEqual(filename_naming_for(_cfg(remove=['live']), None),
                         ('{title} GLOBAL', [('live', 'remove')]))

    def test_a_damaged_list_reads_as_empty_and_a_name_in_both_is_remove(self):
        p = self._profile(filename_template='{title}', filename_tags_remove='not json',
                          filename_tags_replace='["live"]')
        self.assertEqual(profile_tag_cleanup(p), [('live', 'replace')])
        p.filename_tags_remove = '["live"]'
        self.assertEqual(profile_tag_cleanup(p), [('live', 'remove')])


class ProfileApiTests(_Case):

    def _body(self, **kw):
        body = {'name': 'Sports', 'filename_template': '{title}',
                'filename_tags_remove': ['live'], 'filename_tags_replace': []}
        body.update(kw)
        return body

    def test_create_stores_and_returns_the_lists(self):
        r = self.client.post('/api/profiles', json=self._body())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        prof = r.get_json()['profile']
        self.assertEqual(prof['filename_tags_remove'], ['live'])
        self.assertEqual(prof['filename_tags_replace'], [])
        row = db.session.get(RecordingProfile, prof['id'])
        self.assertEqual(json.loads(row.filename_tags_remove), ['live'])

    def test_no_template_stores_no_lists(self):
        r = self.client.post('/api/profiles', json=self._body(filename_template=''))
        row = db.session.get(RecordingProfile, r.get_json()['profile']['id'])
        self.assertIsNone(row.filename_template)
        self.assertIsNone(row.filename_tags_remove)
        self.assertIsNone(row.filename_tags_replace)

    def test_a_name_in_both_lists_is_kept_in_remove_only(self):
        r = self.client.post('/api/profiles', json=self._body(
            filename_tags_replace=['live', 'hd']))
        prof = r.get_json()['profile']
        self.assertEqual(prof['filename_tags_remove'], ['live'])
        self.assertEqual(prof['filename_tags_replace'], ['hd'])

    def test_an_edit_can_clear_the_lists(self):
        pid = self.client.post('/api/profiles', json=self._body()).get_json()['profile']['id']
        r = self.client.put(f'/api/profiles/{pid}', json=self._body(filename_tags_remove=[]))
        self.assertEqual(r.get_json()['profile']['filename_tags_remove'], [])

    def test_a_list_that_is_not_names_is_a_400(self):
        for bad in ('live', [1], {'a': 1}):
            r = self.client.post('/api/profiles', json=self._body(filename_tags_remove=bad))
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(RecordingProfile.query.count(), 0)


class SuggestedNameSurfaceTests(_Case):
    """Every surface that names a showing uses the profile's pair, not the global lists."""

    def setUp(self):
        super().setUp()
        self.acct = seed.make_account()
        self.prof = self._profile(filename_template='{title} [P]', filename_tags_remove='[]',
                                  filename_tags_replace='[]')
        self.ch = seed.make_channel(self.acct, name='Chan', default_profile_id=self.prof.id)
        self.start = datetime.utcnow().replace(second=0, microsecond=0) + timedelta(hours=1)
        self.entry = seed.make_epg_entry(self.ch, title='Match LIVE', start_time=self.start)
        db.session.commit()
        # The global lists remove the pattern; the profile's (empty) lists must win.
        self.cfg = _cfg(remove=['live'])

    def test_guide_grid(self):
        end = self.start + timedelta(hours=2)
        with mock.patch('app.routes.guide.load_config', return_value=self.cfg):
            r = self.client.get(
                f'/api/guide/epg?channel_id={self.ch.id}'
                f'&start={self.start:%Y-%m-%dT%H:%M:%S}&end={end:%Y-%m-%dT%H:%M:%S}')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        progs = [p for c in r.get_json()['channels'] for p in c['programs']
                 if p['title'] == 'Match LIVE']
        self.assertEqual([p['suggested_name'] for p in progs], ['Match LIVE [P]'])

    def test_search_record_context(self):
        with mock.patch('app.routes.channel_search.load_config', return_value=self.cfg):
            r = self.client.get(f'/api/channels/airings/{self.entry.id}/record-context')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()['program']['suggested_name'], 'Match LIVE [P]')

    def test_the_profile_removes_what_the_global_lists_do_not(self):
        self.prof.filename_tags_remove = '["live"]'
        db.session.commit()
        with mock.patch('app.routes.channel_search.load_config', return_value=_cfg()):
            r = self.client.get(f'/api/channels/airings/{self.entry.id}/record-context')
        self.assertNotIn('LIVE', r.get_json()['program']['suggested_name'])


class BulkPlannerNamesWithTheChosenProfileTests(_Case):
    """dev/docs/BUGS.md 2026-09-29 @ 08:56 - the bulk planner named every showing with the
    channel's DEFAULT profile's template even when another profile was picked."""

    def test_the_picked_profile_names_the_recording(self):
        acct = seed.make_account(max_connections=2)
        default = self._profile(name='Default', filename_template='{title} DEFAULT')
        picked = self._profile(name='Picked', filename_template='{title} PICKED')
        ch = seed.make_channel(acct, name='Chan', default_profile_id=default.id)
        entry = seed.make_epg_entry(ch, title='Show', offset_minutes=60)
        db.session.commit()
        with mock.patch('app.routes.recordings.schedule_recording'):
            r = self.client.post('/api/recordings/bulk-schedule', json={
                'items': [{'epg_id': entry.id}], 'profile': picked.id})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rec = Recording.query.one()
        self.assertEqual(rec.profile_id, picked.id)
        self.assertEqual(rec.name, 'Show PICKED')


class RecordModalRenameTests(_Case):

    def _post(self, **kw):
        body = {'title': 'Match LIVE', 'sub_title': '', 'description': '', 'category': '',
                'channel_name': 'Chan', 'start_time': '2026-09-29T18:00:00',
                'stop_time': '2026-09-29T19:00:00'}
        body.update(kw)
        with mock.patch('app.routes.guide.load_config', return_value=_cfg()):
            return self.client.post('/api/guide/suggested-name', json=body)

    def test_names_with_the_profiles_template_and_cleanup(self):
        p = self._profile(filename_template='{channel} - {title}',
                          filename_tags_remove='["live"]')
        r = self._post(profile_id=p.id)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()['name'], 'Chan - Match')

    def test_no_profile_is_the_global_naming(self):
        self.assertEqual(self._post(profile_id=None).get_json()['name'], 'Match LIVE GLOBAL')

    def test_bad_input(self):
        self.assertEqual(self._post(profile_id=999).status_code, 404)
        self.assertEqual(self._post(profile_id='x').status_code, 400)
        self.assertEqual(self._post(start_time='soon').status_code, 400)


class BackfillTests(_Case):
    """Migration 83 registers an obligation; create_app() discharges it with the global lists
    so a profile that already set a template keeps naming files exactly as before."""

    def _register(self):
        db.session.execute(db.text(M._BACKFILL_LEDGER_DDL))
        db.session.execute(db.text(
            "INSERT OR REPLACE INTO migration_backfills (name, registered_at) VALUES (:n, 'x')"),
            {'n': M._BF_PROFILE_FILENAME_CLEANUP})
        db.session.commit()

    def test_profiles_with_a_template_get_the_global_lists_and_the_rest_are_untouched(self):
        own = self._profile(name='Own', filename_template='{title}')
        inherit = self._profile(name='Inherit')
        self._register()
        backfill_profile_filename_cleanup(_cfg(remove=['live'], replace=['live', 'hd']))
        db.session.expire_all()
        own = db.session.get(RecordingProfile, own.id)
        self.assertEqual(profile_tag_cleanup(own), [('live', 'remove'), ('hd', 'replace')])
        self.assertIsNone(db.session.get(RecordingProfile, inherit.id).filename_tags_remove)
        self.assertFalse(M.obligation_pending(M._BF_PROFILE_FILENAME_CLEANUP))

    def test_nothing_happens_without_the_obligation_or_after_it_is_discharged(self):
        own = self._profile(name='Own', filename_template='{title}')
        backfill_profile_filename_cleanup(_cfg(remove=['live']))
        db.session.expire_all()
        self.assertIsNone(db.session.get(RecordingProfile, own.id).filename_tags_remove)
        self._register()
        backfill_profile_filename_cleanup(_cfg(remove=['live']))
        # A user's later edit is never overwritten by a second startup.
        own = db.session.get(RecordingProfile, own.id)
        own.filename_tags_remove = '[]'
        db.session.commit()
        backfill_profile_filename_cleanup(_cfg(remove=['live']))
        db.session.expire_all()
        self.assertEqual(db.session.get(RecordingProfile, own.id).filename_tags_remove, '[]')


class MigrationStepTests(unittest.TestCase):

    def test_adds_both_columns_and_registers_the_obligation_first(self):
        with tempfile.TemporaryDirectory() as d:
            conn = sqlite3.connect(os.path.join(d, 'm.db'))
            cur = conn.cursor()
            cur.execute('CREATE TABLE recording_profiles (id INTEGER PRIMARY KEY, '
                        'name TEXT, filename_template TEXT)')
            M._m083_profile_filename_cleanup(conn, cur)
            cols = {r[1] for r in cur.execute('PRAGMA table_info(recording_profiles)')}
            self.assertLessEqual({'filename_tags_remove', 'filename_tags_replace'}, cols)
            self.assertTrue(M._backfill_pending(cur, M._BF_PROFILE_FILENAME_CLEANUP))
            # Re-runnable from the top.
            M._m083_profile_filename_cleanup(conn, cur)
            conn.close()


if __name__ == '__main__':
    unittest.main()
