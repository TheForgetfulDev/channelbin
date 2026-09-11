"""config.yaml is written atomically, under one lock, with its permissions preserved.

Guards dev/docs/BUGS.md 2026-08-15 @ 05:32 PM: every writer of config.yaml did
open(_CONFIG_PATH, 'w') and then dumped, so an exception, a crash or an OOM-kill between
the truncate and the last byte left the file empty or half-written - and config.yaml holds
flask.secret_key and auth.password_hash, so losing it invalidates every session and locks
the user out. save_config() also had no lock around its read-merge-write, so two saves in
flight (settings.js auto-saves per field, against a threaded server) each merged onto the
same stale base and the loser's change vanished with no error anywhere.

The temp file must be created in the directory holding the real config.yaml: os.replace()
is only atomic within one filesystem, and that file is the repo root's in production, a
per-test temp dir under ConfigSandbox, and - in the Docker image - the target of a symlink
on another filesystem entirely. SymlinkedConfigWriteTests below covers that last case,
which took the shipped container down for three weeks (dev/changelog/909).
"""
import contextlib
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import yaml

from app import config as cfgmod
from app import config_backup as cbmod
from tests.support.app import make_test_app, write_sandbox_config
from tests.support.config_sandbox import ConfigSandbox


def _stamp():
    return {'config_version': cfgmod.CURRENT_CONFIG_VERSION}


class AtomicWriteTests(ConfigSandbox):
    """A failed write must leave the previous config.yaml exactly as it was."""

    def setUp(self):
        super().setUp()
        # A directory of this test's own, not the shared system temp dir ConfigSandbox
        # hands out. _litter() lists the config file's directory, and every other
        # ConfigSandbox in the process puts its config.yaml straight in /tmp - so a save
        # in flight anywhere else in the shard was counted as a temp file THIS test
        # leaked, failing it for something it did not do (dev/docs/BUGS.md 2026-08-18).
        os.remove(self._cfg_path)
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, True)
        self._cfg_path = os.path.join(self._dir, 'config.yaml')
        path_patch = mock.patch.object(cfgmod, '_CONFIG_PATH', self._cfg_path)
        path_patch.start()
        self.addCleanup(path_patch.stop)
        cfgmod._yaml_cache = None

    def _litter(self):
        """Temp files this module left behind in the config's directory."""
        return [n for n in os.listdir(self._dir) if n.startswith('.config.yaml.')]

    def test_failed_dump_leaves_the_previous_config_byte_identical(self):
        self._write_cfg({'flask': {'secret_key': 'keep-me'}, **_stamp()})
        with open(self._cfg_path, 'rb') as f:  # direct-config-read: asserting on stored bytes
            before = f.read()

        with mock.patch.object(cfgmod._yaml_rt, 'dump', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                cfgmod.save_config({'flask': {'secret_key': 'overwritten'}, **_stamp()})

        with open(self._cfg_path, 'rb') as f:  # direct-config-read: asserting on stored bytes
            after = f.read()
        self.assertEqual(before, after,
                         'a dump that raised mid-write changed the stored config.yaml')
        self.assertEqual(self._litter(), [], 'temp file left behind by a failed write')
        cfgmod._yaml_cache = None
        self.assertEqual(cfgmod.load_config()['flask']['secret_key'], 'keep-me')

    def test_config_is_never_observable_truncated_mid_write(self):
        """Whatever is at _CONFIG_PATH is always a complete file: the new content is
        written elsewhere and renamed over the old one, so a reader mid-write sees the
        previous file, never a truncated one."""
        self._write_cfg({'flask': {'secret_key': 'keep-me'}, **_stamp()})
        seen = []

        real_dump = cfgmod._yaml_rt.dump

        def dump_then_peek(data, stream, *a, **kw):
            result = real_dump(data, stream, *a, **kw)
            # Mid-write, from the writer's own thread: the live path must still resolve
            # to the complete previous file.
            with open(self._cfg_path) as f:  # direct-config-read: asserting on stored bytes
                seen.append(yaml.safe_load(f.read()))
            return result

        with mock.patch.object(cfgmod._yaml_rt, 'dump', side_effect=dump_then_peek):
            cfgmod.save_config({'flask': {'secret_key': 'new'}, **_stamp()})

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['flask']['secret_key'], 'keep-me',
                         'config.yaml was already truncated/rewritten while the dump was '
                         'still running - the write is not atomic')
        cfgmod._yaml_cache = None
        self.assertEqual(cfgmod.load_config()['flask']['secret_key'], 'new')

    def test_temp_file_is_created_in_the_configs_own_directory(self):
        """os.replace() is only atomic within one filesystem. A temp file in the system
        temp dir would silently degrade the rename back into a copy on any install whose
        config.yaml is not on that filesystem."""
        captured = {}
        real_mkstemp = tempfile.mkstemp

        def spy(*a, **kw):
            captured.update(kw)
            return real_mkstemp(*a, **kw)

        with mock.patch.object(tempfile, 'mkstemp', side_effect=spy):
            cfgmod.save_config({'flask': {'debug': True}, **_stamp()})

        self.assertEqual(captured.get('dir'), os.path.dirname(self._cfg_path))
        self.assertEqual(self._litter(), [])

    def test_existing_permissions_survive_a_save(self):
        """os.replace() carries the temp file's own mode with it, so a private
        config.yaml would silently widen to the temp file's mode on every save.

        Guards the atomic write against introducing a defect of its own rather than a
        pre-existing one: the truncating write this replaced preserved an existing file's
        mode for free, so this assertion passes against the old code too."""
        self._write_cfg({'flask': {'debug': False}, **_stamp()})
        os.chmod(self._cfg_path, 0o640)

        cfgmod.save_config({'flask': {'debug': True}, **_stamp()})

        self.assertEqual(os.stat(self._cfg_path).st_mode & 0o777, 0o640)


class FreshConfigWriteTests(unittest.TestCase):
    """The very first write, when no config.yaml exists yet."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self._path = os.path.join(self._dir, 'config.yaml')
        patch = mock.patch.object(cfgmod, '_CONFIG_PATH', self._path)
        patch.start()
        self.addCleanup(patch.stop)
        cfgmod._yaml_cache = None
        self.addCleanup(lambda: setattr(cfgmod, '_yaml_cache', None))

    def test_a_config_created_from_scratch_is_private(self):
        """config.yaml carries flask.secret_key and the auth hash, so a file this app
        creates is 0600 from birth rather than umask-derived."""
        self.assertFalse(os.path.exists(self._path))
        cfgmod.save_config({'flask': {'secret_key': 'brand-new'}, **_stamp()})
        self.assertEqual(os.stat(self._path).st_mode & 0o777, 0o600)


class SaveSerializationTests(ConfigSandbox):
    """Two concurrent per-field saves must not lose either one's change.

    save_config() writes whatever it is handed, and every settings route builds that
    argument by load_config() → mutate. So the unit that has to be atomic starts at the
    ROUTE's read, not inside save_config() - a lock that only spanned save_config()'s own
    body would leave this race exactly as it was.
    """

    def setUp(self):
        super().setUp()
        self._write_cfg({'recording': {'post_process': {'video_crf': 20,
                                                        'audio_bitrate_kbps': 96}},
                         **_stamp()})
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.addCleanup(self.t.cleanup)

    def test_concurrent_field_saves_do_not_drop_a_field(self):
        """Forces the interleave that loses an update: the first request is held between
        its read and its write while a second runs start to finish. Under the lock the
        second request cannot get past its own read, so the hold just times out and the
        two serialize."""
        first_in_write = threading.Event()
        second_done = threading.Event()
        held = []
        real_save = cfgmod.save_config

        def hold_first_save(data):
            if not held:
                held.append(True)
                first_in_write.set()
                # Bounded: with the lock held this window can never open, so the wait
                # times out and the save simply proceeds.
                second_done.wait(timeout=0.5)
            return real_save(data)

        results = []

        def post(path, value, done=None):
            try:
                client = self.t.app.test_client()
                resp = client.post('/api/settings/field',
                                   json={'path': path, 'value': value})
                results.append(resp.status_code)
            finally:
                if done is not None:
                    done.set()

        with mock.patch('app.routes.settings.save_config', side_effect=hold_first_save):
            a = threading.Thread(target=post,
                                 args=('recording.post_process.video_crf', 31))
            a.start()
            self.assertTrue(first_in_write.wait(timeout=5),
                            'first save never reached save_config()')
            b = threading.Thread(target=post,
                                 args=('recording.post_process.audio_bitrate_kbps', 256,
                                       second_done))
            b.start()
            a.join(timeout=10)
            b.join(timeout=10)

        self.assertEqual(results, [200, 200])
        cfgmod._yaml_cache = None
        pp = cfgmod.load_config()['recording']['post_process']
        self.assertEqual((pp['video_crf'], pp['audio_bitrate_kbps']), (31, 256),
                         'a concurrent settings save was silently dropped - the route must '
                         'hold config_write_lock across its whole load → mutate → save')


class RouteLockCoverageTests(ConfigSandbox):
    """Every mutating settings route reads config.yaml under the write lock.

    The behavioral test above proves the race is closed for one route; this one is the
    cheap guard that the next route added to this file does not reintroduce it.
    """

    def setUp(self):
        super().setUp()
        self._write_cfg(_stamp())
        self.t = make_test_app()
        self.t.app.config['WTF_CSRF_ENABLED'] = False
        self.addCleanup(self.t.cleanup)
        self.client = self.t.app.test_client()

    # A mutating route reads through whichever of these suits it: load_for_edit() when it
    # validates against the effective config and writes the raw file dict, _load_config_file()
    # when it only writes, load_config() when it only reads. Spying on one name alone lets a
    # route switch to a sibling and silently stop being covered (dev/changelog/727).
    _READERS = ('load_config', '_load_config_file', 'load_for_edit')

    def _lock_held_during_read(self, call):
        held = []

        def spy_for(name):
            real = getattr(cfgmod, name)

            def spy(*a, **kw):
                held.append(cfgmod.config_write_lock._is_owned())
                return real(*a, **kw)
            return spy

        # Patched on the route module, not app.config: settings.py binds these with a
        # module-top `from ..config import ...`, so a patch of app.config never reaches it.
        with contextlib.ExitStack() as stack:
            for name in self._READERS:
                stack.enter_context(
                    mock.patch(f'app.routes.settings.{name}', side_effect=spy_for(name)))
            call()
        self.assertTrue(held, 'route never read the config at all')
        return held

    def test_field_save_reads_under_the_lock(self):
        held = self._lock_held_during_read(lambda: self.client.post(
            '/api/settings/field', json={'path': 'display.nav_poll_interval_seconds',
                                         'value': 30}))
        self.assertTrue(all(held), 'api_settings_field read config.yaml outside the lock')

    def test_notification_routing_save_reads_under_the_lock(self):
        held = self._lock_held_during_read(lambda: self.client.post(
            '/api/notifications/routing', json={}))
        self.assertTrue(all(held),
                        'api_notifications_routing_save read config.yaml outside the lock')

    def test_rate_limit_save_reads_under_the_lock(self):
        held = self._lock_held_during_read(lambda: self.client.post(
            '/api/notifications/rate-limit', json={'seconds': 90}))
        self.assertTrue(all(held),
                        'api_notifications_rate_limit read config.yaml outside the lock')

    def test_a_read_only_route_does_not_take_the_lock(self):
        """Control: the guard above discriminates. A pure read has nothing to serialize
        and must not pay for the lock."""
        held = self._lock_held_during_read(lambda: self.client.get('/api/settings'))
        self.assertFalse(any(held))


class SharedYamlInstanceTests(ConfigSandbox):
    """The one ruamel YAML() instance is used for both load and dump."""

    def test_parsing_holds_the_config_write_lock(self):
        """ruamel's YAML object carries per-instance representer/serializer state, so a
        dump running concurrently with this load can corrupt either. The write lock is
        what serializes them - a parse outside it leaves that race open."""
        owned = []
        real_load = cfgmod._yaml_rt.load

        def load_spy(stream, *a, **kw):
            owned.append(cfgmod.config_write_lock._is_owned())
            return real_load(stream, *a, **kw)

        cfgmod._yaml_cache = None
        with mock.patch.object(cfgmod._yaml_rt, 'load', side_effect=load_spy):
            cfgmod._parse_config_file()

        self.assertEqual(owned, [True],
                         '_parse_config_file() parsed without holding config_write_lock, '
                         'so a concurrent dump can corrupt the shared _yaml_rt instance')


class RestoreAtomicityTests(ConfigSandbox):
    """apply_backup() overwrites the live config.yaml - the same atomicity applies."""

    def setUp(self):
        super().setUp()
        # Its own directory, for the reason AtomicWriteTests.setUp() spells out: the litter
        # scan below lists the config file's directory, and ConfigSandbox hands out a path
        # straight in the shared system temp dir, where any other sandboxed save in this
        # process leaves a `.config.yaml.*.tmp` of its own mid-write (the temp prefix is
        # fixed, not derived from the target's name). The sibling class was fixed for this
        # in dev/docs/BUGS.md 2026-08-18; this one was missed.
        os.remove(self._cfg_path)
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, True)
        self._cfg_path = os.path.join(self._dir, 'config.yaml')
        live_patch = mock.patch.object(cfgmod, '_CONFIG_PATH', self._cfg_path)
        live_patch.start()
        self.addCleanup(live_patch.stop)
        cfgmod._yaml_cache = None

        self._backup = os.path.join(os.path.dirname(self._cfg_path), 'a-backup.yaml')
        with open(self._backup, 'w') as f:
            yaml.dump({'flask': {'secret_key': 'from-backup'}, **_stamp()}, f)
        self.addCleanup(lambda: os.path.exists(self._backup) and os.remove(self._backup))
        # apply_backup() resolves the live path through config_backup._config_path().
        p = mock.patch.object(cbmod, '_config_path', return_value=self._cfg_path)
        p.start()
        self.addCleanup(p.stop)

    def test_restore_replaces_the_config_atomically(self):
        self._write_cfg({'flask': {'secret_key': 'current'}, **_stamp()})
        cbmod.apply_backup(self._backup)
        cfgmod._yaml_cache = None
        self.assertEqual(cfgmod.load_config()['flask']['secret_key'], 'from-backup')

    def test_failed_restore_leaves_the_live_config_intact(self):
        self._write_cfg({'flask': {'secret_key': 'current'}, **_stamp()})
        with open(self._cfg_path, 'rb') as f:  # direct-config-read: asserting on stored bytes
            before = f.read()

        with mock.patch.object(cfgmod.shutil, 'copyfileobj', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                cbmod.apply_backup(self._backup)

        with open(self._cfg_path, 'rb') as f:  # direct-config-read: asserting on stored bytes
            after = f.read()
        self.assertEqual(before, after,
                         'a restore that failed partway through still damaged config.yaml')
        litter = [n for n in os.listdir(os.path.dirname(self._cfg_path))
                  if n.startswith('.config.yaml.')]
        self.assertEqual(litter, [], 'temp file left behind by a failed restore')


class SymlinkedConfigWriteTests(unittest.TestCase):
    """A config.yaml reached through a symlink is written where the symlink POINTS.

    Guards dev/docs/BUGS.md 2026-09-10 @ 12:14 PM: both writers derived their temp
    directory from os.path.dirname(_CONFIG_PATH), i.e. the directory the symlink lives in
    rather than the one holding the real file. The Docker image is exactly that layout -
    /app/config.yaml is a symlink onto the /config volume - so create_app()'s
    migrate_config() raised PermissionError writing /app/.config.yaml.*.tmp as the
    unprivileged runtime user, and the container crashed at import without ever serving a
    request. It failed DURING the migration write, so the migration never landed and every
    restart repeated it.

    Two failures hide behind that first one, which is why realpath() rather than a
    permission fix: the two directories are different filesystems in the image, so
    os.replace() would raise EXDEV even with permission, and a rename that did land would
    replace the symlink itself with a regular file, ending config persistence at the next
    image upgrade.
    """

    def setUp(self):
        self._base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._base, True)
        # Named for what they stand in for: /app is the image layer holding the symlink,
        # /config is the volume holding the real file.
        self._approot = os.path.join(self._base, 'app')
        self._volume = os.path.join(self._base, 'config')
        os.mkdir(self._approot)
        os.mkdir(self._volume)
        self._real = os.path.join(self._volume, 'config.yaml')
        self._link = os.path.join(self._approot, 'config.yaml')
        write_sandbox_config(self._real, {'flask': {'secret_key': 'seeded'}, **_stamp()})
        os.symlink(self._real, self._link)

        patch = mock.patch.object(cfgmod, '_CONFIG_PATH', self._link)
        patch.start()
        self.addCleanup(patch.stop)
        cfgmod._yaml_cache = None
        self.addCleanup(lambda: setattr(cfgmod, '_yaml_cache', None))

    def _litter(self, directory):
        return [n for n in os.listdir(directory) if n.startswith('.config.yaml.')]

    def test_the_temp_file_lands_beside_the_real_file_not_beside_the_symlink(self):
        """os.replace() is atomic only within one filesystem, and the symlink's own
        directory is not guaranteed to be the target's - in the image it is a read-only
        layer on a different filesystem entirely."""
        captured = {}
        real_mkstemp = tempfile.mkstemp

        def spy(*a, **kw):
            captured.update(kw)
            return real_mkstemp(*a, **kw)

        with mock.patch.object(tempfile, 'mkstemp', side_effect=spy):
            cfgmod.save_config({'flask': {'secret_key': 'written'}, **_stamp()})

        self.assertEqual(captured.get('dir'), self._volume)
        self.assertEqual(self._litter(self._volume), [])
        self.assertEqual(self._litter(self._approot), [])

    def test_a_save_leaves_the_symlink_a_symlink(self):
        """Replacing the link with a regular file would silently end config persistence:
        the volume keeps the stale file and the container's writes live in a layer that
        is discarded at the next image upgrade."""
        cfgmod.save_config({'flask': {'secret_key': 'written'}, **_stamp()})

        self.assertTrue(os.path.islink(self._link),
                        'the write replaced the symlink with a regular file')
        self.assertEqual(os.readlink(self._link), self._real)
        with open(self._real) as f:  # direct-config-read: asserting on stored bytes
            self.assertIn('written', f.read())

    def test_a_save_succeeds_when_the_symlinks_own_directory_is_read_only(self):
        """The container's actual symptom: /app is root-owned and the app runs as
        PUID/PGID 1000, so a temp file resolved to the symlink's directory raises
        PermissionError before create_app() ever returns."""
        if os.geteuid() == 0:
            self.skipTest('root ignores directory permissions, so this proves nothing')
        os.chmod(self._approot, 0o555)
        self.addCleanup(os.chmod, self._approot, 0o755)

        cfgmod.save_config({'flask': {'secret_key': 'written'}, **_stamp()})

        cfgmod._yaml_cache = None
        self.assertEqual(cfgmod.load_config()['flask']['secret_key'], 'written')

    def test_a_restore_through_a_symlink_lands_on_the_real_file(self):
        """_replace_config_file_from() is the other writer and had the identical bug -
        a config backup restored inside the container would have hit the same wall."""
        backup = os.path.join(self._base, 'a-backup.yaml')
        with open(backup, 'w') as f:
            yaml.dump({'flask': {'secret_key': 'from-backup'}, **_stamp()}, f)

        with cfgmod.config_write_lock:
            cfgmod._replace_config_file_from(backup)

        self.assertTrue(os.path.islink(self._link))
        cfgmod._yaml_cache = None
        self.assertEqual(cfgmod.load_config()['flask']['secret_key'], 'from-backup')
        self.assertEqual(self._litter(self._approot), [])


if __name__ == '__main__':
    unittest.main()
