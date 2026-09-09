"""Tier 1 - the filename designer's three JSON endpoints (app/routes/settings.py).

Guards dev/docs/BUGS.md 2026-08-03 @ "the filename designer previews a string that is
never the filename".

The old editor's preview rendered the template and stopped there. The rendered template
becomes the recording's NAME; the file on disk is _safe_name(name) (app/recorder.py),
which turns every character outside [\\w\\-.] into "_". So the screen whose entire purpose
is to show what the file will be called was showing a string that can never appear in
/dvr - `2026-08-02 - Arsenal v Man City - NBC.mp4` for a file written as
`2026-08-02_-_Arsenal_v_Man_City_-_NBC.mp4`.

The fix is that the preview is computed SERVER-side and returns _safe_name's own output,
so there is exactly one implementation of that rule rather than a JavaScript copy of it
that can drift (DESIGN.md 15.4, decision 3). These tests are what pin that.

Spec and reasoning: dev/changelog/441.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config as app_config  # noqa: E402
from app.database import db, Account, Channel, EPGEntry, Tag, TagPattern  # noqa: E402
from app.recorder import _safe_name  # noqa: E402
from tests.support import make_test_app  # noqa: E402


class TemplatePreviewApiTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _get(self, **params):
        res = self.client.get('/api/template-preview', query_string=params)
        return res.status_code, (res.get_json() or {})

    def _tag(self, name, patterns, color='#58a6ff'):
        tag = Tag(name=name, color=color)
        db.session.add(tag)
        db.session.flush()
        for p in patterns:
            db.session.add(TagPattern(tag_id=tag.id, pattern=p))
        db.session.commit()
        return tag

    def _airing(self, title, channel_name, start):
        acct = Account(name='A', account_type='m3u', m3u_url='http://x.test/a.m3u')
        db.session.add(acct)
        db.session.flush()
        ch = Channel(account_id=acct.id, stream_id=1, name=channel_name,
                     stream_url='http://x.test/1')
        db.session.add(ch)
        db.session.flush()
        entry = EPGEntry(channel_id=ch.id, title=title, start_time=start,
                         stop_time=start + timedelta(hours=1))
        db.session.add(entry)
        db.session.commit()
        return entry

    # ── The defect this file exists for ──────────────────────────────────────────

    def test_preview_returns_the_on_disk_name_not_only_the_rendered_one(self):
        """`disk` is _safe_name's output, and it is the field the designer shows. Asserted
        against _safe_name itself rather than against a hand-typed expectation, so the two
        cannot drift apart without this failing."""
        code, d = self._get(template='{title} - {channel}', src='custom',
                            title='Arsenal v Man City', channel='NBC Sports HD')
        self.assertEqual(code, 200)
        self.assertEqual(d['name'], 'Arsenal v Man City - NBC Sports HD')
        self.assertEqual(d['disk'], _safe_name(d['name']))
        self.assertEqual(d['disk'], 'Arsenal_v_Man_City_-_NBC_Sports_HD')

    def test_changed_is_true_only_when_safe_name_actually_rewrote_something(self):
        """The note under the filename is written off this flag. A template that survives
        _safe_name intact must not carry a sentence explaining a substitution that did not
        happen (DESIGN.md 15.4)."""
        _, dirty = self._get(template='{title}', src='custom', title='A B')
        self.assertTrue(dirty['changed'])
        _, clean = self._get(template='{title}', src='custom', title='Clean-Name.2026')
        self.assertFalse(clean['changed'])
        self.assertEqual(clean['disk'], clean['name'])

    def test_a_slash_in_a_template_is_flattened_not_turned_into_a_folder(self):
        """Independent evidence for DESIGN.md 15.1's decision to drop folder paths from
        templates: a `/` never produced a directory, _safe_name always flattened it."""
        _, d = self._get(template='{channel}/{title}', src='custom',
                         channel='NBC', title='Show')
        self.assertNotIn('/', d['disk'])
        self.assertEqual(d['disk'], 'NBC_Show')

    # ── Program sources ──────────────────────────────────────────────────────────

    def test_sample_source_is_the_builtin_program_and_says_so(self):
        _, d = self._get(template='{title}')
        self.assertEqual(d['subject']['source'], 'sample')
        self.assertFalse(d['subject']['fell_back'])
        self.assertIn('Tonight Show', d['subject']['title'])

    def test_epg_source_renders_the_real_airing(self):
        entry = self._airing('Premier League', 'NBC Sports',
                             datetime.utcnow() + timedelta(hours=3))
        _, d = self._get(template='{title} - {channel}', src='epg', epg_id=entry.id)
        self.assertEqual(d['subject']['source'], 'epg')
        self.assertEqual(d['subject']['title'], 'Premier League')
        self.assertEqual(d['disk'], 'Premier_League_-_NBC_Sports')

    def test_a_vanished_airing_falls_back_to_the_sample_and_admits_it(self):
        """A picked showing swept out of the EPG must degrade LOUDLY - the designer draws a
        note off `fell_back`. Silently renaming what is on screen is the class of quiet
        wrongness this app exists against."""
        _, d = self._get(template='{title}', src='epg', epg_id=999999)
        self.assertEqual(d['subject']['source'], 'sample')
        self.assertTrue(d['subject']['fell_back'])

    def test_a_custom_program_ending_after_midnight_keeps_end_after_start(self):
        """Without the next-day roll, an 11:30pm-12:30am program renders {end_time} an hour
        BEFORE {start_time} - a state the user cannot reach any other way."""
        _, d = self._get(template='{start_time}-{end_time}', src='custom',
                         title='Late', date='2026-08-03', start='23:30', end='00:30')
        self.assertEqual(d['name'], '2330-0030')

    # ── Unknown tokens ───────────────────────────────────────────────────────────

    def test_unknown_variables_are_named(self):
        _, d = self._get(template='{title} {nope} {alsonope}', src='custom', title='X')
        self.assertEqual(d['unknown'], ['{nope}', '{alsonope}'])

    def test_a_repeated_unknown_variable_is_reported_once(self):
        _, d = self._get(template='{nope} {nope}', src='custom', title='X')
        self.assertEqual(d['unknown'], ['{nope}'])

    def test_a_tag_token_naming_no_tag_is_unknown_and_a_real_one_is_not(self):
        """`live` is one of the two tags create_app seeds, so this asserts against a real
        row rather than one this test invented."""
        _, d = self._get(template='{title} {tag:live} {tag:ghost}', src='custom', title='X')
        self.assertEqual(d['unknown'], ['{tag:ghost}'])

    # ── The examples menu rides along on the same computation ────────────────────

    def test_also_renders_extra_templates_against_the_same_program(self):
        """`Start from an example` shows what each example would produce. Rendering those
        anywhere else would be a second renderer that can disagree with the live preview
        about the very thing being compared (DESIGN.md 15.7)."""
        _, d = self._get(template='{title}', src='custom', title='A B',
                         also=['{title} - {channel}', '{title}'])
        self.assertEqual([a['template'] for a in d['also']],
                         ['{title} - {channel}', '{title}'])
        self.assertEqual(d['also'][1]['disk'], d['disk'])

    def test_also_is_bounded(self):
        """A hand-built request must not be able to turn one preview into an unbounded
        render loop."""
        _, d = self._get(template='{title}', src='custom', title='X',
                         also=['{title}'] * 30)
        self.assertLessEqual(len(d['also']), 8)

    # ── Envelope ─────────────────────────────────────────────────────────────────

    def test_an_empty_template_is_a_400_with_the_error_envelope(self):
        code, d = self._get(template='')
        self.assertEqual(code, 400)
        self.assertIn('error', d)
        self.assertNotIn('success', d)

    def test_success_envelope(self):
        code, d = self._get(template='{title}')
        self.assertEqual(code, 200)
        self.assertTrue(d['success'])


class FilenameDesignerBootApiTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def test_boot_carries_everything_the_component_needs_to_open(self):
        """One payload rather than page template context - the component is meant to open
        from any host page, and a per-page boot block would have to be duplicated on each
        of them (DESIGN.md 15.1).

        The display timezone and clock format are deliberately NOT here: they come from
        base.html's <meta> tags via util.js, which is page-neutral in the same way this
        payload is, and having them in two places is what let them drift
        (dev/changelog/654)."""
        res = self.client.get('/api/filename-designer')
        self.assertEqual(res.status_code, 200)
        d = res.get_json()
        self.assertTrue(d['success'])
        for key in ('template', 'remove', 'replace', 'tags', 'variables', 'extension'):
            self.assertIn(key, d)
        self.assertTrue(d['variables'], 'the variable registry must not come back empty')
        for gone in ('timezone', 'hour12'):
            self.assertNotIn(gone, d, 'display settings belong to util.js, not this payload')

    def test_tags_carry_their_patterns_and_color(self):
        """The two cleanup pickers draw a colour dot and list each tag's patterns beside
        its name, so both have to travel in the boot payload."""
        tag = Tag(name='UHD 4K', color='#d29922')
        db.session.add(tag)
        db.session.flush()
        db.session.add(TagPattern(tag_id=tag.id, pattern='2160p'))
        db.session.commit()
        d = self.client.get('/api/filename-designer').get_json()
        row = next(t for t in d['tags'] if t['name'] == 'UHD 4K')
        self.assertEqual(row['color'], '#d29922')
        self.assertEqual(row['patterns'], ['2160p'])


class FilenameTemplateSaveApiTests(unittest.TestCase):
    """The save endpoint writes config.yaml, so this class writes to a TEMP one.

    make_test_app's overrides are not visible to a runtime load_config()/save_config() -
    the route resolves _CONFIG_PATH itself at call time, so an unpatched test here edits
    the real config.yaml. That is the sandbox-escape defect CLAUDE.md's testing section
    warns about, and it is not hypothetical: writing this file without the patch below set
    recording.filename_template to '{title}' in production, which turned the airing
    search's scaling test red because a configured tag-cleanup list makes
    render_filename_template query Tag once per row.

    Patching _CONFIG_PATH in all three of config.py's own module-level users is what makes
    the write land in the temp file - the read path, the write path and the mtime cache all
    resolve it independently.
    """

    def setUp(self):
        self.t = make_test_app()
        # CSRF-protected app-wide like every other mutating route; the token is a browser
        # concern, not what this file is asserting.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

        self._tmpdir = tempfile.mkdtemp(prefix='dvr_cfgtest_')
        self._cfg_path = os.path.join(self._tmpdir, 'config.yaml')
        with open(self._cfg_path, 'w', encoding='utf-8') as fh:
            yaml.safe_dump({'config_version': 1, 'recording': {}}, fh)
        self._patch = mock.patch.object(app_config, '_CONFIG_PATH', self._cfg_path)
        self._patch.start()
        app_config._yaml_cache = None

    def tearDown(self):
        self._patch.stop()
        app_config._yaml_cache = None
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        self.ctx.pop()
        self.t.cleanup()

    def _post(self, payload):
        res = self.client.post('/api/filename-template', data=json.dumps(payload),
                               content_type='application/json')
        return res.status_code, (res.get_json() or {})

    def _stored(self):
        with open(self._cfg_path, encoding='utf-8') as fh:
            return yaml.safe_load(fh) or {}

    def test_the_real_config_is_never_touched(self):
        """The guard for this file's own hazard: if the patch above ever stops working,
        this fails here rather than silently rewriting production."""
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertNotEqual(os.path.realpath(app_config._CONFIG_PATH),
                            os.path.realpath(os.path.join(repo, 'config.yaml')))
        self._post({'template': '{title}'})
        self.assertEqual(self._stored()['recording']['filename_template'], '{title}')

    def test_an_empty_template_is_refused(self):
        code, d = self._post({'template': '   '})
        self.assertEqual(code, 400)
        self.assertIn('error', d)

    def test_a_name_in_both_lists_is_kept_in_remove_only(self):
        """The two modes are mutually exclusive by construction and the control enforces
        it, but a hand-built request must not be able to store a state the designer cannot
        render."""
        code, d = self._post({'template': '{title}',
                              'remove': ['live'], 'replace': ['live', 'nascar']})
        self.assertEqual(code, 200)
        self.assertEqual(d['remove'], ['live'])
        self.assertEqual(d['replace'], ['nascar'])


class TemplateEditorIsGoneTests(unittest.TestCase):
    """The old page is deleted outright, not switched off (DESIGN.md 11.4)."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_the_old_editor_url_is_gone(self):
        self.assertEqual(self.client.get('/settings/template').status_code, 404)

    def test_no_endpoint_named_template_editor_survives(self):
        names = {r.endpoint for r in self.t.app.url_map.iter_rules()}
        self.assertNotIn('settings.template_editor', names)

    def test_the_template_file_is_deleted(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertFalse(os.path.exists(os.path.join(repo, 'templates', 'template_editor.html')))


if __name__ == '__main__':
    unittest.main()
