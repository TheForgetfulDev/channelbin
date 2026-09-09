"""The Tags page and its JSON API against the design that produced them.

Chunk 5 part 4 of the fableUI rollout (dev/changelog/448) converted /tags onto the generic
components - it has no DESIGN.md section of its own, so what it must obey is sections 3.2,
3.6, 3.10, 3.12 and 3.14. Each case here is a decision a careless edit would quietly undo:

  * The list is `.tbl` inside a `.card` inside `.table-scroll` (3.2), not the bare
    `.table` in a `.table-responsive` this page carried.
  * Add and Edit are one modal over a JSON API (3.12), the way dev/changelog/356 did it
    for the two profile types. templates/tag_form.html and the two view functions behind
    it are DELETED, not left dead - a form page that still renders is a second way to
    write the row, and the two drift.
  * Row actions live behind a kebab (3.6).
  * Deleting a tag names what still references it. A tag is referenced by NAME, so the
    reference does not error when the row disappears - `{tag:live}` simply renders nothing
    from then on, which is exactly the silent behavior change CLAUDE.md principle 1 exists
    to stop.
  * The API is the enforcement half. The modal runs the same rules for presentation only,
    so every rule is asserted HERE against the endpoint, not against the JavaScript.

The colour, name and pattern rules the modal spells out client-side are proved to agree
with this file in tests/test_tag_modal_js.py.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_tags_page_conformance
"""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from app import db  # noqa: E402
from app.database import Tag, TagPattern, RecordingProfile  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tr_rows(html):
    body = html.split('<tbody>')[1].split('</tbody>')[0]
    return re.findall(r'<tr\b.*?</tr>', body, re.S)


def _page_body(html):
    """Just this page's own markup - base.html's nav carries several style="display:none"
    chips of its own, so a whole-document scan for hidden content answers about the shell
    rather than about the page."""
    return html.split('<div class="page-head">')[1].split('<footer class="app-footer">')[0]


def _unescaped(html):
    """Jinja autoescaping turns the double quotes in `the "Sports" recording profile` into
    &#34;. Asserting on the escaped spelling would make these cases fail the day the label
    stops containing quotes, which is not what they are about."""
    return html.replace('&#34;', '"').replace('&quot;', '"')


class _Base(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        # The API is CSRF-protected app-wide (CLAUDE.md), which the browser satisfies from
        # the meta tag. These cases are about the endpoint's rules, not its protection.
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()
        self.ctx = self.t.app.app_context()
        self.ctx.push()
        # create_app seeds 'live' and 'new' as starter tags, so an empty table is not the
        # default state - clear them or every name a case picks is already taken.
        for tag in Tag.query.all():
            db.session.delete(tag)
        db.session.commit()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def make_tag(self, name='live', color='#f85149', patterns=('LIVE',)):
        tag = Tag(name=name, color=color)
        db.session.add(tag)
        db.session.flush()
        for p in patterns:
            db.session.add(TagPattern(tag_id=tag.id, pattern=p))
        db.session.commit()
        return tag


class PageChromeTests(_Base):

    def setUp(self):
        super().setUp()
        self.make_tag()
        self.html = self.client.get('/tags').get_data(as_text=True)

    def test_the_page_renders(self):
        self.assertEqual(self.client.get('/tags').status_code, 200)

    def test_page_head_is_the_shared_chrome(self):
        """DESIGN.md 3.10 - .page-head, not the pre-redesign .page-header."""
        self.assertIn('<div class="page-head">', self.html)
        self.assertNotIn('class="page-header"', self.html)

    def test_page_has_exactly_one_h1_carrying_the_count(self):
        h1s = re.findall(r'<h1[^>]*>(.*?)</h1>', self.html, re.S)
        self.assertEqual(len(h1s), 1)
        self.assertIn('Tags', h1s[0])
        self.assertIn('1 tag', h1s[0])

    def test_the_count_is_singular_and_plural_correctly(self):
        self.make_tag(name='new', patterns=('NEW',))
        html = self.client.get('/tags').get_data(as_text=True)
        self.assertIn('2 tags', re.findall(r'<h1[^>]*>(.*?)</h1>', html, re.S)[0])

    def test_the_table_is_the_shared_component_in_a_card(self):
        """DESIGN.md 3.2 - .tbl in a .card, not .table in .table-responsive."""
        self.assertRegex(self.html, r'<div class="card-body table-scroll">\s*<table class="tbl">')
        self.assertNotIn('table-responsive', self.html)
        self.assertNotIn('<table class="table">', self.html)

    def test_add_is_a_button_not_a_link_to_a_form_page(self):
        """The create flow is the modal. A link would go to a page that no longer exists."""
        self.assertIn('id="tag-add"', self.html)
        self.assertNotIn('/tags/new', self.html)

    def test_row_actions_are_behind_a_kebab(self):
        """DESIGN.md 3.6 - the actions cell used to hold a loose button and an
        inline <form method="post">."""
        for row in _tr_rows(self.html):
            self.assertIn('data-menu', row)
            self.assertIn('data-act="edit"', row)
            self.assertIn('data-act="delete"', row)
        self.assertNotIn('<form method="post"', self.html)

    def test_no_confirm_dialog_survives(self):
        """DESIGN.md 3.12 / 4 - confirms are buildModal with verb-named buttons."""
        self.assertNotIn('confirm(', self.html)

    def test_the_colour_is_a_dot_on_the_name_not_its_own_column(self):
        headers = re.findall(r'<th[^>]*>(.*?)</th>', self.html, re.S)
        self.assertNotIn('Color', [h.strip() for h in headers])
        row = _tr_rows(self.html)[0]
        self.assertRegex(row, r'<span class="color-dot" style="background: #f85149">')

    def test_the_dot_uses_the_shared_class_not_an_inline_built_circle(self):
        """CLAUDE.md CSS - a sixth hand-rolled copy of "a small round div" is the
        duplication the shared .color-dot was promoted to style.css to stop."""
        self.assertNotIn('border-radius:50%', self.html.replace(' ', ''))
        css = open(os.path.join(REPO, 'static', 'css', 'style.css')).read()
        self.assertIn('.color-dot {', css)

    def test_nothing_is_hidden_at_a_breakpoint(self):
        """DESIGN.md 16.5 point 5 - hiding a column at a width deletes information
        rather than rearranging it."""
        body = _page_body(self.html)
        self.assertNotIn('display: none', body)
        self.assertNotIn('display:none', body)

    def test_empty_state_offers_the_action(self):
        """DESIGN.md 3.14 - one line of text plus a primary CTA when an action exists."""
        for tag in Tag.query.all():
            db.session.delete(tag)
        db.session.commit()
        html = self.client.get('/tags').get_data(as_text=True)
        self.assertIn('empty-state', html)
        self.assertIn('id="tag-add-empty"', html)

    def test_the_page_boots_the_modal_from_its_own_row_data(self):
        """The modal edits what the page rendered. Refetching per open would let the
        two disagree about a tag that changed underneath."""
        self.assertIn('window.TAG_CONFIG', self.html)
        self.assertIn('js/tag-modal.js', self.html)
        self.assertIn('js/tags.js', self.html)


class UsageColumnTests(_Base):
    """A tag is referenced by NAME. Deleting or renaming one therefore changes what a
    filename template renders, with nothing raising - so the page has to say where the
    name is spelled out (CLAUDE.md principle 1)."""

    def test_a_profile_template_referencing_the_tag_is_named(self):
        self.make_tag()
        db.session.add(RecordingProfile(name='Sports',
                                        filename_template='{title} {tag:live}'))
        db.session.commit()
        html = _unescaped(self.client.get('/tags').get_data(as_text=True))
        self.assertIn('the "Sports" recording profile', html)

    def test_the_global_filename_template_is_named(self):
        self.make_tag()
        with mock.patch('app.routes.tags.load_config',
                                 return_value={'recording': {
                                     'filename_template': '{date} {title}{tag:live}'}}):
            html = self.client.get('/tags').get_data(as_text=True)
        self.assertIn('the global filename template', html)

    def test_the_filename_cleanup_lists_are_named(self):
        self.make_tag()
        with mock.patch('app.routes.tags.load_config',
                                 return_value={'recording': {
                                     'filename_tags_remove': ['live'],
                                     'filename_tags_replace': ['live']}}):
            html = self.client.get('/tags').get_data(as_text=True)
        self.assertIn('filename cleanup (remove)', html)
        self.assertIn('filename cleanup (replace)', html)

    def test_an_unreferenced_tag_says_so_rather_than_rendering_blank(self):
        self.make_tag()
        html = self.client.get('/tags').get_data(as_text=True)
        row = _tr_rows(html)[0]
        self.assertIn('-', row)

    def test_a_profile_referencing_a_different_tag_is_not_named(self):
        self.make_tag()
        db.session.add(RecordingProfile(name='Movies',
                                        filename_template='{title} {tag:new}'))
        db.session.commit()
        html = _unescaped(self.client.get('/tags').get_data(as_text=True))
        self.assertNotIn('the "Movies" recording profile', html)


class ApiTests(_Base):
    """The enforcement half. Every rule the modal shows is really applied here."""

    def test_create_stores_name_patterns_and_colour(self):
        resp = self.client.post('/api/tags', json={
            'name': 'Live', 'color': '#f85149', 'patterns': ['LIVE', 'ᴸᶦᵛᵉ']})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        tag = Tag.query.one()
        self.assertEqual(tag.name, 'live')          # normalized, not stored as typed
        self.assertEqual(tag.color, '#f85149')
        self.assertEqual([p.pattern for p in tag.patterns], ['LIVE', 'ᴸᶦᵛᵉ'])

    def test_create_cleans_the_pattern_list(self):
        self.client.post('/api/tags', json={
            'name': 'live', 'color': '#f85149',
            'patterns': ['  LIVE  ', '', 'LIVE', 'NEW']})
        self.assertEqual([p.pattern for p in Tag.query.one().patterns], ['LIVE', 'NEW'])

    def test_a_name_is_required(self):
        resp = self.client.post('/api/tags', json={'name': '  ', 'patterns': ['X']})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('required', resp.get_json()['error'])

    def test_a_name_of_only_separators_is_rejected(self):
        """`'-_'.replace('-','').replace('_','')` is empty, and ''.isalnum() is False -
        the check has to reject it rather than pass it through as "no disallowed chars"."""
        resp = self.client.post('/api/tags', json={'name': '-_', 'patterns': ['X']})
        self.assertEqual(resp.status_code, 400)

    def test_a_name_with_punctuation_is_rejected(self):
        resp = self.client.post('/api/tags', json={'name': 'live!', 'patterns': ['X']})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('letters, numbers, hyphens', resp.get_json()['error'])

    def test_a_non_ascii_name_is_accepted(self):
        """str.isalnum() is Unicode-aware and these tags exist to catch stylized Unicode.
        An ASCII-only rule here would refuse names the app has no reason to refuse."""
        resp = self.client.post('/api/tags', json={'name': 'directo', 'patterns': ['X']})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post('/api/tags', json={'name': 'ñoño', 'patterns': ['X']})
        self.assertEqual(resp.status_code, 200)

    def test_at_least_one_pattern_is_required(self):
        resp = self.client.post('/api/tags', json={'name': 'live', 'patterns': ['  ']})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('match pattern', resp.get_json()['error'])

    def test_a_duplicate_name_is_rejected_case_insensitively(self):
        self.make_tag(name='live')
        resp = self.client.post('/api/tags', json={'name': 'LIVE', 'patterns': ['X']})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('already exists', resp.get_json()['error'])

    def test_a_junk_colour_is_rejected(self):
        """The stored colour lands in a style="background: ..." attribute on every
        matching guide cell, so it cannot be free text."""
        resp = self.client.post('/api/tags',
                                json={'name': 'live', 'patterns': ['X'], 'color': 'red;}'})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('hex', resp.get_json()['error'])

    def test_a_missing_colour_falls_back_to_the_default(self):
        self.client.post('/api/tags', json={'name': 'live', 'patterns': ['X']})
        self.assertEqual(Tag.query.one().color, '#58a6ff')

    def test_update_replaces_the_pattern_set(self):
        tag = self.make_tag(patterns=('LIVE', 'OLD'))
        resp = self.client.put(f'/api/tags/{tag.id}',
                               json={'name': 'live', 'color': '#3fb950',
                                     'patterns': ['NEW ONLY']})
        self.assertEqual(resp.status_code, 200)
        db.session.expire_all()
        fresh = db.session.get(Tag, tag.id)
        self.assertEqual([p.pattern for p in fresh.patterns], ['NEW ONLY'])
        self.assertEqual(fresh.color, '#3fb950')

    def test_update_does_not_collide_with_its_own_name(self):
        """The duplicate check must exclude the row being edited, or saving a tag
        without renaming it reports its own name as taken."""
        tag = self.make_tag(name='live')
        resp = self.client.put(f'/api/tags/{tag.id}',
                               json={'name': 'live', 'color': '#f85149',
                                     'patterns': ['LIVE']})
        self.assertEqual(resp.status_code, 200)

    def test_update_rejects_a_name_another_tag_already_has(self):
        self.make_tag(name='live')
        other = self.make_tag(name='new', patterns=('NEW',))
        resp = self.client.put(f'/api/tags/{other.id}',
                               json={'name': 'live', 'color': '#f85149',
                                     'patterns': ['NEW']})
        self.assertEqual(resp.status_code, 400)

    def test_update_of_a_missing_tag_is_404(self):
        resp = self.client.put('/api/tags/999',
                               json={'name': 'x', 'patterns': ['Y'], 'color': '#58a6ff'})
        self.assertEqual(resp.status_code, 404)

    def test_delete_removes_the_tag_and_its_patterns(self):
        tag = self.make_tag(patterns=('LIVE', 'ᴸᶦᵛᵉ'))
        resp = self.client.delete(f'/api/tags/{tag.id}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['name'], 'live')
        self.assertEqual(Tag.query.count(), 0)
        self.assertEqual(TagPattern.query.count(), 0)

    def test_delete_of_a_missing_tag_is_404(self):
        self.assertEqual(self.client.delete('/api/tags/999').status_code, 404)

    def test_errors_use_the_shared_envelope(self):
        """CLAUDE.md JSON API envelope - {'error': msg}, never {'ok': False}."""
        body = self.client.post('/api/tags', json={'name': ''}).get_json()
        self.assertEqual(list(body), ['error'])

    def test_a_body_that_is_not_json_does_not_500(self):
        resp = self.client.post('/api/tags', data='not json',
                                content_type='application/json')
        self.assertEqual(resp.status_code, 400)


class DeletedFormPageTests(_Base):
    """The form page is gone, not merely unlinked."""

    def test_the_template_file_is_deleted(self):
        self.assertFalse(os.path.exists(os.path.join(REPO, 'templates', 'tag_form.html')))

    def test_the_form_routes_are_gone(self):
        for path in ('/tags/new', '/tags/1/edit'):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_delete_is_no_longer_a_form_post(self):
        """The old POST /tags/<id>/delete redirected; leaving it alive would be a second
        way to delete a row, one of them without the confirm that names the fallout."""
        tag = self.make_tag()
        self.assertEqual(self.client.post(f'/tags/{tag.id}/delete').status_code, 404)

    def test_nothing_still_links_to_the_form_page(self):
        for root, _dirs, files in os.walk(os.path.join(REPO, 'templates')):
            for fname in files:
                text = open(os.path.join(root, fname)).read()
                self.assertNotIn('tags.new_tag', text, fname)
                self.assertNotIn('tags.edit_tag', text, fname)

    def test_the_channel_search_create_affordance_points_at_the_modal(self):
        """Its "+ Create tag" used to navigate to /tags/new. That URL is now a 404, and
        a dead link is worse than the page it used to reach."""
        html = self.client.get('/channels').get_data(as_text=True)
        self.assertIn('"/tags?new=1"', html)


if __name__ == '__main__':
    unittest.main()
