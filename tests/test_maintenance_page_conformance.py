"""The Maintenance page against the design that produced it.

DESIGN.md 7 ruled backup/restore, service control, the search index and storage
off Settings; DESIGN.md 16 (approved 2026-08-03) settles the page they landed on.
Rollout: dev/changelog/444. Each case here is a decision from that round that a
careless edit would quietly undo:

  * The card ORDER is the design, not an accident of the file - Storage and Index
    are what you read, Backup and Service are what you act on, and round 2 settled
    that reading comes first.
  * The return to Settings is a BACK link, not a forward jump-off. Rounds 1-8 drew
    it as a header pill naming Settings, which is the wrong half of DESIGN.md 4:
    these panels came off Settings, so going there is going back.
  * The four panels exist on exactly one page. Their absence from Settings is
    asserted in tests/test_settings_page_conformance.py; their presence here is
    the other half of the same claim.
  * The disk meter is drawn from a proportion the server actually sends. It was
    added to /api/system/storage-details in this same change, so the payload
    assertion is what stops the meter from being rendered against a field that
    quietly went away.

Layout itself (the meter's painted width, the 375px stack) needs real geometry and
is Tier 4 rather than anything this file can assert.

Runs against a throwaway temp SQLite DB - never the live dvr.db.
  python3 -m unittest tests.test_maintenance_page_conformance
"""
import re
import unittest

from tests.support.app import make_test_app


class MaintenancePageConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One app and one render for the whole class: every case reads the markup and
        # none rewrites the state it was rendered from (dev/changelog/979).
        cls.t = make_test_app()
        cls.client = cls.t.app.test_client()
        cls.html = cls.client.get('/maintenance').get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def test_the_page_renders(self):
        self.assertEqual(self.client.get('/maintenance').status_code, 200)

    def test_page_has_exactly_one_h1_and_it_names_the_page(self):
        """DESIGN.md 3.10, and 16.1's ruling that the page is called Maintenance.

        The mockup's own h1 still read "System" - stale from before round 2 settled
        the name - so the page title and the nav label agreeing is a real claim
        rather than a formality.
        """
        h1s = re.findall(r'<h1[^>]*>(.*?)</h1>', self.html, re.S)
        self.assertEqual(len(h1s), 1)
        self.assertIn('Maintenance', h1s[0])

    def test_the_cards_render_in_the_approved_order(self):
        """DESIGN.md 16.1: Storage, Index, Backup, Service, plus two later cards.

        The bundle is its own card between Backup and Service rather than a row
        inside Backup - it is neither a backup nor a restore, and inside that card
        it sat between two rows of backup prose (dev/changelog/841).

        External tools sits second, with Storage, on 16.1's own read-then-act split:
        it is a readout with no controls (dev/changelog/910).

        Readiness check sits FIRST, above all of it, settled at mockup round 1: a verdict
        below the fold on a seven-card page is not a verdict (dev/changelog/945, built
        in dev/changelog/950).
        """
        self.assertEqual(re.findall(r'<div class="card" id="(m-[a-z]+)"', self.html),
                         ['m-readiness', 'm-storage', 'm-tools', 'm-index', 'm-backup',
                          'm-bundle', 'm-service'])

    def test_the_return_to_settings_is_a_back_link(self):
        """DESIGN.md 4/16.1: a back link above the h1, never a forward jump-off."""
        back = re.search(r'<a class="back" href="([^"]+)"[^>]*>(.*?)</a>', self.html, re.S)
        self.assertIsNotNone(back, 'the back link is gone')
        self.assertEqual(back.group(1), '/settings')
        self.assertIn('Settings', back.group(2))
        self.assertLess(self.html.index('class="back"'), self.html.index('<h1'))

    def test_every_panel_the_move_brought_across_is_here(self):
        """The controls, not just the cards: a card that arrived without its own
        action is a panel that looks moved and is not."""
        for hook in ('btn-refresh-storage', 'storage-details-content',
                     'search-index-rows', 'btn-rebuild-index',
                     'backup-select', 'btn-show-diff', 'btn-apply-backup', 'btn-backup-now',
                     'btn-restart-now'):
            self.assertIn(f'id="{hook}"', self.html, f'{hook} did not make the move')

    def test_the_page_loads_its_own_script_and_not_the_settings_one(self):
        """settings.js still owns the settings search and scrollspy; none of that
        exists here, and loading it would run a module against a DOM it cannot find."""
        self.assertIn('js/maintenance.js', self.html)
        self.assertNotIn('js/settings.js', self.html)

    def test_the_disk_meter_row_stacks(self):
        """BUGS.md 2026-08-03 @ 05:26 PM ET - the meter rendered 0px wide.

        `.meter` has no intrinsic width, so as a flex ITEM beside `.disk-head` it
        is sized by its content and its percentage-width fill resolves against
        zero. The bar was simply not there, with no error anywhere, and it looked
        correct at 375px only because the media query turns `.mrow` into a block.

        The width itself needs real layout and is Tier 4. What this asserts is the
        coupling: the JS emits the class and the CSS defines it, so neither can be
        dropped alone.
        """
        with open('static/js/maintenance.js') as fh:
            js = fh.read()
        with open('templates/maintenance.html') as fh:
            page = fh.read()
        meter_row = re.search(r'return `<div class="([^"]*)">\s*<div class="disk-head"', js)
        self.assertIsNotNone(meter_row, 'the disk meter row is gone')
        self.assertIn('stacked', meter_row.group(1),
                      'the meter row lost the class that stops it collapsing')
        desktop = page.split('@media')[0]
        self.assertRegex(desktop, r'\.mrow\.stacked\s*\{\s*display:\s*block')

    def test_no_confirm_or_alert_survives_the_move(self):
        """The restart flow used confirm() on Settings. Both dialogs became
        buildModal()/showToast() in the move (DESIGN.md 3.7)."""
        with open('static/js/maintenance.js') as fh:
            js = fh.read()
        self.assertNotIn('confirm(', js, 'a blocking browser dialog survived the move')
        self.assertNotIn('alert(', js, 'a blocking browser dialog survived the move')
        self.assertIn('buildModal(', js)


class StorageDetailsPayloadTests(unittest.TestCase):
    """The disk meter's two ends come from the endpoint, not from the sidebar's
    rounded-GB stats block.

    /api/system/stats has reported disk usage in GB for the sidebar since long
    before this page existed, but it rounds and it answers a different question.
    The meter needs the same units the rest of the storage payload is measured in,
    so storage-details grew disk_total/disk_free in bytes (changelog 444).
    """

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_storage_details_carries_both_ends_of_the_proportion(self):
        payload = self.client.get('/api/system/storage-details').get_json()
        self.assertIn('disk_total', payload)
        self.assertIn('disk_free', payload)
        for key in ('disk_total', 'disk_free'):
            value = payload[key]
            # None is a real answer (an unmounted path); the page draws no meter
            # rather than a meter reading zero.
            self.assertTrue(value is None or isinstance(value, int),
                            f'{key} is {value!r}, which is neither bytes nor null')

    def test_the_two_disk_readouts_agree_with_each_other(self):
        """One statvfs, two consumers. The sidebar's rounded GB and the meter's raw
        bytes describe the same filesystem, so a mount that reports oddly must
        report the same way on both surfaces rather than in two spellings.
        """
        storage = self.client.get('/api/system/storage-details').get_json()
        stats = self.client.get('/api/system/stats').get_json()
        if storage['disk_total'] is None or not stats.get('disk_dvr'):
            self.skipTest('no measurable filesystem for the configured dvr dir')
        self.assertAlmostEqual(storage['disk_total'] / 1073741824,
                               stats['disk_dvr']['total_gb'], delta=0.1)


class ReadinessCardConformanceTests(unittest.TestCase):
    """The Readiness card's own markup against the decisions that produced it.

    Design record: dev/mockups/40-readiness.html, dev/changelog/945, 948, 950. Each case
    is a call a careless edit would quietly undo.
    """

    @classmethod
    def setUpClass(cls):
        # One app and one render for the whole class: every case reads the markup and
        # none rewrites the state it was rendered from (dev/changelog/979).
        cls.t = make_test_app()
        cls.client = cls.t.app.test_client()
        cls.html = cls.client.get('/maintenance').get_data(as_text=True)

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def test_the_card_head_carries_its_own_state_pill(self):
        """On this page the verdict sits below a heading that says Maintenance, so the head
        has to say whether the install is ready without being read to the bottom."""
        self.assertRegex(self.html, r'<h2>Readiness check <span class="badge [^"]*" id="rd-head-pill"')

    def test_the_first_paint_is_the_nothing_active_state(self):
        """Server-rendered initial state must equal the nothing-active state - JS may
        upgrade it, never be required to calm it down (CLAUDE.md frontend rule). So the
        card ships a neutral pill and no verdict at all until /api/readiness answers."""
        head = self.html[self.html.index('id="m-readiness"'):self.html.index('id="m-storage"')]
        self.assertIn('b-abort', head, 'the head pill starts neutral, never Ready or Not ready')
        self.assertNotIn('rd-verdict', head, 'the verdict is never server-rendered')

    def test_the_card_loads_its_own_script(self):
        self.assertIn('js/readiness.js', self.html)

    def test_the_maintenance_nav_link_carries_both_counts(self):
        """The amber count sits beside the red one, the way Alerts shows both.

        Their own class names, deliberately: static/js/nav-alerts.js queries
        .nav-count-bad / .nav-count-warn app-wide, so reusing those would write the ALERT
        numbers into this badge.
        """
        link = re.search(r'<a class="nav-link[^"]*" href="/maintenance">.*?</a>',
                         self.html, re.S)
        self.assertIsNotNone(link, 'the Maintenance nav link is gone')
        self.assertIn('nav-count-ready-bad', link.group(0))
        self.assertIn('nav-count-ready-warn', link.group(0))
        self.assertNotIn('nav-count-bad', link.group(0))
        self.assertNotIn('nav-count-warn', link.group(0))

    def test_the_counts_start_hidden(self):
        link = re.search(r'<a class="nav-link[^"]*" href="/maintenance">.*?</a>',
                         self.html, re.S).group(0)
        for cls in ('nav-count-ready-bad', 'nav-count-ready-warn', 'pip-ready'):
            self.assertRegex(link, rf'{cls}"[^>]*style="display:none"',
                             f'{cls} is rendered visible before anything has been measured')


if __name__ == '__main__':
    unittest.main()
