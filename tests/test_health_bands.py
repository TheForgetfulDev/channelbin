"""Tier 0/2 - the one declared health-band list (app/health_bands.py, dev/changelog/771).

Before this, "is 63 red, amber or green?" was an inline `< 50 / < 80` ternary re-typed at
eleven sites across Python, Jinja and JS, and every one of them ended in a trailing `else`
that rendered a real band - the shape CLAUDE.md's "one flag, one meaning; states are
enumerated" rule exists to stop, because the next band added lands in that `else` silently
at whichever sites were missed.

Covers, in order:
  - BandResolutionTests: the four bands, their labels, and the boundary scores.
  - BadConfigTests: unusable cut points fall back loudly instead of being repaired quietly.
  - FailingBandTests: "which band counts as failing" and the numeric threshold derived
    from it, including channel_failing_reason's rule 3.
  - ConfigMigrationTests: config_version 2 - failing_score_threshold -> failing_band.
  - SettingsRouteTests: the save path refuses cut points that do not descend, and an
    unknown band, server-side (the GUI's own guard is not the enforcement).
  - SingleSourceTests: static scans proving no second copy of the numbers came back and
    every band key has a CSS rule to color it.

No network, no real config.yaml writes outside a temp dir - see CLAUDE.md §Testing.
Run standalone:
  python3 -m unittest tests.test_health_bands
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import health_bands  # noqa: E402
from app.config import _cfg_m002_failing_band  # noqa: E402
from app.database import Channel  # noqa: E402
from app.health_score import channel_failing_reason  # noqa: E402
from tests.support import make_test_app  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cfg(**channel_testing):
    return {'channel_testing': channel_testing}


class BandResolutionTests(unittest.TestCase):
    """The default scale, its labels, and every boundary score."""

    def test_defaults_are_four_bands_highest_first(self):
        bands = health_bands.resolve_bands({})
        self.assertEqual([b.key for b in bands], ['great', 'good', 'fair', 'poor'])
        self.assertEqual([b.floor for b in bands], [90, 80, 50, 0])

    def test_labels_carry_the_configured_range(self):
        bands = health_bands.resolve_bands({})
        self.assertEqual([b.label for b in bands],
                         ['Great (90+)', 'Good (80-89)', 'Fair (50-79)', 'Poor (under 50)'])

    def test_labels_follow_configured_cut_points(self):
        bands = health_bands.resolve_bands(_cfg(health_bands={'great': 95, 'good': 70, 'fair': 40}))
        self.assertEqual([b.label for b in bands],
                         ['Great (95+)', 'Good (70-94)', 'Fair (40-69)', 'Poor (under 40)'])

    def test_band_for_at_every_boundary(self):
        bands = health_bands.resolve_bands({})
        cases = [(100, 'great'), (90, 'great'), (89, 'good'), (80, 'good'),
                 (79, 'fair'), (50, 'fair'), (49, 'poor'), (0, 'poor')]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(health_bands.band_for(score, bands), expected)

    def test_never_tested_is_not_the_worst_band(self):
        """DESIGN.md 12.4: no measurement must never render as a bad measurement."""
        bands = health_bands.resolve_bands({})
        self.assertEqual(health_bands.band_for(None, bands), 'untested')

    def test_negative_score_lands_in_the_bottom_band(self):
        """Manual health adjustment is applied unclamped (channel_search.effective_health),
        so a score below every floor is reachable and must still band."""
        bands = health_bands.resolve_bands({})
        self.assertEqual(health_bands.band_for(-20, bands), 'poor')

    def test_configured_cut_points_move_the_boundaries(self):
        bands = health_bands.resolve_bands(_cfg(health_bands={'great': 95, 'good': 70, 'fair': 40}))
        self.assertEqual(health_bands.band_for(94, bands), 'good')
        self.assertEqual(health_bands.band_for(69, bands), 'fair')
        self.assertEqual(health_bands.band_for(39, bands), 'poor')

    def test_payload_carries_key_name_label_and_css_for_every_band(self):
        payload = health_bands.bands_payload(health_bands.resolve_bands({}))
        self.assertEqual(len(payload['bands']), 4)
        for entry in payload['bands']:
            for field in ('key', 'name', 'label', 'floor', 'css'):
                self.assertIn(field, entry)
            self.assertEqual(entry['css'], f"hb-{entry['key']}")
        self.assertEqual(payload['untested']['css'], 'hb-none')


class BadConfigTests(unittest.TestCase):
    """Unusable cut points fall back to the defaults and SAY so - product principle 1: a
    number the user cannot explain is worse than no number."""

    def test_non_descending_cut_points_fall_back_and_warn(self):
        cfg = _cfg(health_bands={'great': 40, 'good': 80, 'fair': 50})
        with self.assertLogs('app.health_bands', level='WARNING') as logs:
            bands = health_bands.resolve_bands(cfg)
        self.assertEqual([b.floor for b in bands], [90, 80, 50, 0])
        self.assertIn('descend', '\n'.join(logs.output))

    def test_non_numeric_cut_point_falls_back_and_warns(self):
        cfg = _cfg(health_bands={'great': 'high', 'good': 80, 'fair': 50})
        with self.assertLogs('app.health_bands', level='WARNING'):
            bands = health_bands.resolve_bands(cfg)
        self.assertEqual([b.floor for b in bands], [90, 80, 50, 0])

    def test_out_of_range_cut_point_falls_back(self):
        cfg = _cfg(health_bands={'great': 900, 'good': 80, 'fair': 50})
        with self.assertLogs('app.health_bands', level='WARNING'):
            bands = health_bands.resolve_bands(cfg)
        self.assertEqual([b.floor for b in bands], [90, 80, 50, 0])

    def test_validate_floors_accepts_a_usable_scale(self):
        self.assertEqual(health_bands.validate_floors({'great': 95, 'good': 70, 'fair': 40}), '')

    def test_unknown_failing_band_falls_back_and_warns(self):
        with self.assertLogs('app.health_bands', level='WARNING'):
            self.assertEqual(health_bands.failing_band_key(_cfg(failing_band='terrible')), 'poor')


class FailingBandTests(unittest.TestCase):
    """"Counts as failing" is declared as a band; the number is derived from it."""

    def test_default_failing_band_is_poor(self):
        self.assertEqual(health_bands.failing_band_key({}), 'poor')

    def test_threshold_is_the_ceiling_of_the_failing_band(self):
        self.assertEqual(health_bands.failing_threshold(_cfg(failing_band='poor')), 50)
        self.assertEqual(health_bands.failing_threshold(_cfg(failing_band='fair')), 80)
        self.assertEqual(health_bands.failing_threshold(_cfg(failing_band='good')), 90)

    def test_threshold_follows_configured_cut_points(self):
        cfg = _cfg(failing_band='poor', health_bands={'great': 95, 'good': 70, 'fair': 40})
        self.assertEqual(health_bands.failing_threshold(cfg), 40)

    def test_none_disables_the_rule_entirely(self):
        self.assertIsNone(health_bands.failing_threshold(_cfg(failing_band='none')))

    def test_top_band_failing_means_every_score_fails(self):
        self.assertEqual(health_bands.failing_threshold(_cfg(failing_band='great')), 101)

    def test_band_is_failing_covers_the_band_and_everything_below(self):
        cfg = _cfg(failing_band='fair')
        self.assertTrue(health_bands.band_is_failing('fair', cfg))
        self.assertTrue(health_bands.band_is_failing('poor', cfg))
        self.assertFalse(health_bands.band_is_failing('good', cfg))
        self.assertFalse(health_bands.band_is_failing('great', cfg))

    def test_untested_is_never_failing(self):
        self.assertFalse(health_bands.band_is_failing('untested', _cfg(failing_band='great')))

    def test_channel_failing_reason_names_the_band(self):
        channel = Channel(account_id=None, stream_id=1, name='Banded',
                          stream_url='http://example.test/live/1', health_score=30,
                          consecutive_test_failures=0)
        reason = channel_failing_reason(None, channel, _cfg(failing_band='poor',
                                                            failing_streak_threshold=3))
        self.assertIsNotNone(reason)
        self.assertIn('Poor', reason)

    def test_channel_failing_reason_respects_a_raised_failing_band(self):
        """A Fair channel is fine by default and failing once Fair is declared failing -
        the setting has to actually reach the pre-recording warning."""
        channel = Channel(account_id=None, stream_id=1, name='Fairish',
                          stream_url='http://example.test/live/1', health_score=60,
                          consecutive_test_failures=0)
        self.assertIsNone(channel_failing_reason(None, channel, _cfg(failing_band='poor')))
        self.assertIsNotNone(channel_failing_reason(None, channel, _cfg(failing_band='fair')))


class ConfigMigrationTests(unittest.TestCase):
    """config_version 2: the raw failing_score_threshold became a band name."""

    def test_default_threshold_becomes_poor(self):
        cfg = {'channel_testing': {'failing_score_threshold': 25}}
        with self.assertLogs('app.config', level='WARNING'):
            out = _cfg_m002_failing_band(cfg)
        self.assertEqual(out['channel_testing']['failing_band'], 'poor')
        self.assertNotIn('failing_score_threshold', out['channel_testing'])

    def test_zero_threshold_becomes_none(self):
        cfg = {'channel_testing': {'failing_score_threshold': 0}}
        with self.assertLogs('app.config', level='WARNING'):
            out = _cfg_m002_failing_band(cfg)
        self.assertEqual(out['channel_testing']['failing_band'], 'none')

    def test_threshold_inside_the_fair_band_becomes_fair(self):
        cfg = {'channel_testing': {'failing_score_threshold': 60}}
        with self.assertLogs('app.config', level='WARNING'):
            out = _cfg_m002_failing_band(cfg)
        self.assertEqual(out['channel_testing']['failing_band'], 'fair')

    def test_a_config_without_the_old_key_is_untouched(self):
        cfg = {'channel_testing': {'failing_band': 'fair'}}
        self.assertEqual(_cfg_m002_failing_band(cfg)['channel_testing']['failing_band'], 'fair')


class SettingsRouteTests(unittest.TestCase):
    """Enforcement lives server-side (CLAUDE.md) - the GUI's own guard is not the guard."""

    def setUp(self):
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def _save(self, path, value):
        return self.client.post('/api/settings/field', json={'path': path, 'value': value})

    def test_cut_points_that_do_not_descend_are_refused(self):
        resp = self._save('channel_testing.health_bands.good', 95)
        self.assertEqual(resp.status_code, 400)
        self.assertIn('descend', resp.get_json()['error'])

    def test_non_numeric_cut_point_is_refused(self):
        resp = self._save('channel_testing.health_bands.fair', 'fifty')
        self.assertEqual(resp.status_code, 400)

    def test_unknown_failing_band_is_refused(self):
        resp = self._save('channel_testing.failing_band', 'terrible')
        self.assertEqual(resp.status_code, 400)

    def test_a_usable_cut_point_is_accepted(self):
        resp = self._save('channel_testing.health_bands.fair', 45)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))


class MetaTagTests(unittest.TestCase):
    """base.html serves the resolved bands so the browser bands from the same list."""

    def setUp(self):
        self.t = make_test_app()
        self.client = self.t.app.test_client()

    def tearDown(self):
        self.t.cleanup()

    def test_every_page_carries_the_health_bands_meta_tag(self):
        html = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('name="health-bands"', html)
        for key in health_bands.BAND_KEYS:
            self.assertIn(key, html)


class SingleSourceTests(unittest.TestCase):
    """Static scans: the numbers must not come back, and a band must not ship uncolored."""

    #: Health-score banding, specifically. `frame_pct` and bitrate comparisons legitimately
    #: use these numbers for a different measurement and are not this rule's business.
    _TERNARY_RE = re.compile(r'(health|hscore|\bscore\b)[^;\n]{0,80}<\s*(50|80)\b', re.I)

    def _scan(self, rel_dirs, exts):
        hits = []
        for rel in rel_dirs:
            for dirpath, _dirs, files in os.walk(os.path.join(ROOT, rel)):
                for name in files:
                    if not name.endswith(exts):
                        continue
                    path = os.path.join(dirpath, name)
                    with open(path, encoding='utf-8') as fh:
                        for i, line in enumerate(fh, 1):
                            if self._TERNARY_RE.search(line):
                                hits.append(f'{os.path.relpath(path, ROOT)}:{i}')
        return hits

    def test_no_hardcoded_health_cut_point_in_templates_or_js(self):
        hits = self._scan(['templates', os.path.join('static', 'js')], ('.html', '.js'))
        self.assertEqual(
            hits, [],
            'A health score banded against a literal cut point instead of the one declared '
            f'list (app/health_bands.py). Use `| health_css` / healthBandCss(): {hits}')

    def test_every_band_key_has_a_css_rule(self):
        """A band with no `.hb-*` rule renders in plain body color and is silently absent -
        the same invisible-UI class CssClassDefinedTests guards."""
        css = ''
        for name in ('style.css', 'guide.css', 'channel-search.css'):
            path = os.path.join(ROOT, 'static', 'css', name)
            if os.path.exists(path):
                with open(path, encoding='utf-8') as fh:
                    css += fh.read()
        missing = [k for k in health_bands.BAND_KEYS
                   if not re.search(rf'\.hb-{k}\b', css)]
        self.assertEqual(missing, [], f'Health band with no CSS rule: {missing}')
        self.assertRegex(css, r'\.hb-none\b')

    def test_every_band_key_has_a_display_name(self):
        for key in health_bands.BAND_KEYS:
            self.assertIn(key, health_bands.BAND_NAMES)


if __name__ == '__main__':
    unittest.main()
