"""The Notifications surface against the design standard it was converted to.

DESIGN.md 15.2/15.3/15.6/15.7 (approved 2026-08-02) and the app-wide rules it
inherits. Rollout: dev/changelog/440. The Playwright pass that produced the approved
design logged two conformance gaps in the shipped pages; the search one is guarded in
tests/test_settings_page_conformance.py, and the one this file opens with - no h1 on
this surface at all - is the other (dev/docs/BUGS.md 2026-08-03).

Layout (the popover's clamping, the routing table becoming cards) needs real geometry
and is Tier 4; what is assertable here is the markup contract those behaviors need.
"""
import json
import re
import unittest
from unittest.mock import patch

from tests.support.app import make_test_app


def _cfg(services=None, routing=None):
    """A config with a known notifications block.

    The route calls load_config() at run time, so make_test_app overrides cannot reach
    it (CLAUDE.md §Testing) - patching the module's own reference is the only way to
    make what the page renders deterministic.
    """
    return {
        'notifications': {
            'push_rate_limit_seconds': 90,
            'base_url': 'http://dvr.local:5000',
            'services': services if services is not None else {},
            'routing': routing or {},
        },
    }


class NotificationsPageConformanceTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _html(self, cfg=None):
        with patch('app.routes.settings.load_config', return_value=cfg or _cfg()):
            return self.client.get('/settings/notifications').get_data(as_text=True)

    def _boot(self, cfg=None):
        html = self._html(cfg)
        blob = re.search(r'<script type="application/json" id="notif-boot">(.*?)</script>',
                         html, re.S)
        self.assertIsNotNone(blob, 'the page carries no boot payload')
        return json.loads(blob.group(1))

    def test_page_has_exactly_one_h1(self):
        """DESIGN.md 3.10: a page announces itself once, with an h1.

        This surface opened on an <h2> and had no h1 at all - the second of the two
        conformance gaps the chunk-4 design round logged against shipped code.
        """
        self.assertEqual(len(re.findall(r'<h1[ >]', self._html())), 1)

    def test_the_services_region_has_no_server_rendered_cards(self):
        """DESIGN.md 15.7: one writer for the add bar, the cards and the empty state.

        A Jinja first paint plus a JS re-render after every add/remove is two writers,
        and two writers can disagree about which services are added. The server renders
        the two empty containers and the data; notifications.js writes all three
        regions from that one computation.
        """
        html = self._html(_cfg({'pushover': {'enabled': True, 'url': 'pover://x'}}))
        self.assertIn('<div id="svc-bar"></div>', html)
        self.assertIn('<div class="svc-grid" id="svc-grid"></div>', html)
        self.assertNotIn('data-svc-url=', html)
        self.assertNotIn('class="svc-body"', html)

    def test_added_is_derived_from_enabled_or_a_stored_url(self):
        """DESIGN.md 15.2: added and enabled are two different things, and `added`
        needs no new config key - it is "enabled, or a URL is stored".

        The paused-but-configured service is the case that matters: it keeps its card,
        and therefore its URL, instead of vanishing when it is switched off.
        """
        boot = self._boot(_cfg({
            'pushover': {'enabled': True, 'url': 'pover://tok@user'},
            'smtp2go': {'enabled': False, 'url': 'mailtos://u:p@mail.smtp2go.com/x'},
            'discord': {'enabled': False, 'url': ''},
        }))
        self.assertTrue(boot['services']['pushover']['added'])
        self.assertTrue(boot['services']['smtp2go']['added'], 'a paused service lost its card')
        self.assertFalse(boot['services']['discord']['added'])

    def test_every_known_service_is_offered_even_when_unconfigured(self):
        """`Add a service` can only offer what the payload names, so the payload
        carries every service the app supports - not only the configured ones."""
        from app.notifications import SERVICE_LABELS
        boot = self._boot(_cfg({}))
        self.assertEqual(set(boot['services']), set(SERVICE_LABELS))

    def test_routing_covers_every_alert_type(self):
        """A type missing from config.yaml is still routable: the payload is
        ALERT_TYPES merged over what is stored, never just what is stored."""
        from app.alerts import ALERT_TYPES
        boot = self._boot(_cfg({}, {'CONVERSION_FAILED': {'in_app': False, 'push_services': []}}))
        self.assertEqual(set(boot['routing']), set(ALERT_TYPES))
        self.assertFalse(boot['routing']['CONVERSION_FAILED']['in_app'])

    def test_routing_drops_push_targets_that_are_not_services(self):
        """A stale key left in config.yaml must not reach the page: the dropdown
        renders labels from the service map, and an unknown key has none."""
        boot = self._boot(_cfg({}, {'CONVERSION_FAILED': {
            'in_app': True, 'push_services': ['pushover', 'gone_service']}}))
        self.assertEqual(boot['routing']['CONVERSION_FAILED']['push_services'], ['pushover'])

    def test_the_routing_row_labels_its_controls_for_mobile(self):
        """DESIGN.md 15.5 item 4: the routing table becomes cards at <=768px, and each
        card labels its two controls in words - which is what a scrolled table loses
        the moment its header scrolls off. The labels are in one markup, hidden above
        the breakpoint, so there is no second rendering of the same rows.
        """
        import os
        js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'static', 'js', 'notifications.js'), encoding='utf-8').read()
        self.assertIn('Show in the Alert Center', js)
        self.assertIn('>Push to<', js)
        html = self._html()
        self.assertIn('.rt-cl { display: none;', html)
        mobile = re.search(r'@media \(max-width: 768px\) \{(.*?)\n\}', html, re.S)
        self.assertIsNotNone(mobile, 'the page has no 768px block')
        self.assertIn('.rt-cl { display: block; }', mobile.group(1))

    def test_routing_table_has_search_and_severity_filters(self):
        """dev/changelog/532: 21+ alert types is already more than fits on a screen, so
        the routing table gets a dedicated search + severity/push filter bar (this page
        has no page-wide search of its own to scope into - it's a standalone settings
        subpage). DESIGN.md 3.11 requires a clear `x` on every search/filter input.
        """
        from app.alerts import ALERT_TYPES
        html = self._html()
        self.assertIn('id="rt-search"', html)
        self.assertIn('id="rt-search-clear"', html, 'search input has no clear x (DESIGN.md 3.11)')
        self.assertIn('id="rt-sev-chips"', html)
        self.assertIn('id="rt-push-chip"', html)
        self.assertIn('id="rt-no-results"', html, 'no empty state for a filter with zero matches')

        import os
        js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'static', 'js', 'notifications.js'), encoding='utf-8').read()
        sevs = re.search(r"SEVERITIES = \[(.*?)\]", js)
        self.assertIsNotNone(sevs, 'notifications.js declares no severity chip list')
        chip_severities = set(re.findall(r"'(\w+)'", sevs.group(1)))
        # The chip list must cover every severity ALERT_TYPES actually uses, or a
        # future alert type with a new severity becomes unfilterable-to.
        real_severities = {meta['severity'] for meta in ALERT_TYPES.values()}
        self.assertEqual(chip_severities, real_severities)


class ServiceRemovalTests(unittest.TestCase):
    """DELETE /api/notifications/services/<name>.

    Teardown releases everything the create path acquired: a removal that cleared the
    card but left the service selected in half the routing rows would silently come
    back the moment it was re-added, and a URL left behind is a credential the page
    claims it deleted.
    """

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False  # jsonFetch supplies the token in the app
        self.client = self.t.app.test_client()
        self.saved = {}

    def tearDown(self):
        self.t.cleanup()

    def _delete(self, name, cfg):
        # save_config would rewrite the REAL config.yaml, so it is captured rather
        # than run (CLAUDE.md §Testing - never touches production). The route reads the
        # raw config.yaml dict, not the merged config, so that its save persists only what
        # the user actually set (dev/changelog/727) - so that is the reader to stand in for.
        with patch('app.routes.settings._load_config_file', return_value=cfg), \
             patch('app.routes.settings.save_config',
                   side_effect=lambda c: self.saved.update(c) or []):
            return self.client.delete(f'/api/notifications/services/{name}')

    def test_removal_clears_the_url_and_the_enabled_flag(self):
        cfg = _cfg({'pushover': {'enabled': True, 'url': 'pover://tok@user'}})
        resp = self._delete('pushover', cfg)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()['success'])
        self.assertEqual(self.saved['notifications']['services']['pushover'],
                         {'enabled': False, 'url': ''})

    def test_removal_prunes_the_service_from_every_routing_row(self):
        cfg = _cfg(
            {'pushover': {'enabled': True, 'url': 'pover://tok@user'}},
            {'CONVERSION_FAILED': {'in_app': True, 'push_services': ['pushover', 'discord']},
             'SYNC_EPG_FETCH_FAILED': {'in_app': True, 'push_services': ['pushover']}},
        )
        self._delete('pushover', cfg)
        routing = self.saved['notifications']['routing']
        self.assertEqual(routing['CONVERSION_FAILED']['push_services'], ['discord'])
        self.assertEqual(routing['SYNC_EPG_FETCH_FAILED']['push_services'], [])

    def test_unknown_service_is_rejected(self):
        resp = self._delete('not_a_service', _cfg({}))
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())
        self.assertEqual(self.saved, {}, 'a rejected removal still wrote config.yaml')


if __name__ == '__main__':
    unittest.main()
