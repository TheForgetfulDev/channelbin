"""The "About this page" panel (DESIGN.md 3.21, dev/changelog/1377).

A page's what-it-is-for paragraph used to sit open under its header on every visit - half a
portrait phone's screen on Channel Groups, for a user who read it months ago. It is now the
`page_about()` macro: one visible line, a native <details> rendered closed. Two things a
careless edit would undo:

  * a new page putting its intro straight under the header again as a bare paragraph;
  * the panel being rendered open, which is the old page with extra markup.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_page_about
"""
import os
import re
import unittest

from tests.support.app import make_test_app

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(REPO, 'templates')

_DIV_RE = re.compile(r'<div\b|</div>')
_COMMENT_RE = re.compile(r'\A\s*\{#.*?#\}', re.S)
_BARE_INTRO_RE = re.compile(r'\A\s*<(p|div)\b[^>]*class="[^"]*\bpage-(intro|sub)\b')


def _after_page_head(text):
    """The markup that follows the `.page-head` block, Jinja comments stripped, or None."""
    start = text.find('class="page-head"')
    if start < 0:
        return None
    start = text.rfind('<div', 0, start)
    depth = 0
    for m in _DIV_RE.finditer(text, start):
        depth += 1 if m.group(0) == '<div' else -1
        if depth == 0:
            rest = text[m.end():]
            while _COMMENT_RE.match(rest):
                rest = rest[_COMMENT_RE.match(rest).end():]
            return rest
    return None


class NoBareIntroUnderAPageHeadTests(unittest.TestCase):
    def test_no_page_opens_with_a_bare_intro_paragraph(self):
        """What a page is for goes in `{% call page_about() %}`, never a paragraph left open
        under the header (DESIGN.md 3.21)."""
        offenders = []
        for root, _dirs, files in os.walk(TPL_DIR):
            for name in files:
                if not name.endswith('.html'):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding='utf-8') as fh:
                    rest = _after_page_head(fh.read())
                if rest is not None and _BARE_INTRO_RE.match(rest):
                    offenders.append(os.path.relpath(path, REPO))
        self.assertEqual(offenders, [],
                         'Put the page intro in {% call page_about() %} (_macros.html):\n  '
                         + '\n  '.join(sorted(offenders)))

    def test_the_check_sees_the_old_shape(self):
        old = ('<div class="page-head">\n  <h1>Tags</h1>\n</div>\n{# why #}\n'
               '<p class="page-sub">Tags find...</p>')
        self.assertRegex(_after_page_head(old), _BARE_INTRO_RE)
        new = ('<div class="page-head"><h1>Tags</h1><div class="x"></div></div>\n'
               '{% call page_about() %}Tags find...{% endcall %}')
        self.assertNotRegex(_after_page_head(new), _BARE_INTRO_RE)


class RenderedPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = make_test_app()
        cls.client = cls.t.app.test_client()
        cls.pages = {url: cls.client.get(url).get_data(as_text=True)
                     for url in ('/channel-groups', '/tags')}

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def _panel(self, url):
        m = re.search(r'<details class="page-about"[^>]*>.*?</details>', self.pages[url], re.S)
        self.assertIsNotNone(m, f'{url} has no About this page panel')
        return m.group(0)

    def test_the_panel_is_rendered_closed(self):
        """Server-rendered state is the nothing-open state; nothing may open it by default."""
        for url in self.pages:
            with self.subTest(url=url):
                self.assertRegex(self._panel(url), r'\A<details class="page-about">')

    def test_the_closed_panel_still_names_itself(self):
        for url in self.pages:
            with self.subTest(url=url):
                self.assertRegex(self._panel(url),
                                 r'<summary>.*data-icon="about".*About this page.*</summary>')

    def test_the_intro_text_is_inside_the_panel(self):
        self.assertIn('A channel group holds one or more channels', self._panel('/channel-groups'))
        self.assertIn('Tags find the little markers', self._panel('/tags'))

    def test_one_panel_per_page(self):
        for url, html in self.pages.items():
            with self.subTest(url=url):
                self.assertEqual(html.count('class="page-about"'), 1)


if __name__ == '__main__':
    unittest.main()
