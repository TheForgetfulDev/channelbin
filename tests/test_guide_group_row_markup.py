"""A channel group's TV Guide row has to be legible AS a group at a glance.

`Channel.in_guide` is independent of group membership, so a group and one of its own
members can both hold guide rows at the same time - and the group's row draws its logo
from whichever member is currently serving it, so the two rows can carry near-identical
names and the identical image. Only one of them fails over when the stream drops; the
other is a plain channel recording with no failover at all. Until dev/changelog/856 the
entire difference between them was a `--fs-3xs` muted-grey chip, and the wrong row got
recorded for real.

These pin the three cues that replaced it, and the specificity trap that would have
silently disabled one of them:

  * the row carries `is-group`, which is what the tint hangs off;
  * the logo tile carries `is-grouplogo`, which is what the offset square hangs off -
    a SHAPE cue, because it is the only one of the three that survives the 60px
    logo-only mobile column where the name is not rendered at all;
  * `.guide-group-badge` overrides `.qbadge`'s muted treatment rather than being a new
    `.badge` caller, which DESIGN.md 11.1 forbids in the guide.

The hover assertion is not decoration. `.guide-channel-row:hover` and
`.guide-channel-row.is-group` have equal specificity and the hover rule is earlier in
the file, so the tint wins on hover unless `.guide-channel-row.is-group:hover` is
declared - which would make a group row the one row in the guide that never
acknowledges the pointer.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE_CSS = os.path.join(REPO, 'static', 'css', 'guide.css')

# The channel column's per-row markup, one match per row, so a test can ask about the
# group's row and the plain channel's row separately rather than about the page.
ROW_RE = re.compile(r'<div class="guide-channel-row([^"]*)"(.*?)(?=<div class="guide-channel-row|<div class="guide-program-area)', re.S)


def _rows(html):
    """{channel name: (row classes, that row's markup)} for every rendered guide row."""
    out = {}
    for classes, body in ROW_RE.findall(html):
        name = re.search(r'<span class="guide-channel-name">([^<]*)</span>', body)
        out[name.group(1).strip()] = (classes, body)
    return out


class GuideGroupRowMarkupTests(unittest.TestCase):
    """The rendered /guide marks a group row and leaves a plain channel row alone."""

    @classmethod
    def setUpClass(cls):
        # One app and one render for the whole class: every case reads the markup and
        # none rewrites the state it was rendered from (dev/changelog/979).
        cls.t = make_test_app()
        cls.ctx = cls.t.app.app_context()
        cls.ctx.push()
        acct = seed.make_account()
        # The confusable pair the cues exist for: a member channel holding its own row
        # next to the group that also holds one.
        cls.member = seed.make_channel(acct, stream_id=1, name='Fox Sports 1',
                                        in_guide=True)
        seed.make_group(name='FS1', members=[cls.member], in_guide=True)
        db.session.commit()
        cls.rows = _rows(cls.t.client.get('/guide').get_data(as_text=True))

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()
        cls.t.cleanup()

    def test_both_rows_render(self):
        self.assertIn('FS1', self.rows)
        self.assertIn('Fox Sports 1', self.rows)

    def test_the_group_row_is_marked_as_one(self):
        self.assertIn('is-group', self.rows['FS1'][0])

    def test_a_plain_channel_row_is_not(self):
        self.assertNotIn('is-group', self.rows['Fox Sports 1'][0])

    def test_the_group_logo_tile_is_marked(self):
        self.assertIn('is-grouplogo', self.rows['FS1'][1])

    def test_a_plain_channel_logo_tile_is_not(self):
        self.assertNotIn('is-grouplogo', self.rows['Fox Sports 1'][1])

    def test_every_row_has_a_logo_tile_to_hang_the_square_off(self):
        """The wrapper is unconditional - an <img> cannot carry a ::before."""
        for name, (_, body) in self.rows.items():
            self.assertIn('guide-logo-tile', body, name)

    def test_the_group_badge_survives_in_the_meta_strip(self):
        self.assertIn('guide-group-badge', self.rows['FS1'][1])
        self.assertNotIn('guide-group-badge', self.rows['Fox Sports 1'][1])


class GuideGroupRowStyleTests(unittest.TestCase):
    """The three cues are actually declared, including the one specificity trap."""

    @classmethod
    def setUpClass(cls):
        with open(GUIDE_CSS) as fh:
            cls.css = fh.read()

    def test_the_group_row_is_tinted(self):
        self.assertRegex(self.css, r'\.guide-channel-row\.is-group\s*\{[^}]*background:')

    def test_the_tint_does_not_swallow_the_hover_state(self):
        self.assertRegex(
            self.css, r'\.guide-channel-row\.is-group:hover\s*\{[^}]*background:')

    def test_the_tint_stops_at_the_channel_column(self):
        """`.guide-row` is the program-cell half; tinting it bands the whole grid."""
        self.assertNotRegex(self.css, r'\.guide-row\.is-group\s*\{')

    def test_the_stacked_tile_is_drawn(self):
        self.assertRegex(self.css, r'\.guide-logo-tile\.is-grouplogo::before\s*\{')

    def test_the_square_is_isolated_into_the_tile(self):
        """A bare negative z-index falls behind the ROW's background instead."""
        self.assertRegex(
            self.css, r'\.guide-logo-tile\.is-grouplogo\s*\{[^}]*isolation:\s*isolate')

    def test_the_badge_overrides_the_muted_qbadge_treatment(self):
        badge = re.search(r'\.guide-group-badge\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(badge)
        self.assertIn('color:', badge.group(1))
        self.assertIn('background:', badge.group(1))
        # .guide-channel-meta is nowrap + overflow:hidden, so the cue that names the row
        # must not be what the strip clips.
        self.assertIn('flex: 0 0 auto', badge.group(1))

    def test_the_badge_stays_on_the_guides_own_dense_badge_language(self):
        """DESIGN.md 11.1 - no `.badge`/`.b-*` callers inside the guide."""
        self.assertNotRegex(self.css, r'\.guide-group-badge[^{]*\{[^}]*@extend')
        with open(os.path.join(REPO, 'templates', 'guide.html')) as fh:
            self.assertIn('qbadge guide-group-badge', fh.read())


if __name__ == '__main__':
    unittest.main()
