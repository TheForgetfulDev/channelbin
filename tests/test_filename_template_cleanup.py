"""Tier 1 - the tag-cleanup pass in app/accounts.py::render_filename_template.

Guards dev/docs/BUGS.md 2026-08-03 @ "replace-mode tag cleanup rewrites its own output".

The cleanup pass used to be a sequential loop of str.replace, one call per pattern per
tag. A sequential loop rescans what it just wrote, so a tag whose NAME contains one of its
own PATTERNS re-matches its own replacement and cascades: `UHD 4K` with patterns
2160p/UHD/4K turned "Show 2160p" into "Show UHD UHD 4K UHD 4K". That is not an exotic
configuration - it is exactly what a normalizing tag is for, which is the entire point of
`replace` mode.

The fix is one alternation pass, longest pattern first, so no replacement is ever
rescanned. `remove` mode was never affected: replacing with '' cannot re-match.

Spec and reasoning: dev/changelog/441.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.accounts import render_filename_template  # noqa: E402
from app.database import db, Tag, TagPattern  # noqa: E402
from tests.support import make_test_app  # noqa: E402


class TagCleanupCascadeTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.ctx = self.t.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()
        self.t.cleanup()

    def _tag(self, name, patterns, color='#58a6ff'):
        tag = Tag(name=name, color=color)
        db.session.add(tag)
        db.session.flush()
        for p in patterns:
            db.session.add(TagPattern(tag_id=tag.id, pattern=p))
        db.session.commit()
        return tag

    def _render(self, template, title, cleanup):
        return render_filename_template(
            template,
            {'title': title, 'sub_title': '', 'description': '',
             'channel_name': 'CH', 'category': '', 'start_time': None, 'stop_time': None},
            tag_cleanup=cleanup,
        )

    def test_replace_does_not_cascade_when_the_name_contains_a_pattern(self):
        """The defect itself: `UHD 4K` owns the patterns `UHD` and `4K` that its own name
        contains, so a rescanning pass rewrites its own replacement forever."""
        self._tag('UHD 4K', ['2160p', 'UHD', '4K'])
        out = self._render('{title}', 'Show 2160p', [('UHD 4K', 'replace')])
        self.assertEqual(out, 'Show UHD 4K')

    def test_replace_is_idempotent_on_text_that_already_reads_as_the_name(self):
        """Rendering a title that already contains the tag's name must not grow it. This
        is the same cascade seen from the other end, and it is the state a re-render of an
        already-normalized name lands in."""
        self._tag('UHD 4K', ['2160p', 'UHD', '4K'])
        out = self._render('{title}', 'Show UHD 4K', [('UHD 4K', 'replace')])
        self.assertEqual(out, 'Show UHD 4K')

    def test_a_pattern_inside_the_name_still_normalizes_on_its_own(self):
        """Registering the tag's name as self-mapping must not disable the normalization
        the tag exists for: `UHD` alone is still a pattern and still becomes `UHD 4K`. If
        this passes only because nothing matched, the previous test would be passing for
        the wrong reason too."""
        self._tag('UHD 4K', ['2160p', 'UHD', '4K'])
        out = self._render('{title}', 'Show UHD Broadcast', [('UHD 4K', 'replace')])
        self.assertEqual(out, 'Show UHD 4K Broadcast')

    def test_longest_pattern_wins_over_an_overlapping_shorter_one(self):
        """`HD` is a prefix of `HDR`, so a pass that tried the short one first would leave
        an `R` stranded behind the replacement."""
        self._tag('HighDef', ['HD', 'HDR'])
        out = self._render('{title}', 'Movie HDR Cut', [('HighDef', 'replace')])
        self.assertEqual(out, 'Movie HighDef Cut')

    def test_remove_mode_still_deletes_every_pattern(self):
        """The half that was never broken, pinned so the rewrite cannot regress it."""
        self._tag('junk', ['2160p', 'UHD'])
        out = self._render('{title}', 'Show 2160p UHD Extra', [('junk', 'remove')])
        self.assertEqual(out, 'Show   Extra'.replace('  ', '  '))

    def test_remove_and_replace_apply_in_one_pass_across_tags(self):
        """Two tags, two modes, one pass - a second tag must not re-scan what the first
        one wrote either."""
        self._tag('UHD 4K', ['2160p'])
        self._tag('noise', ['(backup)'])
        out = self._render('{title}', 'Show 2160p (backup)',
                           [('noise', 'remove'), ('UHD 4K', 'replace')])
        self.assertEqual(out.strip(), 'Show UHD 4K')

    def test_a_pattern_with_regex_metacharacters_is_matched_literally(self):
        """Patterns are literal strings, not expressions - the alternation escapes them.
        `(backup)` would otherwise compile as a capture group and match nothing."""
        self._tag('clean', ['(backup)'])
        out = self._render('{title}', 'Show (backup) Tonight', [('clean', 'remove')])
        self.assertNotIn('(backup)', out)
        self.assertIn('Show', out)
        self.assertIn('Tonight', out)


if __name__ == '__main__':
    unittest.main()
