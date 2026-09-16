"""Support bundle export (DESIGN-secrets.md §7, changelog/253).

The stated concern driving this file's depth: a future change accidentally
un-scrubbing this export would be a real leak, not a cosmetic regression - so the tests
here scan the *decompressed* zip content for probe secrets and URLs, not just
spot-checks of individual JSON fields.

Masking policy, an explicit call: every <scheme>://...
token anywhere in the bundle - any field, any table, the log tail, config.yaml - keeps
its scheme and loses everything else, host included. Not just the known secret fields:
a live-verification pass against the real database and log found two real
gaps a narrower, field-by-field approach missed (Recording.url, never masked anywhere
else in the app; and a Pluto TV/mediatailor-style URL with a real filled-in token query
param that mask_creds() doesn't recognize as credential-shaped). The blanket
redact_urls_in_text() sweep closes both without needing to enumerate every column that
might carry a URL - which is the point: "even the domain name is private" means nothing
here tries to judge which URLs matter.

Where that sweep runs matters as much as that it runs. Structured files are redacted per
value and then serialized (build_support_bundle()'s _write_json closure); only free-form
text is redacted whole (_write_text). Redacting serialized JSON instead destroys the
first line after every URL, because a newline in a JSON string literal is two characters
rather than whitespace - TextAdjacentToUrlSurvivesTests below is what holds that line.

Every test here is hermetic w.r.t. config: build_support_bundle() calls load_config()
with no args at several points, which - per CLAUDE.md's own documented trap - reads the
REAL config.yaml and dvr.log path, not make_test_app()'s extra_overrides. _BundleTestCase
patches app.config.load_config for the duration of every test (support_bundle.py imports
it locally per call site specifically so this reaches it), pointed at a private temp log
file, so nothing here ever reads the real config.yaml or dvr.log.

No BUGS.md entry - feature work, no pre-existing defect fixed.
"""
import json
import os
import sys
import unittest
import zipfile
from datetime import datetime
from io import BytesIO
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.config as config_mod  # noqa: E402
import app.support_bundle as sb  # noqa: E402
from app import db  # noqa: E402
from app.database import (  # noqa: E402
    Alert, ChannelEvent, ChannelGroupEvent, RecordingEvent, RecordingSegment,
    SECRET_ACCOUNT_FIELDS, XtreamAccount, GROUP_FAILOVER, GROUP_MEMBER_SELECTED,
)
from app.url_utils import redact_urls_in_text  # noqa: E402
from app.version import __version__  # noqa: E402
from tests.support import seed  # noqa: E402
from tests.support.app import make_test_app  # noqa: E402


def _unzip(data):
    return zipfile.ZipFile(BytesIO(data))


def _all_decompressed_content(data):
    """Concatenated decompressed bytes of every entry in the zip.

    build_support_bundle() uses ZIP_DEFLATED, so scanning the raw zip *container* bytes
    for a plaintext substring is close to meaningless - DEFLATE already obscures literal
    strings inside compressed entries regardless of whether the underlying content was
    actually masked. The real invariant ("no raw secret value is retrievable from this
    bundle") has to be checked against what a user actually gets after unzipping.
    """
    zf = _unzip(data)
    return b'\n'.join(zf.read(name) for name in zf.namelist())


def _channel_rows(data):
    """The individually-shipped channel rows. channels.json became an object carrying
    the implicated rows plus whole-table counts in dev/changelog/841 - a bare list would
    have let a reader mistake the handful of shipped rows for the entire catalog."""
    return json.loads(_unzip(data).read('channels.json'))['channels']


def _implicate(ch):
    """Give `ch` a reason to be shipped in full. Only channels something else in the
    bundle points at get a row; everything else is a line in the aggregate, so a test
    asserting on a channel's shipped columns has to reference it from somewhere first."""
    return seed.make_channel_test(ch)


class _BundleTestCase(unittest.TestCase):
    """Hermetic base: patches load_config so every call inside build_support_bundle()
    sees a private temp log file instead of the real one, for the lifetime of the test."""

    def setUp(self):
        self.t = make_test_app()
        self.log_path = os.path.join(self.t._tmpdir, 'bundle_test.log')
        open(self.log_path, 'w').close()
        self._overrides = {'logging': {'file': self.log_path}}
        self._real_load_config = config_mod.load_config
        config_mod.load_config = self._fake_load_config

    def tearDown(self):
        config_mod.load_config = self._real_load_config
        self.t.cleanup()

    def _fake_load_config(self, *a, **k):
        return config_mod._deep_merge(self._real_load_config(), self._overrides)

    def set_config(self, overrides):
        """Merge additional overrides on top of the base (logging.file) for one test."""
        self._overrides = config_mod._deep_merge(self._overrides, overrides)


class SecretScrubbingTests(_BundleTestCase):
    """The core invariant, byte-scanned rather than field-by-field."""

    def test_no_raw_secret_account_field_value_or_host_survives(self):
        """Iterates SECRET_ACCOUNT_FIELDS itself (not a hardcoded copy of it) so a field
        added to that constant in the future is automatically covered here too. Distinct
        sub-hosts per URL field so each is independently checkable - under the blanket
        policy the host must be gone too, not just a path/query token."""
        acc = seed.make_account(name='Probe Account')
        probes = {}
        for field in SECRET_ACCOUNT_FIELDS:
            if field in ('base_url', 'm3u_url', 'epg_url'):
                host = f'probe-{field.replace("_", "-")}.test'
                setattr(acc, field, f'http://{host}/some/path?x=1')
                probes[field] = host
            else:
                token = f'PROBE_{field.upper()}_9f3e1a'
                setattr(acc, field, token)
                probes[field] = token
        db.session.commit()

        data = sb.build_support_bundle()
        content = _all_decompressed_content(data)

        for field, probe in probes.items():
            self.assertNotIn(probe.encode(), content,
                             f'raw {field} probe (host or value) leaked into the bundle')

    def test_scrubbing_applies_to_every_account_not_just_the_first(self):
        """A common bug shape: masking logic that only runs once, or a list comprehension
        that reuses a shared dict template across rows."""
        for i in range(3):
            acc = seed.make_account(name=f'Account {i}')
            acc.username = f'user_probe_{i}_7c2b'
            acc.password = f'pass_probe_{i}_7c2b'
        db.session.commit()

        data = sb.build_support_bundle()
        content = _all_decompressed_content(data)

        for i in range(3):
            self.assertNotIn(f'user_probe_{i}_7c2b'.encode(), content)
            self.assertNotIn(f'pass_probe_{i}_7c2b'.encode(), content)

    def test_xtream_account_base_url_is_fully_redacted_host_included(self):
        """SECRET_ACCOUNT_FIELDS includes base_url, which only XtreamAccount sets
        meaningfully - covered separately since seed.make_account() defaults to M3U.
        Also covers a base_url with NO path (mask_url_path used to leave these fully
        visible - "nothing to hide in the path" - which is exactly wrong under the
        blanket policy, since the host itself is what must be gone)."""
        acc = XtreamAccount(name='Xtream Probe', account_type='xtream',
                           base_url='http://xtream-probe-host.test',
                           username='xuser', password='xpass')
        db.session.add(acc)
        db.session.commit()

        data = sb.build_support_bundle()
        content = _all_decompressed_content(data)

        self.assertNotIn(b'xtream-probe-host.test', content)
        self.assertIn(b'http://[url redacted]', content)

    def test_recording_url_is_fully_redacted_host_included(self):
        """The original design-gap finding still applies under the new policy:
        Recording.url is never masked anywhere else in the app."""
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        seed.make_recording(
            channel_id=ch.id,
            url='http://recording-probe-host.test/live/user/pass/1')
        db.session.commit()

        data = sb.build_support_bundle()
        content = _all_decompressed_content(data)

        self.assertNotIn(b'recording-probe-host.test', content)
        self.assertIn(b'http://[url redacted]', content)

    def test_channel_stream_url_is_fully_redacted_host_included(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        ch.stream_url = 'http://channel-probe-host.test/live/user/pass/1'
        _implicate(ch)
        db.session.commit()

        data = sb.build_support_bundle()

        self.assertEqual(_channel_rows(data)[0]['stream_url'], 'http://[url redacted]')

    def test_any_scheme_is_redacted_not_just_http(self):
        """Real IPTV streams in this app can be rtmp/rtsp/udp/srt, not just http(s)
        (see tests/support/netguard.py's own recognized scheme list) - the redaction
        must not be http(s)-only."""
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        seed.make_recording(channel_id=ch.id, url='rtsp://rtsp-probe-host.test/live/u/p/1')
        db.session.commit()

        data = sb.build_support_bundle()
        recordings = json.loads(_unzip(data).read('recordings.json'))

        self.assertEqual(recordings[0]['url'], 'rtsp://[url redacted]')

    def test_real_world_query_token_url_is_fully_redacted(self):
        """Reproduces the real leak shape found via live verification against real
        channel data: a Pluto TV/mediatailor-style URL with a filled-in token query
        param, which mask_creds() does not recognize as credential-shaped (it only
        looks for username=/password=). The blanket policy doesn't need to recognize
        it - it redacts every URL regardless of shape. The token below is a fake
        stand-in; only its shape matters (dev/changelog/519)."""
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        ch.stream_url = (
            'https://real-shape-probe.test/v1/master/xyz/master.m3u8'
            '?token=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef'
            '&ads.device_did=%7BPSID%7D')
        db.session.commit()

        data = sb.build_support_bundle()
        content = _all_decompressed_content(data)

        self.assertNotIn(b'real-shape-probe.test', content)
        self.assertNotIn(b'deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef',
                         content)

    def test_bundle_is_a_fixed_point_of_redaction(self):
        """The strongest form of the invariant, across a realistic mix of tables and
        schemes at once: if any <scheme>://... token survived anywhere in the bundle,
        running redact_urls_in_text over the whole thing again would change it."""
        acc = seed.make_account(name='Mixed probe account')
        acc.epg_url = 'https://epg-fixed-point.test/TESTPATHTOKEN1'
        ch = seed.make_channel(acc)
        ch.stream_url = 'https://stream-fixed-point.test/live/u/p/1'
        seed.make_recording(channel_id=ch.id, url='rtsp://rec-fixed-point.test/live/u/p/9')
        db.session.commit()

        data = sb.build_support_bundle()
        content = _all_decompressed_content(data).decode()

        self.assertEqual(redact_urls_in_text(content), content,
                         'a further redaction pass changed the bundle - something '
                         'URL-shaped survived the first pass')

    def test_channel_raw_stream_url_is_never_shipped_at_all(self):
        """raw_stream_url carries the same credential shape pre-normalization and isn't
        even in the design's field list - dropped entirely, not masked."""
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        ch.raw_stream_url = 'http://provider.test/RAW_URL_PROBE_5e7a.ts'
        _implicate(ch)
        db.session.commit()

        data = sb.build_support_bundle()

        self.assertNotIn(b'RAW_URL_PROBE_5e7a', _all_decompressed_content(data))
        self.assertNotIn('raw_stream_url', _channel_rows(data)[0])

    def test_config_secret_key_is_masked(self):
        self.set_config({'flask': {'secret_key': 'PROBE_SECRET_KEY_c81f'}})

        data = sb.build_support_bundle()

        self.assertNotIn(b'PROBE_SECRET_KEY_c81f', _all_decompressed_content(data))
        cfg = json.loads(_unzip(data).read('config.yaml'))
        self.assertEqual(cfg['flask']['secret_key'], config_mod.MASK_SENTINEL)

    def test_notification_webhook_url_is_masked(self):
        self.set_config({'notifications': {'services': {
            'pushover': {'enabled': True, 'url': 'pover://PROBE_TOKEN_ff21@app/'}}}})

        data = sb.build_support_bundle()

        self.assertNotIn(b'PROBE_TOKEN_ff21', _all_decompressed_content(data))

    def test_masking_replaces_rather_than_deletes(self):
        """Guards against a trivial 'wipe everything' implementation that would pass the
        no-leak tests above for the wrong reason. The masked placeholders and legitimate
        non-secret data must both still be present. `name` is a pseudonym rather than the
        real value since dev/changelog/840, but it is still a *value* - a wipe would leave
        it empty."""
        acc = seed.make_account(name='Visible Account Name')
        acc.username = 'a_username_that_must_not_appear'
        db.session.commit()

        data = sb.build_support_bundle()
        accounts = json.loads(_unzip(data).read('accounts.json'))
        self.assertEqual(accounts[0]['name'], f'account {acc.id}')
        self.assertEqual(accounts[0]['status'], 'OK')
        self.assertEqual(accounts[0]['username'], '***')

    def test_legitimate_diagnostic_data_survives(self):
        """The bundle must remain useful, not just safe - recording/alert identity and
        status information is not a secret and must come through. Channel names are
        deliberately NOT in this list any more: they are pseudonymized by default
        (dev/changelog/840) and NameSanitizationTests owns that rule."""
        acc = seed.make_account()
        ch = seed.make_channel(acc, name='My Distinctive Channel Name')
        seed.make_recording(name='My Distinctive Recording', channel_id=ch.id,
                            status='FAILED', with_events=True, with_segment=True)
        alert = Alert(alert_type='TEST_ALERT', severity='WARN',
                     title='My Distinctive Alert Title')
        db.session.add(alert)
        db.session.commit()

        data = sb.build_support_bundle()
        content = _all_decompressed_content(data)

        self.assertIn(b'My Distinctive Recording', content)
        self.assertIn(b'My Distinctive Alert Title', content)
        self.assertIn(b'FAILED', content)


class TextAdjacentToUrlSurvivesTests(_BundleTestCase):
    """BUGS.md 2026-08-28 12:06 - redaction used to run over the serialized JSON, where a
    newline inside a string literal is the two characters '\\' and 'n'. Neither ends
    _ANY_SCHEME_URL_RE's run, so the redactor consumed the line break and everything up
    to the next real space, silently deleting the first line after any URL. Every test
    here seeds the exact shape that was measured in a shipped bundle: a URL at the end of
    one line, diagnostic text at the start of the next.
    """

    _BODY = ('Sync failed: http://redaction-probe.test/get.php?password=hunter2\n'
             'Traceback (most recent call last):\n'
             '  File "app/accounts.py", line 1, in sync\n')

    def _alert_body_from_bundle(self, data):
        alerts = json.loads(_unzip(data).read('alerts.json'))
        return next(a['body'] for a in alerts if a['alert_type'] == 'REDACTION_PROBE')

    def _seed_alert(self):
        db.session.add(Alert(alert_type='REDACTION_PROBE', severity='WARN',
                             title='Probe alert', body=self._BODY))
        db.session.commit()

    def test_the_line_after_a_url_survives(self):
        self._seed_alert()

        body = self._alert_body_from_bundle(sb.build_support_bundle())

        self.assertIn('Traceback (most recent call last):', body)

    def test_the_line_break_after_a_url_survives(self):
        """Not just the word - the newline itself was being eaten, which is what joined
        the traceback header onto the redacted URL's line."""
        self._seed_alert()

        body = self._alert_body_from_bundle(sb.build_support_bundle())

        self.assertIn('[url redacted]\n', body)
        self.assertEqual(body.count('\n'), self._BODY.count('\n'))

    def test_the_shipped_body_differs_from_the_original_only_by_its_urls(self):
        """The whole contract in one assertion. Asserting on a single surviving word
        understates the guard, because how much text was destroyed depended on where the
        next real space happened to fall."""
        self._seed_alert()

        body = self._alert_body_from_bundle(sb.build_support_bundle())

        self.assertEqual(body, redact_urls_in_text(self._BODY))

    def test_the_url_on_that_line_is_still_fully_redacted(self):
        """Companion guard for the opposite direction, and it passes against the old code
        too - the defect over-redacted, it never leaked. It is here because the fix moves
        where redaction happens, and moving it is exactly how the sweep gets dropped."""
        self._seed_alert()

        content = _all_decompressed_content(sb.build_support_bundle())

        self.assertNotIn(b'redaction-probe.test', content)
        self.assertNotIn(b'hunter2', content)

    def test_it_holds_for_a_free_text_column_on_another_table(self):
        """account.last_error carries stringified requests exceptions, which is the other
        place a URL is routinely followed by a newline and then the useful part. The
        marker leads its line, so the old code's run to the first real space ate it."""
        acc = seed.make_account()
        acc.last_error = ('http://redaction-probe.test/player_api.php\n'
                          'DISTINCTIVE_TAIL_9f2c: connection refused')
        db.session.commit()

        content = _all_decompressed_content(sb.build_support_bundle())

        self.assertIn(b'DISTINCTIVE_TAIL_9f2c', content)
        self.assertNotIn(b'redaction-probe.test', content)


class FutureSecretFieldDefaultTests(_BundleTestCase):
    """Guards the fail-open default fixed in dev/changelog (support bundle account-secret
    masking): _account_to_dict() must mask any SECRET_ACCOUNT_FIELDS member by default,
    and only skip the flat mask for the explicit URL-shaped opt-in list. Simulates a
    future non-URL secret (an API key, a token) without a schema change: patches the
    module-level SECRET_ACCOUNT_FIELDS tuple and sets a plain, unmapped attribute on a
    seeded account - _account_to_dict()'s getattr() doesn't care whether the attribute is
    a real column."""

    def test_a_future_non_url_secret_field_is_masked_by_default(self):
        acc = seed.make_account(name='Future Secret Probe')
        acc.hypothetical_api_token = 'PROBE_FUTURE_SECRET_9f3e1a'
        db.session.commit()

        with patch.object(sb, 'SECRET_ACCOUNT_FIELDS',
                          SECRET_ACCOUNT_FIELDS + ('hypothetical_api_token',)):
            data = sb.build_support_bundle()

        content = _all_decompressed_content(data)
        self.assertNotIn(b'PROBE_FUTURE_SECRET_9f3e1a', content)


class BundleContentsTests(_BundleTestCase):

    def test_meta_json_shape(self):
        data = sb.build_support_bundle()
        meta = json.loads(_unzip(data).read('meta.json'))
        self.assertEqual(meta['app_version'], __version__)
        self.assertIsInstance(meta['schema_version'], int)
        self.assertIn('generated_at', meta)
        self.assertIn('python_version', meta)
        self.assertIn('platform', meta)

    def test_meta_json_reports_the_deployment_shape(self):
        """A traceback means different things on a bare-metal install and in a container,
        and platform.platform() cannot tell them apart - inside a container it reports the
        host's kernel. The uid is what turned a PermissionError inside the application tree
        from unattributable into obvious (dev/changelog/981)."""
        meta = json.loads(_unzip(sb.build_support_bundle()).read('meta.json'))
        runtime = meta['runtime']
        self.assertIs(runtime['containerized'], False)
        self.assertEqual(runtime['uid'], os.getuid())
        self.assertEqual(runtime['gid'], os.getgid())
        # Not a container, so neither path is a symlink - and the key is present saying so
        # rather than absent, which would read as "this bundle predates the check".
        self.assertIsNone(runtime['config_yaml_symlink'])
        self.assertIsNone(runtime['instance_symlink'])

    def test_meta_json_reports_a_containerized_install(self):
        with patch.dict(os.environ, {'CHANNELBIN_DOCKER': '1'}):
            meta = json.loads(_unzip(sb.build_support_bundle()).read('meta.json'))
        self.assertIs(meta['runtime']['containerized'], True)

    def test_the_deployment_shape_carries_no_install_path(self):
        """The symlink targets are the container's own fixed paths. An install root is
        routinely a home directory carrying the user's name, which is exactly the identity
        the rest of this module exists to remove."""
        runtime = json.loads(
            _unzip(sb.build_support_bundle()).read('meta.json'))['runtime']
        base = os.path.dirname(os.path.dirname(os.path.abspath(sb.__file__)))
        self.assertNotIn(base, json.dumps(runtime))

    def test_log_tail_is_capped(self):
        """Patches the cap down to a small, test-owned value rather than deriving the
        write count from the real production constant: writing "cap + 500" lines when
        the cap itself is the thing under test would write a huge file the moment
        someone breaks the cap by making it too large (dev/changelog/253). None of
        these lines match the timestamped log format, so each is its own kept record
        (dev/changelog/838) - counted by content rather than by raw newlines, which
        is fragile to the noise-filtering summary line's exact length."""
        real_cap = sb._LOG_TAIL_LINES
        sb._LOG_TAIL_LINES = 10
        try:
            with open(self.log_path, 'w') as f:
                for i in range(25):
                    f.write(f'line {i}\n')
            data = sb.build_support_bundle()
        finally:
            sb._LOG_TAIL_LINES = real_cap

        tail = _unzip(data).read('dvr.log').decode()
        kept = [ln for ln in tail.splitlines() if ln.startswith('line ')]
        self.assertEqual(len(kept), 10)
        self.assertIn('line 24', tail)          # newest line kept
        self.assertNotIn('line 0\n', tail)      # oldest line dropped

    def test_historical_style_raw_url_in_the_log_tail_is_redacted(self):
        """Reproduces the exact real-world defect found via live verification against
        the real dvr.log: a log line written before a masking fix
        landed for that call site still had a real URL in full, and the tail is capped
        by line count (not time), so a quiet server can keep such a line in range
        indefinitely. Under the blanket write-time pass this is caught unconditionally -
        it no longer matters whether a matching account still exists or ever did, unlike
        the narrower account-URL-specific pass this design superseded."""
        with open(self.log_path, 'a') as f:
            f.write('2026-01-01 00:00:00,000 [INFO] app.accounts: Fetching M3U for '
                    'account 1 from https://historical-leak-host.test/TESTPATHTOKEN2/'
                    '?movies=false\n')

        data = sb.build_support_bundle()

        tail = _unzip(data).read('dvr.log').decode()
        self.assertNotIn('historical-leak-host.test', tail)
        self.assertNotIn('TESTPATHTOKEN2', tail)
        self.assertIn('https://[url redacted]', tail)

    def test_every_url_in_the_log_is_redacted_including_ones_belonging_to_no_account(self):
        """Unlike the narrower account-URL-only pass this design replaced, the blanket
        write-time redaction does not try to tell 'a known account's URL' apart from any
        other URL - anything shaped like scheme://... is redacted, full stop. A relative
        request path (no scheme) is unaffected, since it was never a URL to begin with.
        Uses a non-werkzeug source for the relative-path line: werkzeug lines are now
        dropped from the tail entirely (dev/changelog/838, NoiseFilteringTests below),
        which is a separate concern from the redaction this test is about."""
        with open(self.log_path, 'a') as f:
            f.write('2026-01-01 00:00:00,000 [INFO] some.module: GET /api/nav-status\n')
            f.write('2026-01-01 00:00:01,000 [INFO] some.module: unrelated mention of '
                    'http://totally-unrelated-host.test/status\n')

        data = sb.build_support_bundle()

        tail = _unzip(data).read('dvr.log').decode()
        self.assertIn('/api/nav-status', tail)                    # relative path, untouched
        self.assertNotIn('totally-unrelated-host.test', tail)      # any URL, redacted
        self.assertIn('http://[url redacted]', tail)

    def test_missing_log_file_does_not_crash_the_export(self):
        self.set_config({'logging': {'file': '/nonexistent/path/does-not-exist.log'}})

        data = sb.build_support_bundle()

        zf = _unzip(data)
        self.assertIn('dvr.log', zf.namelist())
        self.assertNotIn('errors.json', zf.namelist())   # handled, not a failure

    def test_unset_log_file_does_not_crash_the_export(self):
        self.set_config({'logging': {'file': None}})

        data = sb.build_support_bundle()

        zf = _unzip(data)
        self.assertIn('dvr.log', zf.namelist())
        self.assertNotIn('errors.json', zf.namelist())

    def test_config_yaml_is_valid_masked_json(self):
        data = sb.build_support_bundle()
        cfg = json.loads(_unzip(data).read('config.yaml'))
        self.assertIn('recording', cfg)

    def test_config_yaml_notifications_base_url_is_redacted_too(self):
        """Side effect explicitly confirmed as wanted: the app's own base_url
        (not a provider secret, just this server's LAN/domain address) is a URL, so
        it's in scope for the bundle even though _is_sensitive_path doesn't flag it."""
        self.set_config({'notifications': {'base_url': 'http://192.168.1.50:5000'}})

        data = sb.build_support_bundle()

        cfg = json.loads(_unzip(data).read('config.yaml'))
        self.assertEqual(cfg['notifications']['base_url'], 'http://[url redacted]')


class NoiseFilteringTests(_BundleTestCase):
    """dev/changelog/838: of the 2,000 raw lines the tail used to ship, roughly 93%
    was werkzeug HTTP access logging and routine APScheduler job start/finish chatter,
    capable of pushing a real incident's own log lines out of the capped tail
    entirely. The bundle now scans much further back and keeps application records
    only, per _filter_log_noise."""

    def test_werkzeug_access_lines_are_dropped_from_the_tail(self):
        with open(self.log_path, 'w') as f:
            for i in range(50):
                f.write(
                    f'2026-01-01 00:00:{i:02d},000 [INFO] werkzeug: 192.168.1.1 - - '
                    f'[01/Jan/2026 00:00:{i:02d}] "GET /api/nav-status HTTP/1.1" 200 -\n'
                )
            f.write('2026-01-01 00:01:00,000 [ERROR] app.recorder: capture failed for '
                    'channel 5\n')

        data = sb.build_support_bundle()

        tail = _unzip(data).read('dvr.log').decode()
        self.assertNotIn('GET /api/nav-status', tail)   # the summary note may name
        self.assertIn('capture failed for channel 5', tail)  # "werkzeug" as a category

    def test_routine_apscheduler_job_start_finish_lines_are_dropped(self):
        with open(self.log_path, 'w') as f:
            for i in range(50):
                f.write(
                    f'2026-01-01 00:00:{i:02d},000 [INFO] apscheduler.executors.default: '
                    f'Running job "_logo_cache_job (trigger: interval[0:05:00])" '
                    f'(scheduled at 2026-01-01 00:00:{i:02d})\n'
                )
                f.write(
                    f'2026-01-01 00:00:{i:02d},500 [INFO] apscheduler.executors.default: '
                    f'Job "_logo_cache_job (trigger: interval[0:05:00])" executed '
                    f'successfully\n'
                )
            f.write('2026-01-01 00:02:00,000 [ERROR] app.recorder: capture failed for '
                    'channel 5\n')

        data = sb.build_support_bundle()

        tail = _unzip(data).read('dvr.log').decode()
        self.assertNotIn('executed successfully', tail)
        self.assertNotIn('Running job', tail)
        self.assertIn('capture failed for channel 5', tail)

    def test_a_failed_apscheduler_job_survives_the_filter(self):
        """The routine filter is message-shape-specific, not a blanket drop of every
        apscheduler.executors.default line - a job that actually raised must stay
        visible, for the same reason the noise around it is dropped: so it doesn't get
        pushed out by the very chatter this item exists to remove."""
        with open(self.log_path, 'w') as f:
            f.write('2026-01-01 00:00:00,000 [ERROR] apscheduler.executors.default: '
                    'Job "_logo_cache_job" raised an exception\n')

        data = sb.build_support_bundle()

        tail = _unzip(data).read('dvr.log').decode()
        self.assertIn('raised an exception', tail)

    def test_traceback_continuation_lines_stay_with_their_header(self):
        """A continuation line (no timestamp - the next frame of a traceback) has to
        survive or drop with the header line it belongs to. It never matches the
        werkzeug/apscheduler source check on its own, so a naive per-line filter would
        keep every continuation line regardless of its header's fate; grouping is what
        prevents that."""
        with open(self.log_path, 'w') as f:
            f.write('2026-01-01 00:00:00,000 [ERROR] app.recorder: capture failed\n')
            f.write('Traceback (most recent call last):\n')
            f.write('  File "app/recorder.py", line 1, in x\n')

        data = sb.build_support_bundle()

        tail = _unzip(data).read('dvr.log').decode()
        self.assertIn('Traceback (most recent call last):', tail)
        self.assertIn('File "app/recorder.py"', tail)

    def test_tail_states_how_many_records_were_dropped_as_noise(self):
        """Product principle 1: a filtered view has to say it's filtered, not present
        itself as the whole log."""
        with open(self.log_path, 'w') as f:
            f.write('2026-01-01 00:00:00,000 [INFO] werkzeug: GET /api/nav-status\n')
            f.write('2026-01-01 00:00:01,000 [ERROR] app.recorder: capture failed\n')

        data = sb.build_support_bundle()

        tail = _unzip(data).read('dvr.log').decode()
        self.assertIn('dropped', tail)
        self.assertIn('noise', tail)


class NameSanitizationTests(_BundleTestCase):
    """dev/changelog/840. The bundle strips the host out of every URL on the ruling that
    even a bare provider hostname is private in the IPTV context, and then used to ship
    the same provider identity in cleartext through the free-text `name` columns - on the
    install this was measured against, all four account names carried a provider token
    that dev/leak-terms.yaml independently classifies as must-never-leak, and one carried
    a personal first name.

    Two halves, treated differently on purpose. Account names are few and distinctive, and
    around twenty alert types title themselves f'{account.name}: ...', so they are
    pseudonymized in their column AND swept out of every other string. Channel names are
    six figures of mostly-ordinary words, so they are pseudonymized in their column only;
    sweeping them would corrupt the text it was meant to protect.
    """

    def _seed_named(self):
        acc = seed.make_account(name='zephyr-provider-nine')
        ch = seed.make_channel(acc, name='Zephyr Sports One',
                              category_name='Zephyr | SPORTS')
        _implicate(ch)
        db.session.commit()
        return acc, ch

    def test_account_name_is_not_shipped_verbatim(self):
        self._seed_named()
        content = _all_decompressed_content(sb.build_support_bundle())
        self.assertNotIn(b'zephyr-provider-nine', content)

    def test_account_name_is_replaced_by_a_stable_pseudonym(self):
        acc, _ = self._seed_named()
        accounts = json.loads(_unzip(sb.build_support_bundle()).read('accounts.json'))
        self.assertEqual(accounts[0]['name'], f'account {acc.id}')

    def test_account_name_embedded_in_an_alert_is_swept(self):
        """The half a column-only fix misses: app/accounts.py titles ~20 alert types
        f'{account.name}: ...', so the name reaches the bundle again through alerts.json
        even when accounts.json itself is clean."""
        acc, _ = self._seed_named()
        db.session.add(Alert(alert_type='SYNC_FAILED', severity='ERROR',
                             title=f'{acc.name}: EPG fetch failed',
                             body=f'Account "{acc.name}" returned no channels.'))
        db.session.commit()

        alerts = json.loads(_unzip(sb.build_support_bundle()).read('alerts.json'))
        self.assertNotIn('zephyr-provider-nine', alerts[0]['title'])
        self.assertNotIn('zephyr-provider-nine', alerts[0]['body'])
        self.assertEqual(alerts[0]['title'], f'account {acc.id}: EPG fetch failed')

    def test_account_name_in_the_log_tail_is_swept(self):
        acc, _ = self._seed_named()
        with open(self.log_path, 'w') as fh:
            fh.write(f'2026-08-28 09:00:00 INFO [app.accounts] Xtream auth OK for {acc.name}\n')

        tail = _unzip(sb.build_support_bundle()).read('dvr.log').decode()
        self.assertNotIn('zephyr-provider-nine', tail)
        self.assertIn(f'account {acc.id}', tail)

    def test_account_name_matching_is_case_insensitive(self):
        acc, _ = self._seed_named()
        db.session.add(Alert(alert_type='TEST_ALERT', severity='WARN',
                             title='ZEPHYR-Provider-Nine went away'))
        db.session.commit()

        content = _all_decompressed_content(sb.build_support_bundle())
        self.assertNotIn(b'ZEPHYR-Provider-Nine', content)

    def test_channel_name_and_category_are_pseudonymized(self):
        _, ch = self._seed_named()
        channels = _channel_rows(sb.build_support_bundle())
        self.assertEqual(channels[0]['name'], f'channel {ch.id}')
        self.assertNotIn('Zephyr', channels[0]['category_name'])

    def test_channels_sharing_a_category_share_its_pseudonym(self):
        """The pseudonym has to preserve equality or it destroys the only thing the
        column was diagnostic for: which channels group together."""
        acc = seed.make_account(name='zephyr-provider-nine')
        for i in range(3):
            ch = seed.make_channel(acc, name=f'Channel {i}',
                                   category_name=('Shared Category' if i < 2
                                                  else 'Other Category'))
            _implicate(ch)
        db.session.commit()

        channels = sorted(_channel_rows(sb.build_support_bundle()),
                          key=lambda c: c['id'])
        cats = [c['category_name'] for c in channels]
        self.assertEqual(cats[0], cats[1])
        self.assertNotEqual(cats[0], cats[2])

    def test_epg_channel_id_is_pseudonymized(self):
        acc = seed.make_account(name='zephyr-provider-nine')
        ch = seed.make_channel(acc, name='A')
        ch.epg_channel_id = 'zephyr.sports.one'
        _implicate(ch)
        db.session.commit()

        content = _all_decompressed_content(sb.build_support_bundle())
        self.assertNotIn(b'zephyr.sports.one', content)

    def test_a_short_account_name_is_not_swept_out_of_ordinary_text(self):
        """A 1-3 character account name is a substring of ordinary English, so sweeping
        it would carve holes in every string in the bundle rather than protect anything.
        Its column is still pseudonymized; only the free-text sweep skips it, and
        meta.json says how many were skipped."""
        seed.make_account(name='TV')
        db.session.add(Alert(alert_type='TEST_ALERT', severity='WARN',
                             title='The TV stopped responding'))
        db.session.commit()

        zf = _unzip(sb.build_support_bundle())
        alerts = json.loads(zf.read('alerts.json'))
        self.assertEqual(alerts[0]['title'], 'The TV stopped responding')
        self.assertEqual(json.loads(zf.read('accounts.json'))[0]['name'][:7], 'account')
        meta = json.loads(zf.read('meta.json'))
        self.assertEqual(meta['redactions']['account_names_too_short_to_sweep_safely'], 1)

    def test_meta_discloses_the_pseudonymization_and_its_limit(self):
        """Product principle 1: a filtered artifact never presents itself as the whole
        truth. A reader has to be able to tell a pseudonym from an empty field, and to
        learn where the sweep stops rather than assuming it was total."""
        acc, _ = self._seed_named()
        db.session.add(Alert(alert_type='TEST_ALERT', severity='WARN',
                             title=f'{acc.name}: something happened'))
        db.session.commit()

        meta = json.loads(_unzip(sb.build_support_bundle()).read('meta.json'))
        red = meta['redactions']
        self.assertEqual(red['mode'], 'names pseudonymized')
        self.assertIn('accounts.name', red['pseudonymized_fields'])
        self.assertIn('channels.name', red['pseudonymized_fields'])
        self.assertIn('channel_groups.name', red['pseudonymized_fields'])
        self.assertGreater(red['account_name_occurrences_swept_from_free_text'], 0)
        self.assertIn('Channel and group names', red['known_limit'])

    def test_opting_in_ships_the_real_names(self):
        acc, _ = self._seed_named()
        zf = _unzip(sb.build_support_bundle(include_names=True))
        self.assertEqual(json.loads(zf.read('accounts.json'))[0]['name'], acc.name)
        self.assertEqual(json.loads(zf.read('channels.json'))['channels'][0]['name'],
                         'Zephyr Sports One')
        self.assertEqual(json.loads(zf.read('meta.json'))['redactions']['mode'],
                         "names included at the user's request")

    def test_opting_in_still_redacts_every_url(self):
        """The opt-in is about names only. A user who wants names must not silently get
        provider hostnames back with them."""
        acc, _ = self._seed_named()
        acc.m3u_url = 'http://opt-in-probe-host.test/list.m3u'
        db.session.commit()

        content = _all_decompressed_content(sb.build_support_bundle(include_names=True))
        self.assertNotIn(b'opt-in-probe-host.test', content)


class PartialFailureTests(_BundleTestCase):
    """DESIGN-secrets.md §7's explicit observability requirement: a failed table is
    recorded inside the bundle and logged, never a silent gap; total failure raises
    instead of returning an empty zip."""

    def test_one_table_failing_lands_in_errors_json_others_still_present(self):
        seed.make_account()
        db.session.commit()

        with patch.object(sb.Alert, 'query') as mock_q:
            mock_q.all.side_effect = RuntimeError('simulated DB error')
            data = sb.build_support_bundle()

        zf = _unzip(data)
        self.assertIn('errors.json', zf.namelist())
        errors = json.loads(zf.read('errors.json'))
        self.assertIn('alerts.json', errors)
        self.assertIn('accounts.json', zf.namelist())
        self.assertIn('recordings.json', zf.namelist())

    def test_two_tables_failing_both_land_in_errors_json(self):
        with patch.object(sb.Alert, 'query') as mock_alert, \
             patch.object(sb.RecordingEvent, 'query') as mock_evt:
            mock_alert.all.side_effect = RuntimeError('alert boom')
            mock_evt.all.side_effect = RuntimeError('event boom')
            data = sb.build_support_bundle()

        errors = json.loads(_unzip(data).read('errors.json'))
        self.assertEqual(set(errors), {'alerts.json', 'recording_events.json'})

    def test_every_table_failing_raises_instead_of_returning_empty_zip(self):
        with patch.object(zipfile.ZipFile, 'writestr', side_effect=RuntimeError('disk full')):
            with self.assertRaises(RuntimeError) as ctx:
                sb.build_support_bundle()
        self.assertIn('failed entirely', str(ctx.exception))

    def test_channel_table_failure_does_not_take_down_accounts(self):
        acc = seed.make_account()
        seed.make_channel(acc)
        db.session.commit()

        with patch.object(sb, '_implicated_channel_ids',
                          side_effect=RuntimeError('channels boom')):
            data = sb.build_support_bundle()

        zf = _unzip(data)
        self.assertIn('accounts.json', zf.namelist())
        self.assertEqual(len(json.loads(zf.read('accounts.json'))), 1)
        self.assertIn('channels.json', json.loads(zf.read('errors.json')))

    def test_account_table_failure_aborts_the_export_rather_than_leaking(self):
        """The one deliberate exception to per-table degradation (dev/changelog/840).
        The account list is what the name sweep is built from, so without it the bundle
        cannot remove account names from alert bodies, event details or the log tail.
        Degrading here would ship a bundle that looks complete and leaks the exact
        strings the sweep exists to remove, so this one fails closed."""
        acc = seed.make_account()
        seed.make_channel(acc)
        db.session.commit()

        with patch.object(sb.Account, 'query') as mock_q:
            mock_q.all.side_effect = RuntimeError('accounts boom')
            with self.assertRaises(RuntimeError) as ctx:
                sb.build_support_bundle()
        self.assertIn('account names could not be removed', str(ctx.exception))


class ChannelSelectionTests(_BundleTestCase):
    """dev/changelog/841. channels.json was 137,260 rows and 98.5% of a real bundle,
    while the column that justified its size - stream_url - collapsed to five distinct
    values once the write-time redaction had removed every host and path. Full rows now
    ship only for the channels something else in the bundle refers to; the rest are
    counted by the facets that survive redaction.
    """

    def _channels_json(self, **kw):
        return json.loads(_unzip(sb.build_support_bundle(**kw)).read('channels.json'))

    def test_an_unreferenced_channel_ships_no_row(self):
        acc = seed.make_account()
        for i in range(5):
            seed.make_channel(acc, name=f'Unreferenced {i}')
        db.session.commit()

        self.assertEqual(self._channels_json()['channels'], [])

    def test_a_channel_with_a_recording_ships_in_full(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        seed.make_channel(acc, name='Other')
        seed.make_recording(channel_id=ch.id)
        db.session.commit()

        rows = self._channels_json()['channels']
        self.assertEqual([r['id'] for r in rows], [ch.id])

    def test_a_channel_named_only_by_a_group_failover_event_ships_in_full(self):
        """The hardest case to follow in the audited incident: a group-driven recording
        names the member that served it and the one it failed over from inside
        extra_data, not through any foreign key. Without this the reader gets bare
        integers with no row behind them."""
        acc = seed.make_account()
        served = seed.make_channel(acc, name='Served')
        failed = seed.make_channel(acc, name='Failed')
        seed.make_channel(acc, name='Uninvolved')
        rec = seed.make_recording()
        db.session.add(RecordingEvent(
            recording_id=rec.id, event_type=GROUP_FAILOVER, detail='failed over',
            extra_data=json.dumps({'from_channel_id': failed.id,
                                   'to_channel_id': served.id})))
        db.session.commit()

        rows = self._channels_json()['channels']
        self.assertEqual(sorted(r['id'] for r in rows), sorted([served.id, failed.id]))

    def test_a_malformed_event_extra_data_does_not_take_the_file_down(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        rec = seed.make_recording()
        db.session.add(RecordingEvent(
            recording_id=rec.id, event_type=GROUP_MEMBER_SELECTED,
            detail='selected', extra_data='{not json'))
        seed.make_channel_test(ch)
        db.session.commit()

        rows = self._channels_json()['channels']
        self.assertEqual([r['id'] for r in rows], [ch.id])

    def test_the_aggregate_counts_every_channel_including_the_shipped_ones(self):
        """The counts describe the whole table, not the leftovers - otherwise the
        bundle cannot answer 'how many channels does this install have'."""
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        for i in range(9):
            seed.make_channel(acc, name=f'Bulk {i}')
        seed.make_channel_test(ch)
        db.session.commit()

        payload = self._channels_json()
        self.assertEqual(payload['total_channel_count'], 10)
        self.assertEqual(sum(r['count'] for r in payload['aggregate']), 10)
        self.assertEqual(payload['included_channel_count'], 1)

    def test_the_shipped_row_count_does_not_grow_with_the_catalog(self):
        """The whole point: a bundle's size must track what happened on the install,
        not how large a catalog the user's subscription happens to carry."""
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        seed.make_channel_test(ch)
        for i in range(400):
            seed.make_channel(acc, name=f'Bulk {i}')
        db.session.commit()

        payload = self._channels_json()
        self.assertEqual(payload['total_channel_count'], 401)
        self.assertEqual(len(payload['channels']), 1)

    def test_the_aggregate_separates_channels_by_shape_and_flags(self):
        acc = seed.make_account()
        a = seed.make_channel(acc, name='A', in_guide=True)
        a.stream_url = 'http://provider.test/live/u/p/1'
        b = seed.make_channel(acc, name='B')
        b.stream_url = 'https://provider.test/live/u/p/2.m3u8'
        c = seed.make_channel(acc, name='C')
        c.stream_url = 'https://provider.test/live/u/p/3.m3u8'
        db.session.commit()

        rows = self._channels_json()['aggregate']
        by_shape = {(r['url_scheme'], r['url_extension'], r['in_guide']): r['count']
                    for r in rows}
        self.assertEqual(by_shape[('http', None, True)], 1)
        self.assertEqual(by_shape[('https', 'm3u8', False)], 2)

    def test_the_aggregate_carries_no_url_value_and_no_hostname_fragment(self):
        """The extension is allowlisted rather than read as 'whatever follows the last
        dot': a stream URL with no path yields the tail of its HOSTNAME under a naive
        parse, which is the provider identity every other rule here exists to strip."""
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        ch.stream_url = 'http://aggregate-probe-host.example.com'
        db.session.commit()

        payload = self._channels_json()
        self.assertNotIn(b'aggregate-probe-host',
                         _all_decompressed_content(sb.build_support_bundle()))
        shapes = {(r['url_scheme'], r['url_extension']) for r in payload['aggregate']}
        self.assertEqual(shapes, {('http', None)})

    def test_channels_json_says_it_is_not_the_whole_table(self):
        """Product principle 1. A reader who opens this file and finds two rows must
        not conclude the install has two channels."""
        acc = seed.make_account()
        seed.make_channel(acc)
        db.session.commit()

        payload = self._channels_json()
        self.assertIn('NOT every channel', payload['note'])
        self.assertIn('recording', payload['included_because'])

    def test_an_aggregate_failure_still_ships_the_implicated_rows(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        seed.make_channel_test(ch)
        db.session.commit()

        with patch.object(sb, '_channel_aggregate',
                          side_effect=RuntimeError('aggregate boom')):
            zf = _unzip(sb.build_support_bundle())

        payload = json.loads(zf.read('channels.json'))
        self.assertEqual([r['id'] for r in payload['channels']], [ch.id])
        self.assertIn('not the whole table', payload['aggregate_error'])
        self.assertIn('channels.json:aggregate', json.loads(zf.read('errors.json')))


class FilesystemStatTests(_BundleTestCase):
    """dev/changelog/842. The bundle named Recording.output_path and
    RecordingSegment.file_path as bare strings and never asked whether those files were
    still there - which in the audited incident was the single most actionable fact, the
    difference between "click Retry conversion" and "it is gone, re-record". Answering it
    took an email round trip.
    """

    def _recordings(self, data):
        return json.loads(_unzip(data).read('recordings.json'))

    def test_an_existing_output_file_reports_exists_and_its_size(self):
        path = os.path.join(self.t._tmpdir, 'present.ts')
        with open(path, 'wb') as fh:
            fh.write(b'x' * 1234)
        seed.make_recording(output_path=path)
        db.session.commit()

        stat = self._recordings(sb.build_support_bundle())[0]['output_path_stat']
        self.assertEqual(stat, {'exists': True, 'size_bytes': 1234})

    def test_a_missing_output_file_reports_exists_false(self):
        """A deleted file is an ANSWER, not an error - it is exactly what the reader is
        trying to learn - so it must not read as a failed check."""
        seed.make_recording(
            output_path=os.path.join(self.t._tmpdir, 'gone.ts'))
        db.session.commit()

        stat = self._recordings(sb.build_support_bundle())[0]['output_path_stat']
        self.assertEqual(stat, {'exists': False})
        meta = json.loads(_unzip(sb.build_support_bundle()).read('meta.json'))
        self.assertEqual(meta['filesystem_check']['paths_erroring'], 0)

    def test_a_recording_with_no_output_path_gets_a_null_stat(self):
        seed.make_recording()
        db.session.commit()

        self.assertIsNone(self._recordings(sb.build_support_bundle())[0]['output_path_stat'])

    def test_a_segment_file_path_is_checked_too(self):
        """A recording's segments are the salvageable material, so their presence is the
        other half of the same question."""
        path = os.path.join(self.t._tmpdir, 'seg.ts')
        with open(path, 'wb') as fh:
            fh.write(b'ab')
        rec = seed.make_recording(with_segment=True)
        seg = RecordingSegment.query.filter_by(recording_id=rec.id).one()
        seg.file_path = path
        db.session.commit()

        segs = json.loads(
            _unzip(sb.build_support_bundle()).read('recording_segments.json'))
        self.assertEqual(segs[0]['file_path_stat'], {'exists': True, 'size_bytes': 2})

    def test_a_hard_stat_error_is_recorded_and_the_export_continues(self):
        """/dvr is a network mount that has answered a live syscall with a stale file
        handle. That must degrade to a stated gap, never to a failed export."""
        seed.make_recording(output_path='/dvr/stale.ts')
        db.session.commit()

        with patch.object(sb.os, 'stat', side_effect=OSError(116, 'Stale file handle')):
            zf = _unzip(sb.build_support_bundle())

        stat = json.loads(zf.read('recordings.json'))[0]['output_path_stat']
        self.assertIsNone(stat['exists'])
        self.assertIn('Stale file handle', stat['error'])
        self.assertNotIn('errors.json', zf.namelist())
        self.assertEqual(
            json.loads(zf.read('meta.json'))['filesystem_check']['paths_erroring'], 1)

    def test_the_stat_pass_stops_after_a_run_of_hard_failures(self):
        """A dead mount must not charge the export one failing syscall per row."""
        for i in range(sb._PathStatter.MAX_CONSECUTIVE_ERRORS + 5):
            seed.make_recording(output_path=f'/dvr/dead_{i}.ts')
        db.session.commit()

        with patch.object(sb.os, 'stat',
                          side_effect=OSError(116, 'Stale file handle')) as stat_mock:
            zf = _unzip(sb.build_support_bundle())

        # Filtered to our own paths: os.stat is patched module-wide, and config.py's
        # mtime cache and the log tail legitimately call it too.
        ours = [c for c in stat_mock.call_args_list
                if str(c.args[0]).startswith('/dvr/dead_')]
        self.assertEqual(len(ours), sb._PathStatter.MAX_CONSECUTIVE_ERRORS)
        check = json.loads(zf.read('meta.json'))['filesystem_check']
        self.assertIn('stopped_early', check)
        stats = [r['output_path_stat'] for r in json.loads(zf.read('recordings.json'))]
        self.assertTrue(any('stopped after' in s['error'] for s in stats))

    def test_meta_reports_how_many_paths_were_checked(self):
        seed.make_recording(output_path=os.path.join(self.t._tmpdir, 'a.ts'))
        seed.make_recording(output_path=os.path.join(self.t._tmpdir, 'b.ts'))
        db.session.commit()

        check = json.loads(_unzip(sb.build_support_bundle()).read('meta.json'))['filesystem_check']
        self.assertEqual(check['paths_checked'], 2)
        self.assertEqual(check['paths_erroring'], 0)
        self.assertNotIn('stopped_early', check)


class AddedTableTests(_BundleTestCase):
    """dev/changelog/842. Six tables were missing outright, so a group-driven recording
    was diagnosable only by the accident that GROUP_MEMBER_SELECTED happens to be a
    RecordingEvent rather than a group event.
    """

    def _names(self, **kw):
        return _unzip(sb.build_support_bundle(**kw)).namelist()

    def _group_row(self, group_id, **kw):
        """By id, not by position: every app carries the pinned 'TV Guide Channels'
        system group, so the seeded group is never the first row."""
        rows = json.loads(
            _unzip(sb.build_support_bundle(**kw)).read('channel_groups.json'))
        return next(r for r in rows if r['id'] == group_id)

    def test_all_six_missing_tables_now_ship(self):
        for name in ('channel_groups.json', 'channel_group_members.json',
                     'channel_group_events.json', 'channel_events.json',
                     'job_runs.json', 'apscheduler_jobs.json'):
            self.assertIn(name, self._names())

    def test_group_membership_and_its_participation_switches_ship(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        grp = seed.make_group(members=[ch])
        db.session.commit()

        members = json.loads(
            _unzip(sb.build_support_bundle()).read('channel_group_members.json'))
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]['group_id'], grp.id)
        self.assertTrue(members[0]['recording_enabled'])

    def test_a_group_name_is_pseudonymized_by_default(self):
        grp = seed.make_group(name='Zephyr Sports Group')
        db.session.commit()

        content = _all_decompressed_content(sb.build_support_bundle())
        self.assertNotIn(b'Zephyr Sports Group', content)
        self.assertEqual(self._group_row(grp.id)['name'], f'group {grp.id}')

    def test_opting_in_ships_the_real_group_name(self):
        grp = seed.make_group(name='Zephyr Sports Group')
        db.session.commit()

        self.assertEqual(self._group_row(grp.id, include_names=True)['name'],
                         'Zephyr Sports Group')

    def test_a_job_run_row_ships(self):
        from app.database import JobRun
        db.session.add(JobRun(job_id='nightly_thing', started_at=datetime(2026, 8, 28),
                              finished_at=datetime(2026, 8, 28), outcome='FAILED'))
        db.session.commit()

        runs = json.loads(_unzip(sb.build_support_bundle()).read('job_runs.json'))
        self.assertEqual([r['job_id'] for r in runs], ['nightly_thing'])

    def test_a_channel_named_only_by_a_group_event_ships_a_full_row(self):
        """ChannelGroupEvent joins _CHANNEL_ID_SOURCES: a membership switch that flipped
        is useless if the channel it names has no row behind it."""
        acc = seed.make_account()
        named = seed.make_channel(acc, name='Named by the group event')
        seed.make_channel(acc, name='Uninvolved')
        grp = seed.make_group(name='G', members=[])
        db.session.add(ChannelGroupEvent(
            group_id=grp.id, channel_id=named.id,
            event_type='GROUP_MEMBER_PARTICIPATION', detail='switch moved'))
        db.session.commit()

        rows = _channel_rows(sb.build_support_bundle())
        self.assertEqual([r['id'] for r in rows], [named.id])


class ApschedulerJobTests(_BundleTestCase):
    """dev/changelog/842. 'My recording never started' is usually a missing or misdated
    row in the scheduler's own job store, which the bundle could not see at all."""

    def _create_table(self, rows):
        from sqlalchemy import text
        db.session.execute(text(
            'CREATE TABLE apscheduler_jobs (id VARCHAR(191) NOT NULL PRIMARY KEY, '
            'next_run_time FLOAT, job_state BLOB NOT NULL)'))
        for job_id, next_run, state in rows:
            db.session.execute(
                text('INSERT INTO apscheduler_jobs (id, next_run_time, job_state) '
                     'VALUES (:i, :n, :s)'),
                {'i': job_id, 'n': next_run, 's': state})
        db.session.commit()

    def test_a_scheduled_job_ships_with_its_next_run_time(self):
        self._create_table([('recording_start_7', 1787000000.0, b'\x80\x05pickle')])

        payload = json.loads(
            _unzip(sb.build_support_bundle()).read('apscheduler_jobs.json'))
        self.assertEqual(payload['job_count'], 1)
        self.assertEqual(payload['jobs'][0]['id'], 'recording_start_7')
        self.assertTrue(payload['jobs'][0]['next_run_time'].startswith('2026-'))

    def test_a_paused_job_keeps_its_row_with_a_null_next_run_time(self):
        self._create_table([('paused_job', None, b'\x80\x05pickle')])

        payload = json.loads(
            _unzip(sb.build_support_bundle()).read('apscheduler_jobs.json'))
        self.assertEqual(payload['jobs'][0]['id'], 'paused_job')
        self.assertIsNone(payload['jobs'][0]['next_run_time'])

    def test_the_job_state_pickle_is_never_shipped(self):
        """It is a pickle of the job's callable and arguments - unreadable here, and able
        to carry values nothing in the bundle could sanitize."""
        self._create_table([('a_job', 1787000000.0, b'PICKLE-PROBE-VALUE')])

        self.assertNotIn(b'PICKLE-PROBE-VALUE',
                         _all_decompressed_content(sb.build_support_bundle()))

    def test_a_missing_table_is_a_stated_fact_not_an_export_error(self):
        zf = _unzip(sb.build_support_bundle())
        payload = json.loads(zf.read('apscheduler_jobs.json'))
        self.assertEqual(payload['jobs'], [])
        self.assertIn('never started', payload['table_missing'])
        self.assertNotIn('errors.json', zf.namelist())


class ChannelEventCapTests(_BundleTestCase):
    """dev/changelog/842. channel_events is an activity table with one writer that is
    not: accounts.py::_write_channel_url_drift_events writes a row per drifted channel,
    and a mass domain move drifts thousands in one sync. Uncapped, that single case
    rebuilds the 137k-row file dev/changelog/841 removed - by two routes, since a
    whole-table DISTINCT over those rows would also drag a full channel row back in for
    every channel touched.
    """

    def _events(self, data):
        return json.loads(_unzip(data).read('channel_events.json'))

    def _seed_events(self, channel, count, base_id=0):
        for i in range(count):
            db.session.add(ChannelEvent(
                channel_id=channel.id, event_type='CHANNEL_URL_CHANGED',
                timestamp=datetime(2026, 8, 28, 0, 0, base_id + i),
                detail=f'drift {base_id + i}'))
        db.session.commit()

    def test_every_event_ships_when_under_the_cap(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        self._seed_events(ch, 3)

        payload = self._events(sb.build_support_bundle())
        self.assertEqual(payload['total_event_count'], 3)
        self.assertEqual(payload['included_event_count'], 3)

    def test_the_newest_events_survive_the_cap_and_the_total_is_stated(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc)
        self._seed_events(ch, 6)

        with patch.object(sb, '_CHANNEL_EVENT_LIMIT', 2):
            payload = self._events(sb.build_support_bundle())

        self.assertEqual(payload['total_event_count'], 6)
        self.assertEqual(payload['included_event_count'], 2)
        self.assertEqual([e['detail'] for e in payload['events']],
                         ['drift 4', 'drift 5'])

    def test_a_channel_named_only_by_a_shipped_event_gets_a_full_row(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc, name='Drifted')
        seed.make_channel(acc, name='Uninvolved')
        self._seed_events(ch, 2)

        rows = _channel_rows(sb.build_support_bundle())
        self.assertEqual([r['id'] for r in rows], [ch.id])

    def test_a_channel_named_only_by_a_capped_out_event_gets_no_row(self):
        """The half that keeps the cap meaningful: if the event did not ship, the
        channel it named is not 'referenced by something in this bundle', and asking the
        table instead would restore the 137k-row channels.json in exactly the scenario
        the cap exists for."""
        acc = seed.make_account()
        old = seed.make_channel(acc, name='Capped out')
        recent = seed.make_channel(acc, name='Still in the window')
        self._seed_events(old, 2, base_id=0)
        self._seed_events(recent, 2, base_id=10)

        with patch.object(sb, '_CHANNEL_EVENT_LIMIT', 2):
            rows = _channel_rows(sb.build_support_bundle())

        self.assertEqual([r['id'] for r in rows], [recent.id])


class ChannelHealthColumnTests(_BundleTestCase):
    """dev/changelog/842. The incident the audit was built from destroyed a channel's
    health score, and _CHANNEL_FIELDS shipped none of the health columns - the score was
    reconstructable only because channel_tests happens to keep lifetime_score_after."""

    def test_the_health_columns_ship_on_an_implicated_channel(self):
        acc = seed.make_account()
        ch = seed.make_channel(acc, health_score=41.5, health_score_sample_count=7,
                               manual_health_adjustment=-10,
                               consecutive_test_failures=3)
        _implicate(ch)
        db.session.commit()

        row = _channel_rows(sb.build_support_bundle())[0]
        self.assertEqual(row['health_score'], 41.5)
        self.assertEqual(row['health_score_sample_count'], 7)
        self.assertEqual(row['consecutive_test_failures'], 3)

    def test_the_manual_adjustment_ships_so_the_score_can_be_read_correctly(self):
        """Every band the UI shows is health_score + manual_health_adjustment, so the
        measured score alone reads wrong on any channel the user has nudged."""
        acc = seed.make_account()
        ch = seed.make_channel(acc, health_score=80.0, manual_health_adjustment=-30)
        _implicate(ch)
        db.session.commit()

        self.assertEqual(_channel_rows(sb.build_support_bundle())[0]
                         ['manual_health_adjustment'], -30)


class HostResourceTests(_BundleTestCase):
    """dev/changelog/842. meta.json carried the platform string and nothing about what
    the host had left, which are the numbers that let a remote reader say anything at all
    about a machine that died mid-recording."""

    def _meta(self):
        return json.loads(_unzip(sb.build_support_bundle()).read('meta.json'))

    @unittest.skipUnless(os.path.exists('/proc/meminfo'), 'Linux /proc only')
    def test_meta_carries_memory_swap_uptime_and_boot_id(self):
        host = self._meta()['host']
        self.assertGreater(host['memory_total_bytes'], 0)
        self.assertIn('swap_total_bytes', host)
        self.assertGreater(host['uptime_seconds'], 0)
        self.assertTrue(host['boot_id'])

    def test_meta_carries_the_cpu_count(self):
        self.assertGreater(self._meta()['host']['cpu_count'], 0)

    def test_a_host_that_cannot_answer_still_produces_a_bundle(self):
        """Every value is optional on purpose: /proc is Linux-only, and a non-Linux host
        must lose the numbers rather than the bundle."""
        with patch('builtins.open', side_effect=OSError('no /proc here')):
            host = sb._host_resources()

        self.assertNotIn('memory_total_bytes', host)
        self.assertNotIn('uptime_seconds', host)
        self.assertNotIn('boot_id', host)
        self.assertIn('cpu_count', host)


class RouteTests(_BundleTestCase):

    def test_route_returns_a_downloadable_zip(self):
        resp = self.t.client.get('/api/settings/support-bundle')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, 'application/zip')
        self.assertIn('attachment', resp.headers.get('Content-Disposition', ''))
        zf = zipfile.ZipFile(BytesIO(resp.data))
        self.assertIn('meta.json', zf.namelist())

    def test_route_defaults_to_pseudonymized_names(self):
        seed.make_account(name='zephyr-provider-nine')
        db.session.commit()
        resp = self.t.client.get('/api/settings/support-bundle')
        self.assertNotIn(b'zephyr-provider-nine',
                         _all_decompressed_content(resp.data))
        self.assertNotIn('with-names', resp.headers.get('Content-Disposition', ''))

    def test_route_names_param_opts_in_and_says_so_in_the_filename(self):
        """The download name carries the mode so an opted-in bundle stays identifiable
        after it has been saved, mailed or moved away from the page that produced it."""
        seed.make_account(name='zephyr-provider-nine')
        db.session.commit()
        resp = self.t.client.get('/api/settings/support-bundle?names=1')
        self.assertIn(b'zephyr-provider-nine', _all_decompressed_content(resp.data))
        self.assertIn('with-names', resp.headers.get('Content-Disposition', ''))

    def test_route_returns_json_error_envelope_on_total_failure(self):
        with patch('app.support_bundle.build_support_bundle', side_effect=RuntimeError('boom')):
            resp = self.t.client.get('/api/settings/support-bundle')
        self.assertEqual(resp.status_code, 500)
        body = resp.get_json()
        self.assertIn('error', body)


if __name__ == '__main__':
    unittest.main()
