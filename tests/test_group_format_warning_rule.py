"""A group format warning appears only for a hand-pinned format with a recording member off it.

dev/changelog/925 (the rule is dev/changelog/923 Decision 4). Under every automatic strategy and
`unmanaged`, members spanning formats is expected and moving between them is the point, so the
groups list's `Mixed format` badge, the group page's format banners and the loud per-member pill
stay quiet. Recording-off members never count. Before this, the badge counted every member under
every strategy and its tooltip claimed mixed formats "break failover recordings", which
dev/changelog/754 measured false.
"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support.seed import (make_account, make_channel, make_group,  # noqa: E402
                                make_channel_test)
from app import db  # noqa: E402
from app.channel_groups import pinned_format_offenders  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
        return fh.read()


class PinnedFormatOffendersTests(unittest.TestCase):
    """The one definition both pages ask."""

    LOCK = ('1920x1080', 60)

    def _group(self, strategy, lock=LOCK):
        return SimpleNamespace(format_strategy=strategy, locked_format_key=lock)

    def setUp(self):
        self.on = SimpleNamespace(id=1)
        self.off = SimpleNamespace(id=2)
        self.untested = SimpleNamespace(id=3)
        self.latest = {
            1: SimpleNamespace(resolution='1920x1080', fps=59.94),
            2: SimpleNamespace(resolution='1280x720', fps=60.0),
        }
        self.members = [self.on, self.off, self.untested]

    def test_a_hand_pinned_format_names_the_member_off_it(self):
        self.assertEqual(pinned_format_offenders(self._group('manual'), self.members, self.latest),
                         [self.off], '59.94 rounds to the pinned 60; the untested member is unknown')

    def test_no_automatic_strategy_warns_even_with_a_lock_in_force(self):
        for strategy in ('highest_score', 'highest_resolution', 'highest_bitrate',
                         'most_channels', 'balanced', 'unmanaged', 'health_check_only'):
            with self.subTest(strategy=strategy):
                self.assertEqual(
                    pinned_format_offenders(self._group(strategy), self.members, self.latest), [])

    def test_manual_with_no_pin_warns_about_nothing(self):
        self.assertEqual(
            pinned_format_offenders(self._group('manual', lock=None), self.members, self.latest), [])

    def test_no_group_warns_about_nothing(self):
        self.assertEqual(pinned_format_offenders(None, self.members, self.latest), [])


class _GroupFixture(unittest.TestCase):
    """Two measured members, 1080p60 and 720p60, both recording unless told otherwise."""

    def setUp(self):
        self.t = make_test_app()
        self.app = self.t.app
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _seed(self, strategy, lock=('1920x1080', 60), b_recording=True):
        with self.app.app_context():
            acct = make_account()
            a = make_channel(acct, name='FS1 East')
            b = make_channel(acct, name='FS1 West')
            make_channel_test(a, all_null=False, status='COMPLETED', resolution='1920x1080',
                              fps=60.0, bitrate_kbps=6000.0)
            make_channel_test(b, all_null=False, status='COMPLETED', resolution='1280x720',
                              fps=60.0, bitrate_kbps=3000.0)
            grp = make_group('FS1', members=[a, b], in_guide=False,
                             disabled=None if b_recording else [b.id],
                             format_strategy=strategy,
                             format_resolution=lock[0] if lock else None,
                             format_fps=lock[1] if lock else None)
            db.session.commit()
            return grp.id, b.id


class GroupsListBadgeTests(_GroupFixture):
    """The groups list's red badge, its health label and its `Needs attention` filter token."""

    def _row(self, gid):
        html = self.client.get('/channel-groups').get_data(as_text=True)
        start = html.index(f'class="grp-item" data-group="{gid}"')
        end = html.find('class="grp-item"', start + 1)
        return html[start:end if end != -1 else len(html)]

    def test_a_hand_pinned_format_with_a_recording_member_off_it_shows_the_badge(self):
        gid, _ = self._seed('manual')
        row = self._row(gid)
        self.assertIn('data-tip="Mixed format.&#10;', row)
        self.assertIn('pinned by hand to 1920x1080 @ 60', row)
        self.assertIn('FS1 West (1280x720 @ 60)', row, 'the tooltip names the member off the pin')
        self.assertNotIn('&#9940;', row, 'mockup 39: the pill carries no glyph')
        self.assertIn('data-issues="mismatch', row)

    def test_a_recording_off_member_never_counts(self):
        gid, _ = self._seed('manual', b_recording=False)
        row = self._row(gid)
        self.assertNotIn('Mixed format', row)
        self.assertNotIn('mismatch', row.split('data-issues="', 1)[1].split('"', 1)[0])

    def test_an_automatic_lock_with_a_member_off_it_is_quiet(self):
        """The lock still filters (see BannerFactsRuleTests), but the strategy moving the
        group between formats is its job, so the list says nothing."""
        for strategy in ('highest_bitrate', 'most_channels', 'balanced', 'highest_resolution'):
            with self.subTest(strategy=strategy):
                gid, _ = self._seed(strategy)
                row = self._row(gid)
                self.assertNotIn('Mixed format', row)
                self.assertNotIn('mismatch', row.split('data-issues="', 1)[1].split('"', 1)[0])

    def test_unmanaged_and_floating_groups_with_differing_members_are_quiet(self):
        for strategy in ('unmanaged', 'highest_score'):
            with self.subTest(strategy=strategy):
                gid, _ = self._seed(strategy, lock=None)
                self.assertNotIn('Mixed format', self._row(gid))

    def test_the_false_failover_claim_is_gone(self):
        """dev/changelog/754 measured mixed-format concat and remux exiting clean."""
        self.assertNotIn('break failover', _read('templates/channels/groups.html'))


class BannerFactsRuleTests(_GroupFixture):
    """`format_warns` gates both group page banners and the loud per-member pill."""

    def _payload(self, gid):
        r = self.client.get(f'/api/channel-groups/{gid}/detail-rows')
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_a_hand_pinned_format_warns(self):
        gid, _ = self._seed('manual')
        w = self._payload(gid)['warnings']
        self.assertTrue(w['format_warns'])
        self.assertEqual(w['format_blocked_count'], 1)

    def test_an_automatic_lock_filters_but_does_not_warn(self):
        """Selection is untouched: the member off the lock is still format_blocked, which is
        what keeps its quiet per-row note honest. Only the warning is gone."""
        gid, b_id = self._seed('highest_bitrate')
        payload = self._payload(gid)
        self.assertFalse(payload['warnings']['format_warns'])
        self.assertEqual(payload['warnings']['format_blocked_count'], 1)
        rows = {r['channel_id']: r for r in payload['rows']}
        self.assertTrue(rows[b_id]['format_blocked'])

    def test_unmanaged_does_not_warn(self):
        gid, _ = self._seed('unmanaged', lock=None)
        self.assertFalse(self._payload(gid)['warnings']['format_warns'])

    def test_the_group_page_still_renders(self):
        gid, _ = self._seed('manual')
        self.assertEqual(self.client.get(f'/channel-groups/{gid}').status_code, 200)


class ClientGatingTests(unittest.TestCase):
    """Source assertions, in the style of test_group_detail_page_conformance.py: jsdom is not
    wired up for group-detail.js."""

    def setUp(self):
        self.js = _read('static/js/group-detail.js')

    def test_the_unmanaged_banner_is_gone(self):
        banners = self.js[self.js.index('function renderBanners()'):
                          self.js.index('function muteWarning(')]
        self.assertNotIn('No format management, and these members differ', banners)
        self.assertNotIn('plays back wrong', banners)

    def test_the_override_banner_needs_a_hand_pinned_format(self):
        banners = self.js[self.js.index('function renderBanners()'):
                          self.js.index('function muteWarning(')]
        self.assertIn('WARN.format_warns && WARN.format_override', banners)

    def test_the_loud_row_pill_needs_a_hand_pinned_format(self):
        warn = self.js[self.js.index('function rowWarnings(r)'):self.js.index('const rowDims =')]
        loud = warn.index("pill: 'Format mismatch'")
        self.assertIn('r.format_blocked && WARN && WARN.format_warns', warn[:loud])
        quiet = warn[warn.index('} else if (r.format_blocked) {'):warn.index('floatingMismatch(r)')]
        self.assertIn('note: true', quiet)
        self.assertNotIn('b-warn', quiet)

    def test_the_unmanaged_strategy_help_makes_no_false_playback_claim(self):
        self.assertNotIn('plays back wrong', _read('static/js/format-plan.js'))

    def test_a_format_change_is_a_neutral_log_line(self):
        tl = _read('templates/channels/_timeline.html')
        self.assertIn("'CHANNEL_GROUP_FORMAT_MISMATCH': 'neutral'", tl)
        self.assertIn("'CHANNEL_GROUP_FORMAT_RESOLVED': 'neutral'", tl)


if __name__ == '__main__':
    unittest.main()
