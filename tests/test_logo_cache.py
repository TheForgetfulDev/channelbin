"""Local channel-logo caching (dev/changelog/601).

Covers app/logo_cache.py: resolve_logo_url() (pure passthrough-vs-local-route switch),
the eligibility rule (TV Guide or any group membership only - never the whole catalog),
the change-detection rule (refetch only when logo_url itself changes, never on an
unchanged URL, and never repeatedly on a URL that already failed), the serving route,
and account-delete teardown of cached files.

Runs against a throwaway temp SQLite DB and a throwaway temp directory - never the live
dvr.db or a real network call (requests.get is always patched).
    python3 -m unittest tests.test_logo_cache
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support.app import make_test_app  # noqa: E402
from tests.support import seed  # noqa: E402
from app import db  # noqa: E402
from app.database import Channel  # noqa: E402
from app.logo_cache import (  # noqa: E402
    resolve_logo_url, run_logo_cache_batch, delete_cached_logos, get_logo_cache_dir,
)
import app.logo_cache as logo_cache_mod  # noqa: E402


def _fake_response(content_type='image/png', body=b'\x89PNG-fake-bytes', status=200):
    # MagicMock, not Mock: _fetch_one_logo does `with requests.get(...) as resp:`, which
    # needs __enter__/__exit__ - mirrors requests.Response's own context-manager support.
    resp = mock.MagicMock()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    resp.headers = {'Content-Type': content_type}
    resp.raise_for_status = mock.Mock()
    if status >= 400:
        resp.raise_for_status.side_effect = Exception(f'HTTP {status}')
    resp.iter_content = lambda chunk_size=65536: [body]
    return resp


class ResolveLogoUrlTests(unittest.TestCase):
    """resolve_logo_url() is a pure function of already-loaded columns - no I/O - since
    every row-builder that renders logos calls it inside a per-row loop."""

    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_uncached_channel_falls_back_to_provider_url(self):
        acct = seed.make_account()
        ch = seed.make_channel(acct, logo_url='http://provider.test/logo.png')
        db.session.commit()
        self.assertEqual(resolve_logo_url(ch), 'http://provider.test/logo.png')

    def test_no_logo_url_at_all_returns_empty_string(self):
        acct = seed.make_account()
        ch = seed.make_channel(acct)
        db.session.commit()
        self.assertEqual(resolve_logo_url(ch), '')

    def test_cached_channel_points_at_local_route(self):
        acct = seed.make_account()
        ch = seed.make_channel(acct, logo_url='http://provider.test/logo.png')
        ch.logo_cache_path = f'{ch.id}.png'
        db.session.commit()
        self.assertEqual(resolve_logo_url(ch), f'/api/channels/{ch.id}/logo')


class EligibilityTests(unittest.TestCase):
    """Only TV Guide channels or channels in any group (either kind) are eligible - a
    catalog can be 100k+ channels and this must never try to cache all of it."""

    def setUp(self):
        self.t = make_test_app()
        self.cache_dir = os.path.join(self.t._tmpdir, 'logo-cache')
        self.cfg = {'recording': {'logo_cache': {'enabled': True, 'dir': self.cache_dir}}}

    def tearDown(self):
        self.t.cleanup()

    def _run(self):
        with mock.patch.object(logo_cache_mod, 'load_config', return_value=self.cfg), \
             mock.patch.object(logo_cache_mod.requests, 'get', return_value=_fake_response()):
            return run_logo_cache_batch(limit=50)

    def test_channel_not_in_guide_or_group_is_never_fetched(self):
        acct = seed.make_account()
        seed.make_channel(acct, name='Orphan', logo_url='http://provider.test/a.png',
                          in_guide=False)
        db.session.commit()
        attempted = self._run()
        self.assertEqual(attempted, 0)

    def test_in_guide_channel_is_eligible(self):
        acct = seed.make_account()
        ch = seed.make_channel(acct, name='Guide', logo_url='http://provider.test/a.png',
                               in_guide=True)
        db.session.commit()
        attempted = self._run()
        self.assertEqual(attempted, 1)
        db.session.refresh(ch)
        self.assertIsNotNone(ch.logo_cache_path)

    def test_channel_in_a_health_check_only_group_is_eligible(self):
        acct = seed.make_account()
        ch = seed.make_channel(acct, name='HC member', logo_url='http://provider.test/a.png',
                               in_guide=False)
        db.session.commit()
        seed.make_group(name='Health Check Group', members=[ch], recording=False)
        db.session.commit()
        attempted = self._run()
        self.assertEqual(attempted, 1)


class ChangeDetectionTests(unittest.TestCase):
    """Refetch only when logo_url itself changed since the last attempt - not on an
    unchanged URL, and not repeatedly on a URL that already failed."""

    def setUp(self):
        self.t = make_test_app()
        self.cache_dir = os.path.join(self.t._tmpdir, 'logo-cache')
        self.cfg = {'recording': {'logo_cache': {'enabled': True, 'dir': self.cache_dir}}}

    def tearDown(self):
        self.t.cleanup()

    def _run(self, response=None):
        with mock.patch.object(logo_cache_mod, 'load_config', return_value=self.cfg), \
             mock.patch.object(logo_cache_mod.requests, 'get',
                               return_value=response or _fake_response()):
            return run_logo_cache_batch(limit=50)

    def test_already_cached_unchanged_url_is_not_refetched(self):
        acct = seed.make_account()
        seed.make_channel(acct, logo_url='http://provider.test/a.png', in_guide=True)
        db.session.commit()
        self.assertEqual(self._run(), 1)  # first run caches it
        self.assertEqual(self._run(), 0)  # second run: nothing changed, no fetch

    def test_url_change_triggers_a_refetch(self):
        acct = seed.make_account()
        ch = seed.make_channel(acct, logo_url='http://provider.test/a.png', in_guide=True)
        db.session.commit()
        self.assertEqual(self._run(), 1)
        ch = db.session.get(Channel, ch.id)
        ch.logo_url = 'http://provider.test/b.png'
        db.session.commit()
        self.assertEqual(self._run(), 1)
        db.session.refresh(ch)
        self.assertEqual(ch.logo_cache_source_url, 'http://provider.test/b.png')

    def test_failed_fetch_is_not_retried_until_the_url_changes(self):
        acct = seed.make_account()
        ch = seed.make_channel(acct, logo_url='http://provider.test/broken.png', in_guide=True)
        db.session.commit()
        broken = _fake_response(status=500)
        self.assertEqual(self._run(response=broken), 1)  # one attempt, records the failure
        db.session.refresh(ch)
        self.assertIsNone(ch.logo_cache_path)
        self.assertEqual(ch.logo_cache_source_url, 'http://provider.test/broken.png')
        # Same broken URL again: must NOT be retried (would hammer a dead provider link
        # every 5 minutes forever).
        self.assertEqual(self._run(response=broken), 0)

    def test_non_image_content_type_is_recorded_but_not_cached(self):
        acct = seed.make_account()
        ch = seed.make_channel(acct, logo_url='http://provider.test/error.html', in_guide=True)
        db.session.commit()
        html_resp = _fake_response(content_type='text/html')
        self.assertEqual(self._run(response=html_resp), 1)
        db.session.refresh(ch)
        self.assertIsNone(ch.logo_cache_path)
        self.assertEqual(ch.logo_cache_source_url, 'http://provider.test/error.html')
        self.assertEqual(self._run(response=html_resp), 0)

    def test_svg_content_type_is_rejected_not_cached(self):
        """A stored SVG served through channel_logo executes provider-supplied <script>
        on the app's own origin (dev/docs/BUGS.md 2026-08-14) - only a raster allowlist
        may be cached."""
        acct = seed.make_account()
        ch = seed.make_channel(acct, logo_url='http://provider.test/evil.svg', in_guide=True)
        db.session.commit()
        svg_resp = _fake_response(content_type='image/svg+xml',
                                   body=b'<svg onload="alert(1)"></svg>')
        self.assertEqual(self._run(response=svg_resp), 1)
        db.session.refresh(ch)
        self.assertIsNone(ch.logo_cache_path)
        self.assertEqual(ch.logo_cache_source_url, 'http://provider.test/evil.svg')

    def test_response_is_closed_on_every_exit_path(self):
        """A leaked streamed connection holds a pooled socket open until GC
        (dev/docs/BUGS.md 2026-08-14) - _fetch_one_logo must close the response on a
        rejected content type, an oversize body, and a successful fetch alike."""
        acct = seed.make_account()
        seed.make_channel(acct, logo_url='http://provider.test/a.html', in_guide=True)
        db.session.commit()
        rejected = _fake_response(content_type='text/html')
        self._run(response=rejected)
        rejected.__exit__.assert_called_once()

        acct2 = seed.make_account()
        seed.make_channel(acct2, logo_url='http://provider.test/b.png', in_guide=True)
        db.session.commit()
        accepted = _fake_response(content_type='image/png')
        self._run(response=accepted)
        accepted.__exit__.assert_called_once()


class DisabledConfigTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()

    def tearDown(self):
        self.t.cleanup()

    def test_disabled_by_default_does_nothing(self):
        acct = seed.make_account()
        seed.make_channel(acct, logo_url='http://provider.test/a.png', in_guide=True)
        db.session.commit()
        cfg = {'recording': {'logo_cache': {'enabled': False, 'dir': '/nonexistent'}}}
        with mock.patch.object(logo_cache_mod, 'load_config', return_value=cfg), \
             mock.patch.object(logo_cache_mod.requests, 'get') as get_mock:
            attempted = run_logo_cache_batch(limit=50)
        self.assertEqual(attempted, 0)
        get_mock.assert_not_called()


class SvgPurgeTests(unittest.TestCase):
    """A logo cached as .svg before the raster-only allowlist existed must be dropped -
    channel_logo serves whatever's cached regardless of the enabled flag, so the purge
    must run even when logo caching is currently turned off (dev/docs/BUGS.md 2026-08-14)."""

    def setUp(self):
        self.t = make_test_app()
        self.cache_dir = os.path.join(self.t._tmpdir, 'logo-cache')
        os.makedirs(self.cache_dir, exist_ok=True)

    def tearDown(self):
        self.t.cleanup()

    def _seed_stale_svg(self):
        acct = seed.make_account()
        ch = seed.make_channel(acct, logo_url='http://provider.test/evil.svg', in_guide=True)
        with open(os.path.join(self.cache_dir, f'{ch.id}.svg'), 'wb') as f:
            f.write(b'<svg onload="alert(1)"></svg>')
        ch.logo_cache_path = f'{ch.id}.svg'
        ch.logo_cache_source_url = 'http://provider.test/evil.svg'
        db.session.commit()
        return ch

    def test_stale_svg_is_purged_on_next_batch_tick(self):
        """When caching is enabled, purging clears the channel's eligibility columns, so
        the very same batch tick also re-fetches it - ending with a safe raster file, not
        a bare 'nothing cached' state. The disabled-caching sibling test below covers the
        purge in isolation."""
        ch = self._seed_stale_svg()
        cfg = {'recording': {'logo_cache': {'enabled': True, 'dir': self.cache_dir}}}
        with mock.patch.object(logo_cache_mod, 'load_config', return_value=cfg), \
             mock.patch.object(logo_cache_mod.requests, 'get', return_value=_fake_response()):
            run_logo_cache_batch(limit=50)
        db.session.refresh(ch)
        self.assertEqual(ch.logo_cache_path, f'{ch.id}.png')
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, f'{ch.id}.svg')))
        self.assertTrue(os.path.exists(os.path.join(self.cache_dir, f'{ch.id}.png')))

    def test_stale_svg_is_purged_even_when_caching_is_disabled(self):
        ch = self._seed_stale_svg()
        cfg = {'recording': {'logo_cache': {'enabled': False, 'dir': self.cache_dir}}}
        with mock.patch.object(logo_cache_mod, 'load_config', return_value=cfg), \
             mock.patch.object(logo_cache_mod.requests, 'get') as get_mock:
            attempted = run_logo_cache_batch(limit=50)
        self.assertEqual(attempted, 0)
        get_mock.assert_not_called()
        db.session.refresh(ch)
        self.assertIsNone(ch.logo_cache_path)
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, f'{ch.id}.svg')))


class ServingRouteTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.cache_dir = os.path.join(self.t._tmpdir, 'logo-cache')
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cfg = {'recording': {'logo_cache': {'enabled': True, 'dir': self.cache_dir}}}

    def tearDown(self):
        self.t.cleanup()

    def test_cached_logo_is_served(self):
        acct = seed.make_account()
        ch = seed.make_channel(acct, logo_url='http://provider.test/a.png', in_guide=True)
        db.session.commit()
        with open(os.path.join(self.cache_dir, f'{ch.id}.png'), 'wb') as f:
            f.write(b'fake-png-bytes')
        ch.logo_cache_path = f'{ch.id}.png'
        db.session.commit()
        with mock.patch.object(logo_cache_mod, 'load_config', return_value=self.cfg):
            resp = self.t.client.get(f'/api/channels/{ch.id}/logo')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b'fake-png-bytes')
        self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')

    def test_uncached_channel_returns_404(self):
        acct = seed.make_account()
        ch = seed.make_channel(acct, logo_url='http://provider.test/a.png', in_guide=True)
        db.session.commit()
        resp = self.t.client.get(f'/api/channels/{ch.id}/logo')
        self.assertEqual(resp.status_code, 404)

    def test_unknown_channel_returns_404(self):
        resp = self.t.client.get('/api/channels/999999/logo')
        self.assertEqual(resp.status_code, 404)


class TeardownTests(unittest.TestCase):
    """CLAUDE.md's teardown rule: deleting an account must clean up everything the
    create path acquired, including cached logo files on disk."""

    def setUp(self):
        self.t = make_test_app()
        self.cache_dir = os.path.join(self.t._tmpdir, 'logo-cache')
        os.makedirs(self.cache_dir, exist_ok=True)

    def tearDown(self):
        self.t.cleanup()

    def test_deleting_an_account_removes_its_cached_logo_files(self):
        from app.routes.accounts import _delete_account_and_jobs

        acct = seed.make_account()
        ch = seed.make_channel(acct, logo_url='http://provider.test/a.png', in_guide=True)
        cache_path = f'{ch.id}.png'
        full_path = os.path.join(self.cache_dir, cache_path)
        with open(full_path, 'wb') as f:
            f.write(b'fake-png-bytes')
        ch.logo_cache_path = cache_path
        db.session.commit()

        cfg = {'recording': {'logo_cache': {'enabled': True, 'dir': self.cache_dir}}}
        with mock.patch.object(logo_cache_mod, 'load_config', return_value=cfg):
            _delete_account_and_jobs(acct.id)

        self.assertFalse(os.path.exists(full_path))

    def test_deleting_an_account_with_no_cached_logos_does_not_error(self):
        from app.routes.accounts import _delete_account_and_jobs

        acct = seed.make_account()
        seed.make_channel(acct, logo_url='http://provider.test/a.png', in_guide=True)
        db.session.commit()
        _delete_account_and_jobs(acct.id)  # must not raise


class DeleteCachedLogosTests(unittest.TestCase):
    def setUp(self):
        self.t = make_test_app()
        self.cache_dir = os.path.join(self.t._tmpdir, 'logo-cache')
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cfg = {'recording': {'logo_cache': {'enabled': True, 'dir': self.cache_dir}}}

    def tearDown(self):
        self.t.cleanup()

    def test_missing_file_is_ignored(self):
        with mock.patch.object(logo_cache_mod, 'load_config', return_value=self.cfg):
            delete_cached_logos(['does-not-exist.png'])  # must not raise

    def test_get_logo_cache_dir_resolves_from_cfg(self):
        self.assertEqual(get_logo_cache_dir(self.cfg), self.cache_dir)


if __name__ == '__main__':
    unittest.main()
